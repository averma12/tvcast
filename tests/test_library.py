import os
import tempfile
import unittest

from tvcast.library import Movie, pick_subtitles, scan_library


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
        return Movie(self.root, touch(self.root, *parts), None, alone)

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


class PickSubtitlesTest(LibraryTest):
    def pick(self, video_stem, alone):
        names = os.listdir(self.root)
        files = [n for n in names if os.path.isfile(os.path.join(self.root, n))]
        dirs = [n for n in names if os.path.isdir(os.path.join(self.root, n))]
        return pick_subtitles(self.root, files, dirs, video_stem, alone)

    def test_same_stem_wins_even_beside_other_videos(self):
        touch(self.root, "Film.srt")
        touch(self.root, "Other.srt")
        self.assertEqual(self.pick("Film", alone=False), os.path.join(self.root, "Film.srt"))

    def test_unmatched_srt_only_for_a_video_alone(self):
        srt = touch(self.root, "subtitles.en.srt")
        self.assertEqual(self.pick("Film", alone=True), srt)
        self.assertIsNone(self.pick("Film", alone=False))

    def test_english_in_subs_folder_prefers_plain_over_sdh(self):
        touch(self.root, "Subs", "2_English_SDH.srt")
        plain = touch(self.root, "Subs", "3_English.srt")
        touch(self.root, "Subs", "4_French.srt")
        self.assertEqual(self.pick("Film", alone=True), plain)

    def test_never_commentary(self):
        touch(self.root, "Subs", "5_English_Commentary.srt")
        self.assertIsNone(self.pick("Film", alone=True))
        sdh = touch(self.root, "Subs", "2_English_SDH.srt")
        self.assertEqual(self.pick("Film", alone=True), sdh)


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


if __name__ == "__main__":
    unittest.main()
