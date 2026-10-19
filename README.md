# TV Cast

Play the videos on your Mac on any TV in your home. No app on the TV, no cloud, no converting.
Control it from a web page or the `tv` command. On Samsung TVs it also works as a remote.

![TV Cast with playback controls, a queue and removable folder tags](https://raw.githubusercontent.com/thepushkarp/tvcast/main/docs/images/demo-dark.jpg)

<details>
<summary>Light theme</summary>

![TV Cast in light mode](https://raw.githubusercontent.com/thepushkarp/tvcast/main/docs/images/demo-light.jpg)

</details>

Screenshots use a demo library and simulated TV.

## Quick start

Needs macOS with the Command Line Tools (`xcode-select --install`).

Install with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install tvcast
tv install                 # starts now and at every login
tv ui                      # open the control panel
```

`pipx install tvcast` also works. The package has no runtime dependencies.
Optional: `brew install ffmpeg` so TV Cast can flag videos the TV can't play.

<details>
<summary>Run from source</summary>

```sh
git clone https://github.com/thepushkarp/tvcast.git ~/Projects/tvcast
cd ~/Projects/tvcast
./tv install
./tv ui
```

Keep the checkout outside Documents, Desktop and Downloads; macOS restricts login services
there. To use `tv` from any folder, symlink it into a directory on your `PATH`.

</details>

In the panel:

1. Turn the TV on and pick it at the top (**Find TVs** if it isn't listed).
   If macOS asks to find devices on your local network, allow it.
2. Samsung only: **Remote → Pair with TV**, then press Allow on the TV.
3. Hit **Play** on any movie.

The panel shows the TV's volume, mute state and current supported app. Use **Apps** to switch;
**Open on TV** appears only after the TV confirms it. Search your library or choose
**Continue watching** to find unfinished movies. Add movies to **Queue** to play them in order.

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

## Updates

Stop any active cast, then run `uv tool upgrade tvcast` (or `pipx upgrade tvcast`) followed by
`tv install` to restart the login service with the installed version. `tv --version` shows the
CLI version. Settings, pairing and playback history stay in Application Support.

Before removing the package, run `tv uninstall`, then `uv tool uninstall tvcast` or
`pipx uninstall tvcast`.

## What works on which TV

| | Any DLNA TV | Samsung (2016 or newer) |
|---|---|---|
| Play, pause, seek, volume, resume | ✓ | ✓ |
| Subtitles (`.srt`) | if the TV supports them | ✓, with on/off |
| Remote buttons, apps, typing | – | ✓ |

## More

- [Your library](https://github.com/thepushkarp/tvcast/blob/main/docs/library.md): folders, subtitles, resume and queue
- [Troubleshooting](https://github.com/thepushkarp/tvcast/blob/main/docs/troubleshooting.md)
- [How it works](https://github.com/thepushkarp/tvcast/blob/main/docs/how-it-works.md): for developers
- [Releasing](https://github.com/thepushkarp/tvcast/blob/main/docs/releasing.md)

MIT licensed. Bundled icons retain their [Lucide and Feather notices](https://github.com/thepushkarp/tvcast/blob/main/LICENSE-ICONS).
