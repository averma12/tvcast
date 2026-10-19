"""Persistent state: library folders, playback queue, resume points, codec probes and known TVs."""
import json
import os
import threading

DEFAULT_STATE = os.path.expanduser("~/Library/Application Support/tvcast/state.json")


class State:
    """The daemon's state, persisted as JSON after every change."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        except ValueError as e:
            raise SystemExit("tvcast state file %s is not valid JSON (%s); fix or delete it" % (path, e))
        self.data.setdefault("movies", {})  # resume points, keyed by absolute video path
        self.data.setdefault("probes", {})  # ffprobe summaries, keyed by absolute video path
        self.data.setdefault("tvs", {})  # every TV ever found, keyed by its UPnP device id
        self.data.pop("autoplay", None)
        self.data.setdefault("queue", [])

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            self._write()

    def movie(self, path):
        with self.lock:
            return dict(self.data["movies"].get(path, {}))

    def update_movie(self, path, **fields):
        with self.lock:
            self.data["movies"].setdefault(path, {}).update(fields)
            self._write()

    def probe(self, path):
        with self.lock:
            return self.data["probes"].get(path)

    def set_probe(self, path, entry):
        with self.lock:
            self.data["probes"][path] = entry
            self._write()

    def tvs(self):
        with self.lock:
            return [dict(tv) for tv in self.data["tvs"].values()]

    def tv(self, tv_id):
        with self.lock:
            tv = self.data["tvs"].get(tv_id)
            return dict(tv) if tv else None

    def update_tv(self, tv_id, **fields):
        # Merging keeps fields the update doesn't carry, such as the remote pairing when
        # discovery refreshes a TV's address.
        with self.lock:
            self.data["tvs"].setdefault(tv_id, {}).update(fields)
            self._write()

    def _write(self):
        # Write-then-rename so a crash mid-write never leaves a truncated state file.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            os.fchmod(f.fileno(), 0o600)  # it holds the remote pairing tokens
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
