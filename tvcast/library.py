"""The video library: finding videos in folders, and checking their codecs and subtitle tracks."""
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess

from .subtitles import find_subtitles

# Video files the library picks up, with the MIME type announced to the TV. MP4 and Matroska
# (HEVC video, AAC audio) are verified on a 2021 Samsung Tizen TV; Matroska goes out as
# video/x-mkv, the type minidlna uses for Samsung TVs.
VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-mkv",
    ".avi": "video/x-msvideo", ".webm": "video/webm", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".wmv": "video/x-ms-wmv",
}
# Codecs a typical Samsung Tizen TV from 2018 on decodes, per Samsung's spec sheets. Samsung
# dropped DTS and TrueHD audio from its TVs in 2018, so files with those as the main track would
# play silent. Other TVs differ; the panel flags videos outside this table but offers "Try anyway".
TV_VIDEO_CODECS = {"h264", "hevc", "mpeg4", "mpeg2video", "vp8", "vp9", "vc1", "wmv3"}
TV_AUDIO_CODECS = {"aac", "ac3", "eac3", "mp3", "mp2", "flac", "opus", "vorbis", "wmav2", "pcm_s16le", "pcm_s24le"}
COVER_ART_CODECS = {"mjpeg", "png"}  # attached pictures that ffprobe lists as video streams
SKIP_DIR_SUFFIXES = (".app", ".bundle", ".fcpbundle", ".photoslibrary", ".imovielibrary", ".tvlibrary", ".musiclibrary")
# Release subfolders that hold bonus material, tracker notes or subtitles rather than the movie.
EXTRAS_DIRS = {
    "featurettes", "extras", "bonus", "bonus features", "special features", "behind the scenes",
    "deleted scenes", "trailers", "interviews", "other", "subs", "subtitles",
}
SAMPLE = re.compile(r"(^|[\W_])samples?([\W_]|$)", re.I)  # release "sample" clips and folders
FFPROBE = shutil.which("ffprobe", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")


class Movie:
    def __init__(self, root, path, subs, alone_in_folder):
        st = os.stat(path)
        self.root = root
        self.path = path
        self.subs = subs
        self.size = st.st_size
        self.mtime = int(st.st_mtime)
        self.ext = os.path.splitext(path)[1].lower()
        self.id = hashlib.sha1(path.encode()).hexdigest()[:12]
        folder = os.path.dirname(path)
        rel = os.path.relpath(folder, root)
        parts = [] if rel == "." else rel.split(os.sep)
        # A collection is a root subfolder of release folders (root/Star Wars/A New Hope (1977)/…);
        # videos in the root or in a root-level release folder belong to the root itself.
        self.group = os.path.join(root, parts[0]) if len(parts) >= 2 else root
        # A release folder holding one video carries the clean name; otherwise (loose files,
        # season folders) only the file name tells videos apart.
        if folder != root and alone_in_folder:
            name = os.path.basename(folder)
        else:
            name = os.path.splitext(os.path.basename(path))[0]
        if name.count(".") + name.count("_") > name.count(" "):  # Dotted.Release.Name.2001.1080p
            name = re.sub(r"[._]", " ", name)
        # Prefer "(2001)"; a bare year counts only after the start, so "1917 (2019)" keeps its title.
        year = re.search(r"\(((?:19|20)\d\d)\)", name) or re.search(r"(?<=\s)((?:19|20)\d\d)\b", name)
        self.year = int(year[1]) if year else None
        if year:
            name = name[:year.start()]
        self.title = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", name)).strip(" -([") or os.path.basename(path)


def scan_library(roots):
    """Videos under roots (collections first, each in release order), readable problems per
    root, and per root the subfolders skipped because macOS keeps them private."""
    movies, problems, private = [], {}, {}
    for root in roots:
        issues = problems.setdefault(root, [])
        skipped = private.setdefault(root, [])
        if not os.path.isdir(root):
            issues.append("folder not found (unmounted drive?)")
            continue

        def onerror(e, root=root, issues=issues, skipped=skipped):
            # os.walk skips unreadable folders silently unless told. macOS privacy protection
            # (Desktop, Documents, Downloads, external drives, app libraries such as ~/Movies/TV)
            # surfaces here as EPERM: an error for the root, a note for a subfolder.
            if e.errno == errno.EPERM and e.filename != root:
                skipped.append(os.path.relpath(e.filename, root))
            elif e.errno == errno.EPERM:
                issues.append("macOS blocks this service from reading it; move the videos elsewhere"
                              " or give Python Full Disk Access")
            else:
                issues.append(str(e))

        found = []
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            subdirs = list(dirnames)
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and not SAMPLE.search(d) and d.lower() not in EXTRAS_DIRS
                and not d.lower().endswith(SKIP_DIR_SUFFIXES)
            )
            videos = [
                f for f in filenames
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in VIDEO_TYPES
                and not SAMPLE.search(os.path.splitext(f)[0])
            ]
            for name in videos:
                try:
                    siblings = [os.path.splitext(v)[0] for v in videos if v != name]
                    subs = find_subtitles(dirpath, filenames, subdirs, os.path.splitext(name)[0], siblings)
                    found.append(Movie(root, os.path.join(dirpath, name), subs, len(videos) == 1))
                except OSError as e:
                    issues.append(str(e))
        # Collections first (alphabetically), then loose videos; each in release order.
        found.sort(key=lambda m: (m.group == root, m.group.lower(), m.year or 9999, m.title.lower(), m.path))
        movies.extend(found)
    return movies, problems, private


def probe(path):
    """Codec summary of a video from ffprobe; {"error": ...} when it can't be read."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired"
             ":format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe timed out"}
    if r.returncode != 0:
        return {"error": r.stderr.strip()[:300] or "ffprobe exited with %d" % r.returncode}
    data = json.loads(r.stdout)
    streams = data.get("streams", [])
    duration = (data.get("format") or {}).get("duration")
    return {
        "video": [s["codec_name"] for s in streams if s.get("codec_type") == "video" and s.get("codec_name")],
        "audio": [s["codec_name"] for s in streams if s.get("codec_type") == "audio" and s.get("codec_name")],
        "subtitles": [
            {
                "codec": s.get("codec_name"),
                "lang": (s.get("tags") or {}).get("language"),
                "title": (s.get("tags") or {}).get("title"),
                "forced": bool((s.get("disposition") or {}).get("forced")),
                "sdh": bool((s.get("disposition") or {}).get("hearing_impaired")),
            }
            for s in streams if s.get("codec_type") == "subtitle"
        ],
        "duration": float(duration) if duration else None,
    }


def playability(info):
    """Why the TV can't play a probed video, or None when it should (or nothing is known)."""
    if info is None:
        return None
    if "error" in info:
        return "can't read the file: %s" % info["error"]
    video = [c for c in info["video"] if c not in COVER_ART_CODECS]
    if not video:
        return "no video track"
    if video[0] not in TV_VIDEO_CODECS:
        return "the TV can't decode %s video" % video[0]
    if info["audio"] and info["audio"][0] not in TV_AUDIO_CODECS:
        return "the TV can't decode %s audio (it would play silent)" % info["audio"][0]
    return None


def choose(what, dialog):
    """Run an AppleScript "choose …" dialog on this Mac and return the chosen POSIX path; None
    when the user cancels. what names the picker in errors."""
    r = subprocess.run(
        # Without activate the dialog can open behind the browser.
        ["/usr/bin/osascript", "-e", "activate", "-e", "POSIX path of (%s)" % dialog],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if "(-128)" in r.stderr:  # AppleScript's "User canceled."
            return None
        raise RuntimeError("%s picker failed: %s" % (what, r.stderr.strip()))
    return r.stdout.strip()


def choose_folder():
    """Show the native folder picker on this Mac; None when the user cancels."""
    path = choose("folder", 'choose folder with prompt "Choose a folder with videos for TV Cast"'
                            " default location (path to movies folder)")
    return None if path is None else path.rstrip("/") or "/"


def choose_subtitle_file(folder):
    """Show the native file picker for a subtitle file, opening in folder; None when the user cancels."""
    # In an AppleScript string literal only backslash and double quote need escaping.
    start = folder.replace("\\", "\\\\").replace('"', '\\"')
    return choose("subtitle file", 'choose file with prompt "Choose a subtitle file (.srt, .ass, .ssa or .vtt)"'
                                   ' default location (POSIX file "%s")' % start)
