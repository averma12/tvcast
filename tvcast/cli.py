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
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "tv")
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
    print("  ".join(line))


def list_tvs(port, find):
    if find:
        found = api(port, "discover", {})["tvs"]
        print("Found %d TV%s on the network" % (len(found), "" if len(found) == 1 else "s"))
    st = api(port, "status")
    if not st["tvs"]:
        print('No TVs known yet; search the network with "tv tvs --find"')
    for i, tv in enumerate(st["tvs"], 1):
        fields = [tv["name"], tv["model"], tv["ip"], "Samsung remote" if tv["remote"] else ""]
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


def open_video(port, query, from_start):
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
    movie = pick(query, [(video_label(m), m) for m in st["library"]], "video")
    if movie["problem"]:
        print("warning: %s; trying anyway" % movie["problem"], file=sys.stderr)
    print("Opening %s" % video_label(movie))
    api(port, "open", {"id": movie["id"], "from_start": from_start})


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
    command("play", "resume playback")
    command("pause", "pause playback")
    command("stop", "stop playback")
    c = command("seek", "jump to a position")
    c.add_argument("target", metavar="H:MM:SS")
    c = command("volume", "set the volume")
    c.add_argument("level", type=int, metavar="0-100")
    c = command("mute", "mute or unmute")
    c.add_argument("on", choices=["on", "off"])
    c = command("subs", "show or hide the subtitles of the playing video")
    c.add_argument("on", choices=["on", "off"])
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
        open_video(args.panel_port, " ".join(args.query), args.from_start)
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
        api(args.panel_port, "subtitles", {"on": args.on == "on"})
    elif args.cmd == "key":
        api(args.panel_port, "keys", {"keys": [key_name(n) for n in args.names]})
