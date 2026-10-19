# TV Cast

Play the videos on your Mac on any TV in your home. No app on the TV, no cloud, no converting.
Control it from a web page or the `tv` command. On Samsung TVs it also works as a remote.

## Quick start

```sh
git clone <repo-url> ~/Projects/tvcast     # not in Documents, Desktop or Downloads
cd ~/Projects/tvcast
./tv install                               # starts now and at every login
ln -s "$PWD/tv" ~/.local/bin/tv            # or any folder on your PATH
tv ui                                      # open the control panel
```

In the panel:

1. Turn the TV on and pick it at the top (**Find TVs** if it isn't listed).
   If macOS asks to find devices on your local network, allow it.
2. Samsung only: **Remote → Pair with TV**, then press Allow on the TV.
3. Hit **Play** on any movie.

Needs macOS with the Command Line Tools (`xcode-select --install`).
Optional: `brew install ffmpeg` so TV Cast can flag videos the TV can't play.

## Everyday commands

| Command | What it does |
|---|---|
| `tv ui` | open the control panel |
| `tv open dune` | play a movie, resuming where you left off |
| `tv open "dune (2021)" --from-start` | pick by year, start from the beginning |
| `tv pause` · `tv play` · `tv stop` | |
| `tv seek 1:05:00` | jump to a time |
| `tv volume 20` · `tv mute on` | |
| `tv subs on` | subtitles on/off (Samsung) |
| `tv key home` · `tv key down down enter` | press remote buttons (Samsung) |
| `tv status` | what's playing, and where |
| `tv tvs --find` · `tv use 2` | find TVs, switch to another TV |

`tv --help` lists everything. To remove TV Cast: `tv uninstall`.

## What works on which TV

| | Any DLNA TV | Samsung (2016 or newer) |
|---|---|---|
| Play, pause, seek, volume, resume | ✓ | ✓ |
| Subtitles (`.srt`) | if the TV supports them | ✓, with on/off |
| Remote buttons, apps, typing | – | ✓ |

## More

- [Your library](docs/library.md): folder layout, subtitles, resume and auto-play
- [Troubleshooting](docs/troubleshooting.md)
- [How it works](docs/how-it-works.md): for developers
