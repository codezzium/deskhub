# Roadmap

Ideas for Desk Hub, roughly in order of value. Each item notes how it would work.

## Integrations

- [ ] **Phone notifications and text messages.**
  - **Approach:** read phone notifications through Windows rather than talking to the phone directly. Windows' Phone Link already mirrors texts and app notifications from Android phones and iPhones into Windows notifications. The companion reads them with the `UserNotificationListener` API (Windows asks the user for permission once), and could show other PC notifications too, such as Discord or Teams.
  - **On the board:** a Messages page plus a small Home widget with the latest few texts (sender, a preview and the time), and a brief pop-up when a new one arrives.
  - **Privacy:** off by default. Options to show the sender only, to hide previews, and to pick which apps count. Message text only goes to the paired board over the LAN and isn't stored.
  - **Limits:** replying from the board isn't possible with this approach. Phone Link has no supported way for other apps to send messages, so replies stay on the phone or in Phone Link. iPhone support in Phone Link is more limited than Android.
  - **Alternative for Android:** the open KDE Connect protocol could give the companion texts, battery level and "find my phone" directly, without Phone Link. It's more work and Android-only.

- [ ] **Media server extras.** A "Recently added" list on the Media page's server card while nothing plays. Plex controls for players that allow remote control.

- [ ] **GitHub extras.** Refresh the contribution chart soon after a `git push` from this PC instead of waiting up to 15 minutes.

## Day-to-day polish

- [ ] **Configurator UI on the PC.** A settings window instead of editing `config.toml` and answering setup questions in a terminal.
  - **Sections:** Wi-Fi and city, pages (toggles and order), a drag-and-drop Home layout with a live preview at 800 × 480, calendars, printers, shortcut tiles (icon picker, colour, action), custom games (pick from open windows), Claude/Codex options, internet, and Steam key.
  - **How:** a small local web app served by the companion on `127.0.0.1` (no new install; opens in the default browser), editing `config.toml` with tomlkit so comments survive. Secrets still go to the Windows credential store.
  - **Apply live:** saving pushes layout, Home and tile changes to the board at once; settings that need firmware (Wi-Fi, city, units) offer **Build and flash**.
  - Opened from the tray icon (next to **Edit config**) and by `uv run deskhub-config`.

## Done

- [x] Sidebar navigation, configurable pages and Home widgets, slim top bar
- [x] PC stats, media, weather, Google/Apple/ICS calendars, shortcuts
- [x] Claude Code usage with live limits, the Claude logo on Home and in the sidebar
- [x] Bambu Lab printers (cloud and local), including dual-nozzle models
- [x] One-command setup (`deskhub-setup`), public README with screenshots, MIT license
- [x] Claude Code "waiting for you" alert on the Home usage card and the AI page, with Show on PC and Not now
- [x] Codex usage on the AI page and Home, with the agent picked automatically and its own logo
- [x] Timer page: presets, countdown ring, pause and reset, a "Time's up" card, and the time left on its sidebar tab
- [x] Shortcut tiles that light up for mic and speaker mute, plus a `mic` action that mutes the microphone in Windows
- [x] Steam: a Games page and Home widget with the game being played, its cover and session time, and the last 5 games to launch with a tap
- [x] Steam extras with your own Web API key: friends in game and total playtime for every game
- [x] Internet stats: a Network page and Home widget with latency, jitter, loss, outages, data used and a Cloudflare speed test
- [x] The board sleeps with the PC: when Windows turns the monitor off, the board goes dark or shows a clock screensaver
- [x] Graphite design: flat near-black cards, an amber accent, cool data colours and the Inter font, on every page
- [x] Calendar dot on the sidebar: an event still to come today, in its calendar's colour, when Home doesn't show Up next
- [x] Readable scrolling: text that doesn't fit rests, eases slowly to its end, rests and comes back, everywhere text can overflow
- [x] Night mode: the board goes dark or shows a clock on a schedule kept on the board, and a touch wakes it for a minute
- [x] Plex and Jellyfin: the Media page splits into this PC and the server, with the server's session (poster, progress, device and user) or its library and version while nothing plays; Jellyfin sessions can be paused, skipped and seeked from the board
- [x] Start with Windows: `deskhub-install` adds a hidden login shortcut (and `--remove` takes it away), offered as setup's last step
- [x] Tray icon for the companion: connection status, Edit config, Run setup, Restart, Open log and Quit; the login shortcut starts it with the icon
- [x] Firmware updates over Wi-Fi: `deskhub-flash` (and setup) send new builds to the board, which asks Install / Deny and rolls back a build that can't start; USB as the fallback
- [x] Home as a 5 × 3 grid: widgets placed by cell with sizes they support, checked by the companion; setup places them for you
- [x] GitHub repository with CI: the firmware builds with ESP-IDF 5.5 and the companion tests run on Windows
- [x] GitHub contribution chart: a Home widget with the last year of contributions, today outlined, the total and the streak, set up with `deskhub-setup github`
- [x] GitHub page: the year's chart with labels, contributions by kind, current and longest streak, this week vs last by day, and top repositories
