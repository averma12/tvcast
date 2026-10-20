"""HTTP request handlers for the stream server (media and subtitles to the TV) and the control
panel, and the transfer Stats they share. app.py creates the servers."""
import collections
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler

from .upnp import DLNA_FEATURES

CHUNK = 256 * 1024
RATE_WINDOW = 10  # seconds of history behind the panel's transfer-rate figure
PANEL_HTML = os.path.join(os.path.dirname(os.path.realpath(__file__)), "panel.html")


class Stats:
    """Transfer counters shared by the stream handler threads and the control panel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.sent = 0
        self.active = 0
        self.last_request = None
        self.recent = collections.deque()  # (timestamp, bytes) within RATE_WINDOW

    def _trim(self, now):
        while self.recent and self.recent[0][0] < now - RATE_WINDOW:
            self.recent.popleft()

    def request(self):
        with self.lock:
            self.last_request = time.time()

    def transfer(self, delta):
        with self.lock:
            self.active += delta

    def add(self, n):
        now = time.time()
        with self.lock:
            self.sent += n
            self.recent.append((now, n))
            self._trim(now)

    def snapshot(self):
        now = time.time()
        with self.lock:
            self._trim(now)
            return {
                "uptime": now - self.started,
                "sent": self.sent,
                "rate": sum(n for _, n in self.recent) / RATE_WINDOW,
                "active": self.active,
                "last_request_age": now - self.last_request if self.last_request else None,
            }


class StreamHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive matters: the TV issues many small Range requests while probing the MP4.
    protocol_version = "HTTP/1.1"
    app = None

    def do_HEAD(self):
        self._serve(send_body=False)

    def do_GET(self):
        self._serve(send_body=True)

    def _serve(self, send_body):
        # The server listens on every interface, so only the selected TV may fetch anything.
        tv = self.app.selected()
        if tv is None or self.client_address[0] != tv["ip"]:
            self.send_error(403)
            return
        entry = self.app.resolve(self.path)
        if entry is None:
            self.send_error(404)
            return
        self.app.stats.request()
        fs_path, ctype, movie = entry
        size = os.path.getsize(fs_path)
        start, end = 0, size - 1
        # Range support is required: the TV seeks and reads the MP4 index with byte ranges.
        range_header = self.headers.get("Range")
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not m or not (m[1] or m[2]):
                self.send_error(416, "Unsupported Range: %s" % range_header)
                return
            if m[1]:
                start = int(m[1])
                end = min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if movie is not None:
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", DLNA_FEATURES)
            caption = self.app.caption_url(movie)
            if caption:
                # Samsung-specific header that points the player at external subtitles; Samsung
                # asks for it with getcaptionInfo.sec, other TVs ignore it.
                self.send_header("CaptionInfo.sec", caption)
        self.end_headers()
        if not send_body:
            return
        self.app.stats.transfer(1)
        try:
            with open(fs_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        raise RuntimeError("%s ended %d bytes early; was it modified?" % (fs_path, remaining))
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                    self.app.stats.add(len(chunk))
        except (BrokenPipeError, ConnectionResetError):
            # The TV abandons in-flight ranges whenever it seeks or has buffered enough.
            # The socket is dead, so don't wait on it for another keep-alive request.
            self.close_connection = True
        finally:
            self.app.stats.transfer(-1)

    def log_request(self, code="-", size="-"):
        # The TV fetches video in many ranges; only subtitle fetches are worth a log line.
        if not self.path.startswith("/media/"):
            super().log_request(code, size)


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app = None

    def do_GET(self):
        if not self._trusted():
            return
        if self.path == "/":
            self.app.request_scan()
            # Read per request so edits to panel.html show up without restarting the service.
            with open(PANEL_HTML, "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif self.path == "/api/status":
            self._send_json(200, self.app.status())
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._trusted():
            return
        # Requiring a custom header forces a CORS preflight, which this server never answers,
        # so other pages open in the browser cannot drive the TV through the panel.
        if self.headers.get("X-TVCast") != "1":
            self.send_error(403, "missing X-TVCast header")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            result = self.app.control(self.path[len("/api/"):], json.loads(body or b"{}"))
        except RuntimeError as e:  # the TV (or the Mac) refused or could not be reached
            self._send_json(502, {"error": str(e)})
            return
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {"error": "bad request: %s" % e})
            return
        self._send_json(200, result)

    def _trusted(self):
        # Checking Host blocks DNS-rebinding pages, which the browser would treat as same-origin.
        if self.headers.get("Host") not in self.app.panel_hosts:
            self.send_error(403, "unexpected Host header")
            return False
        return True

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def log_request(self, code="-", size="-"):
        # The panel polls status every second; only control actions are worth logging.
        if self.command == "POST":
            super().log_request(code, size)
