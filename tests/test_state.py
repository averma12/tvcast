import os
import tempfile
import unittest

from tvcast.state import State


class StateTest(unittest.TestCase):
    def test_rediscovery_keeps_pairing_and_file_stays_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = State(path)
            record = {"id": "uuid:1", "name": "Living Room TV", "ip": "192.0.2.10", "services": {}}
            state.update_tv("uuid:1", **record)
            state.update_tv("uuid:1", remote_token="token", remote_cert="fingerprint")
            state.update_tv("uuid:1", **dict(record, ip="192.0.2.11", remote=True))
            tv = State(path).tv("uuid:1")
            self.assertEqual((tv["ip"], tv["remote_token"], tv["remote_cert"]), ("192.0.2.11", "token", "fingerprint"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
