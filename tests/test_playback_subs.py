"""Choosing, switching and serving subtitles during playback, with the TV replaced."""
import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tvcast import app as appmod
from tvcast.library import scan_library


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


class PlaybackSubtitlesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "Movies")
        self.elsewhere = os.path.join(tmp.name, "Downloads")
        write(os.path.join(self.root, "Film.mp4"), "video")
        write(os.path.join(self.root, "Other.mp4"), "video")
        for name in ("Film.srt", "Film.en.sdh.srt", "Film.fr.srt", "Film.en.commentary.srt", "Other.de.srt",
                     "Other.it.srt"):
            write(os.path.join(self.root, name), SRT)
        with mock.patch.object(appmod, "ThreadingHTTPServer"):
            self.app = appmod.App(argparse.Namespace(
                port=8765, panel_port=0, state=os.path.join(tmp.name, "state", "state.json"), library=[self.root]))
        self.app.movies, _, _ = scan_library([self.root])
        self.app.by_id = {m.id: m for m in self.app.movies}
        self.film, self.other = self.app.movies
        tv = dict(id="test", name="Test TV", ip="192.0.2.10", manufacturer="Samsung Electronics", services={})
        self.app.state.update_tv("test", **tv)
        self.app.state.set("tv", "test")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.transport = stack.enter_context(mock.patch.object(appmod, "transport", return_value=""))
        self.set_caption = stack.enter_context(mock.patch.object(appmod, "set_caption"))
        stack.enter_context(mock.patch.object(appmod, "captions", return_value=([{"CaptionURI": "x"}], [])))
        stack.enter_context(mock.patch.object(appmod, "lan_ip_towards", return_value="192.0.2.20"))
        stack.enter_context(mock.patch.object(appmod.subprocess, "Popen"))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def track(self, movie, label):
        return next(t for t in self.app.subtitle_tracks(movie) if t["label"] == label)

    def url(self, movie, track):
        return "http://192.0.2.20:8765/subs/%s/%s.srt" % (movie.id, track["id"])

    def shown(self):
        """The caption URL of the last Enable or Disable, and whether it was Enable."""
        args = self.set_caption.call_args[0]
        return args[3], args[1]

    def probed(self, *codecs):
        info = {"video": ["h264"], "audio": [], "duration": 60, "subtitles": [
            {"codec": c, "lang": "eng", "title": None, "forced": False, "sdh": False} for c in codecs]}
        self.app.state.set_probe(self.film.path, {"size": self.film.size, "mtime": self.film.mtime, "info": info})

    def test_file_named_like_the_video_by_default_and_only_it_goes_to_the_tv(self):
        self.app.open_movie(self.film.id)
        exact = self.track(self.film, "Film.srt")
        didl = self.transport.call_args_list[0][1]["CurrentURIMetaData"]
        self.assertEqual(didl.count("/subs/"), 3)  # res, CaptionInfoEx and CaptionInfo for one file
        self.assertIn(self.url(self.film, exact), didl)
        self.assertEqual(self.shown(), (self.url(self.film, exact), True))

    def test_tracks_inside_the_video_come_first_unless_the_tv_cant_draw_them(self):
        self.probed("subrip")
        self.app.open_movie(self.film.id)
        self.assertIsNone(self.app.current.subtitle)
        self.assertNotIn("/subs/", self.transport.call_args_list[0][1]["CurrentURIMetaData"])
        self.probed("hdmv_pgs_subtitle")
        self.app.open_movie(self.film.id)
        self.assertEqual(self.app.current.subtitle["label"], "Film.srt")

    def test_other_files_wait_for_a_choice_which_is_remembered(self):
        self.app.open_movie(self.other.id)
        self.assertIsNone(self.app.current.subtitle)
        self.app.control("subtitles", {"track": "german"})
        self.app.open_movie(self.film.id)
        self.assertEqual(self.app.current.subtitle["label"], "Film.srt")  # a choice is per movie
        self.app.open_movie(self.other.id)
        self.assertEqual(self.app.current.subtitle["label"], "German")

    def test_live_switch_off_and_back_on(self):
        self.app.open_movie(self.film.id)
        french = self.track(self.film, "French")
        self.app.control("subtitles", {"track": "french"})
        self.assertEqual(self.shown(), (self.url(self.film, french), True))
        self.app.control("subtitles", {"track": "off"})
        self.assertEqual(self.shown(), (self.url(self.film, french), False))
        self.set_caption.reset_mock()
        self.app.open_movie(self.film.id)
        self.set_caption.assert_not_called()  # this movie stays off
        self.assertIsNone(self.app.current.subtitle)
        self.app.control("subtitles", {"track": "on"})
        self.assertEqual(self.app.current.subtitle["label"], "Film.srt")
        self.app.control("subtitles", {"track": "french"})
        self.app.control("subtitles", {"track": "off"})
        self.app.control("subtitles", {"track": "on"})
        self.assertEqual(self.shown(), (self.url(self.film, french), True))

    def test_named_track_at_open_and_errors_for_unknown_ones(self):
        self.app.open_movie(self.film.id, subtitle="sdh")
        self.assertEqual(self.app.current.subtitle["label"], "English SDH")
        with self.assertRaisesRegex(ValueError, "several subtitles 'ish'"):
            self.app.control("subtitles", {"track": "ish"})
        with self.assertRaisesRegex(ValueError, "no subtitles 'klingon'"):
            self.app.control("subtitles", {"track": "klingon"})

    def test_added_file_from_elsewhere_is_served_switched_to_and_removable(self):
        self.app.open_movie(self.film.id)
        with self.assertRaisesRegex(ValueError, "not a subtitle file"):
            self.app.control("add_subtitles", {"path": write(os.path.join(self.elsewhere, "notes.txt"))})
        with self.assertRaisesRegex(ValueError, "no subtitle file at"):
            self.app.control("add_subtitles", {"path": os.path.join(self.elsewhere, "gone.srt")})
        ass = write(os.path.join(self.elsewhere, "mine.ass"),
                    "[Events]\nFormat: Start, End, Text\nDialogue: 0:00:01.00,0:00:02.00,Mine\n")
        added = self.app.control("add_subtitles", {"path": ass})
        track = self.track(self.film, "mine.ass")
        self.assertEqual(added["track"], track["id"])
        self.assertEqual(self.shown(), (self.url(self.film, track), True))
        served, ctype, movie = self.app.resolve("/subs/%s/%s.srt" % (self.film.id, track["id"]))
        self.assertEqual((ctype, movie), ("text/srt", None))
        with open(served) as f:
            self.assertIn("Mine", f.read())
        self.app.control("remove_subtitles", {"track": track["id"]})
        self.assertEqual(self.shown(), (self.url(self.film, track), False))
        self.assertNotIn("mine.ass", [t["label"] for t in self.app.subtitle_tracks(self.film)])
        self.assertIsNone(self.app.state.movie(self.film.path).get("subtitle"))
        self.app.control("subtitles", {"track": "on"})  # not the removed file
        self.assertEqual(self.app.current.subtitle["label"], "Film.srt")

    def test_only_a_movies_own_tracks_are_served(self):
        german = self.track(self.other, "German")
        self.assertIsNone(self.app.resolve("/subs/%s/%s.srt" % (self.film.id, german["id"])))
        self.assertEqual(self.app.resolve("/subs/%s/%s.srt" % (self.other.id, german["id"]))[0], german["path"])

    def test_embedded_tracks_come_from_the_probe_and_old_probes_are_redone(self):
        stamp = {"size": self.film.size, "mtime": self.film.mtime}
        self.app.state.set_probe(self.film.path, dict(stamp, info={"video": ["h264"], "audio": [], "duration": 60}))
        self.assertIsNone(self.app.probe_info(self.film))  # saved before subtitle tracks were listed
        self.app.state.set_probe(self.film.path, dict(stamp, info={"video": ["h264"], "audio": [], "duration": 60, "subtitles": [
            {"codec": "subrip", "lang": "ger", "title": None, "forced": False, "sdh": False},
            {"codec": "hdmv_pgs_subtitle", "lang": "eng", "title": None, "forced": False, "sdh": False},
        ]}))
        self.assertTrue(self.app.has_embedded_subtitles(self.film))
        self.app.open_movie(self.film.id)
        self.assertEqual(self.app.subtitle_status(self.app.current)["embedded"],
                         [{"label": "German", "image": False}, {"label": "English", "image": True}])
        self.assertEqual(self.app.movie_info(self.film)["subs"], {"count": 6, "languages": ["de", "en", "fr"]})

    def test_broken_default_file_still_plays_the_movie(self):
        os.chmod(os.path.join(self.root, "Film.srt"), 0)
        self.addCleanup(os.chmod, os.path.join(self.root, "Film.srt"), 0o644)
        self.app.open_movie(self.film.id)
        self.assertIsNone(self.app.current.subtitle)
        self.set_caption.assert_not_called()


if __name__ == "__main__":
    unittest.main()
