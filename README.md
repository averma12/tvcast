# TV Cast

TV Cast plays the videos on your Mac on a TV in the same network. A small background service
finds DLNA TVs on the network and streams videos from your library folders to the one you pick,
as they are: no transcoding, no app on the TV, no cloud service. You control it from a web panel
at <http://127.0.0.1:8766> or with the `tv` command. It remembers where you stopped each video,
and when a film in a collection ends it can start the next one. On Samsung smart TVs the panel
also works as a remote: arrow keys, inputs, apps, power and typing.

| | Any DLNA TV | Samsung Tizen TV (2016 or newer) |
|---|---|---|
| Cast library videos; play, pause, seek, stop | Yes | Yes |
| Volume and mute | Yes, if the TV offers UPnP RenderingControl (most do) | Yes |
| Resume points; auto-play the next video in a collection | Yes | Yes |
| External `.srt` subtitles | Offered to the TV; shown only if the TV supports them | Yes |
| Subtitles on/off from TV Cast | No | Yes |
| Remote keys, power off, app launching, typing | No | Yes, after pairing |

"Any DLNA TV" means any UPnP/DLNA media renderer with an AVTransport service. The remote
features use Samsung's own protocol, so they need a TV that answers Samsung's Tizen API.
[docs/how-it-works.md](docs/how-it-works.md) explains the internals.

## Requirements

- macOS.
- Apple's Python, `/usr/bin/python3` (3.9), which comes with the Xcode Command Line Tools:
  `xcode-select --install`. TV Cast uses only the standard library, so there is nothing to
  `pip install`. It must be Apple's Python because the TV connects *to* the Mac to fetch the
  video. The macOS firewall lets Apple-signed programs accept connections without a prompt, and a
  background service can't answer that prompt.
- Optional: `ffprobe` (`brew install ffmpeg`), to flag videos the TV likely can't decode. Without
  it, every video is offered as playable.
- The TV on the same local network as the Mac. Discovery uses multicast, which doesn't cross
  routers or reach guest networks.

## Install

```sh
git clone <repo-url> ~/Projects/tvcast   # anywhere except ~/Documents, ~/Desktop, ~/Downloads
cd ~/Projects/tvcast
./tv install
mkdir -p ~/.local/bin && ln -s "$PWD/tv" ~/.local/bin/tv   # or any folder on your PATH
```

macOS privacy protection (TCC) silently keeps background services out of `~/Documents`,
`~/Desktop`, `~/Downloads` and external drives, so `tv install` refuses the first three. It
writes the LaunchAgent `~/Library/LaunchAgents/local.tvcast.plist`, which runs
`/usr/bin/python3 -I <checkout>/tv daemon` now, at every login, and again if it exits. The agent
points at this checkout, so after moving the checkout, run `tv install` again.

First run:

1. Turn the TV on and open <http://127.0.0.1:8766>. If macOS asks to allow finding devices on
   your local network, allow it.
2. Choose **Find TVs** and pick the TV in the menu at the top. If it is the only TV on the
   network, the service selects it by itself when it starts.
3. Samsung only: under **Remote**, choose **Pair with TV**. Then choose Allow on the TV within
   40 seconds.

The terminal equivalents are `tv tvs --find`, `tv use 1` and `tv pair`.

## Library

The library starts as `~/Movies`. In the panel, use **+ Add folder** (a folder picker opens on
the Mac) and × on a folder to remove it. Folders are rescanned when the service starts, each time
the panel page loads, and before `tv open` searches. TV Cast picks up
`.mp4 .m4v .mov .mkv .avi .webm .ts .m2ts .mpg .mpeg .wmv` files.

```
~/Movies/
├── Some Film (2001)/                   release folder: its name gives the title and year
│   ├── Some.Film.2001.1080p.mkv
│   ├── Some.Film.2001.1080p.srt         subtitles with the video's name
│   ├── Sample/                          skipped
│   └── Featurettes/                     skipped
├── Some Saga/                           collection: a folder of release folders
│   ├── Part One (1999)/part.one.mp4
│   └── Part Two (2002)/part.two.mp4
└── Another.Film.2010.720p.mp4           loose file: its name gives the title and year
```

- **Collections.** A folder in the library root that holds release folders
  (`root/Collection/Release/video`) is listed as a collection. The panel shows collections first,
  alphabetically, then the root's other videos. Videos are sorted by year within each group.
- **Titles.** A release folder holding exactly one video gives that video its title. Otherwise the
  file name does. Dots and underscores become spaces and `[tags]` are dropped. A year in
  parentheses, or a bare 19xx/20xx year after a space, ends the title, so `1917 (2019)` is titled
  "1917".
- **Skipped.** Hidden files and folders, and anything named "sample". Also extras folders:
  Featurettes, Extras, Bonus, Bonus Features, Special Features, Behind the Scenes, Deleted Scenes,
  Trailers, Interviews, Other, Subs and Subtitles. App bundles and Photos, iMovie, Final Cut, TV
  and Music libraries are skipped too.
- **Subtitles** (`.srt` only). TV Cast uses the first of these that exists:
  1. a `.srt` with the video's name (`Film.mkv` and `Film.srt`), even beside other videos;
  2. for a video alone in its folder, the only `.srt` next to it, or else an English `.srt`
     ("eng" or "English" in its name) in a `Subs` or `Subtitles` subfolder. A plain track is
     preferred over SDH or forced, and commentary is never used.

**Resume points.** The position is saved every 10 seconds and when playback stops. Opening a
video again resumes it 5 seconds before where you stopped, provided you were at least 30 seconds
in. **Start over** in the panel, or `tv open --from-start`, ignores the resume point. Stopping
past 95% marks the video watched and clears its resume point. Resume points are stored by the
file's full path, so renaming or moving a file loses its resume point.

**Auto-play next in collection** is on by default. When a video ends, or is stopped in its last
30 seconds, the next playable video of the same group starts. That is the next film of the
collection, or, outside collections, the next video in the same library folder. **Stop** in
TV Cast never auto-plays. While a video plays, the Mac doesn't idle-sleep, but closing a laptop's
lid still sleeps it.

**Codec check.** With `ffprobe` installed, each video is checked once and re-checked only when
the file changes. The panel flags videos with no video track, or whose video or main audio codec
is outside the table in `tvcast/library.py`. That table is what Samsung Tizen TVs from 2018 on
typically decode, so other TVs may play more or less than it says. A flagged video shows **Try
anyway** instead of **Play**, and auto-play skips it.

## Panel

The panel has playback controls, the library, transfer stats with the Mac's battery state, and,
for a paired Samsung TV, a remote. The remote has arrows, Back, Home, Settings, volume, Source,
HDMI 1–3, installed apps, typing into the TV's on-screen keyboard, and power off. Turning the TV
back on needs its own remote. Keys: <kbd>Space</kbd> play/pause, <kbd>←</kbd>/<kbd>→</kbd>
10 seconds, <kbd>↑</kbd>/<kbd>↓</kbd> volume, <kbd>M</kbd> mute, <kbd>C</kbd> subtitles.

## Command line

```
tv status                         the selected TV, its state, the title, position / duration, volume
tv tvs [--find]                   known TVs, * marks the selected one; --find searches the network first
tv use NAME|NUMBER                select a TV by its number in "tv tvs" or part of its name
tv pair                           pair the remote with the selected Samsung TV
tv open QUERY... [--from-start]   play the library video whose title contains QUERY
tv play | tv pause | tv stop
tv seek H:MM:SS
tv volume 0-100
tv mute on|off
tv subs on|off                    subtitles of the playing video (Samsung)
tv key NAME...                    press remote keys (Samsung)
tv daemon [--library DIR]... [--state PATH] [--port 8765] [--panel-port 8766]
tv install | tv uninstall
```

Except for `install` and `uninstall`, every command takes `--panel-port PORT` (default 8766).
For `daemon` it is the port to serve the panel on. For the other commands it says which running
service to talk to, and that service must be running. Errors are printed, and the command exits
with status 1.

```sh
tv use living               # any case-insensitive part of the TV's name
tv open part one            # words are joined and matched against titles, ignoring case
tv open alien               # an exact title ("Alien") wins over partial matches ("Aliens")
tv open "dune (2021)"       # adding the year picks between videos with the same title
tv seek 0:45:00             # always H:MM:SS
tv key down down enter      # KEY_ and case are optional; keys are pressed 0.3 s apart
```

- **`tv open`** has the service rescan the library first, and waits up to 30 seconds for the scan,
  so newly added videos are found. The query is matched against each video's "Title (Year)", not
  against collection names. A title equal to the query, with or without its year, beats partial
  matches. If several videos match and none exactly, it lists them and exits with status 1. A
  video the codec check flagged gets a warning and plays anyway.
- **Key names** are Samsung's: `KEY_HOME`, `KEY_RETURN` (Back), `KEY_ENTER` (OK),
  `KEY_UP`/`DOWN`/`LEFT`/`RIGHT`, `KEY_VOLUP`, `KEY_VOLDOWN`, `KEY_MUTE`, `KEY_SOURCE`,
  `KEY_HDMI1`, `KEY_MENU`, `KEY_POWER` and so on.
- **`tv daemon`** runs the service in the foreground and logs to the terminal. `--library`
  (repeatable; default `~/Movies`) sets the folders of a state file that has none yet. `--state`
  defaults to `~/Library/Application Support/tvcast/state.json`. `--port` is the stream server
  the TV fetches from.
- **`tv install`** is also how to restart the service after updating the code. It refuses while a
  video plays, because a restart makes the TV drop the stream.

## Troubleshooting

The log is `~/Library/Logs/tvcast.log`. The state, including library folders, resume points,
known TVs and pairing, is in `~/Library/Application Support/tvcast/state.json`.

- **"TV Cast isn't running"**, or the panel says "No service": run `tv install`, then check the
  log for the reason it stopped.
- **"No TV selected"**: choose a TV in the panel or with `tv use`.
- **No TV found.** Check these, then choose **Find TVs** again:
  - The TV is on; a TV in standby may not answer.
  - It is on the same network as the Mac, not a guest network or another subnet.
  - Local network access is allowed, under System Settings > Privacy & Security > Local Network.
  - No VPN routes all traffic or captures multicast.

  Devices that answered but couldn't be read are logged as "skipping a device".
- **The TV stopped responding after working before.** It may have a new IP address. **Find TVs**
  updates it and keeps the selection and pairing.
- **A folder says "macOS blocks this service from reading it".** macOS keeps background services
  out of it (see [Install](#install)). Move the videos to a folder such as `~/Movies`. The
  message's other option, giving Python Full Disk Access, lifts that protection for every script
  Apple's Python runs. A "kept private by macOS" note about `~/Movies/TV`, the TV app's library,
  is expected.
- **"folder not found (unmounted drive?)":** connect the drive or remove the folder.
- **A video plays silently, or is flagged "the TV can't decode … audio".** Samsung removed DTS and
  TrueHD audio from its TVs in 2018. **Try anyway** sends the video regardless, which helps when
  the check is wrong for your TV. Otherwise, convert the audio and keep the video as it is:
  `ffmpeg -i in.mkv -map 0:v:0 -map 0:a:0 -c:v copy -c:a ac3 -b:a 640k out.mkv`.
- **Subtitles don't show.** On a Samsung TV, choose Subtitles **On** again (`tv subs on`), since
  the TV can claim subtitles are on without drawing them. Other TVs may not support external
  subtitles over DLNA.
- **The remote is "refused".** Allow TV Cast on the TV under Settings > General > External Device
  Manager > Device Connection Manager > Device List, then pair again. "Presented a different
  certificate" means the TV was reset or updated; pair again. "Didn't answer" means nobody chose
  Allow on the TV within 40 seconds.
- **`tv install` fails with "Bootstrap failed: 5: Input/output error".** launchd sometimes reports
  this right after stopping the previous copy. Run `tv install` again.
- **The TV can't fetch the video** (the panel's "Last TV request" stays "never"). Make sure the
  macOS firewall isn't set to block all incoming connections.

## Security

What TV Cast protects:

- **The panel and its API listen on 127.0.0.1 only.** Requests must carry
  `Host: 127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS-rebinding pages. Control
  requests (POST) also need the header `X-TVCast: 1`. That header forces a CORS preflight, which
  the server never answers, so other websites open in your browser can't drive the TV.
- **The stream server answers only the selected TV's IP address.** Every other address gets 403,
  including a previously selected TV.
- **The Samsung remote pins the TV's certificate.** The TV's TLS certificate is self-signed, so
  its SHA-256 fingerprint is recorded at pairing and must match on every later connection (trust
  on first use). The pairing token and the fingerprint are kept in the state file, which is
  written readable by your user only (mode 0600).
- **Discovery treats device descriptions from the network as untrusted.** It accepts only
  `http://` URLs, and control URLs only on the description's host. It reads at most 1 MiB with a
  timeout, and rejects documents with a DTD, because Apple's Python 3.9 has no guard against
  entity-expansion bombs. A device that fails a check is skipped.

What it doesn't protect against:

- **Anyone who holds the selected TV's IP address can download your library.** The stream server
  checks only the source address. A device can take that address by getting the TV's old DHCP
  lease before TV Cast searches again, or by ARP spoofing. It can then download any library video
  and its subtitles, not only the one playing, as can apps on the TV itself. This holds whenever a
  TV is selected, which is normally always. Stream URLs contain an ID derived from the file's
  path (12 hex digits of its SHA-1), which is not a secret.
- **Fake TVs.** Any device on the network can answer the search and appear as a TV. If no TV is
  selected and that device is the only one that answers when the service starts, it is selected
  automatically.
- **Eavesdropping on the LAN.** Video, subtitles and UPnP control travel unencrypted over HTTP.
- **Other programs on the Mac.** The `Host` and `X-TVCast` checks stop web pages, not local
  software. Any program on the Mac, under any user account, can use the API on 127.0.0.1.

## Uninstall

```sh
tv uninstall           # stops the service and deletes its LaunchAgent
rm ~/.local/bin/tv     # or wherever you linked it
```

The state folder `~/Library/Application Support/tvcast/` and the log `~/Library/Logs/tvcast.log`
stay behind; delete them if you don't want to keep them. To revoke the pairing on a Samsung TV,
remove "TV Cast" from its Device Connection Manager's device list.

## Development

- Run the tests from the repository root. They need no TV or network:
  `/usr/bin/python3 -m unittest discover -s tests -t .`
- Keep to Python 3.9 syntax and the standard library, and run everything with `/usr/bin/python3`.
- Run a test service next to the installed one, on other ports and with its own state file:

  ```sh
  ./tv daemon --port 18765 --panel-port 18766 --state "$(mktemp -d)/state.json" --library /path/to/test/videos
  ./tv status --panel-port 18766    # in another terminal; the panel is at http://127.0.0.1:18766
  ```

  A test service searches the network when it starts. If its state has no TV selected and exactly
  one TV answers, it selects that real TV, so don't open videos from it unless you mean to cast
  them.
- The service reads `tvcast/panel.html` on every page load, so reloading the browser shows panel
  edits. Python changes need a restart: run `tv install` for the installed service.
