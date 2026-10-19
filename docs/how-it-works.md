# How it works

For developers. Details live in the module docstrings and comments; this page is the map.

## The big picture

```
 tv (terminal) ─┐
                ├─ HTTP ─▶ daemon on 127.0.0.1:8766 (login service local.tvcast)
 web panel ─────┘            │
                             ├─ 1. SSDP search ─────────────▶ any DLNA TV on the network
                             ├─ 2. UPnP SOAP: play, seek… ──▶ the selected TV
                             ├◀ 3. the TV downloads the video from the stream server (:8765)
                             ├─ 4. WebSocket (:8002) ───────▶ Samsung only: keys, typing
                             └─ 5. REST (:8001) ───────────▶ Samsung only: app status and launch
```

One daemon owns all state. The panel and the `tv` command are both thin clients of its HTTP API,
so they never disagree.

## Code map

| File | Job |
|---|---|
| `tv` | Entry script. Adds the repo to `sys.path` and calls `cli.main()`. |
| `tvcast/cli.py` | The `tv` command: API client, plus `daemon`, `install` and `uninstall`. |
| `tvcast/app.py` | The daemon: TV selection, playback tracking, resume points, API actions. |
| `tvcast/server.py` | HTTP handlers: the stream server and the panel API. |
| `tvcast/discovery.py` | Finds TVs (SSDP) and reads their device descriptions. |
| `tvcast/upnp.py` | SOAP calls to the TV: play, pause, seek, volume, Samsung subtitles. |
| `tvcast/samsung.py` | Samsung remote: pairing, keys, typing, apps. |
| `tvcast/library.py` | Folder scanning, titles, subtitle matching, codec checks. |
| `tvcast/state.py` | The JSON state file, including the ordered playback queue. |
| `tvcast/panel.html` | One HTML file, plain JavaScript. Status polls every 2 s (10 s in a background tab); app checks every 15 s. |

## Playing a movie

```
open → SetAVTransportURI (video URL + title + subtitles)
     → Play            retried while the TV answers "701: not ready"
     → Seek            to the resume point
     → subtitles on    Samsung only
then every 2 s: ask the TV where it is; save the position every 10 s
when it ends: mark progress, then play the next queued movie if there is one
```

TVs are stored by their device ID (UDN), not their IP address, so a new address from the router
doesn't lose the TV or its pairing.

## Things that look odd but are on purpose

- **Apple's `/usr/bin/python3` only (3.9, standard library).** The TV connects *to* the Mac to
  download the video, and the macOS firewall lets Apple-signed programs accept that without a
  prompt. A background service can't answer a prompt.
- **`-I` and the `sys.path` line in `tv`.** Isolated mode ignores the user's Python environment,
  but it also drops the script's own folder from the import path.
- **Never restart mid-movie.** The TV gives up when its stream connection drops. `tv install`
  refuses while something plays, and the panel page is read from disk on every load, so UI edits
  need no restart.
- **Not in Documents, Desktop or Downloads.** macOS silently blocks login services from those
  folders, so `tv install` refuses them.
- **Retrying Play/Seek on error 701.** TVs load a new video in the background and reject commands
  until ready.
- **Queue consumption after loading.** Entries leave the queue only after playback loads, so
  failed requests can be retried. A playback lock serializes starts, stops and TV selection with
  the monitor's queue advancement; status and queue edits remain responsive during TV requests.
- **Samsung extras.** The `CaptionInfo.sec` header and `sec:` tags attach subtitles; other TVs
  ignore them. MKV files go out as `video/x-mkv`. Subtitles need an explicit "enable" after loading.
- **Remote apps by ID.** Samsung TVs ignore the "list installed apps" request, so known app IDs are
  checked with at most four requests at a time. REST launches use an empty POST, then wait up to
  10 s for `visible: true`; `running` alone can mean a background app. Unconfirmed launches show
  an error. App titles, the current HDMI input and apps outside the known list are not reported.

## Security

- The panel API listens on 127.0.0.1 only and checks the `Host` header. Commands also need an
  `X-TVCast: 1` header, so other websites can't drive your TV.
- The stream server only answers the selected TV's IP address.
- Everything from the network is untrusted. Discovery accepts only `http://` URLs on the device's
  own host, reads at most 1 MB, and rejects XML with a DTD (Apple's Python has no defence
  against XML bombs).
- Samsung pairing pins the TV's certificate. A paired TV only moves to a new address when its old
  address stops answering, exactly one device claims to be it, and that device presents the
  pinned certificate. A device forwarding to the real TV can't pass the first check, because
  forwarding needs the TV still reachable. Any TV claimed by two addresses at once is ignored.
- App status and launch use Samsung's unauthenticated REST API on the local network.
- `state.json` holds the pairing token and is readable only by you.

Not protected: anything on your network can pretend to be an *unpaired* TV, and anything holding
the selected TV's IP address can download library videos. Traffic on the LAN is unencrypted.

## Development

```sh
/usr/bin/python3 -m unittest discover -s tests -t .      # no TV or network needed
./tv daemon --port 18765 --panel-port 18766 --state /tmp/tvcast-test.json --library ~/some/videos
./tv status --panel-port 18766                            # talk to that test daemon
```

A test daemon picks the real TV automatically if it's the only one on the network, so don't play
anything from it unless you mean to. After changing Python code, run `tv install` to restart the
service.
