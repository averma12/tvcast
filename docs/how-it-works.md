# How TV Cast works

This is a guide for reading the code. Module docstrings and comments hold the details. User-facing
behavior and the security model are in the [README](../README.md).

## Architecture

```
 tv (terminal) ──┐
                 ├──HTTP──▶ panel + API, 127.0.0.1:8766 ──▶ tvcast daemon (LaunchAgent local.tvcast)
 browser panel ──┘                                            │  1. SSDP search + description ──▶ any DLNA TV
                                                              │  2. UPnP SOAP control ─────────▶ AVTransport, RenderingControl
                                                              │◀ 3. TV pulls video + subtitles from the stream server, :8765
                                                              │  4. Samsung only: REST :8001 ──▶ remote detection, installed apps
                                                              │     WebSocket/TLS :8002 ───────▶ keys, text, app launch
```

One process, the daemon, owns the state file, the library, the TV selection and playback
tracking. The terminal and the browser panel are both clients of its HTTP API.

| File | Purpose |
|---|---|
| `tv` | Entry script. Puts the repository root on `sys.path` and calls `cli.main()`. |
| `tvcast/cli.py` | argparse front end. HTTP client of the panel API, plus `daemon`, `install` and `uninstall`. |
| `tvcast/app.py` | `App`: TV selection, library scans, playback tracking, resume points, auto-play, and the panel API's actions. |
| `tvcast/server.py` | The stream server (video and subtitles to the TV), the panel server (page and `/api`), transfer stats. |
| `tvcast/library.py` | Folder scanning, titles and grouping, subtitle choice, the ffprobe codec check, the native folder picker. |
| `tvcast/discovery.py` | SSDP search and device-description parsing into TV records. |
| `tvcast/upnp.py` | SOAP calls (AVTransport, RenderingControl), Samsung's caption extensions, time and XML helpers, `log()`. |
| `tvcast/samsung.py` | Samsung Tizen remote: pairing, keys, text, app launch, installed-app lookup, remote detection. |
| `tvcast/state.py` | `State`: the JSON state file. |
| `tvcast/panel.html` | The web panel: one static page in vanilla JS that polls `/api/status` every second. |

```
tv ─▶ cli ─┬─▶ app ─┬─▶ discovery ─▶ upnp
           │        ├─▶ server ────▶ upnp
           │        ├─▶ samsung
           │        ├─▶ library
           │        ├─▶ state
           │        └─▶ upnp
           ├─▶ state   (DEFAULT_STATE)
           └─▶ upnp    (time helpers)
```

`library`, `samsung`, `state` and `upnp` import only the standard library.

**Threads.** `App.run` runs two `ThreadingHTTPServer`s: the stream server on every interface at
`--port`, and the panel on `127.0.0.1` at `--panel-port`. Background threads run the playback
monitor, the start-up TV search, one queue-fed ffprobe worker, and library scans; requests during
a scan coalesce into one more pass. `App.lock` guards playback and library fields and keeps the
TV selection in step with playback. `State.lock` guards the state and its writes. `Remote.lock`
allows one remote connection at a time, so key presses arrive in order.

## Discovery and selection

1. `discovery.search` sends an SSDP `M-SEARCH` for `urn:schemas-upnp-org:device:MediaRenderer:1`
   to `239.255.255.250:1900`. It sends three times over 3 seconds, because SSDP is plain UDP, and
   collects each answer's `LOCATION`.
2. `discovery.describe` fetches each description and finds the MediaRenderer, the root device or
   one in a `deviceList`. It keeps AVTransport and RenderingControl with their exact
   `serviceType`, because the version (`:1`, `:2`, `:3`) varies and SOAP must echo it. Control
   URLs resolve against `URLBase`, or the description URL. The README's security section lists
   the checks on this untrusted input.
3. A device without AVTransport isn't a TV. A device whose description fails is logged and
   skipped, so it can't hide the others. Results are deduplicated by id and sorted by name.
4. Each record is merged into `state["tvs"]`. For an unpaired TV, `remote` is set to whether
   Samsung's Tizen REST API at `:8001/api/v2/` answers with device info. A paired TV keeps
   `remote: true`, so a missed REST answer can't take its remote away.

**TV records are keyed by UDN, not by IP address.** DHCP hands TVs new addresses, but the UDN
stays the same. A new search updates a known TV's address and keeps its pairing. TVs are never
removed from the state.

**Selection.** `state["tv"]` holds the selected UDN. Its stored record is used directly, so a
selected TV works after a restart even if it was off when the daemon started. With none
selected, the start-up search selects the only TV it finds; the `discover` action only records
TVs. Selecting another TV mid-video saves progress and stops tracking without sending Stop.
Because the stream server then serves only the new TV, the old one plays on only until its
buffer runs out.

## Playback lifecycle

```
open
  │  media URL: http://<Mac's address toward the TV>:8765/media/<id><ext>   (+ /subs/<id>.srt)
  │  start caffeinate; save the previous video's progress
  ├─ SetAVTransportURI(CurrentURI, DIDL-Lite metadata)
  ├─ Play                     retried every 0.5 s for up to 30 s while the TV answers UPnP 701
  ├─ Seek REL_TIME            to the resume point minus 5 s (only if it is ≥ 30 s); same retry
  └─ Samsung with subtitles:  wait up to 15 s for X_GetCaptionState to list the file,
                              then X_ControlCaption Enable

monitor thread, every 2 s while a video is current
  GetTransportInfo + GetPositionInfo
  ├─ PLAYING or PAUSED_PLAYBACK, and TrackURI is ours → record position and duration;
  │                                                     write the resume point every 10 s
  ├─ anything else (except TRANSITIONING) after it has played, 2 polls in a row → finish
  └─ TV unreachable for more than 30 s → finish without auto-play

finish
  save progress: past 95 % counts as watched and resets the resume point; stop caffeinate
  within 30 s of the end, with auto-play on → open the next playable video in the same group

monitor thread, every 10 s while nothing is current: adopt
  the selected TV is PLAYING or PAUSED_PLAYBACK a /media/<id> URL of a library video
  → track it again (this is how playback survives a daemon restart)
```

- **Stream URL.** `lan_ip_towards(ip)` "connects" a UDP socket to the TV, which sends nothing but
  selects the local address that routes to it. It is computed per video, so it follows changes
  of the Mac's address.
- **Metadata.** The DIDL-Lite carries the title with the year, `upnp:class`
  `object.item.videoItem.movie`, and a `<res>` with
  `protocolInfo="http-get:*:<mime>:<DLNA flags>"` and the size. With subtitles it adds a
  `text/srt` `<res>` and Samsung's `sec:CaptionInfoEx` and `sec:CaptionInfo` tags.
- **Stream server.** It serves `/media/<id><ext>` and `/subs/<id>.srt` to the selected TV's IP
  and refuses everyone else with 403. It listens on every interface, so it survives changes of
  the Mac's address. A video's id is the first 12 hex digits of the SHA-1 of its absolute path.
  Any library video resolves, not only the current one, so URLs stay valid across restarts and
  rescans while the file stays put. Only subtitle fetches are logged, since the TV makes many
  media requests per video.
- **Stop.** The `stop` action sends Stop and ends tracking without auto-play.
- **Status.** Only the transport calls decide whether the TV is reachable. Volume and mute are
  null when the TV lacks RenderingControl or those calls fail. Subtitles are null on non-Samsung
  TVs, when nothing with subtitles plays, or when the caption call fails.

### TV quirks the code works around

- **UPnP error 701 "Transition not available".** A TV opens a newly set stream asynchronously
  and rejects Play and Seek until its player is ready, so those calls are retried.
- **An explicit subtitle Enable.** Samsung's player can report subtitles as enabled without
  drawing them; this was seen after a restart followed by a seek. Opening a video therefore sends
  `X_ControlCaption Enable` once the file shows up, and the panel's **On** button always re-sends
  it. Its arguments must follow the order in the TV's SCPD.
- **`CaptionInfo.sec` and the `sec:` tags** are how Samsung attaches external subtitles to a
  DLNA stream. They go to every TV, because other TVs ignore them. The caption *calls*
  (`X_GetCaptionState`, `X_ControlCaption`) are made only when the manufacturer contains
  "Samsung", because other makes don't implement them.
- **`video/x-mkv`.** Matroska is announced with the MIME type minidlna uses for Samsung TVs.
- **HTTP/1.1 keep-alive and Range.** The TV reads the MP4 index with many small Range requests
  and seeks by byte range, so keep-alive saves a connection per range. The server answers `206`
  with `Content-Range`, including suffix and unsatisfiable ranges. The TV abandons ranges
  mid-transfer whenever it seeks or has buffered enough, and the server then closes that
  connection instead of waiting on a dead socket.
- **DLNA headers.** Media responses carry `transferMode.dlna.org: Streaming` and the same
  `contentFeatures.dlna.org` flags as the metadata (`DLNA.ORG_OP=01` advertises byte seeking).
- **Never restart mid-movie.** A daemon restart breaks the TV's stream connections, and the TV's
  player gives up when its connection drops. So `tv install` refuses while something plays, and
  the panel page is read from disk on every load, which means UI changes need no restart. If a
  TV keeps playing across a restart anyway, the stable URLs keep its requests working and
  `adopt` resumes tracking.
- **A slow or off TV.** Panel status polls use a 2-second SOAP timeout instead of 8, so an
  unreachable TV doesn't stall the panel. A `STOPPED` state before the first `PLAYING` is the TV
  loading, not the end of the video.

## Panel API

All routes are on `127.0.0.1:<panel port>`. A request is refused (403) unless its `Host` header
is `127.0.0.1:<port>` or `localhost:<port>`.

- `GET /`: `panel.html`, read from disk on every request. Each load also starts a library rescan.
- `GET /api/status`: everything the panel draws: `now`, `up_next`, `libraries`, `groups`,
  `library`, `scanning`, `checking`, `autoplay`, `tvs`, `selected`, `remote {available, paired}`,
  `server` (transfer stats) and `power` (battery, from `pmset`). It also has
  `tv {state, position, duration, uri, volume, muted, subtitles}` (or null) and `tv_error`, which
  is `"No TV selected"` when none is selected.
- `POST /api/<action>`: a JSON body plus the header `X-TVCast: 1`. The reply is `{"ok": true}`
  unless the table says otherwise.

| Action | Body | Notes |
|---|---|---|
| `open` | `id`, `from_start` | Returns once the TV has accepted Play, the resume Seek and, on Samsung, the subtitle Enable. |
| `volume`, `mute` | `level` (0–100), `on` | Need RenderingControl. |
| `subtitles` | `on` | Samsung only, while a library video with subtitles plays. |
| `add_folder` | | Opens the folder picker on the Mac. Returns `{"path"}` or `{"cancelled": true}`. |
| `rescan` | | Starts a background rescan. `tv open` sends it, then polls status until `scanning` is false (for up to 30 s). |
| `discover` | | Synchronous; takes a few seconds. Returns `{"tvs": [...]}` in the shape of `status.tvs`. |
| `pair` | | Waits up to 40 s for Allow on the TV. |
| `apps` | | Returns `{"apps": [{"id", "name"}]}`. |

The other actions are:

- `play`, `pause` and `stop`, which take no body;
- `seek {seconds}`, `autoplay {on}`, `remove_folder {path}`, `select {id}`, `keys {keys: ["KEY_…"]}`,
  `text {text}` and `launch {id}`.

Errors return `{"error": …}`. A `RuntimeError` means the TV or the Mac refused or couldn't be
reached, and gives 502. A `ValueError`, `KeyError` or `TypeError` means a bad request, and gives
400. Messages name the operation and its input, and the CLI prints them as they are. With no TV
selected, TV actions fail with `No TV selected; choose one in the panel or with "tv use"`.

## Samsung remote

- **Detection** (see [discovery](#discovery-and-selection)). Other makes don't listen on `:8001`,
  so any failure there counts as no remote.
- **Channel.** The remote channel is
  `wss://<ip>:8002/api/v2/channels/samsung.remote.control?name=<base64 "TV Cast">[&token=…]`. The
  standard library has no WebSocket client, so `samsung.WebSocket` implements just enough of
  RFC 6455 on top of `ssl`: masked client frames, JSON text frames, fragmented messages, and
  answers to pings.
- **Pairing.** Connecting without a token makes the TV ask whether to allow "TV Cast". TV Cast
  waits up to 40 s for the first message. `ms.channel.connect` carries the token, which is stored
  as `remote_token` in that TV's state record together with the SHA-256 fingerprint of the TV's
  certificate as `remote_cert`. `ms.channel.unauthorized` means the TV's Device Connection
  Manager refused TV Cast.
- **Certificate pinning.** The certificate is self-signed, so no CA can vouch for it and TLS
  verification is off. Every later connection must present the pinned fingerprint instead. A
  mismatch, as after a TV reset, asks for pairing again.
- **Commands.** Each command opens a connection with the token, sends, and closes. Keys must
  match `KEY_[A-Z0-9_]+` and are paced 0.3 s apart, like real button presses. Text goes out as
  `SendInputString` (base64) into whatever on-screen keyboard the TV shows. Apps launch with
  `ed.apps.launch`.
- **Installed apps.** The TV can ignore the WebSocket request that lists installed apps. TV Cast
  therefore asks the REST API about each app ID in `KNOWN_APPS`
  (`GET :8001/api/v2/applications/<id>`, where 404 means not installed).

## macOS constraints

- **Apple's `/usr/bin/python3`.** The TV connects to the Mac to fetch the video. The application
  firewall admits incoming connections to Apple-signed binaries without a prompt, and a
  LaunchAgent has no way to answer one. The daemon therefore runs under that interpreter, which
  means Python 3.9 syntax (no `match`, no `X | Y` unions evaluated at runtime) and the standard
  library only.
- **`-I` and the `sys.path` insert.** The shebang in `tv` and the LaunchAgent both run Python in
  isolated mode. That mode ignores `PYTHON*` environment variables and the user's site-packages,
  so neither can change what the daemon imports. It also leaves the script's folder off
  `sys.path`. `tv` therefore inserts `dirname(realpath(__file__))` itself, using `realpath`
  because `tv` is usually a symlink in a PATH folder.
- **TCC (privacy protection).** A LaunchAgent gets `EPERM`, without any prompt, for `~/Desktop`,
  `~/Documents`, `~/Downloads`, external drives, and app libraries such as `~/Movies/TV`.
  `tv install` refuses a checkout in the first three. The library scan passes an `onerror` to
  `os.walk`, which otherwise skips unreadable folders silently. A blocked root becomes a problem
  shown in the panel, and a blocked subfolder a "kept private by macOS" note.
- **Local Network privacy.** The first multicast send raises macOS's "find devices on your local
  network" alert, and sends fail with `EHOSTUNREACH` until the user allows it. The search turns
  that into an error that says to allow access and search again. The start-up search can fail
  this way on a fresh install.
- **Sleep.** While a video is current, the daemon runs `caffeinate -i -w <daemon pid>`. `-i`
  prevents idle sleep, which would cut the stream. `-w` makes caffeinate exit with the daemon, so
  a crash never leaves the Mac sleepless.
- **Other details.** launchd's PATH is minimal, so the ffprobe lookup also tries
  `/opt/homebrew/bin` and `/usr/local/bin`. The folder picker is AppleScript's `choose folder`,
  run after `activate` so the dialog doesn't open behind the browser. `tv install` boots out any
  loaded copy, writes the plist (`RunAtLoad`, `KeepAlive`, output to `~/Library/Logs/tvcast.log`)
  and bootstraps it into `gui/<uid>`.

## State file

The state is at `~/Library/Application Support/tvcast/state.json`, or wherever
`tv daemon --state` points. Only the daemon writes it, after every change. It writes a
temporary file and renames it over the old one, so a crash never leaves a truncated file. The
file has mode 0600 because it holds the pairing token. If it isn't valid JSON, the daemon exits
and says to fix or delete it.

| Key | Contents |
|---|---|
| `libraries` | Absolute paths of the library folders. Seeded from `--library` (or `~/Movies`) only when the key is missing. |
| `movies` | Resume points, keyed by absolute video path: `position`, `duration` (seconds), `watched`, `updated` (Unix time). |
| `probes` | ffprobe results, keyed by absolute video path. Each keeps the `size` and `mtime` it was taken at; a change in either makes it stale. |
| `autoplay` | Whether auto-play is on. Defaults to true. |
| `tvs` | Every TV ever found, keyed by UDN: the discovery record, plus `remote` and, once paired, `remote_token` and `remote_cert`. |
| `tv` | The selected TV's UDN. Absent when none is selected. |

An example, with paths shortened to `~` (the file stores them absolute):

```json
{
 "autoplay": true,
 "libraries": ["~/Movies"],
 "movies": {"~/Movies/Some Film (2001)/some.film.2001.mkv":
            {"duration": 7260.0, "position": 2712.4, "updated": 1760000000, "watched": false}},
 "probes": {"~/Movies/Some Film (2001)/some.film.2001.mkv":
            {"info": {"audio": ["eac3"], "duration": 7260.1, "video": ["hevc"]},
             "mtime": 1750000000, "size": 4294967296}},
 "tv": "uuid:00000000-0000-0000-0000-000000000001",
 "tvs": {
  "uuid:00000000-0000-0000-0000-000000000001": {
   "id": "uuid:00000000-0000-0000-0000-000000000001",
   "name": "Living Room TV",
   "manufacturer": "Samsung Electronics",
   "model": "Example TV",
   "ip": "192.0.2.10",
   "location": "http://192.0.2.10:9197/dmr",
   "services": {
    "AVTransport": {"type": "urn:schemas-upnp-org:service:AVTransport:1",
                    "url": "http://192.0.2.10:9197/upnp/control/AVTransport1"},
    "RenderingControl": {"type": "urn:schemas-upnp-org:service:RenderingControl:1",
                         "url": "http://192.0.2.10:9197/upnp/control/RenderingControl1"}
   },
   "remote": true,
   "remote_cert": "<SHA-256 of the TV's certificate>",
   "remote_token": "<token the TV issued at pairing>"
  }
 }
}
```

## Design choices

- **Standard library only.** The firewall rule requires Apple's interpreter anyway, and nothing
  needs installing or updating. SSDP, SOAP, the WebSocket client and the Range-capable file server
  each take well under a hundred lines.
- **One daemon, thin clients.** The daemon alone tracks playback and writes the state file. Apart
  from `daemon`, `install` and `uninstall`, `tv` uses the panel's API. The terminal and the panel
  therefore can't disagree, and a command and a click take the same code path.
- **A single static panel file.** There is no build step and no framework, and edits show up
  without a restart. The panel polls `/api/status` once a second; that is cheap apart from the
  SOAP calls to the TV, which have a short timeout.
- **No compatibility shims.** The state format has no version number and no migration code. A
  format change is migrated by hand.
