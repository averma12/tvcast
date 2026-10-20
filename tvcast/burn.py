"""Subtitles burned into the picture, for TVs whose players show no subtitle files at all.

Some renderers (a Philips Smart 4 Google TV with Skyworth's media renderer, for one) ignore every
way a DLNA controller can offer a subtitle file, and the text and bitmap subtitle tracks inside
MP4, MKV and MPEG-TS files too. For those, a subtitle file is drawn once into a Blu-ray PGS file
(render.swift, built with the Command Line Tools' swiftc and cached), and ffmpeg overlays it and
encodes HEVC on the Mac's media engine. Audio is copied; Dolby Vision becomes plain HDR10.

Two ways to play the result, both seekable from the TV's remote:
- HLS: a playlist of every 6-second segment of the movie, each encoded the first time the TV asks
  for it while the next few are prepared ahead. Starts in seconds; a seek waits for its segment.
- burned copy: the whole movie written once in the background (about a third of its running
  time). It plays like any file, with no waiting and no work for the Mac while it plays.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time

from .library import FFPROBE

FFMPEG = shutil.which("ffmpeg", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")
CACHE = os.path.expanduser("~/Library/Caches/tvcast")
# Burned copies are about as big as their source, so they live with the user's movies rather than
# in a cache macOS may purge.
BURNED_DIR = os.path.expanduser("~/Movies/TV Cast subtitles")
RENDER_SRC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "render.swift")
PQ_TEXT = 0.58  # text gray for HDR10 (PQ) video: about 200 nits, where full white would glare
QUALITY = 85  # hevc_videotoolbox -q:v; measured SSIM 0.987 against a 4K HDR source at about its bitrate
SEGMENT = 6.0  # seconds per HLS segment
AHEAD = 10  # segments prepared beyond the one the TV asked for
WORKERS = 2  # segment encodes at a time; the media engine keeps up with two 4K streams
# Added to every segment's timestamps, so none starts at 0, which ffmpeg treats as "no offset".
TS_BASE = 10.0


def available():
    """Why subtitles can't be burned in on this Mac, or None when they can."""
    if not FFMPEG or not FFPROBE:
        return "burning in subtitles needs ffmpeg (brew install ffmpeg)"
    if not shutil.which("swiftc", path="/usr/bin"):
        return "burning in subtitles needs the Command Line Tools (xcode-select --install)"
    return None


def parse_srt(text):
    """(start, end, text) cues from SubRip text, in order, each ending by the next one's start."""
    def seconds(t):
        h, m, s = t.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = block.split("\n")
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        try:
            start, end = (seconds(t.split()[0] if t.split() else "") for t in lines[timing].split("-->"))
        except ValueError:
            continue
        body = re.sub(r"</?[a-zA-Z][^>]*>|\{\\[^}]*\}", "", "\n".join(lines[timing + 1:])).strip()
        if body and end > start:
            cues.append([start, end, body])
    cues.sort(key=lambda c: c[0])
    for a, b in zip(cues, cues[1:]):
        a[1] = min(a[1], b[0])
    return [tuple(c) for c in cues if c[1] > c[0]]


def video_info(path):
    """Size, pixel format, colour tags and duration of the first video stream."""
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,pix_fmt,color_transfer,color_primaries,color_space:format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=60, check=True)
    data = json.loads(r.stdout)
    s = data["streams"][0]
    return {
        "width": s["width"], "height": s["height"], "ten_bit": "10" in s.get("pix_fmt", ""),
        "transfer": s.get("color_transfer"), "primaries": s.get("color_primaries"), "space": s.get("color_space"),
        "duration": float(data.get("format", {}).get("duration") or 0) or None,
    }


def renderer():
    """The compiled render.swift, rebuilt whenever the source changes."""
    with open(RENDER_SRC, "rb") as f:
        tag = hashlib.sha1(f.read()).hexdigest()[:12]
    binary = os.path.join(CACHE, "render-" + tag)
    if not os.path.exists(binary):
        os.makedirs(CACHE, exist_ok=True)
        tmp = binary + ".part"
        subprocess.run(["/usr/bin/swiftc", "-O", RENDER_SRC, "-o", tmp], check=True, capture_output=True, timeout=300)
        os.replace(tmp, binary)
    return binary


def subs_key(movie, srt):
    """Changes whenever the movie or its SubRip subtitles do; names drawn subtitles and copies."""
    st = os.stat(srt)
    key = json.dumps([os.path.realpath(movie.path), movie.size, os.path.realpath(srt), st.st_size, st.st_mtime])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def burned_path(movie, srt):
    """Where the copy of movie with subtitles srt burned in lives (it may not exist yet)."""
    return os.path.join(BURNED_DIR, "%s-%s.mp4" % (movie.id, subs_key(movie, srt)))


def burned(movie, srt):
    """The finished copy of movie with subtitles srt burned in, or None."""
    path = burned_path(movie, srt)
    return path if os.path.isfile(path) else None


def prepare(movie, srt):
    """(PGS file of SubRip subtitles srt drawn for movie's picture, video_info). Cached."""
    info = video_info(movie.path)
    brightness = PQ_TEXT if info["transfer"] == "smpte2084" else 1.0
    key = json.dumps([subs_key(movie, srt), info["width"], info["height"], brightness, renderer()])
    sup = os.path.join(CACHE, "subs", hashlib.sha1(key.encode()).hexdigest()[:16] + ".sup")
    if not os.path.exists(sup):
        os.makedirs(os.path.dirname(sup), exist_ok=True)
        with open(srt, encoding="utf-8-sig", errors="replace") as f:
            cues = parse_srt(f.read())
        if not cues:
            raise ValueError("%s has no subtitles that could be read" % os.path.basename(srt))
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", dir=CACHE, delete=False, encoding="utf-8") as f:
            for start, end, text in cues:
                f.write("%.3f\t%.3f\t%s\n" % (start, end, text.replace("\t", " ").replace("\n", "\\n")))
        try:
            subprocess.run([renderer(), f.name, sup + ".part", str(info["width"]), str(info["height"]), str(brightness)],
                           check=True, capture_output=True, timeout=600)
        finally:
            os.remove(f.name)
        os.replace(sup + ".part", sup)
    return sup, info


def read_sets(sup):
    """[(pts, bytes)] of a PGS file's display sets, in order."""
    with open(sup, "rb") as f:
        data = f.read()
    sets, first, pos = [], 0, 0
    while pos + 13 <= len(data):
        kind, size = data[pos + 10], struct.unpack(">H", data[pos + 11:pos + 13])[0]
        pos += 13 + size
        if kind == 0x80:  # END closes a display set
            sets.append((struct.unpack(">I", data[first + 2:first + 6])[0], data[first:pos]))
            first = pos
    return sets


def segment_sup(sets, start, end, out):
    """Write the display sets for [start, end) seconds as a PGS file timed from start.

    The cue on screen at start shows from 0:00, and render.swift's empty set at 0:00 (the first)
    is kept to pin the start time."""
    cut, stop = round(start * 90000), round(end * 90000)
    pin, cues = sets[0], sets[1:]
    keep = [s for s in cues if cut <= s[0] < stop]
    before = [s for s in cues if s[0] < cut]
    if before and before[-1][1].count(b"PG") > 3:  # it shows a cue; a clear set has three segments
        keep.insert(0, before[-1])
    with open(out, "wb") as f:
        f.write(pin[1])
        for _, data in keep:
            pos = 0
            while pos < len(data):  # re-time each segment of the set
                pts, size = struct.unpack(">I", data[pos + 2:pos + 6])[0], struct.unpack(">H", data[pos + 11:pos + 13])[0]
                f.write(data[pos:pos + 2] + struct.pack(">I", max(0, pts - cut)) + data[pos + 6:pos + 13 + size])
                pos += 13 + size


def video_args(info):
    """ffmpeg arguments that overlay input 1's subtitles on input 0's video and encode it."""
    pix, overlay, profile = ("p010le", "yuv420p10", "main10") if info["ten_bit"] else ("nv12", "yuv420", "main")
    colours = []
    for flag, key in (("-color_primaries", "primaries"), ("-color_trc", "transfer"), ("-colorspace", "space")):
        if info.get(key) and info[key] != "unknown":
            colours += [flag, info[key]]
    return (["-filter_complex", "[0:v][1:s]overlay=format=%s:eof_action=pass,format=%s[v]" % (overlay, pix),
             "-map", "[v]", "-map", "0:a:0?", "-c:v", "hevc_videotoolbox", "-profile:v", profile,
             "-q:v", str(QUALITY), "-tag:v", "hvc1"] + colours + ["-c:a", "copy"])


def burn_command(path, sup, info, out):
    """ffmpeg arguments that write the whole movie with subtitles burned in to MP4 `out`,
    reporting progress as key=value lines (out_time_us=...) on stdout."""
    return ([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-hwaccel", "videotoolbox",
             "-i", path, "-i", sup] + video_args(info)
            + ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", out])


def segment_command(path, sup, start, duration, info, out):
    """ffmpeg arguments that write `duration` seconds of the movie from `start` as MPEG-TS `out`,
    with the subtitles in PGS file `sup` (timed from start) burned in. Timestamps continue the
    movie's own, so the TV joins segments seamlessly."""
    return ([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-hwaccel", "videotoolbox",
             "-ss", "%.3f" % start, "-i", path, "-i", sup] + video_args(info)
            + ["-t", "%.3f" % duration, "-output_ts_offset", "%.3f" % (TS_BASE + start),
               "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", out])


def segment_count(duration):
    return int(math.ceil(duration / SEGMENT))


def playlist(duration):
    """An HLS VOD playlist of every segment of a movie of this many seconds."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % math.ceil(SEGMENT),
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0"]
    for n in range(segment_count(duration)):
        lines += ["#EXTINF:%.3f," % min(SEGMENT, duration - n * SEGMENT), "%d.ts" % n]
    return "\n".join(lines + ["#EXT-X-ENDLIST", ""])


class Hls:
    """HLS segments of one movie with one subtitle file burned in, encoded when first asked for.

    Workers encode the segment the TV asked for, then the next AHEAD; a request elsewhere (a
    seek) moves them there. Segments far from the one playing are deleted."""

    def __init__(self, movie, srt):
        sup, self.info = prepare(movie, srt)
        self.movie, self.sets = movie, read_sets(sup)
        self.duration = self.info["duration"]
        self.count = segment_count(self.duration)
        self.dir = os.path.join(CACHE, "hls", subs_key(movie, srt))
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.lock = threading.Condition()
        self.done, self.busy, self.errors = set(), set(), {}
        self.target = None  # nothing is prepared until the TV asks
        self.stopped = False
        for _ in range(WORKERS):
            threading.Thread(target=self.work, daemon=True).start()

    def path(self, n):
        return os.path.join(self.dir, "%05d.ts" % n)

    def encode(self, n):
        start = n * SEGMENT
        duration = min(SEGMENT, self.duration - start)
        sup, part = self.path(n) + ".sup", self.path(n) + ".part"
        segment_sup(self.sets, start, start + duration, sup)
        try:
            r = subprocess.run(segment_command(self.movie.path, sup, start, duration, self.info, part),
                               capture_output=True, text=True, timeout=120)
        finally:
            os.remove(sup)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg failed on segment %d: %s" % (n, r.stderr.strip()[-300:]))
        os.replace(part, self.path(n))

    def work(self):
        while True:
            with self.lock:
                while True:
                    if self.stopped:
                        return
                    todo = [] if self.target is None else [
                        i for i in range(self.target, min(self.count, self.target + AHEAD + 1))
                        if i not in self.done and i not in self.busy and i not in self.errors]
                    if todo:
                        n = todo[0]
                        self.busy.add(n)
                        break
                    self.lock.wait()
            try:
                self.encode(n)
                error = None
            except Exception as e:  # reported to whoever asks for this segment
                error = str(e)
            with self.lock:
                self.busy.discard(n)
                if error:
                    self.errors[n] = error
                else:
                    self.done.add(n)
                self.lock.notify_all()
            self.prune()

    def prune(self):
        with self.lock:
            if self.target is None:
                return
            old = [i for i in self.done if not self.target - 20 <= i <= self.target + 2 * AHEAD]
            self.done.difference_update(old)
        for i in old:
            with contextlib.suppress(OSError):  # already gone
                os.remove(self.path(i))

    def segment(self, n, timeout=60):
        """Path of segment n once it is encoded; moves the read-ahead there."""
        if not 0 <= n < self.count:
            raise ValueError("no segment %d" % n)
        with self.lock:
            if n in self.done and os.path.exists(self.path(n)):
                self.target = n if self.target is None else max(self.target, n)
            else:
                self.done.discard(n)
                self.errors.pop(n, None)
                self.target = n
            self.lock.notify_all()
            deadline = time.time() + timeout
            while n not in self.done and n not in self.errors and not self.stopped:
                if time.time() > deadline:
                    raise RuntimeError("segment %d took over %d s" % (n, timeout))
                self.lock.wait(1)
            if n in self.errors:
                raise RuntimeError(self.errors[n])
        return self.path(n)

    def stop(self):
        with self.lock:
            self.stopped = True
            self.lock.notify_all()
        shutil.rmtree(self.dir, ignore_errors=True)

