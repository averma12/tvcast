"""Subtitle conversion to SubRip and the files served to the TV."""
import codecs
import os
import tempfile
import unittest

from tvcast.subtitles import ass_to_srt, embedded_label, prepare, subtitle_track, vtt_to_srt


class ConvertTest(unittest.TestCase):
    def test_vtt_keeps_formatting_and_drops_cue_settings_and_notes(self):
        vtt = (
            "WEBVTT\n\nNOTE made by hand\n\nSTYLE\n::cue { color: yellow }\n\n"
            "intro\n00:01.500 --> 00:04.000 line:85%\n<v Ann><i>Hello</i> &amp; <c.loud>welcome</c></v>\n\n"
            "01:00:00.000 --> 01:00:02.250\nLater\n"
        )
        self.assertEqual(vtt_to_srt(vtt), (
            "1\n00:00:01,500 --> 00:00:04,000\n<i>Hello</i> & welcome\n\n"
            "2\n01:00:00,000 --> 01:00:02,250\nLater\n\n"
        ))

    def test_ass_strips_overrides_skips_comments_and_drawings_and_sorts(self):
        ass = (
            "[Script Info]\nTitle: x\n\n[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:05.00,0:00:06.50,Default,,0,0,0,,Second, with a comma\n"
            "Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hidden\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}First{\\i0}\\Nline\\hbreak\n"
            "Dialogue: 0,0:00:03.00,0:00:04.00,Sign,,0,0,0,,{\\p1}m 0 0 l 100 0{\\p0}\n"
        )
        self.assertEqual(ass_to_srt(ass), (
            "1\n00:00:01,000 --> 00:00:02,000\nFirst\nline break\n\n"
            "2\n00:00:05,000 --> 00:00:06,500\nSecond, with a comma\n\n"
        ))

    def test_ass_without_events_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "no \\[Events\\] Format line"):
            ass_to_srt("[Script Info]\nTitle: x\n")


class PrepareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cache = os.path.join(tmp.name, "cache")

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_srt_is_served_as_is_even_in_a_legacy_code_page(self):
        # Samsung TVs detect Windows-1252 themselves.
        path = self.write("film.srt", "1\n00:00:01,000 --> 00:00:02,000\ncafé\n".encode("cp1252"))
        self.assertEqual(prepare(path, self.cache), path)

    def test_utf16_srt_becomes_utf8(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nहिन्दी\n"
        path = self.write("film.srt", codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
        with open(prepare(path, self.cache), encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_legacy_bytes_pass_through_a_conversion(self):
        vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\ncafé élève\n".encode("cp1252")
        with open(prepare(self.write("film.vtt", vtt), self.cache), "rb") as f:
            self.assertIn("café élève".encode("cp1252"), f.read())

    def test_unreadable_conversion_names_the_file(self):
        path = self.write("film.ass", b"not subtitles")
        with self.assertRaisesRegex(ValueError, "can't read subtitles .*film.ass"):
            prepare(path, self.cache)


class LabelTest(unittest.TestCase):
    def test_hi_is_hindi_alone_and_hearing_impaired_after_a_language(self):
        self.assertEqual(subtitle_track("/m/Film.hi.srt", "Film")["label"], "Hindi")
        self.assertEqual(subtitle_track("/m/Film.eng.hi.srt", "Film")["label"], "English SDH")

    def test_embedded_tracks_are_named_from_their_tags(self):
        self.assertEqual(embedded_label({"lang": "fre", "forced": True}), "French Forced")
        self.assertEqual(embedded_label({"lang": "und", "title": "Signs"}), "Signs")


if __name__ == "__main__":
    unittest.main()
