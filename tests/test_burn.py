"""Subtitles burned into the picture for TVs that show no subtitle files: SRT parsing, drawing and
re-timing PGS, the per-TV subtitle mode, and live, copy and background-burn playback."""
import argparse
import contextlib
import io
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tvcast import app as appmod
from tvcast import burn
from tvcast.library import scan_library

REAL_POPEN = subprocess.Popen  # the playback tests patch Popen; the burn-job tests run real shells
REAL_PREPARE = burn.prepare  # the playback tests patch prepare and Thread; one test needs both for real
REAL_THREAD = __import__("threading").Thread
REAL_HLS = burn.Hls
SRT = "1\n00:01:17,936 --> 00:01:21,212\nHi.\n"


def display_sets(data):
    """[(pts seconds, segment kinds)] of a PGS file."""
    sets, kinds, pos = [], [], 0
    while pos < len(data):
        assert data[pos:pos + 2] == b"PG"
        pts, kind, size = struct.unpack(">I4xBH", data[pos + 2:pos + 13])
        kinds.append(kind)
        pos += 13 + size
        if kind == 0x80:
            sets.append((pts / 90000, kinds))
            kinds = []
    return sets


SHOW = (0x16, 0x17, 0x14, 0x15, 0x80)
CLEAR = (0x16, 0x17, 0x80)


def pgs(*sets):
    return b"".join(b"PG" + struct.pack(">IIBH", round(pts * 90000), 0, k, 0) for pts, kinds in sets for k in kinds)


class SrtTest(unittest.TestCase):
    def test_parses_tags_line_endings_and_clips_overlaps(self):
        text = ("1\r\n00:00:01,000 --> 00:00:04,000\r\n<i>Hello</i>\r\n{\\an8}there\r\n\r\n"
                "junk without timing\n\n"
                "2\n00:00:03,500 --> 00:00:05,250\nNext\n\n"
                "3\n00:00:09,000 --> 00:00:08,000\nBackwards\n")
        self.assertEqual(burn.parse_srt(text), [(1.0, 3.5, "Hello\nthere"), (3.5, 5.25, "Next")])


class SegmentSupTest(unittest.TestCase):
    def cut(self, start, end):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in.sup"), os.path.join(tmp, "out.sup")
            with open(src, "wb") as f:
                f.write(pgs((0, CLEAR), (10, SHOW), (12, CLEAR), (20, SHOW), (25, CLEAR)))
            burn.segment_sup(burn.read_sets(src), start, end, out)
            with open(out, "rb") as f:
                return [(round(t, 3), tuple(k)) for t, k in display_sets(f.read())]

    def test_cue_on_screen_at_the_start_shows_from_zero(self):
        self.assertEqual(self.cut(11, 17), [(0, CLEAR), (0, SHOW), (1, CLEAR)])

    def test_only_the_segments_own_cues_are_kept(self):
        self.assertEqual(self.cut(18, 24), [(0, CLEAR), (2, SHOW)])
        self.assertEqual(self.cut(13, 19), [(0, CLEAR)])


class HlsTest(unittest.TestCase):
    def test_playlist_lists_every_segment_with_a_short_last_one(self):
        lines = burn.playlist(20.0).splitlines()
        self.assertEqual([l for l in lines if l.endswith(".ts")], ["0.ts", "1.ts", "2.ts", "3.ts"])
        self.assertIn("#EXTINF:2.000,", lines)
        self.assertEqual(lines[-1], "#EXT-X-ENDLIST")

    def test_segments_encode_their_audio_so_it_starts_where_the_video_does(self):
        info = {"ten_bit": True, "primaries": "bt2020", "transfer": "smpte2084", "space": "bt2020nc"}
        cmd = burn.segment_command("film.mp4", "s.sup", 330.0, 6.0, dict(info, audio="eac3"), "55.ts")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "eac3")
        self.assertEqual(cmd[cmd.index("-output_ts_offset") + 1], "340.000")

    def test_copies_keep_audio_mp4_and_tvs_take_and_convert_the_rest(self):
        info = {"ten_bit": False}
        for codec, want in (("eac3", "copy"), ("aac", "copy"), ("ac3", "copy"), ("truehd", "eac3"), ("dts", "eac3")):
            with self.subTest(codec=codec):
                cmd = burn.burn_command("film.mkv", "s.sup", dict(info, audio=codec), "out.mp4")
                self.assertEqual(cmd[cmd.index("-c:a") + 1], want)

    def session(self, encode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        movie = mock.Mock(path="film.mp4")
        with mock.patch.object(burn, "CACHE", tmp.name), \
                mock.patch.object(burn, "prepare", return_value=("s.sup", {"duration": 100.0})), \
                mock.patch.object(burn, "read_sets", return_value=[]), \
                mock.patch.object(burn, "subs_key", return_value="key"):
            hls = burn.Hls(movie, "film.srt")
        hls.encode = lambda n: encode(hls, n)
        self.addCleanup(hls.stop)
        return hls

    def test_a_segment_is_encoded_when_asked_for_and_the_next_ones_ahead(self):
        def encode(hls, n):
            with open(hls.path(n), "w") as f:
                f.write(str(n))
        hls = self.session(encode)
        with open(hls.segment(5)) as f:
            self.assertEqual(f.read(), "5")
        for _ in range(100):  # the read-ahead finishes in the background
            with hls.lock:
                if set(range(5, 5 + burn.AHEAD + 1)) <= hls.done:
                    break
            time.sleep(0.02)
        self.assertTrue(set(range(5, 5 + burn.AHEAD + 1)) <= hls.done)
        self.assertNotIn(4, hls.done)

    def test_a_failed_segment_reports_its_error(self):
        def encode(hls, n):
            raise RuntimeError("ffmpeg failed on segment %d" % n)
        with self.assertRaisesRegex(RuntimeError, "segment 3"):
            self.session(encode).segment(3)


@unittest.skipUnless(shutil.which("swiftc", path="/usr/bin"), "needs the Command Line Tools")
class RenderTest(unittest.TestCase):
    def test_draws_each_cue_as_a_display_set_and_a_clear(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(burn, "CACHE", tmp):
            cues = os.path.join(tmp, "cues.tsv")
            with open(cues, "w") as f:
                f.write("1.000\t2.500\tHello\n3.000\t4.000\tTwo\\nlines\n")
            out = os.path.join(tmp, "out.sup")
            subprocess.run([burn.renderer(), cues, out, "1920", "1080", "1"], check=True, capture_output=True)
            with open(out, "rb") as f:
                sets = display_sets(f.read())
        self.assertEqual([round(t, 3) for t, _ in sets], [0, 1, 2.5, 3, 4])
        self.assertEqual([tuple(k) for _, k in sets], [CLEAR, SHOW, CLEAR, SHOW, CLEAR])


class SubtitleModeTest(unittest.TestCase):
    def test_skyworth_renderers_burn_and_others_send_files_unless_a_mode_is_saved(self):
        self.assertEqual(appmod.subtitle_mode({"manufacturer": "Skyworth-Coocaa"}), "burn")
        self.assertEqual(appmod.subtitle_mode({"manufacturer": "Samsung Electronics"}), "file")
        self.assertEqual(appmod.subtitle_mode({"manufacturer": "Skyworth-Coocaa", "subtitle_mode": "file"}), "file")
        self.assertEqual(appmod.subtitle_mode({"manufacturer": "Sony", "subtitle_mode": "burn"}), "burn")


class FakeHls:
    """burn.Hls without encoding: a fixed running time and segment paths that just exist."""

    def __init__(self, movie, srt):
        self.movie, self.srt, self.duration, self.stopped = movie, srt, 8700.0, False

    def segment(self, n):
        return "/segments/%d.ts" % n

    def stop(self):
        self.stopped = True


class BurnCase(unittest.TestCase):
    """A library with two movies and their .srt files, a TV that burns subtitles in, and the
    TV, encoders and threads replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "Movies")
        os.makedirs(self.root)
        for name, text in (("Film.mp4", "video"), ("Film.srt", SRT), ("Other.mp4", "video"), ("Other.srt", SRT)):
            with open(os.path.join(self.root, name), "w") as f:
                f.write(text)
        with mock.patch.object(appmod, "ThreadingHTTPServer"):
            self.app = appmod.App(argparse.Namespace(
                port=8765, panel_port=0, state=os.path.join(tmp.name, "state", "state.json"), library=[self.root]))
        self.app.movies, _, _ = scan_library([self.root])
        self.app.by_id = {m.id: m for m in self.app.movies}
        self.film, self.other = self.app.movies
        self.track = self.app.subtitle_tracks(self.film)[0]
        self.set_tv("Skyworth-Coocaa")
        self.calls = []

        def transport(tv, action, **args):
            self.calls.append((action, args.get("CurrentURI"), args.get("CurrentURIMetaData", ""), args.get("Target")))
            return ""

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.burned_dir = os.path.join(tmp.name, "burned")
        stack.enter_context(mock.patch.object(burn, "BURNED_DIR", self.burned_dir))
        stack.enter_context(mock.patch.object(burn, "available", return_value=None))
        stack.enter_context(mock.patch.object(burn, "prepare", return_value=("subs.sup", {"duration": 8700.0})))
        stack.enter_context(mock.patch.object(burn, "Hls", FakeHls))
        stack.enter_context(mock.patch.object(appmod, "transport", side_effect=transport))
        stack.enter_context(mock.patch.object(appmod, "set_caption"))
        stack.enter_context(mock.patch.object(appmod, "captions", return_value=([], [])))
        stack.enter_context(mock.patch.object(appmod, "lan_ip_towards", return_value="192.0.2.20"))
        stack.enter_context(mock.patch.object(appmod, "mac_power", return_value={}))
        stack.enter_context(mock.patch.object(appmod, "tv_status", side_effect=RuntimeError("off")))
        stack.enter_context(mock.patch.object(appmod.subprocess, "Popen"))
        self.threads = stack.enter_context(mock.patch.object(appmod.threading, "Thread"))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def set_tv(self, manufacturer):
        self.app.state.update_tv("tv", id="tv", name="Test TV", ip="192.0.2.10", manufacturer=manufacturer, services={})
        self.app.state.set("tv", "tv")

    def url(self, kind, movie=None):
        movie = movie or self.film
        track = self.app.subtitle_tracks(movie)[0]
        if kind == "hls":
            return "http://192.0.2.20:8765/hls/%s/%s/index.m3u8" % (movie.id, track["id"])
        return "http://192.0.2.20:8765/burned/%s/%s.mp4" % (movie.id, track["id"])

    def make_copy(self):
        os.makedirs(self.burned_dir, exist_ok=True)
        with open(burn.burned_path(self.film, self.track["path"]), "w") as f:
            f.write("burned copy")



class BurnPlaybackTest(BurnCase):
    def test_without_a_copy_it_plays_hls_and_makes_no_copy(self):
        self.app.state.update_movie(self.film.path, position=430)
        self.app.open_movie(self.film.id)
        self.assertEqual([c[0] for c in self.calls], ["SetAVTransportURI", "Play", "Seek"])
        self.assertEqual((self.calls[0][1], self.calls[2][3]), (self.url("hls"), "0:07:05"))
        didl = self.calls[0][2]
        self.assertIn("application/vnd.apple.mpegurl", didl)
        self.assertIn('duration="2:25:00.000"', didl)
        self.assertNotIn("/subs/", didl)
        self.assertEqual(self.app.status()["burns"], [])
        self.threads.assert_not_called()

    def test_hls_urls_serve_the_playlist_and_segments_of_one_movie_at_a_time(self):
        self.app.open_movie(self.film.id)
        base = "/hls/%s/%s/" % (self.film.id, self.track["id"])
        playlist, ctype = self.app.hls_file(base + "index.m3u8")
        self.assertEqual(ctype, "application/vnd.apple.mpegurl")
        self.assertIn(b"1449.ts", playlist)
        self.assertEqual(self.app.hls_file(base + "12.ts"), ("/segments/12.ts", "video/mp2t"))
        self.assertIsNone(self.app.hls_file("/hls/%s/f0000000000/index.m3u8" % self.film.id))
        first = self.app.hls
        self.app.open_movie(self.other.id)
        self.assertTrue(first.stopped)

    def test_a_finished_copy_plays_like_a_file_unless_hls_is_asked_for(self):
        self.make_copy()
        self.app.open_movie(self.film.id)
        self.assertEqual(self.calls[0][1], self.url("copy"))
        self.assertIn("video/mp4", self.calls[0][2])
        self.assertEqual(self.app.resolve("/burned/%s/%s.mp4" % (self.film.id, self.track["id"]), {})[0],
                         burn.burned_path(self.film, self.track["path"]))
        self.app.open_movie(self.film.id, style="hls")
        self.assertEqual(self.calls[-2][1], self.url("hls"))

    def test_asking_for_a_copy_that_isnt_there_starts_making_one(self):
        with self.assertRaisesRegex(ValueError, "no copy with these subtitles yet; making one now"):
            self.app.open_movie(self.film.id, style="copy")
        self.assertEqual([(j["subtitle"], j["running"]) for j in self.app.status()["burns"]], [("Film.srt", True)])
        self.threads.assert_called_once()

    def test_hls_or_copy_needs_a_tv_that_burns_subtitles(self):
        self.set_tv("Samsung Electronics")
        with self.assertRaisesRegex(ValueError, "needs a TV that burns subtitles"):
            self.app.open_movie(self.film.id, style="hls")

    def test_seeking_is_left_to_the_tv(self):
        self.app.open_movie(self.film.id)
        self.app.control("seek", {"seconds": 3600})
        self.assertEqual(self.calls[-1][::3], ("Seek", "1:00:00"))

    def test_subtitles_off_reloads_the_plain_video_where_it_was(self):
        self.app.open_movie(self.film.id)
        with mock.patch.object(appmod, "transport_status", return_value=dict(position=630.0)):
            self.app.switch_subtitles("off")
        self.assertEqual(self.calls[-3][1], "http://192.0.2.20:8765/media/%s.mp4" % self.film.id)
        self.assertEqual(self.calls[-1][::3], ("Seek", "0:10:30"))

    def test_copies_are_listed_and_deleted_on_request_but_not_while_playing(self):
        self.make_copy()
        self.assertEqual([(c["title"], c["size"]) for c in self.app.status()["copies"]], [("Film", 11)])
        self.assertTrue(self.app.movie_info(self.film)["copy"])
        self.app.open_movie(self.film.id)
        with self.assertRaisesRegex(ValueError, "playing from its copy"):
            self.app.control("delete_copy", {"id": self.film.id})
        self.app.open_movie(self.other.id)
        self.assertEqual(self.app.control("delete_copy", {"id": self.film.id}), {"deleted": 1, "bytes": 11})
        self.assertEqual(self.app.status()["copies"], [])
        with self.assertRaisesRegex(ValueError, "no copy"):
            self.app.control("delete_copy", {"id": self.film.id})

    def test_samsung_tvs_still_get_the_file(self):
        self.set_tv("Samsung Electronics")
        self.app.open_movie(self.film.id)
        self.assertEqual(self.calls[0][1], "http://192.0.2.20:8765/media/%s.mp4" % self.film.id)
        self.assertIn("/subs/", self.calls[0][2])

    def test_without_ffmpeg_default_subtitles_go_out_as_a_file_but_asked_for_ones_fail(self):
        with mock.patch.object(burn, "available", return_value="needs ffmpeg"):
            self.app.open_movie(self.film.id)
            self.assertIn("/subs/", self.calls[0][2])
            with self.assertRaisesRegex(ValueError, "needs ffmpeg"):
                self.app.open_movie(self.film.id, subtitle="1")

    def test_subtitle_mode_is_saved_per_tv(self):
        self.assertEqual(self.app.control("subtitle_mode", {"mode": "file"}), {"mode": "file", "subtitles": "file"})
        self.app.open_movie(self.film.id)
        self.assertIn("/subs/", self.calls[0][2])
        tv = self.app.status()["tvs"][0]
        self.assertEqual((tv["subtitle_mode"], tv["subtitles"]), ("file", "file"))
        with self.assertRaises(ValueError):
            self.app.control("subtitle_mode", {"mode": "sometimes"})

    def test_tracks_inside_the_video_dont_hide_the_file_on_a_tv_that_shows_none(self):
        info = {"video": ["hevc"], "audio": [], "duration": 60, "subtitles": [
            {"codec": "mov_text", "lang": "eng", "title": None, "forced": False, "sdh": False}]}
        self.app.state.set_probe(self.film.path, {"size": self.film.size, "mtime": self.film.mtime, "info": info})
        self.app.open_movie(self.film.id)
        self.assertEqual(self.app.current.subtitle["id"], self.track["id"])
        self.set_tv("Samsung Electronics")
        self.app.open_movie(self.film.id)
        self.assertIsNone(self.app.current.subtitle)  # a Samsung shows the video's own tracks

    def run_burn(self, script):
        fake = lambda path, sup, info, out: ["/bin/sh", "-c", script % out]
        popen = lambda args, **kw: mock.Mock() if args[0] == "/usr/bin/caffeinate" else REAL_POPEN(args, **kw)
        with mock.patch.object(burn, "burn_command", side_effect=fake), \
                mock.patch.object(appmod.subprocess, "Popen", side_effect=popen):
            self.assertEqual(self.app.start_burn(self.film), {"started": True})
            self.app._burn(self.film, self.track["path"], self.track["path"])  # what the background thread runs
        return self.app.status()["burns"][0]

    def test_burn_job_reports_progress_and_leaves_a_finished_copy(self):
        job = self.run_burn("printf 'out_time_us=4350000000\\nprogress=continue\\n'; echo x > '%s'")
        self.assertEqual((job["progress"], job["running"], job["error"]), (1.0, False, None))
        self.assertTrue(burn.burned(self.film, self.track["path"]))
        self.assertEqual(self.app.start_burn(self.film), {"ready": True})

    def test_failed_burn_job_reports_the_error_and_leaves_nothing(self):
        job = self.run_burn("echo x > '%s'; echo 'disk full' >&2; exit 1")
        self.assertIn("disk full", job["error"])
        self.assertFalse(job["running"])
        self.assertEqual(os.listdir(self.burned_dir), [])

    def test_a_running_burn_estimates_when_it_is_ready(self):
        self.app.burns[self.film.id] = {"title": "Film", "subtitle": "Film.srt", "progress": 0.5, "running": True,
                                        "error": None, "started": time.time() - 600}
        self.assertRegex(self.app.burn_eta(self.film.id), r"^ready around \d\d:\d\d$")
        self.app.burns[self.film.id]["progress"] = 0.01
        self.assertIsNone(self.app.burn_eta(self.film.id))


class ReviewFixesTest(BurnCase):
    """Fixes from the first review of the burn-in change."""

    def test_legacy_code_page_subtitles_render_as_text(self):
        path = os.path.join(self.root, "legacy.srt")
        with open(path, "wb") as f:
            f.write(b"1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9 \x93hi\x94\n")
        self.assertEqual(burn.parse_srt(burn.read_text(path)), [(1.0, 2.0, "Café “hi”")])

    def test_concurrent_drawing_of_the_same_subtitles_doesnt_collide(self):
        script = os.path.join(self.root, "render.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nsleep 0.2\necho drawn > \"$2\"\n")
        os.chmod(script, 0o755)
        cache = os.path.join(self.root, "cache")
        results, errors = [], []
        with mock.patch.object(burn, "prepare", REAL_PREPARE), mock.patch.object(burn, "CACHE", cache), \
                mock.patch.object(appmod.subprocess, "Popen", REAL_POPEN), \
                mock.patch.object(burn, "renderer", return_value=script), \
                mock.patch.object(burn, "video_info", return_value={"width": 1920, "height": 1080, "transfer": None,
                                                                    "duration": 60.0}):
            def run():
                try:
                    results.append(burn.prepare(self.film, self.track["path"])[0])
                except Exception as e:  # noqa: BLE001 - reported below
                    errors.append(e)
            threads = [REAL_THREAD(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        with open(results[0]) as f:
            self.assertEqual(f.read(), "drawn\n")
        self.assertEqual([n for n in os.listdir(os.path.dirname(results[0])) if n.endswith(".part")], [])

    def test_the_copies_folder_is_not_scanned_as_movies(self):
        burned = os.path.join(self.root, "TV Cast subtitles")
        os.makedirs(burned)
        with open(os.path.join(burned, "abc-def.part.mp4"), "w") as f:
            f.write("half a copy")
        with mock.patch("tvcast.library.BURNED_DIR", burned):
            movies, _, _ = scan_library([self.root])
        self.assertEqual(sorted(m.title for m in movies), ["Film", "Other"])

    def test_a_stale_hls_request_doesnt_replace_the_playing_session(self):
        self.app.open_movie(self.film.id)
        old = "/hls/%s/%s/3.ts" % (self.film.id, self.track["id"])
        self.app.open_movie(self.other.id)
        playing = self.app.hls
        self.assertIsNone(self.app.hls_file(old))
        self.assertIs(self.app.hls, playing)
        self.assertFalse(playing.stopped)

    def test_a_samsung_set_to_burn_reloads_to_change_subtitles(self):
        self.set_tv("Samsung Electronics")
        self.app.control("subtitle_mode", {"mode": "burn"})
        self.app.open_movie(self.film.id)
        self.assertTrue(self.app.current.hls)
        with mock.patch.object(appmod, "transport_status", return_value=dict(position=60.0)):
            self.app.switch_subtitles("off")
        self.assertEqual(self.calls[-3][1], "http://192.0.2.20:8765/media/%s.mp4" % self.film.id)
        appmod.set_caption.assert_not_called()

    def test_status_scans_the_copies_folder_once(self):
        with mock.patch.object(self.app, "copies", wraps=self.app.copies) as copies:
            self.app.status()
        self.assertEqual(copies.call_count, 1)

    def test_a_copy_counts_only_for_the_subtitles_it_has(self):
        self.make_copy()
        with open(os.path.join(self.root, "Film.fr.srt"), "w") as f:
            f.write(SRT)
        self.app.movies, _, _ = scan_library([self.root])
        self.app.by_id = {m.id: m for m in self.app.movies}
        self.film = self.app.by_id[self.film.id]
        self.app.open_movie(self.film.id, subtitle="french")
        self.assertIsNone(self.app.subtitle_status(self.app.current)["copy"])
        self.app.open_movie(self.film.id, subtitle="Film.srt")
        self.assertEqual(self.app.subtitle_status(self.app.current)["copy"], 11)


class LifecycleTest(BurnCase):
    """Copies survive upgrades, HLS sessions end with their playback, and leftovers go at start."""

    def test_a_copy_is_found_by_the_chosen_file_even_when_its_converted_version_moves(self):
        self.make_copy()
        with mock.patch.object(appmod, "prepare", return_value="/cache/converted-by-another-version.srt"):
            self.app.open_movie(self.film.id)
        self.assertEqual(self.calls[0][1], self.url("copy"))

    def test_stopping_ends_the_hls_session(self):
        self.app.open_movie(self.film.id)
        session = self.app.hls
        self.app.control("stop", {})
        self.assertIsNone(self.app.hls)
        self.assertTrue(session.stopped)

    def test_plain_or_copy_playback_ends_the_hls_session(self):
        self.app.open_movie(self.film.id)
        session = self.app.hls
        with mock.patch.object(appmod, "transport_status", return_value=dict(position=60.0)):
            self.app.switch_subtitles("off")
        self.assertTrue(session.stopped)
        self.assertIsNone(self.app.hls)
        self.app.open_movie(self.film.id, subtitle="1")
        self.make_copy()
        session = self.app.hls
        self.app.open_movie(self.film.id)  # the copy now
        self.assertTrue(session.stopped)

    def test_choosing_another_tv_ends_the_hls_session(self):
        self.app.open_movie(self.film.id)
        session = self.app.hls
        self.app.state.update_tv("tv2", id="tv2", name="Other TV", ip="192.0.2.11", manufacturer="Sony", services={})
        self.app.select("tv2")
        self.assertTrue(session.stopped)

    def test_after_a_restart_the_tv_keeps_getting_its_hls_segments(self):
        url = self.url("hls")
        reply = dict(state="PLAYING", uri=url, position=100.0, duration=8700.0)
        with mock.patch.object(appmod, "transport_status", return_value=reply):
            segment = self.app.hls_file("/hls/%s/%s/17.ts" % (self.film.id, self.track["id"]))
        self.assertEqual(segment, ("/segments/17.ts", "video/mp2t"))
        self.assertTrue(self.app.current.hls)

    def test_start_up_removes_unfinished_copies_and_hls_segments(self):
        os.makedirs(self.burned_dir)
        for name in ("a-1.part.mp4", "b-2.mp4"):
            with open(os.path.join(self.burned_dir, name), "w") as f:
                f.write("x")
        cache = os.path.join(self.root, "cache")
        os.makedirs(os.path.join(cache, "hls", "key"))
        with mock.patch.object(burn, "CACHE", cache):
            notes = list(burn.clean_up())
        self.assertEqual(len(notes), 1)
        self.assertIn("a-1.part.mp4", notes[0])
        self.assertEqual(os.listdir(self.burned_dir), ["b-2.mp4"])
        self.assertFalse(os.path.exists(os.path.join(cache, "hls")))


class RaceTest(BurnCase):
    """Two callers at once after a restart (a TV request and the monitor) share one HLS session."""

    def test_sessions_for_the_same_movie_keep_separate_folders(self):
        cache = os.path.join(self.root, "cache")
        with mock.patch.object(burn, "CACHE", cache), \
                mock.patch.object(burn, "prepare", return_value=("s.sup", {"duration": 60.0})), \
                mock.patch.object(burn, "read_sets", return_value=[]):
            first, second = REAL_HLS(self.film, "f.srt"), REAL_HLS(self.film, "f.srt")
        self.assertNotEqual(first.dir, second.dir)
        first.stop()
        self.assertTrue(os.path.isdir(second.dir))
        second.stop()

    def test_concurrent_callers_share_one_session(self):
        made = []

        class SlowHls(FakeHls):
            def __init__(self, movie, srt):
                made.append(self)
                time.sleep(0.2)  # probing and drawing the subtitles take a while
                super().__init__(movie, srt)

        got = []
        with mock.patch.object(burn, "Hls", SlowHls):
            threads = [REAL_THREAD(target=lambda: got.append(self.app.hls_session(self.film, self.track)))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(made), 1)
        self.assertIs(got[0], got[1])
        self.assertFalse(got[0].stopped)
