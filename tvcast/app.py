"""The daemon: TV selection, the library, playback tracking with resume points and a queue,
and the actions behind the control panel's API."""
import collections
import contextlib
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from xml.sax.saxutils import escape

from . import burn, discovery, samsung
from .library import FFPROBE, VIDEO_TYPES, choose_folder, choose_subtitle_file, playability, probe, scan_library
from .server import PANEL_HTML, PanelHandler, Stats, StreamHandler
from .state import State
from .subtitles import (
    IMAGE_CODECS, LANGUAGE_TOKENS, SUBTITLE_TYPES, distinct_labels, embedded_label, prepare, subtitle_track,
)
from .upnp import (
    DLNA_FEATURES, captions, hms, is_samsung, log, rendering, set_caption, transport, transport_status, tv_status,
)

STATUS_TIMEOUT = 2  # panel status polls; an off TV would otherwise stall the panel for SOAP_TIMEOUT
POLL_INTERVAL = 2  # seconds between playback checks while a movie is loaded
IDLE_POLL_INTERVAL = 10  # seconds between checks for a movie to pick back up after a restart
SAVE_INTERVAL = 10  # seconds between resume-point writes during playback
STOP_CONFIRM = 2  # consecutive non-playing polls before playback counts as over
RESUME_MIN = 30  # resume points earlier than this start the movie from the top
RESUME_REWIND = 5  # replay a few seconds of context when resuming
END_SLACK = 30  # stopping within this many seconds of the end means the movie ran out
WATCHED_FRACTION = 0.95  # stopping past this point (end credits) counts as watched
OFFLINE_GRACE = 30  # seconds the TV may be unreachable before playback is written off
NO_TV = "No TV selected"  # the panel's status shows exactly this


def mac_power():
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        return {"error": "pmset failed: %s" % e}
    m = re.search(r"(\d+)%; ([^;]+);(?: (\d+:\d+) remaining)?", out)
    return {
        "on_battery": "Battery Power" in out,
        "percent": int(m[1]) if m else None,
        "state": m[2] if m else None,
        "remaining": m[3] if m else None,
    }


def lan_ip_towards(ip):
    # A UDP "connect" sends nothing (so any port will do); it only selects the interface address
    # that routes to the TV.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            return s.getsockname()[0]
    except OSError as e:
        raise RuntimeError("no network route to TV at %s: %s" % (ip, e))


def move_refused(known, ip):
    """Why a paired TV's record must not move to ip, or None when the move is proven."""
    if samsung.fingerprint(known["ip"]) == known["remote_cert"]:
        return "the paired TV still answers at its old address %s" % known["ip"]
    if samsung.fingerprint(ip) != known["remote_cert"]:
        return "its certificate doesn't match the paired TV"
    return None


def video_type(movie, tv):
    """The MIME type announced to tv for movie: Samsung players want Matroska as video/x-mkv."""
    return "video/x-mkv" if movie.ext == ".mkv" and is_samsung(tv) else VIDEO_TYPES[movie.ext]


# Renderers whose players ignore subtitle files in every form DLNA offers, and the subtitle
# tracks inside videos too (tested on a Philips Smart 4 Google TV, model "Skyworth Media Renderer").
# Their subtitles are burned into the picture instead.
BURN_MANUFACTURERS = ("Skyworth",)
SUBTITLE_MODES = ("auto", "file", "burn")


def subtitle_mode(tv):
    """How tv shows subtitle files: "file" sends the file alongside the video for the TV's player
    to draw; "burn" draws it into the picture. A mode saved for the TV wins over the default."""
    mode = tv.get("subtitle_mode", "auto")
    if mode in ("file", "burn"):
        return mode
    return "burn" if any(m in tv.get("manufacturer", "") for m in BURN_MANUFACTURERS) else "file"


def tv_list(tvs):
    """TV records as the panel lists them, sorted by name."""
    return [
        {"id": tv["id"], "name": tv["name"], "model": tv.get("model", ""), "ip": tv["ip"], "remote": bool(tv.get("remote")),
         "subtitle_mode": tv.get("subtitle_mode", "auto"), "subtitles": subtitle_mode(tv)}
        for tv in sorted(tvs, key=lambda tv: (tv["name"].lower(), tv["id"]))
    ]


class Playback:
    """One movie loaded on a TV, as last observed by the monitor."""

    def __init__(self, movie, tv, base, subtitle, hls=False, burned=None):
        self.movie = movie
        self.tv = tv
        self.base = base
        self.subtitle = subtitle  # the subtitle file TV Cast shows, or None
        # With subtitles burned in (subtitle_mode "burn"), the TV plays either HLS segments
        # encoded as it asks for them, or a finished burned-in copy (path) like any file.
        self.hls = hls
        self.burned = burned
        self.size = os.path.getsize(burned) if burned else movie.size
        if hls:
            self.media_url = "%s/hls/%s/%s/index.m3u8" % (base, movie.id, subtitle["id"])
        elif burned:
            self.media_url = "%s/burned/%s/%s.mp4" % (base, movie.id, subtitle["id"])
        else:
            self.media_url = "%s/media/%s%s" % (base, movie.id, movie.ext)
        self.previous_subtitle = None  # the file shown before, which "subtitles on" brings back
        self.seen_playing = False  # STOPPED before the first PLAYING is just the TV loading
        self.stopped_polls = 0
        self.position = None
        self.duration = None
        self.last_saved = 0.0
        self.unreachable_since = None

    @property
    def drawn(self):
        """Whether the subtitles are in the picture, so none go out as a file."""
        return self.hls or bool(self.burned)

    def subtitle_url(self, track):
        return "%s/subs/%s/%s.srt" % (self.base, self.movie.id, track["id"])


def match_track(tracks, choice):
    """The subtitle track a user named: its id, its number in tracks (from 1), its language
    (code or name; plain over SDH or forced), or a unique part of its label."""
    usable = [t for t in tracks if not t["missing"]]
    if choice.isdigit() and 1 <= int(choice) <= len(tracks):
        if tracks[int(choice) - 1]["missing"]:
            raise ValueError("subtitle file %s is missing" % tracks[int(choice) - 1]["path"])
        return tracks[int(choice) - 1]
    lang = LANGUAGE_TOKENS.get(choice.lower())
    for matches in (
        [t for t in usable if t["id"] == choice],
        [t for t in usable if t["label"].lower() == choice.lower()],
        sorted((t for t in usable if lang and t["lang"] == lang), key=lambda t: (t["commentary"], t["forced"], t["sdh"])),
    ):
        if matches:
            return matches[0]
    matches = [t for t in usable if choice.lower() in t["label"].lower()]
    if len(matches) == 1:
        return matches[0]
    labels = "\n  ".join(t["label"] for t in (matches or usable)) or "(none)"
    raise ValueError("%s subtitles %r; choose one of:\n  %s"
                     % ("several" if matches else "no", choice, labels))


class App:
    def __init__(self, args):
        self.port = args.port
        self.state = State(args.state)
        if self.state.get("libraries") is None:
            seeds = args.library or ["~/Movies"]
            self.state.set("libraries", [os.path.abspath(os.path.expanduser(r)) for r in seeds])
        self.stats = Stats()
        self.remote = samsung.Remote(self.state)
        # Subtitles converted to SubRip for the TV, next to the state file so test daemons keep their own.
        self.subtitle_cache = os.path.join(os.path.dirname(os.path.abspath(self.state.path)), "subtitles")
        # Guards current, movies, by_id, library_problems, library_private, scanning,
        # rescan_pending, probing, the playback queue and caffeinate.
        self.lock = threading.RLock()
        # Serialize playback changes across HTTP requests and the monitor. Never hold self.lock
        # during a TV request: status and queue editing must stay responsive while a movie loads.
        self.playback_lock = threading.RLock()
        self.queue_error = None
        self.current = None
        self.movies, self.by_id, self.library_problems, self.library_private = [], {}, {}, {}
        self.scanning = self.rescan_pending = False
        self.probe_queue = queue.Queue()
        self.probing = set()  # paths queued for or under ffprobe
        self.burns = {}  # movie id -> background burn-in job: title, subtitle, progress, running, error, started
        self.hls = None  # the burn.Hls whose segments the TV is playing, or None
        self.caffeinate = None
        if not os.path.isfile(PANEL_HTML):
            raise SystemExit("control panel page %s is missing" % PANEL_HTML)
        self.panel_hosts = {"127.0.0.1:%d" % args.panel_port, "localhost:%d" % args.panel_port}
        StreamHandler.app = self
        PanelHandler.app = self
        # Listening on every interface keeps working when the Mac's LAN address changes;
        # StreamHandler refuses everyone but the selected TV.
        self.stream = ThreadingHTTPServer(("", args.port), StreamHandler)
        self.panel = ThreadingHTTPServer(("127.0.0.1", args.panel_port), PanelHandler)

    def run(self):
        servers = [threading.Thread(target=s.serve_forever) for s in (self.stream, self.panel)]
        for t in servers:
            t.start()
        threading.Thread(target=self.monitor, daemon=True).start()
        threading.Thread(target=self.discover_at_start, daemon=True).start()
        if FFPROBE:
            threading.Thread(target=self.prober, daemon=True).start()
        else:
            log("warning: ffprobe not found; videos won't be checked for TV-compatible codecs")
        self.request_scan()
        log("panel at http://127.0.0.1:%d; library: %s"
            % (self.panel.server_address[1], ", ".join(self.state.get("libraries")) or "no folders yet"))
        for t in servers:
            t.join()

    # TVs

    def selected(self):
        """The selected TV's record, or None."""
        tv_id = self.state.get("tv")
        return self.state.tv(tv_id) if tv_id else None

    def require_tv(self):
        tv = self.selected()
        if tv is None:
            raise RuntimeError(NO_TV + '; choose one in the panel or with "tv use"')
        return tv

    def remote_tv(self):
        """The selected TV, provided it has the Samsung remote."""
        tv = self.require_tv()
        if not tv.get("remote"):
            raise RuntimeError(
                "the remote works only with Samsung Tizen TVs, and TV %s didn't answer as one when"
                " last found; search for TVs again if it is one" % tv["name"])
        return tv

    def discover(self):
        """Search the network for TVs and remember them; returns the TVs found."""
        records = discovery.discover()
        claims = collections.Counter(tv["id"] for tv in records)
        found = []
        for tv in records:
            known = self.state.tv(tv["id"]) or {}
            # Any LAN device can claim a known TV's id, and the stream server serves the record's
            # address. A real TV has one address, so a contested id is ignored. A paired TV moves
            # only when its certificate left the old address (relaying :8002 needs the real TV
            # still there) and shows up at the new one; unpaired TVs have no such anchor.
            if claims[tv["id"]] > 1:
                why = "%d devices answer as %s" % (claims[tv["id"]], tv["name"])
            elif known.get("remote_cert") and tv["ip"] != known["ip"]:
                why = move_refused(known, tv["ip"])
            else:
                why = None
            if why:
                log("ignoring %s at %s: %s" % (tv["name"], tv["ip"], why))
                continue
            # A paired TV keeps its remote even when its REST API is slow to answer this time.
            tv["remote"] = samsung.paired(known) or samsung.has_remote(tv["ip"])
            self.state.update_tv(tv["id"], **tv)
            found.append(tv)
        return found

    def discover_at_start(self):
        # A saved TV works without this, since it may be off right now; the search refreshes
        # known TVs' addresses and picks the TV when it is the only one and none is selected.
        try:
            found = self.discover()
        except RuntimeError as e:
            log("TV search failed: %s" % e)
            return
        log("found %d TV(s)%s" % (len(found), "".join("; %s at %s" % (tv["name"], tv["ip"]) for tv in found)))
        if self.state.get("tv") is None and len(found) == 1:
            self.select(found[0]["id"])

    def select(self, tv_id):
        tv = self.state.tv(tv_id)
        if tv is None:
            raise ValueError("no known TV with id %r; search for TVs first" % tv_id)
        with self.playback_lock, self.lock:
            if self.state.get("tv") == tv_id:
                return
            # The old TV gets no Stop, but the stream server stops serving it, so its playback
            # ends once its buffer runs out. Tracking and queue advancement end here.
            self.state.set("tv", tv_id)
            pb, self.current = self.current, None
            self.keep_awake(False)
        if pb is not None:
            self.save_progress(pb)
        log("selected TV %s at %s" % (tv["name"], tv["ip"]))

    # Library

    def request_scan(self):
        """Rescan the library folders in the background; repeated requests coalesce."""
        with self.lock:
            if self.scanning:
                self.rescan_pending = True
                return
            self.scanning = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            while True:
                movies, problems, private = scan_library(self.state.get("libraries"))
                with self.lock:
                    changed = problems != self.library_problems
                    self.movies, self.by_id = movies, {m.id: m for m in movies}
                    self.library_problems, self.library_private = problems, private
                    if FFPROBE:
                        for m in movies:
                            if self.probe_info(m) is None and m.path not in self.probing:
                                self.probing.add(m.path)
                                self.probe_queue.put(m)
                    if not self.rescan_pending:
                        break
                    self.rescan_pending = False
            if changed:
                for root, issues in problems.items():
                    for issue in issues:
                        log("library %s: %s" % (root, issue))
        finally:
            with self.lock:
                self.scanning = False

    def prober(self):
        while True:
            movie = self.probe_queue.get()
            try:
                self.state.set_probe(movie.path, {"size": movie.size, "mtime": movie.mtime, "info": probe(movie.path)})
            except Exception:
                traceback.print_exc()  # leave this video unchecked; the panel lets it play anyway
            finally:
                with self.lock:
                    self.probing.discard(movie.path)

    def probe_info(self, movie):
        entry = self.state.probe(movie.path)
        if entry and entry.get("size") == movie.size and entry.get("mtime") == movie.mtime:
            info = entry["info"]
            # A summary without a subtitle list was saved by an older TV Cast; probe it again.
            if "error" in info or "subtitles" in info:
                return info
        return None

    def lookup(self, movie_id):
        with self.lock:
            return self.by_id.get(movie_id)

    def edit_queue(self, action, body):
        with self.lock:
            items = list(self.state.get("queue"))
            movie_id = str(body.get("id", ""))
            ids = [item["id"] for item in items]
            if action == "queue_add":
                movie = self.lookup(movie_id)
                if movie is None or movie.root not in self.state.get("libraries"):
                    raise ValueError("no video with id %r in the library" % movie_id)
                if self.current and self.current.movie.id == movie_id:
                    raise ValueError("%s is already playing" % movie.title)
                if movie_id not in ids:
                    # Keep the label when a drive is disconnected; unavailable entries remain
                    # visible and removable rather than silently losing the user's queue.
                    items.append({"id": movie.id, "title": movie.title, "year": movie.year})
            elif action == "queue_clear":
                items = []
            elif action in ("queue_remove", "queue_move"):
                if movie_id not in ids:
                    raise ValueError("video %r is no longer in the queue" % movie_id)
                index = ids.index(movie_id)
                if action == "queue_remove":
                    items.pop(index)
                else:
                    direction = body.get("direction")
                    if type(direction) is not int or direction not in (-1, 1):
                        raise ValueError("queue direction must be -1 or 1")
                    target = max(0, min(len(items) - 1, index + direction))
                    items.insert(target, items.pop(index))
            else:
                raise ValueError("unknown queue action %r" % action)
            self.state.set("queue", items)
            self.queue_error = None

    def consume_queued(self, movie_id):
        with self.lock:
            items = self.state.get("queue")
            remaining = [item for item in items if item["id"] != movie_id]
            if remaining != items:
                self.state.set("queue", remaining)
            self.queue_error = None

    def play_next(self):
        with self.playback_lock:
            with self.lock:
                items = self.state.get("queue")
                if not items:
                    raise ValueError("the queue is empty")
                item = items[0]
            try:
                movie = self.lookup(item["id"])
                if movie is None or movie.root not in self.state.get("libraries") or not os.path.isfile(movie.path):
                    raise ValueError("%s is unavailable; reconnect its folder and rescan, or remove it from the queue"
                                     % item["title"])
                self.open_movie(movie.id)
            except (RuntimeError, ValueError) as e:
                with self.lock:
                    self.queue_error = str(e)
                raise

    def add_folder(self):
        path = choose_folder()
        if path is None:
            return {"cancelled": True}
        libraries = self.state.get("libraries")
        if path not in libraries:
            self.state.set("libraries", libraries + [path])
            log("library folder added: %s" % path)
        self.request_scan()
        return {"path": path}

    def remove_folder(self, path):
        libraries = self.state.get("libraries")
        if path not in libraries:
            raise ValueError("%s is not a library folder" % path)
        self.state.set("libraries", [p for p in libraries if p != path])
        log("library folder removed: %s" % path)
        self.request_scan()

    def resolve(self, url_path, tv):
        """(file path, content type, movie or None) behind a stream URL fetched by tv, or None.

        Every library video stays reachable by its id, so a TV that keeps playing across a
        restart of this service never hits a 404."""
        burned = self.drawn_source("burned", url_path)
        if burned:
            movie, _, srt, _ = burned
            path = burn.burned(movie, srt)
            return (path, "video/mp4", movie) if path else None
        m = re.fullmatch(r"/(media|subs)/([0-9a-f]{12})(\.\w+|/(f[0-9a-f]{10})\.srt)", url_path)
        movie = m and self.lookup(m[2])
        if not movie:
            return None
        if m[1] == "media" and m[3] == movie.ext:
            return movie.path, video_type(movie, tv), movie
        track = m[1] == "subs" and next((t for t in self.subtitle_tracks(movie) if t["id"] == m[4]), None)
        if not track or track["missing"]:
            return None
        try:
            return prepare(track["path"], self.subtitle_cache), "text/srt", None
        except (OSError, ValueError) as e:
            log("can't serve subtitles %s: %s" % (track["path"], e))
            return None

    # Subtitles

    def subtitle_tracks(self, movie):
        """The movie's subtitle files: those beside it in the library, then those added in TV Cast.
        Added files that have gone missing stay listed, marked missing, so they can be removed."""
        found = {t["path"] for t in movie.subs}
        tracks = [dict(t, added=False, missing=False) for t in movie.subs]
        for path in self.state.movie(movie.path).get("subtitle_files", []):
            if path not in found:
                tracks.append(dict(subtitle_track(path), added=True, missing=not os.path.isfile(path)))
        distinct_labels(tracks)
        return tracks

    def has_embedded_subtitles(self, movie):
        """Whether the video holds subtitle tracks the TV draws itself (per its last probe)."""
        info = self.probe_info(movie) or {}
        return any(s.get("codec") not in IMAGE_CODECS for s in info.get("subtitles", []))

    def default_subtitle(self, movie, tracks, allow_off=True, tv_shows_embedded=True):
        """The file to show when the movie starts: the one last chosen for it, which may be none;
        else none when the video has subtitle tracks of its own, which the TV shows by itself;
        else a file beside it named just like it, .srt first. Other files wait for the user.
        allow_off=False skips an earlier "off" and the video's own tracks, for "subtitles on".
        tv_shows_embedded=False (a TV that burns subtitles shows no tracks inside videos either)
        doesn't count the video's own tracks."""
        saved = self.state.movie(movie.path).get("subtitle")
        usable = [t for t in tracks if not t["missing"]]
        chosen = next((t for t in usable if t["id"] == saved), None)
        if chosen:
            return chosen
        if allow_off and (saved == "off" or tv_shows_embedded and self.has_embedded_subtitles(movie)):
            return None
        folder = os.path.dirname(movie.path)
        exact = [t for t in usable if t["exact"] and os.path.dirname(t["path"]) == folder]
        return min(exact, key=lambda t: not t["path"].lower().endswith(".srt"), default=None)

    def remember_subtitle(self, movie, track):
        self.state.update_movie(movie.path, subtitle=track["id"] if track else "off")

    def subtitle_ready(self, track):
        """Fail with a readable error when the TV couldn't be given track."""
        try:
            prepare(track["path"], self.subtitle_cache)
        except OSError as e:
            raise ValueError("can't read subtitles %s: %s" % (track["path"], e.strerror or e))

    def switch_subtitles(self, choice):
        """Show another subtitle file on the playing movie, or none: choice is "off", "on" (the
        file shown last, else the default) or anything match_track takes. The choice is
        remembered for the movie. Tracks inside the video are left to the TV's own menu: on a
        Samsung, Disable doesn't hide them, but Enable replaces them.

        A Samsung switches files in place. Other players take a file only with the video, so the
        video loads again at its current position, with a pause of a few seconds."""
        with self.playback_lock:
            with self.lock:
                pb = self.current
            if pb is None:
                raise ValueError("no video from this library is playing")
            tracks = self.subtitle_tracks(pb.movie)
            if choice == "off":
                track = None
            elif choice == "on":
                # The file shown last may have been removed since.
                last = pb.subtitle or pb.previous_subtitle
                track = next((t for t in tracks if last and t["id"] == last["id"] and not t["missing"]), None)
                track = track or self.default_subtitle(pb.movie, tracks, allow_off=False)
                if track is None and not tracks:
                    raise ValueError("%s has no subtitle files; add one" % pb.movie.title)
                if track is None:
                    raise ValueError("choose which subtitles to show:\n  %s" % "\n  ".join(t["label"] for t in tracks))
            else:
                track = match_track(tracks, choice)
            # Burned-in subtitles are in the pixels, so changing them means loading another video.
            if not is_samsung(pb.tv) or subtitle_mode(pb.tv) == "burn":
                choice = track["id"] if track else "off"
                if choice == (pb.subtitle["id"] if pb.subtitle else "off"):
                    self.remember_subtitle(pb.movie, track)
                    return
                position = transport_status(pb.tv)["position"]
                if position is None:
                    position = pb.position or 0
                previous = pb.subtitle or pb.previous_subtitle
                self._open_movie(pb.movie.id, False, choice, at=position)
                with self.lock:
                    if self.current is not None and self.current is not pb:
                        self.current.previous_subtitle = previous
                return
            if track is None:
                if pb.subtitle is not None:
                    set_caption(pb.tv, False, pb.media_url, pb.subtitle_url(pb.subtitle), pb.movie.title)
            else:
                self.subtitle_ready(track)
                set_caption(pb.tv, True, pb.media_url, pb.subtitle_url(track), pb.movie.title)
            pb.previous_subtitle = pb.subtitle or pb.previous_subtitle
            pb.subtitle = track
            self.remember_subtitle(pb.movie, track)

    def subtitle_movie(self, body):
        """The movie a subtitle request names by id, or the playing one."""
        if body.get("id"):
            movie = self.lookup(str(body["id"]))
            if movie is None:
                raise ValueError("no video with id %r in the library" % body["id"])
            return movie
        with self.lock:
            pb = self.current
        if pb is None:
            raise ValueError("no video from this library is playing; name one")
        return pb.movie

    def add_subtitles(self, body):
        """Attach a subtitle file from anywhere on this Mac to a movie and show it. Without a path,
        the native file picker asks for one."""
        movie = self.subtitle_movie(body)
        path = body.get("path")
        if path is None:
            path = choose_subtitle_file(os.path.dirname(movie.path))
            if path is None:
                return {"cancelled": True}
        path = os.path.abspath(os.path.expanduser(str(path)))
        if os.path.splitext(path)[1].lower() not in SUBTITLE_TYPES:
            raise ValueError("%s is not a subtitle file; use .srt, .ass, .ssa or .vtt" % path)
        if not os.path.isfile(path):
            raise ValueError("no subtitle file at %s" % path)
        self.subtitle_ready({"path": path})
        files = self.state.movie(movie.path).get("subtitle_files", [])
        if path not in files and path not in {t["path"] for t in movie.subs}:
            self.state.update_movie(movie.path, subtitle_files=files + [path])
        track = next(t for t in self.subtitle_tracks(movie) if t["path"] == path)
        log("subtitles %s added to %s" % (path, movie.title))
        with self.lock:
            pb = self.current
        if pb is not None and pb.movie.id == movie.id:
            self.switch_subtitles(track["id"])
        else:
            self.remember_subtitle(movie, track)
        return {"track": track["id"], "label": track["label"]}

    def remove_subtitles(self, body):
        """Detach a subtitle file added in TV Cast; files beside the video stay."""
        movie = self.subtitle_movie(body)
        track = next((t for t in self.subtitle_tracks(movie) if t["id"] == str(body["track"])), None)
        if track is None or not track["added"]:
            raise ValueError("only subtitle files added in TV Cast can be removed")
        saved = self.state.movie(movie.path)
        with self.lock:
            pb = self.current
        if pb is not None and pb.movie.id == movie.id and pb.subtitle and pb.subtitle["id"] == track["id"]:
            self.switch_subtitles("off")
        # A choice of the removed file falls back to the default rules rather than to "off".
        self.state.update_movie(
            movie.path,
            subtitle_files=[p for p in saved.get("subtitle_files", []) if p != track["path"]],
            subtitle=None if saved.get("subtitle") == track["id"] else saved.get("subtitle"),
        )

    def subtitle_status(self, pb):
        info = self.probe_info(pb.movie) or {}
        return {
            "live": is_samsung(pb.tv) and not pb.drawn,  # switches in place; otherwise by reloading the video
            "drawn": ("hls" if pb.hls else "copy") if pb.drawn else None,
            "burn": subtitle_mode(pb.tv) == "burn",  # the TV shows no subtitles, not even the video's own
            "copy": self.copy_size(pb.movie, pb.subtitle),  # bytes of a finished copy with these subtitles
            "selected": pb.subtitle["id"] if pb.subtitle else None,
            "tracks": [
                {k: t[k] for k in ("id", "label", "lang", "added", "missing")}
                for t in self.subtitle_tracks(pb.movie)
            ],
            "embedded": [
                {"label": embedded_label(s), "image": s.get("codec") in IMAGE_CODECS}
                for s in info.get("subtitles", [])
            ],
        }

    def caption_url(self, movie):
        """The subtitle URL for the CaptionInfo.sec header on movie's stream, or None."""
        with self.lock:
            pb = self.current
        if pb is None or pb.movie.id != movie.id or pb.subtitle is None or pb.drawn:
            return None
        return pb.subtitle_url(pb.subtitle)

    # Burned-in subtitles

    def drawn_source(self, kind, url_path):
        """(movie, SubRip path, segment number or None) behind a /burned/ or /hls/ URL, or None."""
        pattern = {"burned": r"/burned/([0-9a-f]{12})/(f[0-9a-f]{10})\.mp4()",
                   "hls": r"/hls/([0-9a-f]{12})/(f[0-9a-f]{10})/(index\.m3u8|\d+\.ts)"}[kind]
        m = re.fullmatch(pattern, url_path)
        movie = m and self.lookup(m[1])
        track = movie and next((t for t in self.subtitle_tracks(movie) if t["id"] == m[2] and not t["missing"]), None)
        if not track:
            return None
        try:
            part = m[3].split(".")[0]
            return movie, track, prepare(track["path"], self.subtitle_cache), int(part) if part.isdigit() else None
        except (OSError, ValueError) as e:
            log("can't burn in subtitles %s: %s" % (track["path"], e))
            return None

    def drawn_playback(self, movie, tv, base, track, named, style=None):
        """Playback of movie with subtitle file track burned in. style "copy" plays the finished
        copy (and starts writing one when there is none), "hls" segments encoded as the TV asks
        for them; by default the copy when there is one, else HLS. None (send the file as usual)
        when this Mac can't burn subtitles and nobody asked for them."""
        why = burn.available()
        if why:
            if named or style:
                raise ValueError(why)
            log("warning: %s; sending %s's subtitles as a file" % (why, movie.title))
            return None
        srt = prepare(track["path"], self.subtitle_cache)
        done = burn.burned(movie, srt)
        if done and style != "hls":
            return Playback(movie, tv, base, track, burned=done)
        if style == "copy":
            self.start_burn(movie, track["id"])
            raise ValueError("%s has no copy with these subtitles yet; making one now (%s). Play it"
                             " meanwhile with HLS" % (movie.title, self.burn_eta(movie.id) or "about a third of its"
                                                     " running time"))
        session = self.hls_session(movie, track, srt)
        pb = Playback(movie, tv, base, track, hls=True)
        pb.duration = session.duration
        return pb

    def hls_session(self, movie, track, srt=None):
        """The HLS segments of movie with track burned in, replacing those of another movie."""
        with self.lock:
            current = self.hls
        if current is not None and current.movie.id == movie.id and current.track == track["id"]:
            return current
        session = burn.Hls(movie, srt or prepare(track["path"], self.subtitle_cache))
        session.track = track["id"]
        with self.lock:
            old, self.hls = self.hls, session
        if old is not None:
            old.stop()
        return session

    def hls_file(self, url_path):
        """(bytes or a file path, content type) for an /hls/ URL, or None."""
        source = self.drawn_source("hls", url_path)
        if source is None:
            return None
        movie, track, srt, segment = source
        with self.lock:
            current, pb = self.hls, self.current
        if current is not None and (current.movie.id, current.track) == (movie.id, track["id"]):
            session = current
        elif pb is None or (pb.hls and (pb.movie.id, pb.subtitle["id"]) == (movie.id, track["id"])):
            # The movie being opened, or a TV still playing after TV Cast restarted.
            session = self.hls_session(movie, track, srt)
        else:
            return None  # a request left over from what played before: don't disturb what plays now
        if segment is None:
            return burn.playlist(session.duration).encode(), "application/vnd.apple.mpegurl"
        return session.segment(segment), "video/mp2t"

    def burn_eta(self, movie_id):
        """"ready around HH:MM" for a running burn, or None."""
        with self.lock:
            job = dict(self.burns.get(movie_id) or {})
        if not job.get("running") or job.get("progress", 0) < 0.02:
            return None
        elapsed = time.time() - job["started"]
        return "ready around %s" % time.strftime("%H:%M", time.localtime(job["started"] + elapsed / job["progress"]))

    def copy_size(self, movie, track):
        """Bytes of the finished copy of movie with subtitle file track burned in, or None."""
        if track is None or track.get("missing"):
            return None
        try:
            path = burn.burned(movie, prepare(track["path"], self.subtitle_cache))
        except (OSError, ValueError):
            return None
        return os.path.getsize(path) if path else None

    def copies(self):
        """The burned-in copies on disk, as the panel lists them."""
        try:
            names = sorted(n for n in os.listdir(burn.BURNED_DIR) if n.endswith(".mp4") and ".part." not in n)
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            movie = self.lookup(name[:12])
            path = os.path.join(burn.BURNED_DIR, name)
            out.append({"id": name[:12], "title": movie.title if movie else name, "size": os.path.getsize(path),
                        "path": path})
        return out

    def delete_copies(self, movie_id):
        """Delete movie's burned-in copies (any subtitles), unless one is playing."""
        with self.lock:
            pb = self.current
            job = self.burns.get(movie_id)
        if pb is not None and pb.movie.id == movie_id and pb.burned:
            raise ValueError("%s is playing from its copy; stop it first" % pb.movie.title)
        if job and job["running"]:
            raise ValueError("a copy of %s is still being made" % job["title"])
        gone = [c for c in self.copies() if c["id"] == movie_id]
        if not gone:
            raise ValueError("no copy with burned-in subtitles to delete")
        for c in gone:
            os.remove(c["path"])
            log("deleted %s (%.1f GB)" % (c["path"], c["size"] / 1e9))
        with self.lock:
            self.burns.pop(movie_id, None)
        return {"deleted": len(gone), "bytes": sum(c["size"] for c in gone)}

    def start_burn(self, movie, choice=None):
        """Start writing a copy of movie with a subtitle file burned in, in the background:
        choice names the file as match_track does; by default the movie's usual one."""
        why = burn.available()
        if why:
            raise ValueError(why)
        tracks = self.subtitle_tracks(movie)
        track = match_track(tracks, str(choice)) if choice else self.default_subtitle(movie, tracks, allow_off=False)
        if track is None:
            raise ValueError("%s has no subtitle file to burn in; add one" % movie.title)
        srt = prepare(track["path"], self.subtitle_cache)
        if burn.burned(movie, srt):
            return {"ready": True}
        with self.lock:
            job = self.burns.get(movie.id)
            if job and job["running"]:
                return {"running": True}
            self.burns[movie.id] = {"title": movie.title, "subtitle": track["label"], "progress": 0.0,
                                    "running": True, "error": None, "started": time.time()}
        threading.Thread(target=self._burn, args=(movie, srt), daemon=True).start()
        return {"started": True}

    def _burn(self, movie, srt):
        out = burn.burned_path(movie, srt)
        part = out[:-4] + ".part.mp4"
        try:
            sup, info = burn.prepare(movie, srt)
            os.makedirs(burn.BURNED_DIR, exist_ok=True)
            log("burning subtitles into %s" % movie.title)
            with tempfile.TemporaryFile("w+") as errors:
                proc = subprocess.Popen(burn.burn_command(movie.path, sup, info, part),
                                        stdout=subprocess.PIPE, stderr=errors, text=True)
                # Keep the Mac awake for as long as this encode runs.
                subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(proc.pid)])
                with proc.stdout:
                    for line in proc.stdout:
                        key, _, value = line.strip().partition("=")
                        if key == "out_time_us" and value.isdigit() and info["duration"]:
                            with self.lock:
                                self.burns[movie.id]["progress"] = min(1.0, int(value) / 1e6 / info["duration"])
                proc.wait()
                if proc.returncode != 0:
                    errors.seek(0)
                    raise RuntimeError("ffmpeg failed: %s" % (errors.read().strip()[-300:] or proc.returncode))
            os.replace(part, out)
            log("subtitles burned into %s: %s" % (movie.title, out))
            with self.lock:
                self.burns[movie.id].update(progress=1.0, running=False)
        except Exception as e:
            log("burning subtitles into %s failed: %s" % (movie.title, e))
            with contextlib.suppress(OSError):
                os.remove(part)
            with self.lock:
                self.burns[movie.id].update(running=False, error=str(e))

    # Playback

    def keep_awake(self, on):
        # caffeinate -w exits with this process, so a crash never leaves the Mac sleepless.
        with self.lock:
            if on and self.caffeinate is None:
                self.caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            elif not on and self.caffeinate is not None:
                self.caffeinate.terminate()
                self.caffeinate.wait()
                self.caffeinate = None

    def open_movie(self, movie_id, from_start=False, subtitle=None, style=None):
        with self.playback_lock:
            self._open_movie(movie_id, from_start, subtitle, style=style)

    def _open_movie(self, movie_id, from_start, subtitle, at=None, style=None):
        """Load a movie on the selected TV from its resume point, or from position at when set."""
        movie = self.lookup(movie_id)
        if movie is None or movie.root not in self.state.get("libraries"):
            raise ValueError("no video with id %r in the library" % movie_id)
        if not os.path.isfile(movie.path):
            raise ValueError("%s is unavailable; reconnect its folder and rescan" % movie.title)
        tv = self.require_tv()
        if at is None:
            start = None if from_start else self.state.movie(movie.path).get("position", 0)
            seek = None if start is None or start < RESUME_MIN else max(0, start - RESUME_REWIND)
        else:
            seek = at
        tracks = self.subtitle_tracks(movie)
        if subtitle is not None:  # named by the user: a problem with it is an error
            track = None if subtitle == "off" else match_track(tracks, subtitle)
            if track:
                self.subtitle_ready(track)
            self.remember_subtitle(movie, track)
        else:  # picked by the rules: a broken file must not keep the movie from playing
            track = self.default_subtitle(movie, tracks, tv_shows_embedded=subtitle_mode(tv) != "burn")
            try:
                if track:
                    self.subtitle_ready(track)
            except ValueError as e:
                log("warning: playing %s without subtitles: %s" % (movie.title, e))
                track = None
        base = "http://%s:%d" % (lan_ip_towards(tv["ip"]), self.port)
        pb = None
        if style and not (track and subtitle_mode(tv) == "burn"):
            raise ValueError("%s for %s needs a TV that burns subtitles in, and subtitles chosen"
                             % ("HLS" if style == "hls" else "a copy", movie.title))
        if track and subtitle_mode(tv) == "burn":
            pb = self.drawn_playback(movie, tv, base, track, named=subtitle is not None, style=style)
        if pb is None:
            pb = Playback(movie, tv, base, track)
        with self.lock:
            if self.state.get("tv") != tv["id"]:
                raise RuntimeError("another TV was selected while opening %s; open it again" % movie.title)
            previous, self.current = self.current, pb
            self.keep_awake(True)
        if previous is not None:
            self.save_progress(previous)
        how = " with subtitles burned in (%s)" % ("HLS" if pb.hls else "copy") if pb.drawn else ""
        log("opening %s on %s%s%s" % (movie.title, tv["name"], " at " + hms(seek) if seek else "", how))
        try:
            self.load(pb, seek)
        except BaseException:
            with self.lock:
                if self.current is pb:
                    self.current = None
                    self.keep_awake(False)
            raise
        self.consume_queued(movie.id)

    def load(self, pb, start):
        # DLNA has no standard for subtitle files, so the chosen file goes out every common way:
        # - a res of type text/srt, which Kodi and BubbleUPnP's renderer list as a subtitle track;
        # - Samsung's sec: tags and CaptionInfo.sec header, which Philips Android TVs also read;
        # - pv: attributes on the video's res, which Universal Media Server sends to Sony and
        #   other non-Samsung renderers. Samsung TVs don't get these, so their requests stay as
        #   tested.
        # A Samsung player registers only the first file, so only the chosen one goes out;
        # set_caption or a reload switches to others later.
        sub_xml = pv_attrs = pv_ns = ""
        if pb.subtitle and not pb.drawn:
            url = pb.subtitle_url(pb.subtitle)
            sub_xml = (
                '<res protocolInfo="http-get:*:text/srt:*">%s</res>'
                '<sec:CaptionInfoEx sec:type="srt">%s</sec:CaptionInfoEx>'
                '<sec:CaptionInfo sec:type="srt">%s</sec:CaptionInfo>' % ((url,) * 3)
            )
            if not is_samsung(pb.tv):
                pv_ns = ' xmlns:pv="http://www.pv.com/pvns/"'
                pv_attrs = ' pv:subtitleFileType="SRT" pv:subtitleFileUri="%s"' % url
        title = pb.movie.title + (" (%d)" % pb.movie.year if pb.movie.year else "")
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            ' xmlns:sec="http://www.sec.co.kr/"%s>'
            '<item id="0" parentID="-1" restricted="1"><dc:title>%s</dc:title>'
            "<upnp:class>object.item.videoItem.movie</upnp:class>"
            "%s%s</item></DIDL-Lite>"
            % (pv_ns, escape(title), self.video_res(pb, pv_attrs), sub_xml)
        )
        transport(pb.tv, "SetAVTransportURI", CurrentURI=pb.media_url, CurrentURIMetaData=didl)
        self.when_ready(pb.tv, "Play", Speed=1)
        if start is not None:
            self.when_ready(pb.tv, "Seek", Unit="REL_TIME", Target=hms(start))
        if pb.subtitle and not pb.drawn and is_samsung(pb.tv):
            try:
                self.enable_subtitles_on_load(pb)
            except RuntimeError as e:  # the movie plays anyway; the panel can still switch them on
                log("warning: couldn't switch on subtitles: %s" % e)

    def video_res(self, pb, pv_attrs):
        """The DIDL res element for pb's video."""
        if pb.hls:
            return '<res protocolInfo="http-get:*:application/vnd.apple.mpegurl:*" duration="%s.000">%s</res>' % (
                hms(pb.duration), escape(pb.media_url))
        ctype = "video/mp4" if pb.burned else video_type(pb.movie, pb.tv)
        return '<res protocolInfo="http-get:*:%s:%s" size="%d"%s>%s</res>' % (
            ctype, DLNA_FEATURES, pb.size, pv_attrs, pb.media_url)

    def when_ready(self, tv, action, **args):
        # The TV opens a newly set stream asynchronously and rejects transport actions with
        # UPnP error 701 "Transition not available" until the player is ready for them.
        deadline = time.time() + 30
        while True:
            try:
                return transport(tv, action, **args)
            except RuntimeError as e:
                if "Transition not available" not in str(e) or time.time() > deadline:
                    raise
            time.sleep(0.5)

    def enable_subtitles_on_load(self, pb):
        # The player can list subtitles as enabled without drawing them (seen after a
        # restart-and-seek), so send an explicit Enable once it has parsed the file. Enable works
        # even for a file the player never listed, so a slow listing only delays it.
        deadline = time.time() + 15
        while not captions(pb.tv)[0] and time.time() < deadline:
            time.sleep(0.5)
        set_caption(pb.tv, True, pb.media_url, pb.subtitle_url(pb.subtitle), pb.movie.title)

    def monitor(self):
        last_idle_poll = 0.0
        while True:
            time.sleep(POLL_INTERVAL)
            with self.lock:
                pb = self.current
            try:
                if pb is not None:
                    self.poll(pb)
                elif time.time() - last_idle_poll >= IDLE_POLL_INTERVAL:
                    last_idle_poll = time.time()
                    self.adopt()
            except Exception:
                # Keep tracking playback after an unexpected error; the traceback goes to the log.
                traceback.print_exc()

    def adopt(self):
        """Resume tracking a video the selected TV is still playing from this service (after a restart)."""
        tv = self.selected()
        if tv is None:
            return
        try:
            s = transport_status(tv)
        except RuntimeError:
            return  # TV off or unreachable while nothing is playing: nothing to pick up
        m = re.fullmatch(r"(https?://[^/]+)/media/([0-9a-f]{12})\.\w+", s["uri"] or "")
        movie = m and self.lookup(m[2])
        if not movie or s["state"] not in ("PLAYING", "PAUSED_PLAYBACK"):
            return
        # The TV doesn't say which subtitle file it shows; the movie's remembered choice is the best guess.
        pb = Playback(movie, tv, m[1], self.default_subtitle(movie, self.subtitle_tracks(movie)))
        pb.seen_playing = True
        with self.playback_lock, self.lock:
            # Another TV may have been selected while this one was being asked.
            if self.current is not None or self.state.get("tv") != tv["id"]:
                return
            self.current = pb
            self.consume_queued(movie.id)
            self.keep_awake(True)
        log("picked up %s, already on %s" % (movie.title, tv["name"]))

    def poll(self, pb):
        try:
            s = transport_status(pb.tv)
        except RuntimeError as e:
            now = time.time()
            if pb.unreachable_since is None:
                pb.unreachable_since = now
                log("TV unreachable: %s" % e)
            elif now - pb.unreachable_since > OFFLINE_GRACE:
                self.finish(pb, advance=False)
            return
        pb.unreachable_since = None
        if s["state"] in ("PLAYING", "PAUSED_PLAYBACK") and s["uri"] == pb.media_url:
            pb.seen_playing = True
            pb.stopped_polls = 0
            if s["position"] is not None:
                pb.position = s["position"]
            if s["duration"]:
                pb.duration = s["duration"]
            if time.time() - pb.last_saved >= SAVE_INTERVAL:
                self.save_progress(pb)
        elif pb.seen_playing and s["state"] != "TRANSITIONING":
            # Stopped, ran out, or the TV moved on to other media.
            pb.stopped_polls += 1
            if pb.stopped_polls >= STOP_CONFIRM:
                self.finish(pb, advance=s["state"] in ("STOPPED", "NO_MEDIA_PRESENT")
                            and s["uri"] in (None, "", pb.media_url))

    def save_progress(self, pb):
        if pb.position is None:
            return
        saved = self.state.movie(pb.movie.path)
        finished = bool(pb.duration) and pb.position >= pb.duration * WATCHED_FRACTION
        self.state.update_movie(
            pb.movie.path,
            position=0 if finished else round(pb.position, 1),
            duration=pb.duration or saved.get("duration"),
            watched=finished or saved.get("watched", False),
            updated=round(time.time()),
        )
        pb.last_saved = time.time()

    def finish(self, pb, advance=True):
        with self.playback_lock:
            with self.lock:
                if self.current is not pb:
                    return
                self.current = None
                self.keep_awake(False)
            self.save_progress(pb)
            ran_out = bool(pb.duration) and pb.position is not None and pb.position >= pb.duration - END_SLACK
            log("%s %s at %s" % ("finished" if ran_out else "stopped", pb.movie.title, hms(pb.position)))
            if not (ran_out and advance and self.state.get("queue")) or self.state.get("tv") != pb.tv["id"]:
                return
            try:
                self.play_next()
            except (RuntimeError, ValueError) as e:
                log("queue paused: %s" % e)  # play_next also exposes this error in the panel

    # Panel API

    def movie_info(self, movie, copied=None):
        """The panel's record of movie; copied is the set of movie ids with copies (status() scans
        the copies folder once for all movies)."""
        saved = self.state.movie(movie.path)
        info = self.probe_info(movie)
        with self.lock:
            checking = movie.path in self.probing
        tracks = self.subtitle_tracks(movie)
        embedded = (info or {}).get("subtitles", [])
        languages = {t["lang"] for t in tracks} | {LANGUAGE_TOKENS.get((s.get("lang") or "").lower()) for s in embedded}
        return {
            "id": movie.id,
            "group": movie.group,
            "title": movie.title,
            "year": movie.year,
            "subs": {"count": len(tracks) + len(embedded), "languages": sorted(languages - {None})},
            "copy": movie.id in (copied if copied is not None else {c["id"] for c in self.copies()}),
            "position": saved.get("position", 0),
            "duration": saved.get("duration") or (info or {}).get("duration"),
            "watched": saved.get("watched", False),
            "problem": playability(info),
            "checking": checking,
        }

    def status(self):
        libraries = self.state.get("libraries")
        tv = self.selected()
        with self.lock:
            pb, movies = self.current, list(self.movies)
            problems, private = dict(self.library_problems), dict(self.library_private)
            scanning, checking = self.scanning, len(self.probing)
            queued, queue_error = list(self.state.get("queue")), self.queue_error
            burns = [dict(job, id=movie_id) for movie_id, job in self.burns.items()]
        for job in burns:
            job["eta"] = self.burn_eta(job["id"])
        movies = [m for m in movies if m.root in libraries]
        available = {m.id for m in movies}
        copies = self.copies()
        groups = []
        for m in movies:  # already in display order
            if not groups or groups[-1]["id"] != m.group:
                groups.append({"id": m.group, "name": os.path.basename(m.group) or m.group, "collection": m.group != m.root})
        out = {
            "now": None if pb is None else {
                "id": pb.movie.id, "title": pb.movie.title, "year": pb.movie.year,
                "file": os.path.basename(pb.movie.path), "subtitles": self.subtitle_status(pb),
            },
            "queue": [dict(item, available=item["id"] in available) for item in queued],
            "queue_error": queue_error,
            "burns": burns,
            "copies": copies,
            "libraries": [
                {"path": p, "name": os.path.basename(p) or p, "problems": problems.get(p, []), "private": private.get(p, [])}
                for p in libraries
            ],
            "groups": groups,
            "library": [self.movie_info(m, {c["id"] for c in copies}) for m in movies],
            "scanning": scanning,
            "checking": checking,
            "tvs": tv_list(self.state.tvs()),
            "selected": tv["id"] if tv else None,
            "remote": {"available": bool(tv and tv.get("remote")), "paired": bool(tv and samsung.paired(tv))},
            "server": self.stats.snapshot(),
            "power": mac_power(),
            "tv": None,
            "tv_error": None,
        }
        if tv is None:
            out["tv_error"] = NO_TV
            return out
        try:
            st = tv_status(tv, STATUS_TIMEOUT)
        except RuntimeError as e:
            out["tv_error"] = str(e)
            return out
        # None unless a Samsung TV plays a video from this library with a subtitle file shown by
        # TV Cast and reports it; a failing vendor extension must not make the TV look unreachable.
        st["subtitles"] = None
        if pb is not None and pb.subtitle and is_samsung(tv):
            with contextlib.suppress(RuntimeError):
                available, enabled = captions(tv)
                # None when the player has no subtitle file; the reported on/off can be stale.
                st["subtitles"] = bool(enabled) if available else None
        out["tv"] = st
        return out

    def control(self, action, body):
        if action == "open":
            subtitle, style = body.get("subtitle"), body.get("burn")
            if style not in (None, "hls", "copy"):
                raise ValueError("burn must be hls or copy")
            self.open_movie(str(body["id"]), from_start=bool(body.get("from_start")),
                            subtitle=None if subtitle is None else str(subtitle), style=style)
        elif action == "stop":
            with self.playback_lock:
                tv = self.require_tv()
                with self.lock:
                    pb = self.current
                transport(tv, "Stop")
                if pb is not None:
                    self.finish(pb, advance=False)
        elif action in ("queue_add", "queue_remove", "queue_move", "queue_clear"):
            self.edit_queue(action, body)
        elif action == "queue_next":
            self.play_next()
        elif action == "play":
            transport(self.require_tv(), "Play", Speed=1)
        elif action == "pause":
            transport(self.require_tv(), "Pause")
        elif action == "seek":
            transport(self.require_tv(), "Seek", Unit="REL_TIME", Target=hms(max(0.0, float(body["seconds"]))))
        elif action == "volume":
            rendering(self.require_tv(), "SetVolume", DesiredVolume=max(0, min(100, int(body["level"]))))
        elif action == "mute":
            rendering(self.require_tv(), "SetMute", DesiredMute=1 if body["on"] else 0)
        elif action == "subtitles":
            self.switch_subtitles(str(body["track"]))
        elif action == "add_subtitles":
            return self.add_subtitles(body)
        elif action == "remove_subtitles":
            self.remove_subtitles(body)
        elif action == "burn":
            return self.start_burn(self.subtitle_movie(body), body.get("subtitle"))
        elif action == "delete_copy":
            return self.delete_copies(self.subtitle_movie(body).id)
        elif action == "subtitle_mode":
            tv = self.require_tv()
            mode = str(body["mode"])
            if mode not in SUBTITLE_MODES:
                raise ValueError("subtitle mode must be one of %s" % ", ".join(SUBTITLE_MODES))
            self.state.update_tv(tv["id"], subtitle_mode=mode)
            return {"mode": mode, "subtitles": subtitle_mode(self.state.tv(tv["id"]))}
        elif action == "add_folder":
            return self.add_folder()
        elif action == "remove_folder":
            self.remove_folder(str(body["path"]))
        elif action == "rescan":
            self.request_scan()
        elif action == "discover":
            return {"tvs": tv_list(self.discover())}
        elif action == "select":
            self.select(str(body["id"]))
        elif action == "pair":
            tv = self.remote_tv()
            self.remote.pair(tv)
            log("paired with TV %s for remote control" % tv["name"])
        elif action == "keys":
            self.remote.keys(self.remote_tv(), [str(k) for k in body["keys"]])
        elif action == "text":
            self.remote.text(self.remote_tv(), str(body["text"]))
        elif action == "apps":
            return {"apps": self.remote.apps(self.remote_tv())}
        elif action == "launch":
            return {"ok": True, "app": self.remote.launch(self.remote_tv(), str(body["id"]))}
        else:
            raise ValueError("unknown action %r" % action)
        return {"ok": True}
