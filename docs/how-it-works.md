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
| `tvcast/launcher.py` | Installed `tv` entry point and login-service bootstrap, using Apple's isolated Python. |
| `tvcast/cli.py` | The `tv` command: API client, plus `daemon`, `install` and `uninstall`. |
| `tvcast/app.py` | The daemon: TV selection, playback tracking, resume points, API actions. |
| `tvcast/server.py` | HTTP handlers: the stream server and the panel API. |
| `tvcast/discovery.py` | Finds TVs (SSDP) and reads their device descriptions. |
| `tvcast/upnp.py` | SOAP calls to the TV: play, pause, seek, volume, Samsung subtitles. |
| `tvcast/samsung.py` | Samsung remote: pairing, keys, typing, apps. |
| `tvcast/library.py` | Folder scanning, titles, codec and subtitle-track checks (ffprobe), native pickers. |
| `tvcast/subtitles.py` | Subtitle files: finding and labelling them, converting `.ass/.ssa/.vtt` and UTF-16 to SubRip. |
| `tvcast/state.py` | The JSON state file, including the ordered playback queue. |
| `tvcast/panel.html` | One HTML file, plain JavaScript. Status polls every 2 s (10 s in a background tab); app checks every 15 s. |

## Playing a movie

```
open → SetAVTransportURI (video URL + title + the chosen subtitle file)
     → Play            retried while the TV answers "701: not ready"
     → Seek            to the resume point
     → subtitles on    Samsung only; X_ControlCaption also switches files later
then every 2 s: ask the TV where it is; save the position every 10 s
when it ends: mark progress, then play the next queued movie if there is one
```

TVs are stored by their device ID (UDN), not their IP address, so a new address from the router
doesn't lose the TV or its pairing.

## Things that look odd but are on purpose

- **Apple's `/usr/bin/python3` only (3.9, standard library).** The TV connects *to* the Mac to
  download the video, and the macOS firewall lets Apple-signed programs accept that without a
  prompt. A background service can't answer a prompt. The installed CLI re-executes through
  this interpreter; the login service points to the bootstrap inside the installed package.
- **`-I` and the `sys.path` line in `tv`.** Isolated mode ignores the user's Python environment,
  but it also drops the script's own folder from the import path. The packaged launcher adds
  its installation directory explicitly, so Apple's Python can load it from a uv/pipx environment.
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
- **Samsung extras.** The `CaptionInfo.sec` header (the TV asks for it with
  `getcaptionInfo.sec: 1`) and `sec:` tags attach subtitles. MKV files go out to Samsungs as
  `video/x-mkv`, and to other TVs as the registered `video/x-matroska`. Subtitles need an explicit
  "enable" after loading.
- **Subtitle files on Android TVs (untested on one).** DLNA has no subtitle standard, so the
  chosen file goes out every common way, after what Universal Media Server's renderer profiles
  and Kodi's source show:
  - a `text/srt` `res`, which Kodi's and BubbleUPnP's renderers list as a track;
  - the Samsung tags and header, which Philips Android TVs read;
  - `pv:subtitleFileUri`/`pv:subtitleFileType` on the video's `res`, which UMS sends to Sony and
    others. Samsungs don't get these, so their requests stay as tested.

  These players take a file only with the video, so switching loads the video again at its
  position. A fresh `Playback` replaces the current one, so the monitor doesn't read the reload's
  brief stop as the movie ending.
- **One subtitle file per load, any file after.** Measured on a 2021 Samsung (AU7500):
  - The player registers only the first `CaptionInfoEx` it gets, so only the chosen file goes out.
  - `X_ControlCaption Enable` with any caption URL, even one never sent, fetches and draws it at
    once. That is how files switch mid-movie, with no reload.
  - The player draws tracks embedded in MKV/MP4 itself (the first one by default) and lists them
    in its own menu, but not in `X_GetCaptionState`. TV Cast can't see or switch them.
  - An enabled file replaces an embedded track. `Disable` hides a file but never an embedded
    track.
  - UTF-8 (no BOM) draws accents and Devanagari, and Windows-1252 also draws correctly. So `.srt`
    files go out as they are, and only UTF-16 and the `.ass/.ssa/.vtt` conversions are rewritten,
    keeping their text bytes. Converted copies are cached in `subtitles/` beside `state.json`.
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
