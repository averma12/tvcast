"""Queue ordering and playback transitions, with the TV and macOS boundaries replaced."""
import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tvcast import app as appmod
from tvcast.library import scan_library
from tvcast.state import State


class QueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        for title in ("First", "Second", "Third"):
            with open(os.path.join(self.root, title + ".mp4"), "wb") as f:
                f.write(b"test")
        with mock.patch.object(appmod, "ThreadingHTTPServer"):
            self.app = appmod.App(argparse.Namespace(
                port=0, panel_port=0, state=os.path.join(tmp.name, "state.json"), library=[tmp.name]))
        self.app.movies, _, _ = scan_library([self.root])
        self.app.by_id = {m.id: m for m in self.app.movies}
        self.a, self.b, self.c = self.app.movies
        self.tv = dict(id="test", name="Test TV", ip="192.0.2.10", services={})
        self.app.state.update_tv("test", **self.tv)
        self.app.state.set("tv", "test")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(appmod, "transport", return_value=""))
        stack.enter_context(mock.patch.object(appmod, "lan_ip_towards", return_value="192.0.2.20"))
        stack.enter_context(mock.patch.object(appmod.subprocess, "Popen"))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def enqueue(self, *movies):
        for movie in movies:
            self.app.control("queue_add", {"id": movie.id})

    def ids(self):
        return [item["id"] for item in self.app.state.get("queue")]

    def near_end(self):
        self.app.open_movie(self.a.id)
        pb = self.app.current
        pb.seen_playing, pb.position, pb.duration = True, 998, 1000
        return pb

    def test_edits_preserve_order_and_persist_without_starting_playback(self):
        self.enqueue(self.a, self.b, self.a, self.c)
        self.assertEqual(self.ids(), [self.a.id, self.b.id, self.c.id])
        self.assertIsNone(self.app.current)
        self.app.control("queue_move", {"id": self.c.id, "direction": -1})
        self.app.control("queue_remove", {"id": self.a.id})
        saved = State(self.app.state.path).get("queue")
        self.assertEqual([item["id"] for item in saved], [self.c.id, self.b.id])
        self.app.control("queue_clear", {})
        self.assertEqual(self.ids(), [])

    def test_natural_end_plays_only_the_queue_head_once(self):
        self.enqueue(self.c, self.b)
        pb = self.near_end()
        reply = dict(state="STOPPED", uri=pb.media_url, position=0, duration=1000)
        with mock.patch.object(appmod, "transport_status", return_value=reply):
            self.app.poll(pb)
            self.assertIs(self.app.current, pb)
            self.app.poll(pb)
            self.app.poll(pb)
        self.assertEqual(self.app.current.movie.id, self.c.id)
        self.assertEqual(self.ids(), [self.b.id])
        self.assertTrue(self.app.state.movie(self.a.path)["watched"])

    def test_stop_other_media_and_offline_never_advance(self):
        for action in ("stop", "other_media", "offline"):
            with self.subTest(action=action):
                self.enqueue(self.b)
                pb = self.near_end()
                if action == "stop":
                    self.app.control("stop", {})
                else:
                    reply = dict(state="PLAYING", uri="http://elsewhere/movie.mp4")
                    with mock.patch.object(appmod, "transport_status", return_value=reply,
                                           side_effect=RuntimeError("offline") if action == "offline" else None):
                        if action == "offline":
                            pb.unreachable_since = 1
                        self.app.poll(pb)
                        self.app.poll(pb)
                self.assertIsNone(self.app.current)
                self.assertEqual(self.ids(), [self.b.id])

    def test_failed_load_keeps_head_for_retry_and_exposes_error(self):
        self.enqueue(self.a, self.b)
        with mock.patch.object(appmod, "transport", side_effect=RuntimeError("TV unreachable")):
            with self.assertRaisesRegex(RuntimeError, "TV unreachable"):
                self.app.control("queue_next", {})
        self.assertEqual(self.ids(), [self.a.id, self.b.id])
        self.assertEqual(self.app.queue_error, "TV unreachable")
        self.assertIsNone(self.app.current)
        self.app.control("queue_next", {})
        self.assertEqual(self.app.current.movie.id, self.a.id)
        self.assertEqual(self.ids(), [self.b.id])
        self.assertIsNone(self.app.queue_error)

    def test_removed_folder_keeps_queue_label_but_cannot_play(self):
        self.enqueue(self.a)
        self.app.state.set("libraries", [])
        with self.assertRaisesRegex(ValueError, "First is unavailable"):
            self.app.play_next()
        self.app.state.set("tv", None)
        with mock.patch.object(appmod, "mac_power", return_value={}):
            item = self.app.status()["queue"][0]
        self.assertEqual((item["title"], item["available"]), ("First", False))
        self.assertEqual(self.ids(), [self.a.id])

    def test_invalid_edits_do_not_change_queue(self):
        self.enqueue(self.a)
        for action, body in [("queue_add", {"id": "missing"}),
                             ("queue_move", {"id": self.a.id, "direction": 5}),
                             ("queue_remove", {"id": "missing"})]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.app.control(action, body)
        self.assertEqual(self.ids(), [self.a.id])

    def test_restarting_adopts_current_movie_without_repeating_it(self):
        self.enqueue(self.a, self.b)
        uri = "http://192.0.2.20/media/%s.mp4" % self.a.id
        with mock.patch.object(appmod, "transport_status", return_value=dict(state="PLAYING", uri=uri)):
            self.app.adopt()
        self.assertEqual(self.app.current.movie.id, self.a.id)
        self.assertEqual(self.ids(), [self.b.id])

    def test_empty_queue_does_not_start_a_collection_sibling(self):
        pb = self.near_end()
        self.app.finish(pb)
        self.assertIsNone(self.app.current)
        self.assertEqual(self.ids(), [])
