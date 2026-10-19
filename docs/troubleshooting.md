# Troubleshooting

Log: `~/Library/Logs/tvcast.log` · Settings and resume points: `~/Library/Application Support/tvcast/state.json`

| Problem | Fix |
|---|---|
| "TV Cast isn't running" | `tv install`, then check the log. |
| No TV found | Turn the TV on and make sure it's on the same Wi-Fi as the Mac (not a guest network). If you declined macOS's local-network prompt, allow it in **System Settings → Privacy & Security → Local Network**. Turn off any VPN that routes all traffic. Then **Find TVs** again. |
| Worked before, now the TV doesn't respond | The TV probably has a new address. **Find TVs** updates it. |
| "macOS blocks this service from reading it" | Move the videos out of Documents, Desktop, Downloads or external drives, e.g. into `~/Movies`. |
| Video plays without sound | The audio is DTS or TrueHD. See [Videos the TV can't play](library.md#videos-the-tv-cant-play). |
| Subtitles don't show (Samsung) | `tv subs on` again; the TV sometimes says they're on without drawing them. |
| Remote "refused" | On the TV: **Settings → General → External Device Manager → Device Connection Manager → Device List**, allow TV Cast, then pair again. |
| "Different certificate than when paired" | The TV was reset or updated. Pair again. |
| `tv install` says "Bootstrap failed: 5" | Run it again. |
| Video never starts | Check that the macOS firewall isn't set to block all incoming connections. |
