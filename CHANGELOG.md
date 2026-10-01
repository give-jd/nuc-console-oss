# Changelog

All notable changes to nuc-console, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Every configuration key named here is described in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## [1.5.0] - unreleased

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
  - `ai` is read-only: the unprivileged screen reads the RAM and the GPU, and downloads or starts nothing.
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
  commands to install, use or remove a model; the web page stays read-only and runs none of them. It shows with `[ai] enabled = no`,
  because that is where you choose; `[features] ai = no` removes it and the hardware is never probed.
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

**Releases**

- Archives per system, built by CI on a version tag and attested: `nuc-console-X.Y.Z-linux.tar.gz`, `-macos.tar.gz`,
  `-windows-x64.zip`, `-windows-arm64.zip`, and `SHA256SUMS`. The Windows archives carry the official embeddable Python (SHA-256
  pinned), so that install works offline. The version is `VERSION` in `src/nuc_config.py`; the workflow refuses a tag that does not match.
- Nothing is downloaded twice: the Windows and macOS installers keep the Python they fetched and check it again before use.
- **Portable run**: `run.sh [--console|--web] [--port N]` (Linux: the terminal screen; macOS: the browser) and `run.cmd` / `run.ps1`
  (Windows) run the dashboard from the extracted folder without installing it. No service, nothing outside the folder (config, state and
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

### Security

- **The web view has one script now.** Every page is still GET only, with no JavaScript and `default-src 'none'`, except the MAP's graph
  view: one inline script whose SHA-256 is in that page's Content-Security-Policy. It builds no markup, opens no connection and loads
  nothing; `tests/test_graphjs.py` rejects changes that would let it. The MAP pages are bounded (unknown keys dropped, a capped page cache).
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
- Nothing connects to the Internet by itself. Only what you run does: `nuc-console-ai setup`, `nuc-console-update`, and an installer that
  has to fetch a Python.
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
