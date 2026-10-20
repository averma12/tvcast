import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tvcast import library
from tvcast.library import Movie, probe, scan_library
from tvcast.subtitles import find_subtitles


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


class LibraryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name


class MovieTest(LibraryTest):
    def movie(self, *parts, alone=True):
        return Movie(self.root, touch(self.root, *parts), [], alone)

    def test_dotted_release_name(self):
        m = self.movie("Some.Movie.2001.1080p.BluRay.x264.mkv")
        self.assertEqual((m.title, m.year, m.group), ("Some Movie", 2001, self.root))

    def test_release_folder_names_a_lone_video(self):
        m = self.movie("Another Film (1999) [1080p]", "another.film.1999.mp4")
        self.assertEqual((m.title, m.year), ("Another Film", 1999))

    def test_folder_with_several_videos_uses_file_names(self):
        m = self.movie("Show Season 1", "Show Episode 2.mp4", alone=False)
        self.assertEqual((m.title, m.year), ("Show Episode 2", None))

    def test_year_title_keeps_its_title(self):
        m = self.movie("1917 (2019).mp4")
        self.assertEqual((m.title, m.year), ("1917", 2019))

    def test_parenthesized_year_beats_bare_year(self):
        m = self.movie("Film 2049 Edition (2017).mp4")
        self.assertEqual((m.title, m.year), ("Film 2049 Edition", 2017))

    def test_collection_groups_release_folders(self):
        m = self.movie("Some Saga", "Part One (2001)", "part.one.mp4")
        self.assertEqual((m.title, m.group), ("Part One", os.path.join(self.root, "Some Saga")))


class FindSubtitlesTest(LibraryTest):
    def find(self, video_stem, siblings=()):
        names = os.listdir(self.root)
        files = [n for n in names if os.path.isfile(os.path.join(self.root, n))]
        dirs = [n for n in names if os.path.isdir(os.path.join(self.root, n))]
        tracks = find_subtitles(self.root, files, dirs, video_stem, siblings)
        return [(os.path.relpath(t["path"], self.root), t["label"]) for t in tracks]

    def test_files_named_like_the_video_in_every_language_and_format(self):
        for name in ("Film.srt", "Film.en.forced.srt", "Film.en.hi.SRT", "Film.fr.ass", "Film_hi.vtt", "Other.srt"):
            touch(self.root, name)
        self.assertEqual(self.find("Film", siblings=["Other"]), [
            ("Film.en.forced.srt", "English Forced"), ("Film.en.hi.SRT", "English SDH"),
            ("Film.srt", "Film.srt"), ("Film.fr.ass", "French"), ("Film_hi.vtt", "Hindi"),
        ])

    def test_a_longer_video_name_owns_its_files(self):
        touch(self.root, "Film.en.srt")
        touch(self.root, "Film.2.en.srt")
        touch(self.root, "Film 3.srt")
        self.assertEqual(self.find("Film", siblings=["Film.2", "Film 3"]), [("Film.en.srt", "English")])
        self.assertEqual(self.find("Film.2", siblings=["Film", "Film 3"]), [("Film.2.en.srt", "English")])

    def test_unmatched_files_only_for_a_video_alone(self):
        touch(self.root, "subtitles.en.srt")
        self.assertEqual(self.find("Film"), [("subtitles.en.srt", "English")])
        self.assertEqual(self.find("Film", siblings=["Other"]), [])

    def test_subs_folder_lists_every_language(self):
        touch(self.root, "Subs", "2_English_SDH.srt")
        touch(self.root, "Subs", "3_English.srt")
        touch(self.root, "Subs", "4_French.srt")
        touch(self.root, "Subs", "5_English_Commentary.srt")
        touch(self.root, "Subs", ".hidden.srt")
        self.assertEqual([label for _, label in self.find("Film")],
                         ["English", "English SDH", "French", "English Commentary"])

    def test_episode_folder_in_subs_belongs_to_its_episode(self):
        touch(self.root, "Subs", "Show.S01E01", "2_English.srt")
        touch(self.root, "Subs", "Show.S01E02", "2_English.srt")
        self.assertEqual(self.find("Show.S01E01", siblings=["Show.S01E02"]),
                         [(os.path.join("Subs", "Show.S01E01", "2_English.srt"), "English")])

    def test_shared_labels_name_their_files(self):
        touch(self.root, "Film.pt.srt")
        touch(self.root, "Film.pt-BR.srt")
        self.assertEqual([label for _, label in self.find("Film")],
                         ["Portuguese (Film.pt-BR.srt)", "Portuguese (Film.pt.srt)"])


class ScanLibraryTest(LibraryTest):
    def test_skips_samples_and_extras(self):
        release = os.path.join(self.root, "Some Film (2005)")
        main = touch(release, "some.film.2005.mkv")
        touch(release, "some.film.2005-sample.mkv")
        touch(release, "Sample", "clip.mkv")
        touch(release, "Featurettes", "Making Of.mkv")
        touch(release, "notes.txt")
        movies, problems, private = scan_library([self.root])
        self.assertEqual([m.path for m in movies], [main])
        # The skipped sample doesn't count, so the release folder still names the video.
        self.assertEqual((movies[0].title, movies[0].year), ("Some Film", 2005))
        self.assertEqual((problems, private), ({self.root: []}, {self.root: []}))

    def test_collections_come_first_in_release_order(self):
        touch(self.root, "Loose Film (1990).mp4")
        touch(self.root, "Saga", "Second (2003)", "second.mp4")
        touch(self.root, "Saga", "First (2001)", "first.mp4")
        movies, _, _ = scan_library([self.root])
        self.assertEqual([m.title for m in movies], ["First", "Second", "Loose Film"])

    def test_missing_folder_is_a_problem(self):
        missing = os.path.join(self.root, "unplugged drive")
        movies, problems, _ = scan_library([missing])
        self.assertEqual(movies, [])
        self.assertEqual(problems, {missing: ["folder not found (unmounted drive?)"]})


class ProbeTest(unittest.TestCase):
    def test_subtitle_streams_are_listed_with_language_and_flags(self):
        out = json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"},
             "disposition": {"forced": 0, "hearing_impaired": 1}},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ], "format": {"duration": "60.0"}})
        with mock.patch.object(library.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, "")):
            info = probe("/videos/film.mkv")
        self.assertEqual(info["subtitles"], [
            {"codec": "subrip", "lang": "eng", "title": None, "forced": False, "sdh": True},
            {"codec": "hdmv_pgs_subtitle", "lang": None, "title": None, "forced": False, "sdh": False},
        ])


if __name__ == "__main__":
    unittest.main()
