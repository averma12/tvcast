import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tvcast import app as appmod

TV = {
    "id": "uuid:00000000-0000-0000-0000-000000000001", "name": "Living Room TV", "manufacturer": "Samsung Electronics",
    "model": "", "ip": "192.0.2.10", "location": "http://192.0.2.10:9197/dmr", "services": {},
}


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(appmod, "ThreadingHTTPServer"):  # no sockets
            self.app = appmod.App(argparse.Namespace(
                port=0, panel_port=0, state=os.path.join(tmp.name, "state.json"), library=[tmp.name]))
        self.app.state.update_tv(TV["id"], remote=True, remote_token="token", remote_cert="pinned", **TV)

    def rediscover_at_new_ip(self, cert):
        moved = dict(TV, ip="192.0.2.66", location="http://192.0.2.66:9197/dmr")
        with mock.patch.object(appmod.discovery, "discover", return_value=[moved]), \
                mock.patch.object(appmod.samsung, "fingerprint", return_value=cert) as fingerprint, \
                contextlib.redirect_stdout(io.StringIO()) as log:
            found = self.app.discover()
        fingerprint.assert_called_once_with("192.0.2.66")
        return found, self.app.state.tv(TV["id"]), log.getvalue()

    def test_paired_tv_moves_only_with_its_pinned_certificate(self):
        found, tv, log = self.rediscover_at_new_ip("another certificate")
        self.assertEqual((found, tv["ip"]), ([], "192.0.2.10"))
        self.assertIn("ignoring Living Room TV at 192.0.2.66: its certificate doesn't match the paired TV", log)

        found, tv, _ = self.rediscover_at_new_ip("pinned")
        self.assertEqual([t["ip"] for t in found], ["192.0.2.66"])
        self.assertEqual((tv["ip"], tv["location"], tv["remote_token"]), ("192.0.2.66", "http://192.0.2.66:9197/dmr", "token"))


if __name__ == "__main__":
    unittest.main()
