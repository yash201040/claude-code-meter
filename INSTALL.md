# Claude Code status line: install instructions

These are instructions for Claude Code. Give it this folder or the repo URL and say "install this status line". From a URL, clone the repo into a temporary folder first and install from there.

## What it shows

```
~/code/my-project  main* +2 ~1 ?3 ↑1  PR#12 ✓
[Opus 5.5 · 1M] ⚡high🧠  245K ctx ▓▓▓░░░░░░░ 24%  my-session  [Auto]
lines +540/-30 │ in 3.2M · out 85K · main 92% · agents 8% │ age 5h10m · ran 42m │ api $4.10 🔥$5.86/hr
5h ▲1.6× caps ~1h10m │ this session 5h 30% · 7d 8%        (only when a limit is about to be hit)
```

- Line 1: the folder, then the git branch. `*` means uncommitted changes, `+` staged files, `~` unstaged files, `?` untracked files, `↑`/`↓` commits ahead/behind the remote, `✗` conflicts. After that comes a clickable PR badge.
- Line 2: the model and its context window, effort level, 🧠 when extended thinking is on, and how much context is used (green, then yellow from 200K tokens, red from 500K). Then the session name and the permission mode.
- Line 3:
  - Lines added and removed this session.
  - Tokens in and out, and each origin's share of them (main chat, subagents, workflows).
  - Session `age` (time since it started, idle time included) and how long the model `ran`.
  - The API-equivalent cost and the burn rate per hour of model time. The burn colour is plain text under $20/hr, then green up to $60, yellow up to $140, orange up to $300 and red above that. Orange uses the 256-colour palette, which modern terminals support. To change the bands, edit the `bcls=` line in `statusline-command.sh`.
- Line 4 appears only when the current pace will hit the 5-hour or 7-day limit before it resets.

Costs are API-equivalent estimates, meaning what the usage would cost at API prices. On a subscription they're for scale, not a bill. The 5-hour and 7-day limits need a claude.ai subscription login. With an API key, line 4 stays empty.

## Install steps (for Claude)

1. Check the requirements: `bash`, `jq`, `python3` (3.8+), `git`. Works on macOS and Linux. If `jq` is missing, install it (`brew install jq` or the distro's package manager) after asking the user.
2. Copy `statusline-command.sh`, `statusline-scan.py` and `statusline-global.sh` into `~/.claude/`. If any of those files already exist there, show the user and ask before overwriting.
3. Set `statusLine` in `~/.claude/settings.json`, keeping every other key as it is. If a `statusLine` is already set, show it to the user and ask before replacing it.
   ```json
   "statusLine": { "type": "command", "command": "bash ~/.claude/statusline-command.sh" }
   ```
4. Test it with a sample payload. It should print the lines above without errors:
   ```bash
   echo '{"model":{"display_name":"Opus"},"cwd":"'"$PWD"'","session_id":"test","context_window":{"used_percentage":10,"context_window_size":200000},"cost":{"total_cost_usd":1.5,"total_duration_ms":600000,"total_api_duration_ms":120000,"total_lines_added":5,"total_lines_removed":1}}' | bash ~/.claude/statusline-command.sh
   ```
5. Tell the user the status line updates in open sessions on its next refresh.
6. Optional, macOS only: ask the user whether they want the menu-bar view (below). If yes:
   - SwiftBar must be installed (`brew install --cask swiftbar`, after asking). Its plugin folder is `defaults read com.ameba.SwiftBar PluginDirectory`. If that's empty, SwiftBar asks for a folder on first launch.
   - Copy `swiftbar/cc-usage.1m.py` into that folder as a single file. Make sure no folder gets created along the way.
   - Replace its first line with `#!<absolute path of python3> -I` (from `command -v python3`). SwiftBar runs plugins with a minimal PATH, so `/usr/bin/env python3` may not find the right interpreter.
   - `chmod +x` it and refresh SwiftBar.

## Options

- `CC_PLAN_PRICE`: monthly plan price. By default it's read from the plan in `~/.claude.json` (Pro $20, Max $100 or $200).
- `CC_BILLING_DAY`: the day the billing month starts (default 1).
- Account-wide views. Both are optional and read only the cache the status line keeps fresh, so they cost nothing extra:
  - **Menu bar (SwiftBar, macOS):** `swiftbar/cc-usage.1m.py` shows `5h 3% · 7d 3%` in the menu bar, turning amber or red when a limit is close or will be hit before it resets. Its drop-down shows the reset times and the API-equivalent spend for the 5-hour window, the 7-day window and the billing month, the month's cost as a multiple of the plan price, the prompt-cache hit rate, the model mix and the month's token totals. It reruns every minute and shows how old its data is. With no Claude Code session open, the numbers stop updating, which is fine because usage isn't changing.
  - **Terminal panel:** `bash ~/.claude/statusline-global.sh [secs]` shows the same numbers in a spare terminal tab or Herdr pane.
- All costs count only Claude Code sessions on this machine. claude.ai, the Claude apps and other devices use the same limits but don't show up in the dollar figures.
- The script also sets the terminal tab title to the session's folder. Delete the last block of `statusline-command.sh` to turn that off. In Herdr this does nothing, because Claude Code runs the status line without a terminal attached, so Herdr's sidebar keeps Claude's own session title.
- In Herdr, open the PR badge with Ctrl-click rather than Command-click, unless `mouse_capture = false` is set in `~/.config/herdr/config.toml`.

## Files and data

- Everything is local. The scripts read the JSON Claude Code passes to the status line and the session transcripts in `~/.claude/projects/`. They write only to the cache in `~/.cache/cc-statusline/`. Nothing is sent anywhere.
- `statusline-scan.py` reads the transcripts incrementally, so each render only parses what was written since the last one. A full scan across all sessions runs in the background at most once a minute.
- To uninstall, remove the `statusLine` key, the three files and `~/.cache/cc-statusline/`.
