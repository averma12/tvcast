# Troubleshooting

Log: `~/Library/Logs/tvcast.log` · Settings and resume points: `~/Library/Application Support/tvcast/state.json`

| Problem | Fix |
|---|---|
| "TV Cast isn't running" | `tv install`, then check the log. |
| No TV found | Turn the TV on and make sure it's on the same Wi-Fi as the Mac (not a guest network). If you declined macOS's local-network prompt, allow it in **System Settings → Privacy & Security → Local Network**. Turn off any VPN that routes all traffic. Then **Find TVs** again. |
| Worked before, now the TV doesn't respond | The TV probably has a new address. With the TV fully on, **Find TVs** updates it. |
| "macOS blocks this service from reading it" | Move the videos out of Documents, Desktop, Downloads or external drives, e.g. into `~/Movies`. |
| Video plays without sound | The audio is DTS or TrueHD. See [Videos the TV can't play](library.md#videos-the-tv-cant-play). |
| Subtitles don't show (Samsung) | `tv subs on` again; the TV sometimes says they're on without drawing them. |
| A subtitle file isn't listed | Check its name and place against [Subtitles](library.md#subtitles), or add it with **Add file…** or `tv subs --add FILE`. |
| Subtitles inside the video don't show | Choose them in the TV's own subtitle menu. Blu-ray image subtitles (PGS) may not show at all; see [Subtitles](library.md#subtitles). |
| Remote "refused" | On the TV: **Settings → General → External Device Manager → Device Connection Manager → Device List**, allow TV Cast, then pair again. |
| App did not confirm it opened | Check the TV for a prompt, then try again. **Refresh** under Apps checks the supported installed apps; an app running in the background is not shown as open. |
| "Different certificate than when paired" | The TV was reset or updated. Pair again. |
| `tv install` says "Bootstrap failed: 5" | Run it again. |
| Video never starts | Check that the macOS firewall isn't set to block all incoming connections. |
