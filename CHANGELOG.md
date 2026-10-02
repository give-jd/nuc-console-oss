# Changelog

All notable changes to nuc-console, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Every configuration key named here is described in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## [Unreleased]

### Added

- **Telegram from the web view.** A Telegram page (`/?view=telegram`, linked from the settings) pairs the notifier with your own bot without a terminal
  (paste the token and your @username, tap the link it shows, press Start), switches the alerts on and off and sends a test, with the notifier's last
  message and error. The token goes one way: the web view hands it to the notifier through a folder it can write into but never read
  (`/var/lib/nuc-console-notify/inbox`, 2730; Windows `notify\inbox`, create only) and forgets it. A new pairing and *off* are told to the chat paired
  until then. New key `[telegram] web_actions` (default `yes`; `no`: the page only shows). What the page chooses is kept in the notifier's `web.json`,
  laid over `config.ini`; `--setup`, `--on`, `--off` and `--forget` write it back into `config.ini`. [docs/TELEGRAM.md](docs/TELEGRAM.md#from-the-web-view)

### Changed

- With a web view on this machine and `[telegram] web_actions = yes`, the notifier service keeps running while it is off or not paired, to take the web
  page's requests (it looks at its inbox every 2 s and writes its status every 30 s); on Linux the web view's unit starts it (`Wants=`). Run the installer
  again to get the inbox folder and the web unit's group.
- `nuc-console-telegram --status` says whether the alerts were switched on from the web page and whether the page may change them.

## [2.0.0] - 2026-10-02

A new web interface, the default from this release, built from the same model as the console; the console gets a tab bar, key figures and
`[ui]`. A major version because the web view and some keys change (see the upgrade notes and the keymap below). Still Python 3.8+,
standard library only.

### Upgrade notes

- The web view is the new shell by default (`[ui] web = app`). To keep the classic pages for this release put `web = classic` under `[ui]` in `config.ini`;
  they will be removed in the next one. The display opens the shell too.

### Added

**A new web interface (the default)**

- **The web view is now the shell.** `[ui] web` defaults to `app`: `/` serves the shell described below (the Map, CPU, Health and AI screens drawn natively,
  settings, a layout editor, partial refresh, a wall display), and the display (`render.py --kiosk`, `--open`) opens it (the full-screen window its wall
  display). The classic pages (the console's text turned into HTML) are **kept for one release as a fallback and will be removed**: `[ui] web = classic` in
  `config.ini` serves them for every page, `?app=0` for one URL (`?app=1` gives the shell back); their output is unchanged. Scripts on the default pages: see
  Security below.

- The shell (`[ui] web = app`, now the default; `?app=1` for one URL under `web = classic`) is: a top bar (host, status pill, the five screens with badges, clock, help, settings),
  a row of key figures, the overview as a grid of cards ordered by severity, and the Map, CPU, Health and AI pages in the same frame, in a dark, light,
  high-contrast or automatic theme and three densities. The classic pages stay available and unchanged (see above).
- The shell's pages have `style-src 'self'` and no `'unsafe-inline'`: no inline `<style>` or `style=` anywhere (the graph's size is a class); the classic pages are unchanged.
- `/?view=settings`: appearance (theme, density, preset, order, start view, key figures, each a link), an Export of the `[ui]` block, and a read-only
  **About this machine** (version, installed or portable, web access, display, Telegram, `[ai] web_actions`, config errors). `/?card=<id>` shows one card in full.
- The choices are kept in the `nuc_ui` cookie (`HttpOnly`, `SameSite=Strict`, validated, 256 bytes at most) by `/?set=`; `?ui=` sets them for one URL.
  The style sheet is served at `/s/app.<sha8>.css` (immutable). See [docs/WEB.md](docs/WEB.md#the-shell-the-default-web-interface).
- In the shell the CPU screen is real HTML, not a text screen: key figures, a meter per logical CPU, the temperatures and the process table, whose column
  heads are the sort links (the keys `p m t n u` work too) and whose rows select a process, with its details beside the table. The classic page and the
  console are unchanged.
- The shell has three small first-party scripts, inline and pinned by their hashes in each page's CSP (`script-src`, `connect-src 'self'`, Trusted Types):
  **partial refresh** (it polls the page's fragment, `&frag=1` with an `ETag` and `304`, and replaces only the cards that changed; the focus, the open
  details and the scroll stay; a stopped server shows *stale since HH:MM:SS*, never fresh numbers), **keys** (1-5, `Z`, `?`, arrows and `j`/`k` over lists, from the
  server's own keymap) and **preferences** (theme and density apply without a reload; a **Copy** button on the settings page). Without scripts everything
  works as before and the page reloads by `<meta refresh>` inside `<noscript>`. The classic pages are unchanged and still have no script.
  See [docs/WEB.md](docs/WEB.md#the-shells-scripts) and [SECURITY.md](SECURITY.md#the-new-web-shells-scripts).
- **Layout editor** (`/?app=1&edit=1`, from **Edit layout** in the overview's footer and in the settings): move cards earlier or later, make them
  narrower or wider (1 to 4 columns), hide them and show them again, **Reset layout**, **Done**. Without JavaScript every button is a link that makes one
  step on the server (`/?set=euat`, `/?set=ereset`, ...) and comes back to the editor; with JavaScript a fourth first-party script, pinned by its hash on
  that page only, adds dragging to move and resize with snapping to quarters of the grid, and a keyboard path (Space grabs a card, arrows move it, `+`
  and `-` resize, `x` hides, Esc puts it back, announced in a live region), and saves every change at once. Only the layout and the hidden cards of the
  `nuc_ui` cookie change (still 256 bytes at most). A hidden card keeps its width (`db3x` in the cookie, `hidden = databases:3` in `config.ini`) and comes back with it. A layout from any source (browser, link or
  `config.ini`) keeps its order instead of moving by severity, unless `order = severity` is set. The
  settings page's Export gives the `layout =` and `hidden =` lines for `config.ini`. See [docs/WEB.md](docs/WEB.md#edit-the-layout).
- In the shell the **Health screen** (`?app=1&view=health`) is drawn natively, with no preformatted text: the period as a segmented control of links
  (keys `d`, `w`, `m`), the findings as rows that open to their details and fix, the advisor's answer as a highlighted block, the top CPU and memory,
  events, logs, disks, thermal and boot sections as real tables with SVG bars and sparklines. The console draws the same model and looks exactly as before.
- The shell's overview grid packs densely (each card spans the rows its height needs: estimated by the server, measured and corrected by the page script, a later card fills the holes of an earlier one; the editor keeps strict order), and the wall hides what needs a mouse ("… the whole card", fixes, links) and keeps its scroll position across the ten-minute reload.
- The shell as a wall display: `/?app=1&ui=1.dw&kiosk=1` scrolls one screen every `[dashboard] rotate_seconds` seconds (`&rotate=N` for one URL), shifts the top bar
  every ten minutes against burn-in and keeps a footer with only the way to close the window. `render.py --kiosk` and `--open` open the shell
  (the full-screen window its wall display); `web = classic` opens the classic page as before.
- In the shell the **Map** (`?app=1&view=map`) is drawn natively too: the tree as a list whose rows are links (a symbol and a class for the state, an indent for
  the depth, a mark that opens or closes a branch), the details of the selected row beside it (below it in a narrow window), and expand all, collapse all and
  problems only as a segmented control of links with their keys (`e`, `c`, `p`), next to a link to the graph view. The console draws the same model and
  looks exactly as before.
- In the shell the **AI screen** (`?app=1&view=ai`) is drawn natively too: the switch and a download's progress bar, the chat, the hardware and the status as
  labelled values, the models as a table with the verdict as a pill and a button per row, the selected model's details, and the delete confirmations. The buttons are the
  same forms as on the classic page (same endpoints and CSRF token) and have keys (`e`, `c`, `u`, `x`, `X`, `y`, `n`); a locked page shows a notice and no form. The console
  draws the same model and looks exactly as before.

### Changed

- **The console has a tab bar, a KPI row, states on the sections and honours `[ui]`.** The first line of every console screen is now
  ` host │ [1 Overview]  2 Map  3 CPU  4 Health  5 AI │ 14:13:20 … ✖ N PROBLEMS` (the current screen in reverse video and brackets, a screen
  switched off left out, `[1·Ov] 2·Map 3·CPU 4·Hlth 5·AI` at 79 columns) instead of ` host │ <page> │ time`; from 30 rows up the second line is the KPI row (`[ui] kpis`
  or the preset's), and a section whose card is not fine says so in its title (`── ✖ EXPOSURE ──`). The body has one row less for it, so some screens show a line
  less or move a block. `[ui]` is used by the console now: `theme` (`light`, `high-contrast`; `NO_COLOR` turns the colours off), `density` (`compact`, `wall`),
  `layout` / `hidden` / `preset` / `order` for the overview's cards (without them the order is `[dashboard] sections`, as before; `order = severity` puts the worst
  state first and a card moves only when a state changes) and `start_view` (a console with a keyboard opens at that screen; an idle screen goes back to it).
- **One keymap for every console screen** (`ui.KEYMAP`: the key dispatch, every footer and the new `?` help are made from the one table; the
  README lists the keys). Breaking, compared with 1.5.0:
  - the Health periods are `d` / `w` / `m` (24 hours, 7 days, 30 days); `1` / `7` / `3` are no longer periods, the digits are the screens;
  - `1`-`5` open the Overview, Map, CPU, Health and AI screens from anywhere (a disabled feature has no digit); `Tab` / `Shift+Tab` go round
    the screens (`Tab` no longer opens the Map);
  - the page jumps `1`-`3` of `mode = rotate` are now `←` `→` (and `PgUp` `PgDn`) on the Overview, held for a minute as before; `m` `c` `h` `a`
    still open the Map, CPU, Health and AI screens, from the Overview only;
  - `Esc` closes the details pane, then goes back to the Overview; `q` does the same on a screen and, on the Overview, quits a portable console
    only; `m` / `c` / `h` / `a` no longer leave the screen they opened (they are that screen's letters: `c` collapses on the Map);
  - new: `?` (the keys of this screen), `r` (redraw now), `Z` (pause or resume the redraw; the header says *paused*);
  - the overview footer no longer says "keys 1-3: jump to page", and a monitor with no keyboard shows no keys at all.

### Security

- **The default web pages carry scripts.** Now that the shell is the default, `/` has three first-party inline scripts (partial refresh, keys,
  preferences; a fourth on the layout editor), each pinned by its SHA-256 in that page's Content-Security-Policy (`script-src` lists exactly those
  hashes, `connect-src 'self'`, Trusted Types for the fragment parser, `default-src 'none'`); the classic pages (`web = classic`, `app=0`) stay
  script-free except their MAP graph. See [SECURITY.md](SECURITY.md#the-new-web-shells-scripts).
- Every answer of the web view now carries `Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Opener-Policy: same-origin`: no
  other site can load a page as a resource, and a page opened from another site gets a window of its own.

### Fixed

- `--demo` no longer shows the real machine's memory, disk, uptime and load. Neither does it show its CPU cores, temperatures or network
  traffic: the SYSTEM block, the System page and the header's problems used to read them from the host, so a screenshot or a test showed
  whoever ran it. The demo is now an invented machine per OS (`--demo-os windows|darwin`), the same everywhere and at every run but for the clock.
- A memory, disk, uptime or load figure that cannot be read is `?` in the SYSTEM block and the System page, not an error in the block.
- The `--demo` screens described one machine each (4 cores on the overview, 16 threads on the CPU screen, 32 GB on the AI screen, a 22 s boot in BOOT and a 58 s one in HEALTH). Each demo OS is now a single machine (cores, memory, uptime, load, temperatures, boot, disks) that every screen reads.
- The web view answers `HEAD` like `GET` (same status and headers, no body) instead of `405`: link checkers and monitors that probe
  with `HEAD` work. The token and `Host` checks are the same.
- `/?token=…&view=map` (any view) kept the token in the cookie but landed on the dashboard. The redirect now keeps the view, rebuilt from
  the parameters the page understands, checked; anything else is dropped and the token is never in the new address.
- `render.py --open` (`[display] mode = browser`) ignored `[web] token_file`: the browser got `401`. It now puts the token in the address when
  the logged-in user can read the file; when not (the usual case on macOS) it shows the dashboard from a page written to a file, as the
  full-screen window does, and `display.log` says why.
- Windows, `[display] mode = fullscreen` with neither Edge nor Chrome: nothing opened. Firefox is now found (it opens a window: **F11** for
  full screen), and with no supported browser at all the default one opens (**F11** again), with a line in `display.log`.

## [1.5.0] - 2026-10-02

nuc-console now runs on Linux, macOS and Windows, has three new screens (MAP, CPU, HEALTH) and an optional local AI advisor, and
is released as archives built by CI. Still Python 3.8+, standard library only.

### Upgrade notes

- 1.4.0 and older have no updater: upgrade once by hand (extract the new release or `git pull`, then `sudo ./install.sh`; Windows:
  `install-windows.cmd`). `config.ini` and the port baseline are kept. From 1.5.0 on, `nuc-console-update` does it.
- New keys are not written into your `config.ini`. Each install refreshes `config.ini.dist` next to it:
  `diff /etc/nuc-console/config.ini{,.dist}` lists them (Windows: in `%ProgramData%\nuc-console`).
- The new screens are on by default: `[features] map`, `cpu`, `health` and `ai` default to `yes`. The advisor is off (`[ai] enabled = no`).
  What that means for the root collector, each part off with its key (a disabled feature runs none of it):
  - `map`: `ss` inside the network namespace of **every** running container (1.4.0: the database containers only) and
    `systemctl show` for what depends on a failed unit.
  - `health`: it keeps a history database, see below.
  - `cpu`, macOS and Windows only: it reads the CPU temperature sensors (every 10 s on macOS, every 30 s on Windows).
  - `ai`: the unprivileged screen reads the RAM and the GPU. Nothing is downloaded or started until *you* choose a model on the AI page or
    the AI screen (or run `nuc-console-ai setup`).
- **The AI page of the web view and the AI screen can act** (choose a model, AI on / off, delete, chat), unless `[ai] web_actions = no`
  (default `yes`): see the AI folder below, Security, and [docs/AI.md](docs/AI.md#from-the-browser-and-the-console). The Linux units change:
  `/var/lib/nuc-console/ai` is `nuc-console`'s and both units may write there (`ReadWritePaths=-`); the web unit's limits go from
  256M / 64 tasks to `MemoryMax=85%` / `TasksMax=512`, because the model server it starts is a child in its cgroup (the rest of its
  sandbox is unchanged). Re-run the installer; an AI folder that `sudo nuc-console-ai setup` filled earlier stays root's and usable
  (`sudo chown -R nuc-console:nuc-console /var/lib/nuc-console/ai` lets the page delete it too).
- New files and permissions:
  - `history.db` (SQLite, with `-wal` and `-shm` beside it while the collector runs) in `/var/lib/nuc-console` (Windows:
    `%ProgramData%\nuc-console\lib`). Written only by the collector; **mode 0644, readable by every local user, like the other state
    files**. Names and counts only. A few MB (about 9 MB for 30 days of 200 apps an hour). Uninstalling leaves it; to start over, stop
    the collector, delete it, start the collector.
  - `sensors.json` in the runtime folder (macOS and Windows only). `net.json` gains the container links of the MAP and every state
    file an `os` field.
  - Downloads are kept for the next time: the installers' Python (Windows `%ProgramData%\nuc-console\cache`, macOS
    `/Library/Caches/nuc-console`) and the updater's archive. The AI runtime and models, if you download any, are large and are kept when
    you uninstall (`nuc-console-ai remove` deletes them).
  - The AI folder (the runtime and the models, 0.4 to 19 GB) is the one the page shows: Linux `/var/lib/nuc-console/ai` (owner `nuc-console`),
    macOS `/Library/Application Support/nuc-console/ai` (`_nuc-console`), Windows `%ProgramData%\nuc-console\ai` (LOCAL SERVICE may modify it,
    nothing else of the data), a portable run `data/ai`. It holds `web.json` (the page's choices: on/off, model, endpoint; 0644) and
    `job.lock`. It stays when you uninstall.
  - A portable run writes only to the `data/` folder next to `run.sh` / `run.cmd`.
- `[web] refresh_seconds` is still read while `[dashboard] refresh_seconds` is absent; use the latter.
- `install.sh` now clears `/opt/nuc-console/*.py` before it copies every module: do not keep files of your own there.

### Added

**Windows and macOS**

- One command each: `sudo ./install.sh` on macOS (it hands over to `install-macos.sh`), `install-windows.cmd` on Windows (double-click,
  it asks for administrator rights). Re-run to upgrade; `--uninstall` / `-Uninstall` removes. Linux is unchanged.
- The collector is a LaunchDaemon (root) on macOS and a scheduled task (SYSTEM) on Windows. Per-core CPU, memory, disks, traffic and
  sessions (including Remote Desktop and Screen Sharing) are read with each system's own APIs and tools.
- The dashboard is the read-only web view, served on `127.0.0.1` only. `[display] mode = browser` (default) opens it in a normal browser
  window at install and at every login; `fullscreen` (alias `kiosk`) opens a full-screen window that Alt+F4 / Cmd+Q closes; `none` never
  opens it. `[display] zoom` (50-200 %) and `browser` complete the section. The *nuc-console* shortcut (Start menu, Applications) opens it
  again. Choose at install: `install-windows.cmd -Display ...`, `sudo NUC_CONSOLE_DISPLAY=... ./install.sh`.
- Exposure is judged by the operating system's firewall, per program: Windows Firewall (inbound rules by protocol, port, program, service
  and network profile; a block rule wins; `LocalSubnet` = LAN) and the macOS Application Firewall. What cannot be read with certainty
  (Group Policy rules, port keywords, macOS `pf` rules) is `?` and counts as open.
- Windows: sockets with their owning process (IP Helper API), services and the System event log; macOS: `lsof`, `launchctl`. Docker
  Desktop and Tailscale are supported on both; "who connects" inside containers is not (they live in a VM).
- `nuc-console-problems.cmd` and `nuc-console-accept.cmd` on Windows; `render.py --demo-os windows|darwin` previews those screens anywhere.
- CI runs the tests and a real install and uninstall on Linux, macOS and Windows.

**Screens**

- **MAP** (console `m` or `Tab`, web **map** link, `/?view=map`): who reaches what, and what is behind it. From each zone (Internet/Funnel,
  LAN, tailnet, this machine) through an open port to the process or container behind it and what that one uses
  (`LAN → :8080 → shop-web → shop-api → shop-db`); plus IMPACT (what is down and what depends on it), STACKS (compose projects) and
  OUTBOUND. Every link says how it is known: *seen* (a live connection), *declared* (compose `depends_on`, a container named in another's
  environment, a service dependency) or *same network*. Open and close branches, expand all (`e` / `c`), problems only (`p`), details
  with the evidence of every link (`Enter`). The web page is plain HTML: the state is in the URL, so a view can be bookmarked.
  - Connections are collected from inside every container's network namespace and from the host, and remembered for 24 hours.
    On macOS and Windows container-to-container links are *declared* or *same network* only.
  - **Graph view** in the browser (`/?view=map&as=graph`, switch *tree | graph*): circles and lines coloured by state, solid for seen,
    dashed for declared, dotted for same network. Click to select; the local graph of one node (`local=1` / `local=2`). With the page's
    one script: drag, zoom, pan, and positions that survive the refresh. Without it every circle is still a link.
  - `[features] map`, `render.py --once --view map` (`--expand`, `--select`, `--details`, `--only`).
- **CPU, like htop** (console `c`, web **cpu** link, `/?view=cpu`): model, cores and P/E cores, caches; per-core load (user, system,
  iowait) with frequency and temperature; package temperature and throttling; load average, context switches; processes sortable by
  CPU, memory, time, PID or user, with a details pane. Process **names only**, never command lines. What cannot be read is `?`, never 0.
  - macOS and Windows temperatures come from the collector: `powermetrics` (Intel: die temperature; Apple silicon: thermal pressure and
    cluster frequencies; `smctemp` or `osx-cpu-temp` if installed, run as their owner) and WMI (LibreHardwareMonitor,
    OpenHardwareMonitor, else the ACPI thermal zones). Best effort: without a sensor the screen says `?`.
  - `[features] cpu`, `render.py --once --view cpu` (`--sort`, `--select`, `--details`).
- **HEALTH over time** (console `h`, web **health** link, `/?view=health`): what keeps going wrong over the last day, week or month
  (`1` / `7` / `3` or `period=`). Findings with a level, the numbers behind them and a fix for each, over a small history kept by the
  collector on every OS: CPU hogs, memory that keeps growing, memory pressure, crash and hang loops, out-of-memory kills, restart loops of
  services and containers, failed services, unexpected shutdowns, hardware errors, hot hours and throttling, disks that will be full in N
  days, noisy or new log messages, failed-login spikes, slower boots. The rules are plain thresholds, no AI ([docs/HEALTH.md](docs/HEALTH.md)).
  - Sources: Linux `journalctl` and Docker's restart and OOM data; Windows System and Application event logs; macOS crash reports in
    `DiagnosticReports`. Trends need about a day of history; crashes and OOM kills show from the first one.
  - `[features] health`, `render.py --once --view health` (`--period 1|7|30`, `--select`, `--details`).
- A monitor with no keyboard: `[dashboard] map_in_rotation`, `cpu_in_rotation` and `health_in_rotation` (all `no` by default) add that
  screen to the pages the monitor rotates through.
- The web view's bottom bar has the views (map, cpu, health, ai), the text size (**A− / A+**, `?zoom=`, default `[display] zoom`) and the
  refresh rate, and stays in sight while the page scrolls. Bigger text means fewer columns, laid out again: in a browser window
  (`?fit=1`) every section and item is shown and the page scrolls; a full-screen window (`?rotate=1`) fits one screen and rotates what does
  not fit onto Details pages.
- `[dashboard] refresh_seconds` (1-10, default 2): how often every screen is redrawn, console, full-screen window and browser pages
  (**−** / **+** links on the page, `?refresh=`).
- `render.py --open` (default browser) and `--kiosk` (full-screen window) for the logged-in user.

**Optional local AI advisor** (off by default; everything on this machine; it analyses and never acts)

- **AI screen** (console `a`, web **ai** link, `/?view=ai`, `render.py --once --view ai`): choose a model by what the machine can run. It
  reads the RAM and every GPU (NVIDIA, AMD, Apple silicon with unified memory, integrated Intel) on Linux, macOS and Windows; what it
  cannot read is listed, never guessed. Each model of a short list of open models (permissive licences) gets a verdict and an estimated
  speed (tokens per second, labelled as an estimate): **fits on the GPU**, **GPU+CPU** (some layers on the GPU, the rest in RAM),
  **fits in RAM**, **slows the PC** (it fits, but leaves little room) or **too big**. One model is recommended. The screen shows the exact
  commands to install, use or remove a model. It shows with `[ai] enabled = no`,
  because that is where you choose; `[features] ai = no` removes it and the hardware is never probed.
- **From the browser and the console, with no terminal.** You only choose a model: **use this model** (console `Enter`, `u`) downloads the
  runtime and the model with a progress bar (pinned size and SHA-256, resumed, never fetched twice, disk space checked first), starts the
  model server on `127.0.0.1` and turns the advisor on. A clear **AI on / off** switch at the top of the page (console `e`) starts the
  server with the chosen model (none chosen: the recommended one, after a confirmation that names it and its size) or stops it and turns
  the advisor off; the state is always visible (off, downloading 39%, starting, loading, on, error) and a **Cancel** stops a job. A **chat**
  sits right below it, usable as soon as the server answers, with **advice now** for the last day, week or month. Deleting a model, or
  everything, is secondary and asks first. The models folder, what is downloaded and what is free on that disk are shown on the page, on
  the screen and by `nuc-console-ai models` / `status`. The page needs no script: forms and a 2 s refresh while a job runs. `render.py`
  and `web.py --demo` simulate every action, so you can try it with nothing downloaded or started.
- `[ai] web_actions` (`yes`; `no` makes the page and the screen show the command to run instead). What the page chooses lives in
  `<AI folder>/web.json` and is laid over `[ai]` for the web view, the screens and the advisor; `config.ini` is never written, and root
  (the daily digest, `sudo nuc-console-ask`) never reads `web.json`. `serve --install-service` checks the runtime's and the model's SHA-256
  again before installing a service that runs them.
- `nuc-console-ai`: `models` (hardware and a verdict per model), `setup [MODEL ...]` (downloads the runtime and the models you name, or
  the recommended one, once; size and SHA-256 checked; refuses a *too big* model unless `--force`, warns on *slows the PC*), `use MODEL`,
  `serve` (on `127.0.0.1`, GPU layers chosen from the verdict), `status`, `remove`. Windows: from an administrator prompt.
- `nuc-console-ask "question"`, `--advise`, `--status`: advice on the HEALTH findings and answers about the machine's history. The advice
  also appears on the HEALTH screen, marked "AI, check before acting", from a stored answer (a page never starts a generation).
- Any OpenAI-compatible server on this machine works instead (Ollama, llama.cpp, LM Studio, llamafile): set `[ai] endpoint` and `model`.
- New `[ai]` section: `enabled` (`no`), `endpoint` (`http://127.0.0.1:11434/v1`), `model`, `gpu` (`auto` | `no`), `allow_remote` (`no`),
  `timeout_s` (`120`), `daily` (`no`: one digest a day). New `[features] ai`. Documentation: [docs/AI.md](docs/AI.md).

**Expected vs actual exposure**

- New `[expose]` section: the widest reach you intend for a service (`local`, `tailnet`, `lan`, `internet`), by name (container,
  compose service or project, process, unit, database name or kind, `[webapps]` name) or by port (`8080`, `8080/udp`). ATTENTION raises
  `over-exposed` when the real reach is wider (unknown counts as open; a Funnel is followed to its backend; the most restrictive key wins)
  and `expose-unmatched` for a name that matches nothing. The EXPOSURE matrix marks each declared row `expected: …` or
  `beyond config.ini: …`; the MAP shows the declared reach on the port nodes. It never silences another alarm.

**Telegram alerts** (optional, off by default)

- `nuc-console-telegram --setup` pairs a Telegram bot of your own (free, made with @BotFather) with your **@username**: you tap the link it
  prints and press Start, no number to type. New and resolved ATTENTION problems then reach your phone: titles only unless
  `detail = full`, two checks in a row before a message, at most 20 an hour, a summary on the first run. `--on`, `--off`, `--test`,
  `--status`, `--preview`, `--forget`. New `[telegram]` section: `enabled`, `username`, `detail`, `resolved`. Documentation:
  [docs/TELEGRAM.md](docs/TELEGRAM.md). A portable run does not start it.

**Releases**

- Archives per system **and processor**, built by CI on a version tag and attested: `nuc-console-X.Y.Z-linux-x86_64.tar.gz`,
  `-linux-arm64.tar.gz`, `-macos-arm64.tar.gz`, `-macos-x86_64.tar.gz`, `-windows-x64.zip`, `-windows-arm64.zip`, and `SHA256SUMS`.
  **Every archive carries the Python it runs with**, so that downloading, unpacking and using it needs no Python on the machine and no
  network. Windows: the official embeddable Python (SHA-256 pinned in `install-windows.ps1`). Linux and macOS: a python-build-standalone CPython
  (`install_only_stripped`), already unpacked in `python/`, pinned by version, release, file, SHA-256 and size in `tools/python-pins.json` and
  checked by the release workflow and by the build, which refuses to build while a pin is missing. The updater picks the archive by system and
  processor, and says clearly when there is none (32-bit, RISC-V). The version is `VERSION` in `src/nuc_config.py`; the workflow refuses a tag
  that does not match. Before it publishes anything, the workflow unpacks every archive on a runner of its own system and processor (Linux
  x86-64 and ARM, macOS Apple silicon and Intel, Windows x64 and ARM64) with no Python set up, and runs it (`run.sh --which-python` /
  `run.cmd -WhichPython` must name the Python inside the archive, `--problems`, a `--once --demo` render): a broken archive is never released.
- Nothing is downloaded twice: what the installer of a clone fetches (the python.org package on macOS, the Python zip on Windows) is kept and
  checked again before use. An installer run from a release archive downloads nothing.
- **Portable run**: `run.sh [--console|--web] [--port N]` (Linux: the terminal screen; macOS: the browser) and `run.cmd` / `run.ps1`
  (Windows) run the dashboard from the extracted folder without installing it, with the Python of the archive (`python/`: `run.sh` uses it
  first, `$PYTHON` overrides it, a clone falls back to the machine's `python3`). No service, nothing outside the folder (config, state and
  baseline in `data/`, via `NUC_CONSOLE_HOME`), listens on `127.0.0.1` only. Without root or Administrator it still runs and the screen
  says what it cannot see.
- `nuc-console-update [--check] [--yes]`: asks GitHub for the latest release and does nothing unless it is newer. It downloads only what is
  not already cached with the right hash, verifies `SHA256SUMS` and, when `gh` is installed, the attestation, then runs that release's
  installer (config kept) or refreshes a portable folder (`data/` kept). Never automatic.

### Changed

- A `config.ini` that cannot be read at all (for example a key starting with `:`) raises `config-unreadable` in ATTENTION instead of
  falling back to the defaults in silence; a key written twice no longer makes the whole file unreadable (the last one wins).
- One refresh rate for every screen and page, `[dashboard] refresh_seconds`; `[web] refresh_seconds` is still read until the new key is set.
- The root collector looks inside every running container for the MAP (see the upgrade notes); the state files carry an `os` field.
- `install.sh` copies every module of `src/` and removes the ones no longer shipped.
- The Linux web unit (`nuc-console-web.service`) and the console unit may write `/var/lib/nuc-console/ai` and nothing else new; the web unit's
  `MemoryMax` and `TasksMax` are 85% and 512 (see the upgrade notes).
- `install.sh` (Linux) uses the system's `/usr/bin/python3` when it is 3.8 or newer, else the Python of the release archive, which it copies to
  `/opt/nuc-console/python` (root-owned) and points the units and the commands at; from a clone with no usable `python3` it now stops with a
  message instead of installing units that cannot start. `install-macos.sh` uses the archive's Python (copied to `/opt/nuc-console/python`)
  and downloads the python.org package only from a clone that has no Python.
- `nuc-console-update` takes the archive of the processor that runs it, unpacks the symbolic links of the bundled Python (relative, inside the
  folder) and replaces a portable folder's `python/` as a whole. Archives are 20 to 40 MB instead of a few hundred kB.

### Security

- **The web view has one script now.** Every page is still GET only, with no JavaScript and `default-src 'none'`, except the MAP's graph
  view: one inline script whose SHA-256 is in that page's Content-Security-Policy. It builds no markup, opens no connection and loads
  nothing; `tests/test_graphjs.py` rejects changes that would let it. The MAP pages are bounded (unknown keys dropped, a capped page cache).
- **The web view is no longer purely read-only: the AI page has buttons.** What they can do, and nothing else: download the pinned runtime and
  models into the AI folder, start the pinned runtime on `127.0.0.1` as a child of the web view and stop it, write `web.json`, delete those
  files, and relay a question to the local model. They need the same access as viewing (with `tailscale serve` that is your tailnet, with a
  token its holders, on macOS and Windows every local user: `[ai] web_actions = no` is the lock). Protections: a per-process CSRF token
  (constant-time compare), `Origin`/`Referer`/`Sec-Fetch-Site` same-origin checks, form-encoded bodies of 4 KB at most, a model id must be in
  the catalog (nothing from a request reaches a path, a command line or a shell), Post/Redirect/Get, `form-action 'self'` in the CSP of
  that one page and `'none'` everywhere else, still no JavaScript there. The web account can replace what is in the AI folder, which is why
  `serve --install-service` hashes it again. Details: [SECURITY.md](SECURITY.md), [docs/WEB.md](docs/WEB.md#the-ai-pages-buttons).
- macOS and Windows run the web view by default, bound to `127.0.0.1` with no token: not reachable from the network, but readable by any
  local user or program, like the state files. `[display] mode = none` with `[web] enabled = no` runs none.
- macOS and Windows collector: Apple's tools run as root only from protected system folders; third-party tools (`docker`, `tailscale`,
  `smctemp`) run as their owner, never as root; Windows searches only `%SystemRoot%` and `%ProgramFiles%` and runs fixed PowerShell scripts.
  Python is pinned by SHA-256 and signature-checked (the Windows copy is private to nuc-console); `%ProgramData%\nuc-console` is writable only
  by SYSTEM and Administrators.
- The state files now say more about your topology: `net.json` holds which container or process talks to which, with the addresses of
  clients seen and of the hosts your services connect to. Still no secrets (container environments are matched, never stored), and still
  world-readable on purpose.
- The history keeps names and counts, not text: no command lines, log messages reduced to templates (numbers, ids, addresses, paths, quoted
  strings and `key=value` values replaced), no addresses or user names of failed logins, no message text from the Windows event log, only
  the header of macOS crash reports.
- Process lists show names, never command lines (they can hold passwords).
- The AI part: the model server binds `127.0.0.1` only; the advisor refuses an endpoint that is not on this machine unless
  `[ai] allow_remote = yes`; the model sees the findings as compact data, never raw logs, and answers questions only through a fixed set of
  read-only queries with validated arguments; its text is sanitised and capped; no command is ever run. Downloads are HTTPS only and pinned
  (size, SHA-256, and a Hugging Face commit for models).
- The Telegram notifier (off until you run `--setup`) is HTTPS **out** to `api.telegram.org` only: no listener, no webhook, no commands; the
  service never reads a message (only `--setup` reads the one `/start` that carries its one-time code). The bot token is never in
  `config.ini`: it is in `/var/lib/nuc-console-notify` (0600, Linux user `nuc-console-notify`, not the web view's) or, on Windows,
  `%ProgramData%\nuc-console\notify\private` (SYSTEM, Administrators, NETWORK SERVICE). The uninstallers delete it.
- Nothing connects to the Internet by itself. Only what you run does: `nuc-console-ai setup`, `nuc-console-update`, the installer of a clone
  that has to fetch a Python, and the Telegram notifier once you have set it up.
- The Pythons in the archives (python-build-standalone for Linux and macOS, python.org's embeddable zip for Windows) are pinned by SHA-256 and
  size, checked by the release workflow and again by the build; the pins are filled by a job that reads them from the release, never typed;
  the build refuses a tarball with a member outside `python/`, a link that leaves it, or a device. Details: SECURITY.md, *The Python in the archives*.
- Releases: built from the tag on a CI runner with only the built-in `GITHUB_TOKEN`, actions pinned by commit SHA, build provenance
  attested for every archive (`gh attestation verify`). `SHA256SUMS` shows a file is whole; the attestation shows this repository's
  workflow built it. Neither is a signature by a person, and nothing in the archives is code-signed.

### Fixed

- The web view drew its pages one column narrower than asked: at the default 200 columns that is 199, below the 2-column layout, and
  NETWORK TRAFFIC, SESSIONS, TAILSCALE, DOCKER · DISK and DISKS were dropped. It also showed sessions and disks as "unavailable" on the
  first page after a start.
- A long host name no longer pushes the problem status off the header (`✖ 4 PROBLEMS` was cut to `✖ 4 PROBLE`).
- Docker installed but not running (Docker Desktop closed, the daemon stopped) is shown as such in DATABASE, not as a collector
  error on every cycle.

## [1.4.0] - 2026-10-01

### Added

- `docs/CONFIGURATION.md`: every key, default, command and environment override (a test fails when a key is undocumented).

### Changed

- `install.sh` leaves your `config.ini` untouched and refreshes `/etc/nuc-console/config.ini.dist` on every install: a diff shows the
  options added by a newer version.

## [1.3.1] - 2026-10-01

### Fixed

- The overview capped lists (ufw rules, web apps...) by detail level, not by free space: on a 65-row console it always cut them and
  rotated to a Details page. Each capped section is now expanded when the layout still fits, so Details pages hold only what does not.

## [1.3.0] - 2026-10-01

### Added

- Details pages: everything the overview cuts (ATTENTION, WEB APPS, firewall rules, containers, boot lists, Tailscale peers, disks,
  sessions, traffic, exposure). The monitor, which has no keyboard, rotates the overview (`[dashboard] overview_seconds`) and the Details
  pages (`rotate_seconds`); `details = no` turns it off. The web view has `/?full=1`: the overview plus every Details page.

### Changed

- Traffic, sessions and disks no longer cut silently: they end with "+N more".

## [1.2.0] - 2026-09-30

### Added

- WEB APPS section: the apps declared under `[webapps]` (active, or DOWN when expected but not listening) and the web listeners found on
  their own, with how far each is reachable. Declared ports no longer count as "Docker port bypassing ufw" (database ports excepted).
- Problem inventory: every ATTENTION item has a stable id, an explanation and a fix. `nuc-console-problems` lists them (`--json` for
  scripts); `sudo nuc-console-accept --problem ID --reason "..."` marks a known one (`--forget ID` undoes it). Acceptance is tied to the
  item's severity and text, so a worse situation reappears; port changes stay with the baseline.
- `[dashboard] spacing`: an optional blank line under each section title.
- Docker disk shows dangling images and anonymous volumes.

### Changed

- Sections never vanish on wide consoles; per-core CPU bars are always one per core, compressed only on tiny consoles.

## [1.1.1] - 2026-09-30

### Fixed

- A re-install of 1.1.0 dropped the time zone and the virtual terminal chosen at the first install (clock in UTC, dashboard back on
  tty1). If you are on 1.1.0, install 1.1.1 and give them again: `sudo NUC_CONSOLE_TZ=Your/Zone NUC_CONSOLE_VT=N ./install.sh`.

## [1.1.0] - 2026-09-30

### Added

- Optional read-only web view (`[web]`, [docs/WEB.md](docs/WEB.md)): off by default, GET only, no JavaScript, strict CSP. A `bind` other
  than loopback needs a token from a 0600 file or the service refuses to start; the `Host` header is checked against DNS rebinding.
- `[dashboard] sections`: a fixed, configurable section order. Columns are filled in that order without back-filling, so a line more or
  less in one block no longer moves the others.

### Changed

- Per-core CPU bars are kept in a compact form on small consoles instead of disappearing on a 65-row one.

### Fixed

- The CHANGED-port alarm cut both names at 20 characters, so a changed `tailscale serve` target looked identical on both sides; it now
  starts at the first difference.
- No truncated words in the 3-column layout (notes end with an ellipsis; some lines wrap or drop trailing items).
- `install.sh` exited silently on some re-installs (`sed` killed by SIGPIPE under `pipefail`), leaving the old code in `/opt/nuc-console`.

## [1.0.0] - 2026-09-30

Initial public release, for Linux with systemd. A live, one-screen dashboard for the monitor of a headless machine: every listening port
classified by who can reach it (this machine, LAN, Tailscale, Internet/Funnel) and corrected by the real firewall (ufw, iptables,
`DOCKER-USER`, fail2ban; it flags Docker-published ports that bypass ufw and `tailscale0` accepted before ufw); containers and databases
with who actually connects; boot health, temperatures and throttling, disks, traffic. A baseline of the exposed ports raises an alarm on any
new, changed or vanished port (`sudo nuc-console-accept`). A small root collector and an unprivileged renderer; `[features]` switches
every section; optional helper scripts `scripts/enable-ufw.sh` and `scripts/rebind-all-dbs.sh` (never run by the installer).

[1.5.0]: https://github.com/give-jd/nuc-console-oss/compare/v1.4.0...main
[1.4.0]: https://github.com/give-jd/nuc-console-oss/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/give-jd/nuc-console-oss/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/give-jd/nuc-console-oss/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/give-jd/nuc-console-oss/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/give-jd/nuc-console-oss/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/give-jd/nuc-console-oss/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/give-jd/nuc-console-oss/releases/tag/v1.0.0
