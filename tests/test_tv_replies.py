"""Odd replies from TVs degrade gracefully instead of escaping the RuntimeError contract."""
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tvcast import samsung, upnp

REPLIES = {
    "GetTransportInfo": b"<CurrentTransportState>PLAYING</CurrentTransportState>",
    # Not UTF-8, as some renderers send titles.
    "GetPositionInfo": b"<TrackURI>http://192.0.2.1/a</TrackURI><RelTime>0:01:00</RelTime><Title>Am\xe9lie</Title>",
    "GetVolume": b"<CurrentVolume>10</CurrentVolume>",
    "GetMute": None,  # fails, like a renderer that doesn't implement it
}


class Renderer(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        reply = REPLIES[self.headers["SOAPAction"].strip('"').split("#")[1]]
        body = b"<errorDescription>Invalid Action</errorDescription>" if reply is None else reply
        self.send_response(500 if reply is None else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NotHTTP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(4096)
        self.request.sendall(b"SSH-2.0-other\r\n\r\n")


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FailureTest(unittest.TestCase):
    def test_a_failing_optional_reading_leaves_only_it_unknown(self):
        server = serve(ThreadingHTTPServer(("127.0.0.1", 0), Renderer))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/ctl" % server.server_address[1]
        tv = {"name": "Test TV", "services": {
            name: {"type": "urn:schemas-upnp-org:service:%s:1" % name, "url": url}
            for name in ("AVTransport", "RenderingControl")}}
        status = upnp.tv_status(tv, 2)
        self.assertEqual((status["state"], status["position"], status["uri"]), ("PLAYING", 60.0, "http://192.0.2.1/a"))
        self.assertEqual((status["volume"], status["muted"]), (10, None))

    def test_has_remote_is_false_for_a_service_that_isnt_http(self):
        server = serve(socketserver.ThreadingTCPServer(("127.0.0.1", 0), NotHTTP))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(samsung, "REST_PORT", server.server_address[1]):
            self.assertFalse(samsung.has_remote("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
