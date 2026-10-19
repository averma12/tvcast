"""App launch must be confirmed by foreground state, not just an accepted HTTP request."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tvcast import samsung


class AppsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        app_id = self.path.rsplit("/", 1)[-1]
        info = self.server.apps.get(app_id)
        if info is None:
            self.send_error(404)
            return
        if self.server.launched == app_id:
            self.server.reads_after_launch += 1
            info = dict(info, visible=self.server.reads_after_launch >= 2)
        body = json.dumps(info).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.launched = self.path.rsplit("/", 1)[-1]
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"HTTP/")  # The AU7500 accepts launch with this non-JSON body.

    def log_message(self, *args):
        pass


class AppsTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AppsHandler)
        self.server.apps = {
            "111299001912": {"name": "YouTube", "running": True, "visible": False, "version": "1.0"},
            "3201807016597": {"name": "Apple TV", "running": True, "visible": True},
        }
        self.server.launched = None
        self.server.reads_after_launch = 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = mock.patch.object(samsung, "REST_PORT", self.server.server_address[1])
        port.start()
        self.addCleanup(port.stop)
        self.remote = samsung.Remote(None)
        self.tv = {"ip": "127.0.0.1", "name": "Test TV"}

    def test_apps_distinguish_foreground_from_running_and_skip_missing(self):
        apps = self.remote.apps(self.tv)
        self.assertEqual([a["name"] for a in apps], ["YouTube", "Apple TV"])
        self.assertEqual([(a["running"], a["visible"]) for a in apps], [(True, False), (True, True)])

    def test_launch_waits_for_visible_after_non_json_acknowledgement(self):
        with mock.patch.object(samsung.time, "sleep"):
            app = self.remote.launch(self.tv, "111299001912")
        self.assertTrue(app["visible"])
        self.assertEqual(self.server.reads_after_launch, 2)

    def test_accepted_but_unconfirmed_launch_is_an_error(self):
        with mock.patch.object(samsung.time, "monotonic", side_effect=[0, 0, 11]), \
                mock.patch.object(samsung.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not confirm that YouTube opened"):
                self.remote.launch(self.tv, "111299001912")
        self.assertEqual(self.server.reads_after_launch, 1)

    def test_missing_app_and_invalid_id_never_launch(self):
        for app_id, error in [("missing", "not installed"), ("../other", "invalid app ID")]:
            with self.subTest(app_id=app_id), self.assertRaisesRegex(ValueError, error):
                self.remote.launch(self.tv, app_id)
        self.assertIsNone(self.server.launched)

    def test_malformed_status_is_not_success_or_uninstalled(self):
        self.server.apps["111299001912"] = []
        with self.assertRaisesRegex(RuntimeError, "expected an app object"):
            self.remote.app_status(self.tv, "111299001912")

    def test_missing_or_non_boolean_foreground_state_stays_unknown(self):
        self.server.apps["111299001912"] = {"name": "YouTube", "visible": "false"}
        app = self.remote.app_status(self.tv, "111299001912")
        self.assertIsNone(app["visible"])
        self.assertIsNone(app["running"])
