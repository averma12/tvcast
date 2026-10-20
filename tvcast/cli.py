"""The tv command: control TV Cast from the terminal, run its daemon, or install it as a login service.

Every command except daemon, install and uninstall is a thin client of the running daemon's
panel API on 127.0.0.1, so the terminal and the panel always act on the same state.
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__
from .app import App
from .state import DEFAULT_STATE
from .upnp import hms, seconds_of

PANEL_PORT = 8766
STREAM_PORT = 8765
TIMEOUT = 120  # opening a movie waits on the TV's player, and pairing on someone pressing Allow
SCAN_WAIT = 30  # seconds `tv open` waits for the library rescan before matching what is listed
NOT_RUNNING = ("TV Cast isn't running (nothing on 127.0.0.1:%d); start it with \"tv install\""
               " (login service) or \"tv daemon\"")
LABEL = "local.tvcast"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
LOG = os.path.expanduser("~/Library/Logs/tvcast.log")
SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "launcher.py")
# macOS privacy protection (TCC) keeps launchd agents out of these folders, so a login service
# couldn't even load its code from there.
PRIVATE_FOLDERS = ("~/Desktop", "~/Documents", "~/Downloads")


class NotRunning(SystemExit):
    """Exits like any other CLI error; install catches it because it works without the daemon."""


def api(port, action, body=None):
    """GET /api/status when body is None, else POST body to /api/<action>; the decoded reply."""
    req = urllib.request.Request("http://127.0.0.1:%d/api/%s" % (port, action))
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
        req.add_header("X-TVCast", "1")  # the daemon refuses POSTs without it (see PanelHandler)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            message = json.loads(detail)["error"]
        except (ValueError, KeyError, TypeError):  # not one of the daemon's JSON errors
            message = "HTTP %d %s" % (e.code, e.reason)
        raise SystemExit(message)
    except OSError as e:  # URLError, timeouts
        reason = getattr(e, "reason", e)
        if isinstance(reason, ConnectionRefusedError):
            raise NotRunning(NOT_RUNNING % port)
        raise SystemExit("TV Cast on 127.0.0.1:%d didn't answer %s: %s" % (port, action, reason))


def pick(query, candidates, kind):
    """The candidate whose label contains query, ignoring case. candidates: (label, value).

    A label that is the query, alone or followed by its parenthesized year or IP, beats partial
    matches, so "Alien" is told from "Aliens" and "Dune (2021)" from "Dune (1984)"."""
    q = query.lower()
    matches = [c for c in candidates if q in c[0].lower()]
    exact = [c for c in matches if c[0].lower() == q or c[0].lower().startswith(q + " (")]
    if len(exact) == 1:
        return exact[0][1]
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        raise SystemExit("no %s matches %r" % (kind, query))
    raise SystemExit("%r matches %d %ss:\n  %s" % (query, len(matches), kind, "\n  ".join(c[0] for c in matches)))


def video_label(movie):
    return movie["title"] + (" (%d)" % movie["year"] if movie["year"] else "")


def show_status(port):
    st = api(port, "status")
    tv = next((tv for tv in st["tvs"] if tv["id"] == st["selected"]), None)
    if tv is None:
        raise SystemExit('No TV selected; list TVs with "tv tvs --find" and pick one with "tv use"')
    print("%s (%s)" % (tv["name"], tv["ip"]))
    if st["tv_error"]:
        raise SystemExit(st["tv_error"])
    s = st["tv"]
    line = [s["state"] or "?", video_label(st["now"]) if st["now"] else s["uri"] or "nothing loaded",
            "%s / %s" % (hms(s["position"]), hms(s["duration"]))]
    if s["volume"] is not None:
        line.append("volume %d%s" % (s["volume"], " (muted)" if s["muted"] else ""))
    drawn = st["now"] and st["now"]["subtitles"].get("drawn")
    if drawn:
        line.append("· subtitles burned in (%s)" % ("HLS" if drawn == "hls" else "copy"))
    print("  ".join(line))
    for job in st.get("burns", []):
        state = ("failed: %s" % job["error"] if job["error"]
                 else "%d%%%s" % (job["progress"] * 100, ", " + job["eta"] if job.get("eta") else "") if job["running"]
                 else "ready")
        print("Copy of %s with %s burned in: %s" % (job["title"], job["subtitle"], state))


def list_tvs(port, find):
    if find:
        found = api(port, "discover", {})["tvs"]
        print("Found %d TV%s on the network" % (len(found), "" if len(found) == 1 else "s"))
    st = api(port, "status")
    if not st["tvs"]:
        print('No TVs known yet; search the network with "tv tvs --find"')
    for i, tv in enumerate(st["tvs"], 1):
        mode = "subtitles %s%s" % ("burned in" if tv["subtitles"] == "burn" else "as files",
                                   " (auto)" if tv["subtitle_mode"] == "auto" else "")
        fields = [tv["name"], tv["model"], tv["ip"], "Samsung remote" if tv["remote"] else "", mode]
        print("%s %d. %s" % ("*" if tv["id"] == st["selected"] else " ", i, "  ".join(f for f in fields if f)))


def use_tv(port, which):
    tvs = api(port, "status")["tvs"]
    if which.isdigit():
        if not 1 <= int(which) <= len(tvs):
            raise SystemExit('no TV number %s; "tv tvs" lists %d' % (which, len(tvs)))
        tv = tvs[int(which) - 1]
    else:
        tv = pick(which, [("%s (%s)" % (tv["name"], tv["ip"]), tv) for tv in tvs], "TV")
    api(port, "select", {"id": tv["id"]})
    print("Selected %s (%s)" % (tv["name"], tv["ip"]))


def find_video(port, query):
    # The daemon rescans only when the panel page loads or a folder changes, so ask for one to
    # find videos added since.
    api(port, "rescan", {})
    deadline = time.time() + SCAN_WAIT
    st = api(port, "status")
    while st["scanning"] and time.time() < deadline:
        time.sleep(0.3)
        st = api(port, "status")
    if not st["library"] and st["scanning"]:
        raise SystemExit("the library is still being scanned; try again in a moment")
    return pick(query, [(video_label(m), m) for m in st["library"]], "video")


def subtitle_choice(port, movie, subs):
    """A --subs value as the daemon takes it: a file on this Mac is attached to movie first."""
    if subs is not None and os.path.isfile(os.path.expanduser(subs)):
        path = os.path.abspath(os.path.expanduser(subs))
        return api(port, "add_subtitles", {"id": movie["id"], "path": path})["track"]
    return subs


def burn_video(port, query, subs, list_copies=False, delete=False):
    if list_copies:
        copies = api(port, "status")["copies"]
        for c in copies:
            print("%-40s %6.1f GB  %s" % (c["title"], c["size"] / 1e9, c["path"]))
        total = sum(c["size"] for c in copies)
        print("%d cop%s, %.1f GB" % (len(copies), "y" if len(copies) == 1 else "ies", total / 1e9) if copies
              else "No copies with burned-in subtitles")
        return
    if not query:
        raise SystemExit("name a video, e.g. tv burn dune")
    movie = find_video(port, query)
    if delete:
        result = api(port, "delete_copy", {"id": movie["id"]})
        print("Deleted the copy of %s (%.1f GB)" % (video_label(movie), result["bytes"] / 1e9))
        return
    body = {"id": movie["id"]}
    if subs is not None:
        body["subtitle"] = subtitle_choice(port, movie, subs)
    result = api(port, "burn", body)
    if result.get("ready"):
        print("%s already has these subtitles burned in" % video_label(movie))
    elif result.get("running"):
        print("Already burning subtitles into %s; tv status shows how far" % video_label(movie))
    else:
        print("Burning subtitles into %s in the background (about a third of its running time);"
              " tv status shows how far" % video_label(movie))


def open_video(port, query, from_start, subs, burn=None):
    movie = find_video(port, query)
    if movie["problem"]:
        print("warning: %s; trying anyway" % movie["problem"], file=sys.stderr)
    body = {"id": movie["id"], "from_start": from_start}
    if burn:
        body["burn"] = burn
    if subs is not None:
        body["subtitle"] = subtitle_choice(port, movie, subs)
    print("Opening %s" % video_label(movie))
    api(port, "open", body)


def playing_subtitles(port):
    """The status and the playing video's subtitle summary."""
    st = api(port, "status")
    if not st["now"]:
        raise SystemExit("no video from this library is playing")
    return st, st["now"]["subtitles"]


def subtitles(port, choice, add, remove, mode=None):
    if mode:
        result = api(port, "subtitle_mode", {"mode": mode})
        print("The selected TV shows subtitle files %s%s" % (
            "burned into the picture" if result["subtitles"] == "burn" else "as files its player draws",
            " (chosen for this TV automatically)" if mode == "auto" else ""))
        return
    if add:
        print("Showing %s" % api(port, "add_subtitles", {"path": os.path.abspath(os.path.expanduser(add))})["label"])
        return
    if remove:
        tracks = playing_subtitles(port)[1]["tracks"]
        if remove.isdigit():
            track = tracks[int(remove) - 1] if 1 <= int(remove) <= len(tracks) else None
        else:
            track = pick(remove, [(t["label"], t) for t in tracks if t["added"]], "added subtitle file")
        if track is None or not track["added"]:
            raise SystemExit('no added subtitle file number %s; "tv subs" lists them' % remove)
        api(port, "remove_subtitles", {"track": track["id"]})
        print("Removed %s" % track["label"])
        return
    if choice:
        api(port, "subtitles", {"track": choice})
    st, subs = playing_subtitles(port)
    off = subs["selected"] is None or (st["tv"] or {}).get("subtitles") is False
    own = any(not t["image"] for t in subs["embedded"])
    print("%s    off%s" % ("*" if off else " ", " (the TV shows the video's own subtitles)" if own else ""))
    for i, t in enumerate(subs["tracks"], 1):
        note = " (added%s)" % (", missing" if t["missing"] else "") if t["added"] else ""
        print("%s %2d. %s%s" % ("*" if not off and t["id"] == subs["selected"] else " ", i, t["label"], note))
    if subs["embedded"]:
        print("In the video file; switch or hide these with the TV's own subtitle menu:")
        for t in subs["embedded"]:
            note = " (Blu-ray image subtitles; the TV may not show them)" if t["image"] else ""
            print("       %s%s" % (t["label"], note))
    if not subs["tracks"] and not subs["embedded"]:
        print('No subtitles; add a file with "tv subs --add FILE"')
    if subs.get("drawn") == "hls":
        print("Burned in as the TV plays (HLS); tv burn makes a copy that needs no encoding while playing")
    elif subs.get("drawn") == "copy":
        print("Burned in (playing the finished copy)")


def key_name(name):
    name = name.upper()
    return name if name.startswith("KEY_") else "KEY_" + name


def launchctl(*args):
    r = subprocess.run(["/bin/launchctl"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("launchctl %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip() or "exit status %d" % r.returncode))


def bootout():
    """Stop and unload the login service if it is loaded."""
    target = "gui/%d/%s" % (os.getuid(), LABEL)
    if subprocess.run(["/bin/launchctl", "print", target], capture_output=True).returncode == 0:
        launchctl("bootout", target)


def install():
    for folder in map(os.path.expanduser, PRIVATE_FOLDERS):
        if SCRIPT.lower().startswith(folder.lower() + os.sep):
            raise SystemExit("TV Cast is in %s, which macOS keeps from login services; move it elsewhere"
                             " (e.g. ~/Projects) and run tv install from there" % folder)
    try:
        now = api(PANEL_PORT, "status")["now"]
    except NotRunning:
        now = None
    if now is not None:
        raise SystemExit("%s is playing, and restarting TV Cast would make the TV drop the stream;"
                         " install once it has stopped" % video_label(now))
    bootout()
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump({
            "Label": LABEL,
            # Apple's /usr/bin/python3, because the macOS application firewall admits that signed
            # binary's incoming connections (the TV's) without a prompt; that is also why the
            # code sticks to Python 3.9 and the standard library.
            "ProgramArguments": ["/usr/bin/python3", "-I", SCRIPT, "daemon"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": LOG,
            "StandardErrorPath": LOG,
        }, f)
    launchctl("bootstrap", "gui/%d" % os.getuid(), PLIST)
    print("Installed %s: the panel is at http://127.0.0.1:%d, the log at %s" % (LABEL, PANEL_PORT, LOG))


def uninstall():
    bootout()
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("Uninstalled %s" % LABEL)


def main():
    p = argparse.ArgumentParser(prog="tv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="TV Cast " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    port = argparse.ArgumentParser(add_help=False)
    port.add_argument("--panel-port", type=int, default=PANEL_PORT, help="the daemon's panel port (default %(default)s)")

    def command(name, help, parents=(port,)):
        return sub.add_parser(name, parents=list(parents), help=help, description=help)

    command("status", "show the selected TV, what it plays, the position and the volume")
    command("ui", "open the control panel in your browser")
    c = command("tvs", "list known TVs; * marks the selected one")
    c.add_argument("--find", action="store_true", help="search the network first")
    c = command("use", "select a TV by its number in tv tvs or part of its name")
    c.add_argument("tv", metavar="NAME|NUMBER")
    command("pair", "pair the remote with the selected Samsung TV")
    c = command("open", "play the library video whose title contains QUERY")
    c.add_argument("query", nargs="+", metavar="QUERY")
    c.add_argument("--from-start", action="store_true", help="ignore the resume point")
    c.add_argument("--subs", metavar="LANGUAGE|FILE",
                   help="subtitles to show: a language, part of a track name, off, or a subtitle file")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--hls", dest="burn", action="store_const", const="hls",
                   help="on a TV that burns subtitles in: encode as it plays, even if a copy exists")
    g.add_argument("--copy", dest="burn", action="store_const", const="copy",
                   help="on a TV that burns subtitles in: play the finished copy (starts making one if needed)")
    command("play", "resume playback")
    command("pause", "pause playback")
    command("stop", "stop playback")
    c = command("seek", "jump to a position")
    c.add_argument("target", metavar="H:MM:SS")
    c = command("volume", "set the volume")
    c.add_argument("level", type=int, metavar="0-100")
    c = command("mute", "mute or unmute")
    c.add_argument("on", choices=["on", "off"])
    c = command("subs", "list the playing video's subtitles, or choose one")
    c.add_argument("choice", nargs="?", metavar="on|off|NUMBER|LANGUAGE",
                   help="on, off, a number from the list, a language, or part of a track name")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--add", metavar="FILE", help="show a subtitle file (.srt, .ass, .ssa, .vtt) from this Mac")
    g.add_argument("--remove", metavar="NUMBER|NAME", help="detach a subtitle file added with --add or the panel")
    g.add_argument("--mode", choices=["auto", "file", "burn"],
                   help="how the selected TV shows subtitle files: sent as files for its player to draw, or"
                        " burned into the picture for TVs whose players show none (auto picks per TV)")
    c = command("burn", "make a copy of a video with a subtitle file burned in, in the background, for TVs"
                        " that burn subtitles in; tv open then plays it with no encoding. Copies stay until deleted")
    c.add_argument("query", nargs="*", metavar="QUERY")
    c.add_argument("--subs", metavar="LANGUAGE|FILE", help="the subtitle file to burn in (default: the usual one)")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list the copies and the space they take")
    g.add_argument("--delete", action="store_true", help="delete the video's copy")
    c = command("key", "press remote keys on a Samsung TV, e.g. home, KEY_VOLUP")
    c.add_argument("names", nargs="+", metavar="NAME")
    c = command("daemon", "run the panel, stream server and playback tracking in the foreground")
    c.add_argument("--library", action="append", default=[], metavar="DIR",
                   help="library folder for a fresh state (repeatable; default ~/Movies)")
    # A literal default: argparse would print DEFAULT_STATE expanded to the user's home folder.
    c.add_argument("--state", default=DEFAULT_STATE, metavar="PATH",
                   help="state file (default ~/Library/Application Support/tvcast/state.json)")
    c.add_argument("--port", type=int, default=STREAM_PORT, help="stream server port (default %(default)s)")
    command("install", "run the daemon as a login service (LaunchAgent %s)" % LABEL, parents=())
    command("uninstall", "stop the login service and remove it", parents=())
    args = p.parse_args()

    if args.cmd == "daemon":
        App(args).run()
    elif args.cmd == "install":
        install()
    elif args.cmd == "uninstall":
        uninstall()
    elif args.cmd == "status":
        show_status(args.panel_port)
    elif args.cmd == "ui":
        api(args.panel_port, "status")  # exits with the usual message when the daemon isn't running
        subprocess.run(["/usr/bin/open", "http://127.0.0.1:%d" % args.panel_port], check=True)
    elif args.cmd == "tvs":
        list_tvs(args.panel_port, args.find)
    elif args.cmd == "use":
        use_tv(args.panel_port, args.tv)
    elif args.cmd == "pair":
        print("Accept the TV Cast prompt on the TV with its remote")
        api(args.panel_port, "pair", {})
        print("Paired")
    elif args.cmd == "open":
        open_video(args.panel_port, " ".join(args.query), args.from_start, args.subs, args.burn)
    elif args.cmd in ("play", "pause", "stop"):
        api(args.panel_port, args.cmd, {})
    elif args.cmd == "seek":
        seconds = seconds_of(args.target)
        if seconds is None:
            raise SystemExit("seek needs a position as H:MM:SS, e.g. 1:02:03, not %r" % args.target)
        api(args.panel_port, "seek", {"seconds": seconds})
    elif args.cmd == "volume":
        api(args.panel_port, "volume", {"level": args.level})
    elif args.cmd == "mute":
        api(args.panel_port, "mute", {"on": args.on == "on"})
    elif args.cmd == "subs":
        if args.choice and (args.add or args.remove or args.mode):
            p.error("subs takes a choice or --add/--remove/--mode, not both")
        subtitles(args.panel_port, args.choice, args.add, args.remove, args.mode)
    elif args.cmd == "burn":
        burn_video(args.panel_port, " ".join(args.query), args.subs, args.list, args.delete)
    elif args.cmd == "key":
        api(args.panel_port, "keys", {"keys": [key_name(n) for n in args.names]})
