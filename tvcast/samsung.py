"""Samsung Tizen extras: the WebSocket remote (pairing, keys, text) and REST app control.

These speak Samsung's own protocol, so they work only with Samsung Tizen TVs. Casting and
playback controls go through UPnP (upnp.py) and work with any media renderer.
"""
import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REMOTE_PORT = 8002  # Samsung's WebSocket remote-control channel (TLS, self-signed certificate)
REST_PORT = 8001  # the TV's unauthenticated REST API (device info, per-app status)
REMOTE_NAME = "TV Cast"  # shown in the TV's pairing prompt and its device list
PAIR_TIMEOUT = 40  # seconds to wait for someone to press Allow on the TV
APP_TIMEOUT = 2
LAUNCH_TIMEOUT = 10
# Tizen app IDs the panel offers when they're installed. Tizen TVs can ignore the WebSocket
# request that lists installed apps, so only apps known by ID are looked up.
KNOWN_APPS = [
    {"id": "111299001912", "name": "YouTube"},
    {"id": "3201907018807", "name": "Netflix"},
    {"id": "3201910019365", "name": "Prime Video"},
    {"id": "3201807016597", "name": "Apple TV"},
    {"id": "3201901017640", "name": "Disney+"},
    {"id": "3201606009684", "name": "Spotify"},
    {"id": "3201512006963", "name": "Plex"},
]


def has_remote(ip):
    """Whether the TV at ip offers the Tizen remote, judged by its REST API answering with device
    info. Any failure means no: TVs of other makes don't listen on REST_PORT at all, or run
    something else there."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/v2/" % (ip, REST_PORT), timeout=2) as resp:
            info = json.load(resp)
    except (OSError, ValueError, http.client.HTTPException):  # refused, timed out, not HTTP, not JSON
        return False
    return isinstance(info, dict) and "device" in info


def paired(tv):
    return bool(tv.get("remote_token") and tv.get("remote_cert"))


def open_tls(ip):
    """A TLS socket to the TV's remote channel and the SHA-256 fingerprint of its certificate."""
    ctx = ssl.create_default_context()
    # The TV's certificate is self-signed, so no CA can vouch for it. Instead its fingerprint is
    # pinned at pairing and must match on every later connection.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(socket.create_connection((ip, REMOTE_PORT), timeout=5), server_hostname=ip)
    return sock, hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()


def fingerprint(ip):
    """The certificate fingerprint open_tls sees at ip, or None when nothing there answers TLS."""
    try:
        sock, cert = open_tls(ip)
    except OSError:  # refused, timed out or no TLS: nothing there can match a pinned certificate
        return None
    sock.close()
    return cert


class WebSocket:
    """Just enough of RFC 6455 for the TV's remote channel: JSON text frames both ways."""

    def __init__(self, sock, buffered=b""):
        self.sock = sock
        self.buf = buffered

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed by the TV")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _frame(self, opcode, payload):
        # Client frames must be masked (RFC 6455 section 5.3).
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b1 & 0x0F
            if opcode == 0x8:
                raise OSError("connection closed by the TV")
            if opcode == 0x9:
                self._frame(0xA, data)  # answer pings so the TV keeps the channel open
            elif opcode in (0x0, 0x1, 0x2):
                message += data
                if b1 & 0x80:
                    return json.loads(message.decode())

    def close(self):
        try:
            self._frame(0x8, b"")
        except OSError:
            pass  # the TV may already have dropped the connection; it is closing either way
        finally:
            self.sock.close()


class Remote:
    """A Samsung TV's WebSocket remote-control channel. The first connection makes the TV ask
    whether to allow REMOTE_NAME; accepting issues a token that later connections present.
    The token and certificate pin are stored in that TV's state record."""

    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()  # one connection at a time keeps keypresses in order

    def _connect(self, tv, wait, pairing=False):
        ip, name = tv["ip"], tv["name"]
        query = "name=" + urllib.parse.quote(base64.b64encode(REMOTE_NAME.encode()).decode())
        token = None if pairing else tv.get("remote_token")
        if token:
            query += "&token=" + urllib.parse.quote(token)
        try:
            sock, cert = open_tls(ip)
            if not pairing and cert != tv.get("remote_cert"):
                sock.close()
                raise RuntimeError(
                    "TV %s presented a different certificate than when paired; if the TV was"
                    " updated or reset, pair again" % name)
            sock.sendall((
                "GET /api/v2/channels/samsung.remote.control?%s HTTP/1.1\r\nHost: %s:%d\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
                % (query, ip, REMOTE_PORT, base64.b64encode(os.urandom(16)).decode())
            ).encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("connection closed during the WebSocket handshake")
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 101 " not in status_line + " ":
                raise OSError("WebSocket handshake refused: %s" % status_line)
            ws = WebSocket(sock, rest)
            sock.settimeout(wait)
            try:
                msg = ws.recv()
            except socket.timeout:
                ws.close()
                raise RuntimeError("TV %s didn't answer; accept the %s prompt on the TV (within %ds)" % (name, REMOTE_NAME, wait))
        except OSError as e:
            raise RuntimeError("remote connection to TV %s failed: %s" % (name, e))
        event = msg.get("event")
        if event != "ms.channel.connect":
            ws.close()
            if event == "ms.channel.unauthorized":
                raise RuntimeError(
                    "TV %s refused %s; allow it under Settings > General > External Device Manager"
                    " > Device Connection Manager > Device List" % (name, REMOTE_NAME))
            raise RuntimeError("unexpected first message from TV %s: %s" % (name, event))
        if pairing:
            new_token = (msg.get("data") or {}).get("token")
            if not new_token:
                ws.close()
                raise RuntimeError("TV %s allowed the connection but issued no token to remember" % name)
            self.state.update_tv(tv["id"], remote_cert=cert, remote_token=str(new_token))
        sock.settimeout(5)
        return ws

    def pair(self, tv):
        with self.lock:
            self._connect(tv, PAIR_TIMEOUT, pairing=True).close()

    @contextlib.contextmanager
    def session(self, tv):
        if not paired(tv):
            raise RuntimeError("pair TV Cast with TV %s first (Remote > Pair with TV, or tv pair)" % tv["name"])
        with self.lock:
            ws = self._connect(tv, 5)
            try:
                yield ws
            except OSError as e:
                raise RuntimeError("remote command to TV %s failed: %s" % (tv["name"], e))
            finally:
                ws.close()

    def keys(self, tv, keys):
        for key in keys:
            if not re.fullmatch(r"KEY_[A-Z0-9_]+", key):
                raise ValueError("not a remote key: %r" % key)
        with self.session(tv) as ws:
            for i, key in enumerate(keys):
                if i:
                    time.sleep(0.3)  # pace repeated keys like real button presses
                ws.send({"method": "ms.remote.control", "params": {
                    "Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}})

    def text(self, tv, text):
        # Lands in whatever on-screen keyboard the TV is showing (e.g. a search box).
        with self.session(tv) as ws:
            ws.send({"method": "ms.remote.control", "params": {
                "Cmd": base64.b64encode(text.encode()).decode(), "DataOfCmd": "base64",
                "TypeOfRemote": "SendInputString"}})

    def apps(self, tv):
        """Known installed apps, including foreground state. A running app may be in the background."""
        # Missing apps can take seconds to answer. Bound concurrency so one slow lookup does
        # not hold up every other app, without adding work to the frequent playback status poll.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            return [app for app in pool.map(lambda a: self.app_status(tv, a["id"]), KNOWN_APPS) if app]

    def app_status(self, tv, app_id):
        url = app_url(tv, app_id)
        try:
            with urllib.request.urlopen(url, timeout=APP_TIMEOUT) as resp:
                info = json.load(resp)
            if not isinstance(info, dict) or not isinstance(info.get("name"), str):
                raise ValueError("expected an app object with a name")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError("checking app %s on TV %s failed: HTTP %d" % (app_id, tv["name"], e.code))
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise RuntimeError("checking app %s on TV %s failed: %s" % (app_id, tv["name"], e))
        return {
            "id": app_id, "name": info["name"], "version": info.get("version"),
            "running": info.get("running") if isinstance(info.get("running"), bool) else None,
            "visible": info.get("visible") if isinstance(info.get("visible"), bool) else None,
        }

    def launch(self, tv, app_id):
        app = self.app_status(tv, app_id)
        if app is None:
            raise ValueError("app %s is not installed on TV %s" % (app_id, tv["name"]))
        # Some Tizen TVs silently ignore ed.apps.launch. Their REST endpoint accepts an empty
        # POST and can return non-JSON text, so only the subsequent visible flag proves launch.
        with self.lock:
            req = urllib.request.Request(app_url(tv, app_id), data=b"", method="POST")
            try:
                with urllib.request.urlopen(req, timeout=APP_TIMEOUT) as resp:
                    resp.read(1024)
            except (OSError, http.client.HTTPException) as e:
                raise RuntimeError("opening %s on TV %s failed: %s" % (app["name"], tv["name"], e))
            deadline = time.monotonic() + LAUNCH_TIMEOUT
            while time.monotonic() < deadline:
                current = self.app_status(tv, app_id)
                if current and current["visible"] is True:
                    return current
                time.sleep(0.5)
        raise RuntimeError("TV %s did not confirm that %s opened; check the TV screen and try again"
                           % (tv["name"], app["name"]))


def app_url(tv, app_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", app_id):
        raise ValueError("invalid app ID: %r" % app_id)
    return "http://%s:%d/api/v2/applications/%s" % (tv["ip"], REST_PORT, app_id)
