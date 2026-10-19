import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tvcast import app as appmod

OLD, NEW = "192.0.2.10", "192.0.2.66"
TV = {
    "id": "uuid:00000000-0000-0000-0000-000000000001", "name": "Living Room TV", "manufacturer": "Samsung Electronics",
    "model": "", "ip": OLD, "location": "http://%s:9197/dmr" % OLD, "services": {},
}
MOVED = dict(TV, ip=NEW, location="http://%s:9197/dmr" % NEW)


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(appmod, "ThreadingHTTPServer"):  # no sockets
            self.app = appmod.App(argparse.Namespace(
                port=0, panel_port=0, state=os.path.join(tmp.name, "state.json"), library=[tmp.name]))
        self.app.state.update_tv(TV["id"], remote=True, remote_token="token", remote_cert="pinned", **TV)

    def rediscover(self, records, certs):
        """Run a search that finds records, where certs maps an IP to the certificate it presents."""
        with mock.patch.object(appmod.discovery, "discover", return_value=records), \
                mock.patch.object(appmod.samsung, "fingerprint", side_effect=certs.get), \
                mock.patch.object(appmod.samsung, "has_remote", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            found = self.app.discover()
        return found, self.app.state.tv(TV["id"]), log.getvalue()

    def test_relay_while_the_paired_tv_still_answers_at_its_old_address(self):
        found, tv, log = self.rediscover([MOVED], {OLD: "pinned", NEW: "pinned"})
        self.assertEqual((found, tv["ip"]), ([], OLD))
        self.assertIn("ignoring Living Room TV at %s: the paired TV still answers at its old address" % NEW, log)

    def test_paired_tv_moves_when_its_certificate_moved_with_it(self):
        found, tv, log = self.rediscover([MOVED], {NEW: "another certificate"})
        self.assertEqual((found, tv["ip"]), ([], OLD))
        self.assertIn("its certificate doesn't match the paired TV", log)

        found, tv, _ = self.rediscover([MOVED], {NEW: "pinned"})
        self.assertEqual([t["ip"] for t in found], [NEW])
        self.assertEqual((tv["ip"], tv["location"], tv["remote_token"]), (NEW, MOVED["location"], "token"))

    def test_an_id_claimed_from_two_addresses_is_ignored(self):
        found, tv, log = self.rediscover([TV, MOVED], {NEW: "pinned"})
        self.assertEqual((found, tv["ip"]), ([], OLD))
        self.assertEqual(log.count("2 devices answer as Living Room TV"), 2)


if __name__ == "__main__":
    unittest.main()
