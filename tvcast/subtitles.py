"""Subtitle files: finding them beside a video, naming their language, and turning them into the
SubRip text a TV draws. Embedded tracks are only described; the TV plays those itself."""
import codecs
import hashlib
import html
import os
import re

from . import __version__

SUBTITLE_TYPES = {".srt", ".ass", ".ssa", ".vtt"}
SUBTITLE_DIRS = {"subs", "subtitles"}
# Languages recognised in file names and in ffprobe's stream tags: ISO 639-1, both ISO 639-2
# forms, and common spellings.
LANGUAGES = {
    "en": ("English", "eng"), "hi": ("Hindi", "hin"), "bn": ("Bengali", "ben", "bangla"),
    "ta": ("Tamil", "tam"), "te": ("Telugu", "tel"), "ml": ("Malayalam", "mal"), "kn": ("Kannada", "kan"),
    "mr": ("Marathi", "mar"), "gu": ("Gujarati", "guj"), "pa": ("Punjabi", "pan"), "ur": ("Urdu", "urd"),
    "fr": ("French", "fre", "fra"), "es": ("Spanish", "spa", "castellano", "espanol"),
    "de": ("German", "ger", "deu", "deutsch"), "it": ("Italian", "ita"), "pt": ("Portuguese", "por"),
    "nl": ("Dutch", "dut", "nld"), "sv": ("Swedish", "swe"), "no": ("Norwegian", "nor", "nob", "nb"),
    "da": ("Danish", "dan"), "fi": ("Finnish", "fin"), "pl": ("Polish", "pol"), "cs": ("Czech", "cze", "ces"),
    "hu": ("Hungarian", "hun"), "ro": ("Romanian", "rum", "ron"), "el": ("Greek", "gre", "ell"),
    "tr": ("Turkish", "tur"), "ru": ("Russian", "rus"), "uk": ("Ukrainian", "ukr"), "ar": ("Arabic", "ara"),
    "he": ("Hebrew", "heb"), "fa": ("Persian", "per", "fas", "farsi"), "zh": ("Chinese", "chi", "zho", "chs", "cht"),
    "ja": ("Japanese", "jpn"), "ko": ("Korean", "kor"), "th": ("Thai", "tha"), "vi": ("Vietnamese", "vie"),
    "id": ("Indonesian", "ind"), "ms": ("Malay", "may", "msa"),
}
LANGUAGE_TOKENS = {
    token.lower(): code for code, names in LANGUAGES.items() for token in (code,) + names
}
# Blu-ray image subtitles: Samsung's spec sheets list SubRip, ASS, VobSub and MP4 timed text as
# embedded formats its players draw, but not PGS.
IMAGE_CODECS = {"hdmv_pgs_subtitle"}
SRT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def is_subtitle(name):
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in SUBTITLE_TYPES


def subtitle_track(path, video_stem=None):
    """A subtitle file as a track: {id, path, lang, label, exact, forced, sdh, commentary}.

    The language and flags come from the name past video_stem, so "Film.en.forced.srt" is
    English Forced. "hi" right after a language means hearing impaired, as in "Film.en.hi.srt";
    alone it is Hindi. exact marks the file named just like the video."""
    base = os.path.basename(path)
    name = os.path.splitext(base)[0]
    rest = name[len(video_stem):] if video_stem and name.lower().startswith(video_stem.lower()) else name
    lang, flags = None, set()
    for token in re.split(r"[\W_]+", rest.lower()):
        if token in ("sdh", "cc") or (token == "hi" and lang):
            flags.add("sdh")
        elif token in ("forced", "foreign"):
            flags.add("forced")
        elif token == "commentary":
            flags.add("commentary")
        elif token in LANGUAGE_TOKENS:
            lang = LANGUAGE_TOKENS[token]
    words = [LANGUAGES[lang][0] if lang else base]
    words += [label for flag, label in (("forced", "Forced"), ("sdh", "SDH"), ("commentary", "Commentary")) if flag in flags]
    return {
        "id": "f" + hashlib.sha1(path.encode()).hexdigest()[:10],
        "path": path,
        "lang": lang,
        "label": " ".join(words),
        "exact": rest == "",
        "forced": "forced" in flags,
        "sdh": "sdh" in flags,
        "commentary": "commentary" in flags,
    }


def find_subtitles(dirpath, filenames, subdirs, video_stem, siblings):
    """Subtitle tracks for a video, sorted by label: files named like it ("Film.srt",
    "Film.en.srt") beside it or in Subs/, and files in Subs/<video name>/; for a video alone in
    its folder (no siblings, the names of the other videos there), also every other subtitle
    file beside it and in Subs/."""
    stems = [s.lower() for s in [video_stem] + list(siblings)]

    def owner(f):
        # "Film.2.en.srt" is named like both Film and Film.2; the longer name owns it.
        name = os.path.splitext(f)[0].lower()
        return max((s for s in stems if name == s or (name.startswith(s) and name[len(s)] in "._")), key=len, default=None)

    paths = [os.path.join(dirpath, f) for f in filenames
             if is_subtitle(f) and (not siblings or owner(f) == stems[0])]
    for d in subdirs:
        if d.lower() not in SUBTITLE_DIRS:
            continue
        folder = os.path.join(dirpath, d)
        entries = sorted(os.listdir(folder))
        paths += [os.path.join(folder, f) for f in entries if is_subtitle(f) and (not siblings or owner(f) == stems[0])]
        # TV releases keep one folder of subtitles per episode: Subs/<episode file name>/2_English.srt.
        for e in entries:
            if e.lower() == stems[0] and os.path.isdir(os.path.join(folder, e)):
                paths += [os.path.join(folder, e, f) for f in sorted(os.listdir(os.path.join(folder, e))) if is_subtitle(f)]
    tracks = [subtitle_track(p, video_stem) for p in paths]
    distinct_labels(tracks)
    return sorted(tracks, key=lambda t: (t["commentary"], t["label"].lower(), t["path"]))


def distinct_labels(tracks):
    """Add the file name to labels that several tracks share, so a picker can tell them apart."""
    seen = {}
    for t in tracks:
        seen[t["label"]] = seen.get(t["label"], 0) + 1
    for t in tracks:
        if seen[t["label"]] > 1 and t["label"] != os.path.basename(t["path"]):
            t["label"] += " (%s)" % os.path.basename(t["path"])


def embedded_label(stream):
    """How the panel names a subtitle track inside the video, from its ffprobe summary."""
    lang = LANGUAGE_TOKENS.get((stream.get("lang") or "").lower())
    words = [LANGUAGES[lang][0] if lang else stream.get("title") or "Unknown language"]
    if stream.get("forced"):
        words.append("Forced")
    if stream.get("sdh"):
        words.append("SDH")
    return " ".join(words)


def prepare(path, cache_dir):
    """The SubRip file to serve for a subtitle file: the file itself when it is SRT the TV reads,
    otherwise a converted copy in cache_dir, keyed by content and TV Cast version."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read()
    utf16 = data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    if ext == ".srt" and not utf16:
        return path
    key = hashlib.sha1(__version__.encode() + ext.encode() + data).hexdigest()[:20]
    out = os.path.join(cache_dir, key + ".srt")
    if not os.path.exists(out):
        text, encoding = decode(data)
        convert = {".srt": lambda t: t, ".vtt": vtt_to_srt, ".ass": ass_to_srt, ".ssa": ass_to_srt}[ext]
        try:
            srt = convert(text)
        except ValueError as e:
            raise ValueError("can't read subtitles %s: %s" % (path, e))
        os.makedirs(cache_dir, exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(srt.encode(encoding))
        os.replace(tmp, out)
    return out


def decode(data):
    """A subtitle file's text and the encoding to write its SubRip version in.

    Samsung TVs draw UTF-8 and also detect legacy code pages such as Windows-1252 themselves, so
    only UTF-16 is converted. Other non-UTF-8 text is read as Latin-1, which maps every byte to
    one character and back: the markup can be rewritten while the text bytes pass through
    untouched for the TV to detect."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-8"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def milliseconds(stamp):
    m = SRT_TIME.search(stamp)
    if not m:
        raise ValueError("bad timestamp %r" % stamp.strip())
    h, mins, s, frac = m.groups()
    return ((int(h or 0) * 60 + int(mins)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


def srt_text(cues):
    """SubRip text for (start ms, end ms, text) cues."""
    def stamp(ms):
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)

    if not cues:
        raise ValueError("no subtitle lines found")
    return "".join("%d\n%s --> %s\n%s\n\n" % (n, stamp(a), stamp(b), text)
                   for n, (a, b, text) in enumerate(sorted(cues, key=lambda c: c[:2]), 1))


def vtt_to_srt(text):
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip("\n").split("\n")
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        # The WEBVTT header and NOTE, STYLE and REGION blocks have no timing line.
        if timing is None or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        start, end = lines[timing].split("-->", 1)
        # Keep SubRip's formatting tags; drop voice, class, ruby and timestamp tags.
        body = re.sub(r"<(?!/?[ibu]>)[^>]*>", "", "\n".join(lines[timing + 1:]))
        if body.strip():
            cues.append((milliseconds(start), milliseconds(end), html.unescape(body.strip())))
    return srt_text(cues)


def ass_to_srt(text):
    section, fields, cues = None, None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.lower()
            continue
        key, _, value = line.partition(":")
        if section != "[events]":
            continue
        if key == "Format":
            fields = [f.strip().lower() for f in value.split(",")]
        elif key == "Dialogue" and fields:
            row = dict(zip(fields, value.lstrip().split(",", len(fields) - 1)))
            raw = row.get("text", "")
            # \p1 and up switch to vector drawing commands, which have no text to show.
            if re.search(r"\{[^}]*\\p[1-9]", raw):
                continue
            # \N is a hard line break; \n a soft one that ASS shows as a space unless told otherwise.
            body = re.sub(r"\{[^}]*\}", "", raw).replace("\\N", "\n").replace("\\n", " ").replace("\\h", " ")
            if body.strip():
                cues.append((milliseconds(row["start"]), milliseconds(row["end"]), body.strip()))
    if fields is None:
        raise ValueError("no [Events] Format line")
    return srt_text(cues)
