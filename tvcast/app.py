"""The daemon: TV selection, the library, playback tracking with resume points and auto-play,
and the actions behind the control panel's API."""
import collections
import contextlib
import os
import queue
import re
import socket
import subprocess
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from xml.sax.saxutils import escape

from . import discovery, samsung
from .library import FFPROBE, VIDEO_TYPES, choose_folder, playability, probe, scan_library
from .server import PANEL_HTML, PanelHandler, Stats, StreamHandler
from .state import State
from .upnp import (
    DLNA_FEATURES, captions, has_caption_control, hms, log, rendering, set_subtitles, transport, transport_status,
    tv_status,
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


def tv_list(tvs):
    """TV records as the panel lists them, sorted by name."""
    return [
        {"id": tv["id"], "name": tv["name"], "model": tv.get("model", ""), "ip": tv["ip"], "remote": bool(tv.get("remote"))}
        for tv in sorted(tvs, key=lambda tv: (tv["name"].lower(), tv["id"]))
    ]


class Playback:
    """One movie loaded on a TV, as last observed by the monitor."""

    def __init__(self, movie, tv, base):
        self.movie = movie
        self.tv = tv
        self.media_url = "%s/media/%s%s" % (base, movie.id, movie.ext)
        self.subs_url = "%s/subs/%s.srt" % (base, movie.id) if movie.subs else None
        self.seen_playing = False  # STOPPED before the first PLAYING is just the TV loading
        self.stopped_polls = 0
        self.position = None
        self.duration = None
        self.last_saved = 0.0
        self.unreachable_since = None


class App:
    def __init__(self, args):
        self.port = args.port
        self.state = State(args.state)
        if self.state.get("libraries") is None:
            seeds = args.library or ["~/Movies"]
            self.state.set("libraries", [os.path.abspath(os.path.expanduser(r)) for r in seeds])
        self.stats = Stats()
        self.remote = samsung.Remote(self.state)
        # Guards current, movies, by_id, library_problems, library_private, scanning,
        # rescan_pending, probing and caffeinate, and keeps the TV selection in step with current.
        self.lock = threading.RLock()
        self.current = None
        self.movies, self.by_id, self.library_problems, self.library_private = [], {}, {}, {}
        self.scanning = self.rescan_pending = False
        self.probe_queue = queue.Queue()
        self.probing = set()  # paths queued for or under ffprobe
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
        with self.lock:
            if self.state.get("tv") == tv_id:
                return
            # The old TV gets no Stop, but the stream server stops serving it, so its playback
            # ends once its buffer runs out. Tracking it (resume point, auto-play) ends here.
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
            return entry["info"]
        return None

    def lookup(self, movie_id):
        with self.lock:
            return self.by_id.get(movie_id)

    def next_after(self, movie):
        """The next playable video in the same collection, for auto-play."""
        with self.lock:
            siblings = [m for m in self.movies if m.group == movie.group]
        ids = [m.id for m in siblings]
        if movie.id not in ids:
            return None
        for m in siblings[ids.index(movie.id) + 1:]:
            if playability(self.probe_info(m)) is None:
                return m
        return None

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

    def resolve(self, url_path):
        """(file path, content type, movie or None) behind a stream URL, or None.

        Every library video stays reachable by its id, so a TV that keeps playing across a
        restart of this service never hits a 404."""
        m = re.fullmatch(r"/(media|subs)/([0-9a-f]{12})(\.\w+)", url_path)
        movie = m and self.lookup(m[2])
        if not movie:
            return None
        if m[1] == "media" and m[3] == movie.ext:
            return movie.path, VIDEO_TYPES[movie.ext], movie
        if m[1] == "subs" and m[3] == ".srt" and movie.subs:
            return movie.subs, "text/srt", None
        return None

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

    def open_movie(self, movie_id, from_start=False):
        movie = self.lookup(movie_id)
        if movie is None:
            raise ValueError("no video with id %r in the library" % movie_id)
        tv = self.require_tv()
        start = None if from_start else self.state.movie(movie.path).get("position", 0)
        if start is not None and start < RESUME_MIN:
            start = None
        pb = Playback(movie, tv, "http://%s:%d" % (lan_ip_towards(tv["ip"]), self.port))
        with self.lock:
            if self.state.get("tv") != tv["id"]:
                raise RuntimeError("another TV was selected while opening %s; open it again" % movie.title)
            previous, self.current = self.current, pb
            self.keep_awake(True)
        if previous is not None:
            self.save_progress(previous)
        log("opening %s on %s%s" % (movie.title, tv["name"], " at " + hms(start) if start else ""))
        try:
            self.load(pb, None if start is None else max(0, start - RESUME_REWIND))
        except BaseException:
            with self.lock:
                if self.current is pb:
                    self.current = None
                    self.keep_awake(False)
            raise

    def load(self, pb, start):
        sub_xml = ""
        if pb.subs_url:
            # The sec: tags are Samsung's way to attach external subtitles; other TVs ignore them.
            sub_xml = (
                '<res protocolInfo="http-get:*:text/srt:*">%s</res>'
                '<sec:CaptionInfoEx sec:type="srt">%s</sec:CaptionInfoEx>'
                '<sec:CaptionInfo sec:type="srt">%s</sec:CaptionInfo>' % ((pb.subs_url,) * 3)
            )
        title = pb.movie.title + (" (%d)" % pb.movie.year if pb.movie.year else "")
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            ' xmlns:sec="http://www.sec.co.kr/">'
            '<item id="0" parentID="-1" restricted="1"><dc:title>%s</dc:title>'
            "<upnp:class>object.item.videoItem.movie</upnp:class>"
            '<res protocolInfo="http-get:*:%s:%s" size="%d">%s</res>%s'
            "</item></DIDL-Lite>"
            % (escape(title), VIDEO_TYPES[pb.movie.ext], DLNA_FEATURES, pb.movie.size, pb.media_url, sub_xml)
        )
        transport(pb.tv, "SetAVTransportURI", CurrentURI=pb.media_url, CurrentURIMetaData=didl)
        self.when_ready(pb.tv, "Play", Speed=1)
        if start is not None:
            self.when_ready(pb.tv, "Seek", Unit="REL_TIME", Target=hms(start))
        if pb.subs_url and has_caption_control(pb.tv):
            try:
                self.enable_subtitles_on_load(pb.tv)
            except RuntimeError as e:  # the movie plays anyway; the panel can still try Subtitles: On
                log("warning: couldn't switch on subtitles: %s" % e)

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

    def enable_subtitles_on_load(self, tv):
        # The player can list subtitles as enabled without drawing them (seen after a
        # restart-and-seek), so send an explicit Enable once it has parsed the file.
        deadline = time.time() + 15
        while not captions(tv)[0]:
            if time.time() > deadline:
                log("warning: TV %s never reported the subtitle file; use Subtitles: On in the panel" % tv["name"])
                return
            time.sleep(0.5)
        set_subtitles(tv, True)

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
        pb = Playback(movie, tv, m[1])
        pb.seen_playing = True
        with self.lock:
            # Another TV may have been selected while this one was being asked.
            if self.current is not None or self.state.get("tv") != tv["id"]:
                return
            self.current = pb
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
                self.finish(pb, autoplay=False)
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
                self.finish(pb)

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

    def finish(self, pb, autoplay=True):
        with self.lock:
            if self.current is not pb:
                return
            self.current = None
            self.keep_awake(False)
        self.save_progress(pb)
        ran_out = bool(pb.duration) and pb.position is not None and pb.position >= pb.duration - END_SLACK
        log("%s %s at %s" % ("finished" if ran_out else "stopped", pb.movie.title, hms(pb.position)))
        # Auto-play stays on the TV that ran out; another TV may have been selected meanwhile.
        if not (ran_out and autoplay and self.state.get("autoplay")) or self.state.get("tv") != pb.tv["id"]:
            return
        nxt = self.next_after(pb.movie)
        if nxt is None:
            return
        log("auto-playing %s" % nxt.title)
        try:
            self.open_movie(nxt.id)
        except RuntimeError as e:
            log("could not auto-play %s: %s" % (nxt.title, e))

    # Panel API

    def movie_info(self, movie):
        saved = self.state.movie(movie.path)
        info = self.probe_info(movie)
        with self.lock:
            checking = movie.path in self.probing
        return {
            "id": movie.id,
            "group": movie.group,
            "title": movie.title,
            "year": movie.year,
            "subs": bool(movie.subs),
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
        up_next = self.next_after(pb.movie) if pb is not None else None
        movies = [m for m in movies if m.root in libraries]
        groups = []
        for m in movies:  # already in display order
            if not groups or groups[-1]["id"] != m.group:
                groups.append({"id": m.group, "name": os.path.basename(m.group) or m.group, "collection": m.group != m.root})
        out = {
            "now": None if pb is None else {
                "id": pb.movie.id, "title": pb.movie.title, "year": pb.movie.year,
                "file": os.path.basename(pb.movie.path),
            },
            "up_next": up_next.id if up_next else None,
            "libraries": [
                {"path": p, "name": os.path.basename(p) or p, "problems": problems.get(p, []), "private": private.get(p, [])}
                for p in libraries
            ],
            "groups": groups,
            "library": [self.movie_info(m) for m in movies],
            "scanning": scanning,
            "checking": checking,
            "autoplay": self.state.get("autoplay"),
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
        # None unless a Samsung TV plays a video with subtitles from this library and reports
        # them; a failing vendor extension must not make the TV look unreachable.
        st["subtitles"] = None
        if pb is not None and pb.subs_url and has_caption_control(tv):
            with contextlib.suppress(RuntimeError):
                available, enabled = captions(tv)
                # None when the player has no subtitle file; the reported on/off can be stale.
                st["subtitles"] = bool(enabled) if available else None
        out["tv"] = st
        return out

    def control(self, action, body):
        if action == "open":
            self.open_movie(str(body["id"]), from_start=bool(body.get("from_start")))
        elif action == "stop":
            tv = self.require_tv()
            with self.lock:
                pb = self.current
            transport(tv, "Stop")
            if pb is not None:
                self.finish(pb, autoplay=False)
        elif action == "autoplay":
            self.state.set("autoplay", bool(body["on"]))
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
            with self.lock:
                pb = self.current
            # Without this check the command would toggle subtitles on media from other apps.
            if pb is None or not pb.subs_url:
                raise ValueError("no video with subtitles is playing from this library")
            if not has_caption_control(pb.tv):
                raise RuntimeError("TV %s can't switch subtitles from TV Cast; use its own remote" % pb.tv["name"])
            set_subtitles(pb.tv, bool(body["on"]))
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
            self.remote.launch(self.remote_tv(), str(body["id"]))
        else:
            raise ValueError("unknown action %r" % action)
        return {"ok": True}
