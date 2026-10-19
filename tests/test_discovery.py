import contextlib
import io
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tvcast import discovery


def fixture(name):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", name), "rb") as f:
        return f.read()


RENDERER = fixture("renderer.xml")
PAGES = {
    "/dmr/renderer.xml": RENDERER,
    "/copy/renderer.xml": RENDERER,  # the same TV answering at a second address
    "/embedded.xml": fixture("embedded.xml"),
    "/gateway.xml": fixture("gateway.xml"),
    "/file-control.xml": RENDERER.replace(b"/upnp/control/AVTransport1", b"file:///etc/hosts"),
    "/foreign-control.xml": RENDERER.replace(b"/upnp/control/AVTransport1", b"http://192.0.2.99/avt"),
    "/doctype.xml": RENDERER.replace(b"<root", b'<!DOCTYPE root [<!ENTITY x "x">]><root', 1),
}


class Pages(BaseHTTPRequestHandler):
    """Serves PAGES in place of the devices' description servers."""

    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return "http://127.0.0.1:%d/%s" % (self.server.server_address[1], path)

    def test_root_renderer(self):
        location = self.url("dmr/renderer.xml")
        self.assertEqual(discovery.describe(location), {
            "id": "uuid:00000000-0000-0000-0000-000000000001",
            "name": "Living Room TV",
            "manufacturer": "Example Electronics",
            "model": "Example TV 1000",
            "ip": "127.0.0.1",
            "location": location,
            "services": {  # without URLBase, control URLs resolve against the description URL
                "AVTransport": {
                    "type": "urn:schemas-upnp-org:service:AVTransport:1",
                    "url": self.url("upnp/control/AVTransport1"),
                },
                "RenderingControl": {
                    "type": "urn:schemas-upnp-org:service:RenderingControl:1",
                    "url": self.url("dmr/upnp/control/RenderingControl1"),
                },
            },
        })

    def test_embedded_renderer_without_rendering_control(self):
        tv = discovery.describe(self.url("embedded.xml"))
        self.assertEqual(
            (tv["id"], tv["name"], tv["model"], tv["ip"]),
            ("uuid:00000000-0000-0000-0000-000000000003", "Bedroom TV", "", "127.0.0.1"))
        self.assertEqual(tv["services"], {
            "AVTransport": {
                "type": "urn:schemas-upnp-org:service:AVTransport:3",
                "url": "http://127.0.0.1:49152/base/ctl/avt",  # resolved against URLBase
            },
        })

    def test_failures_name_the_description_url(self):
        for path, reason in [
            ("gateway.xml", "not a UPnP MediaRenderer"),
            ("missing.xml", "HTTP Error 404"),
            ("file-control.xml", "control URL file:///etc/hosts, which is not http://"),
            ("foreign-control.xml", "control URL http://192.0.2.99/avt, which is not http:// on 127.0.0.1"),
            ("doctype.xml", "has a DTD"),
        ]:
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError) as cm:
                    discovery.describe(self.url(path))
                self.assertIn(self.url(path), str(cm.exception))
                self.assertIn(reason, str(cm.exception))

    def test_ssdp_location_header_is_case_insensitive(self):
        for name in ("LOCATION", "Location", "location"):
            response = b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n%s: http://192.0.2.10:9197/dmr\r\n\r\n" % name.encode()
            self.assertEqual(discovery.ssdp_location(response), "http://192.0.2.10:9197/dmr")
        self.assertIsNone(discovery.ssdp_location(b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n\r\n"))

    def test_discover_dedupes_sorts_and_skips_failing_devices(self):
        # A fake device on loopback answers the first M-SEARCH for every description page.
        device = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(device.close)
        device.bind(("127.0.0.1", 0))
        device.settimeout(5)
        pages = ["dmr/renderer.xml", "dmr/renderer.xml", "copy/renderer.xml", "embedded.xml", "gateway.xml"]
        requests = []

        def answer():
            request, sender = device.recvfrom(65535)
            requests.append(request)
            for page in pages:
                device.sendto(b"HTTP/1.1 200 OK\r\nLOCATION: %s\r\n\r\n" % self.url(page).encode(), sender)

        thread = threading.Thread(target=answer)
        thread.start()
        log = io.StringIO()
        with mock.patch.object(discovery, "SSDP_ADDR", device.getsockname()), contextlib.redirect_stdout(log):
            tvs = discovery.discover(timeout=0.5)
        thread.join()
        self.assertIn(b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n", requests[0])
        self.assertEqual([tv["name"] for tv in tvs], ["Bedroom TV", "Living Room TV"])
        self.assertIn("skipping", log.getvalue())
        self.assertIn(self.url("gateway.xml"), log.getvalue())


if __name__ == "__main__":
    unittest.main()
