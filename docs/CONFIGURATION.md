# Configuration reference

Everything is optional: a missing file, a missing key or a broken value means the default shown here, and a broken file never stops the dashboard (the problem goes to the journal).

Where things are described: **this page** is the long form (what a key does, edge cases, examples); the web view's **settings page** shows the same keys with their values and when a change applies; `config/config.ini` itself has one line per key and the values a key takes, nothing more. A test fails when a key is in one of them and not in the others.

| | |
|---|---|
| File | Linux and macOS: `/etc/nuc-console/config.ini` · Windows: `%ProgramData%\nuc-console\config.ini` (created on first install, **never overwritten**; UTF-8) · [portable run](PORTABLE.md): `data/config.ini` in the extracted folder (copied from `config/config.ini` the first time, never overwritten) |
| Reference copy | `config.ini.dist` next to it, refreshed on every install, update and portable start: `diff /etc/nuc-console/config.ini{,.dist}` shows the options added since you copied it |
| Apply | Linux: `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web` · macOS: run `sudo ./install.sh` again · Windows: run `install-windows.cmd` again (the installers restart everything and keep `config.ini`) · portable: quit (Ctrl+C) and start `run.sh` / `run.cmd` again. `nuc-console-update` keeps your `config.ini` |
| Override | `NUC_CONSOLE_CONFIG=<path>` (config file), `NUC_CONSOLE_MODE` (`overview`/`rotate`), `NUC_CONSOLE_HOME=<folder>` ([portable run](#portable-run)) |
| Settings page | the web view's ⚙ settings › **config.ini** lists every key below with its value, what it does, the values it takes and when a change applies. In a portable run and the desktop app it edits them, a section at a time, and checks each value the way the dashboard reads it (a value it would not take is refused and the file stays as it was); the locks (`[ai] web_actions`, `[ai] allow_remote`, `[telegram] web_actions`) and the keys only an installation reads stay for the file. In an installation it shows them all, and changes the presentation ones (`[features]` off, `[dashboard]`, `[ui]`, `[display] zoom`) in an overlay file that is laid over this one, never in this file ([below](#the-settings-overlay-an-installations-settings-page)) |

## Portable run

`run.sh` and `run.cmd` (see [PORTABLE.md](PORTABLE.md)) set `NUC_CONSOLE_HOME` to the `data/` folder next to them, and the collector, the screen and the web view
then keep everything there: the config as `data/config.ini`, the collector's snapshots in `data/run/`, the baseline and the accepted problems in `data/lib/`.
You do not normally set it yourself (set by hand, it also makes the web view always local, see below). `NUC_CONSOLE_CONFIG`, if set, wins over the file in that
folder; the launchers clear it, and the other `NUC_CONSOLE_*` path overrides (state, baseline, ...).

Every key applies as on an installation except the ones that decide how an installation shows itself: `[web] enabled`, `bind`, `port` and `token_file` are ignored (the
web view is on `127.0.0.1`, on the port you give with `--port` / `-Port` or a free one, behind the data folder's own token `data/web.token`: [PORTABLE.md](PORTABLE.md#the-access-token)) and so are `[display] mode` and `browser`
(the launcher opens your default browser itself; `--no-open` / `-NoOpen` prints the address instead). `[display] zoom` still sets the text size.

## Upgrading config.ini

An installation keeps the `config.ini` it started with (the installers never overwrite it), so an older one is still the long, commented file. `sudo nuc-console-config migrate` brings it to the layout of `config.ini.dist` (Windows: `nuc-console-config migrate` from an administrator prompt) and loses no setting: every key you set to a value other than the default is carried over, keys equal to the default go, your `[ui]`, `[webapps]` and `[expose]` lines stay as you wrote them, and a key or section the new layout does not know stays under a `# legacy:` line. Comments on the lines you wrote stay; the long comments of the old layout do not. The new file is written aside and read back with the dashboard's own reader: if the configuration in force would differ in any section, nothing is replaced. The old file is copied first to `config.ini.bak-YYYYMMDD-HHMMSS` in the same folder (mode 0600). `--dry-run` prints the diff and changes nothing; `--yes` does not ask; a second run says "already current" (the `# config-layout:` line of the file is what it compares). It needs the rights to edit `config.ini`, refuses a `config.ini` that is a symbolic link or cannot be read, and never runs by itself: `nuc-console-update` only prints one line when your file is in the old layout.

## `[features]` — switch sections on or off

All default to `yes`. A disabled section is not drawn, raises no alarm and, for the collector-side ones, **its commands are never run as root**.
The collector reads these switches again when `config.ini` changes (within 10 seconds, no restart); the screens of a running web view or console read them at
their start. In a portable run and the desktop app the settings page turns them on and off itself ([WEB.md](WEB.md#the-settings-pages-switches)).

| Key | Section | Runs as root |
|---|---|---|
| `containers` | CONTAINER list and health | `docker` |
| `databases` | DATABASE: ports and who connects | `docker`, `ss`, `nsenter` |
| `exposure` | EXPOSURE, port alarms, Funnel (also the "DB open on LAN" alarm) | `ss`, `tailscale serve` |
| `webapps` | WEB APPS | — |
| `firewall` | FIREWALL: ufw, iptables, `DOCKER-USER`, drops (macOS: Application Firewall and pf; Windows: Windows Firewall) | `ufw`, `iptables`, `journalctl` (macOS: `socketfilterfw`, `pfctl`; Windows: PowerShell `HNetCfg.FwPolicy2`) |
| `fail2ban` | jails and bans (drawn inside FIREWALL) | `fail2ban-client` |
| `tailscale` | TAILSCALE peers | `tailscale` |
| `boot` | BOOT: time, slowest units, failed units, journal (Windows: boot time, failed services, System event log; macOS: launch daemons) | `systemd-analyze`, `journalctl` (Windows: PowerShell; macOS: `launchctl`) |
| `docker_disk` | DOCKER · DISK | `docker system df` |
| `network_traffic`, `sessions`, `disks`, `thermal` | the respective panels (thermal: Linux only) | — (reads `/proc`, `/sys`; macOS/Windows: system calls) |
| `cpu` | the **CPU** screen: per-core load and frequency, temperatures, top processes, like htop (console key `3`, web `cpu` link) | `/proc`, `/sys` (Linux); system calls and `ps` (macOS); Windows API. Temperatures on macOS/Windows: the collector (`powermetrics`; WMI, LibreHardwareMonitor/OpenHardwareMonitor if installed) |
| `health` | the **HEALTH** screen: which apps, services and containers cause trouble over time (CPU, memory, crashes, restarts, OOM), disks filling up, hot hours (console key `4`, web `health` link) | the collector keeps `history.db` (SQLite): per-app CPU/memory per hour, events and log *templates* from `journalctl` (Linux), the Event Log (Windows), crash reports (macOS) |
| `ai` | the **AI** screen: this machine's RAM and GPU, and for each model of its list whether it fits (fits the GPU, GPU+CPU, fits RAM, slows the PC, too big) and how fast it would be (console key `5`, web `ai` link; it is not a rotating page). It reads the hardware, and it is where you set a model up (choose one: it is downloaded, started and turned on; AI on/off; delete) unless `[ai] web_actions = no`; it shows with `[ai] enabled = no` too; `no`: no screen, no key, no link, the hardware is never probed. The advisor itself is `[ai]` below | — (unprivileged: `/proc`, `/sys`, the registry, Windows API; `nvidia-smi`, `sysctl`, `vm_stat`, `system_profiler` with fixed arguments and a time limit of 5 s at most) |
| `map` | the **MAP** screen: who reaches what and what is behind it, navigable (console key `2`, web `map` link) | `docker inspect`, `ss`, `nsenter … ss` inside every running container (Linux); host sockets (macOS/Windows) |

## `[dashboard]` — layout

| Key | Default | Meaning |
|---|---|---|
| `mode` | `overview` | `overview`: one screen, no keyboard needed. `rotate`: 3 pages (System, Network & firewall, Boot), keys `←` `→` move through them (`1` is the first; `2`-`5` are the Map, CPU, Health and AI screens) |
| `sections` | by priority | Fixed on-screen order, top-left to bottom-right. Names: `attention, exposure, webapps, firewall, system, containers, databases, boot, network_traffic, sessions, tailscale, docker_disk, disks`. Names you leave out keep their default place at the end |
| `columns`, `rows` | `0` | Layout size in characters; `0` = the real console size. Never larger than the real console. Use it when elements run off the screen (e.g. `columns = 235`, `rows = 65`) |
| `spacing` | `1` | A blank line under each section title. If something would be cut, the layout is first retried without it, then also without the blank lines between sections: complete content beats spacing |
| `details` | `yes` | The overview cuts a list only when it really does not fit; those sections then get **Details** pages showing everything, rotating on the monitor (it has no keyboard). `no`: never rotate |
| `overview_seconds` | `45` | How long the overview stays before the Details pages (10-600) |
| `cpu_in_rotation` | `no` | `yes`: the CPU screen joins the pages the monitor rotates through (a monitor with no keyboard) |
| `health_in_rotation` | `no` | `yes`: the HEALTH screen joins the pages the monitor rotates through |
| `map_in_rotation` | `no` | `yes`: the MAP, expanded as far as it fits, joins the pages the monitor rotates through (a monitor with no keyboard). The interactive MAP is always one key / one click away |
| `rotate_seconds` | `15` | How long each page stays in `rotate` mode and each Details page (3-600) |
| `refresh_seconds` | `2` | Seconds between two redraws, **1–10** (smaller or larger values are clamped): the console, the full-screen window and the browser pages, where the **− / +** links next to "refresh every" change it while you look. Faster = livelier CPU and traffic bars, a little more CPU |

Sections fill the columns in the given order and never back-fill, so a line more or less in one block does not move the others. Per-core CPU bars are always one per core, except on tiny consoles (the last two fitting levels).

## `[ui]` — look and layout of the screens

> **The console uses this section.** Its theme, density, card order and visibility, KPI row and first screen follow these keys (what each one does there is in
> the table and under "On the console" below). **The web shell reads this section** as well ([WEB.md](WEB.md#the-shell-the-web-interface)).
> `web` is gone: the older classic pages it chose are removed, the shell is the only web interface, and an older file that still has `web = classic` or `web = app` loads as before
> (the key is ignored and reported once on stderr; `nuc-console-config migrate` keeps the line under a `# legacy:` marker). A wrong value is reported on stderr and only that key is skipped, like everywhere else in this file.

Every key is optional. A key left out, blank or wrong means the default, **except** that `hidden =` left blank means "nothing is hidden".

| Key | Default | Meaning |
|---|---|---|
| `theme` | `auto` | `auto` (follows the browser's light or dark setting; the console keeps its usual colours), `dark` (the same on the console), `light` (a light terminal background) or `high-contrast`. The `NO_COLOR` environment variable (set and not empty) turns every console colour off whatever this says |
| `density` | `desk` | `wall` (big text for a monitor across the room, the least detail: the console starts at the third level of detail), `desk` or `compact` (small text, the most on one screen: the console draws no empty line under the section titles, as `[dashboard] spacing = 0`) |
| `start_view` | `overview` | The screen that opens first: `overview`, `map`, `cpu`, `health` or `ai`. On the console only on one with a keyboard, and the screen where an idle one goes back to (a monitor with no keyboard always rotates) |
| `preset` | `default` | A ready-made layout, KPIs and hidden cards, see below: `default`, `security`, `server` or `desktop`. `kpis`, `layout` and `hidden` below override the preset's own |
| `order` | `severity` | `severity`: the cards that need attention move up. `fixed`: every card stays where `layout` puts it. **A `layout` from any source (this file, the browser's layout editor, a `?ui=` link) means fixed, unless you write `order = severity` yourself**, on the console and on the web alike (see below) |
| `kpis` | the preset's | The row of numbers under the title bar: up to 8 of `problems`, `internet`, `lan`, `beyond` (services past the reach you declared in `[expose]`), `db_lan`, `firewall`, `cpu`, `ram`, `disk`, `temp`, `load`, `containers`, `unhealthy`, `failed_units`, `ssh`, `tailnet`, `rx`, `tx`, `uptime`, `health`, `ai`, in the order written. One whose data is missing shows `?` |
| `layout` | the preset's | The cards that show, in order, each `name` or `name:width`: the width is 1 to 4 columns on the web (no suffix: 1; the console ignores it). Names are the sections of `[dashboard] sections`: `attention`, `exposure`, `webapps`, `firewall`, `system`, `containers`, `databases`, `boot`, `network_traffic`, `sessions`, `tailscale`, `docker_disk`, `disks`. Cards you leave out of both `layout` and `hidden` are **appended** in their default order, so a card added by an upgrade shows up at the end. A card whose `[features]` switch is off is never drawn. On the web the **Edit layout** page ([WEB.md](WEB.md#edit-the-layout)) writes this into the browser's cookie, and the settings page's Export shows it as these two keys |
| `hidden` | the preset's | Cards that are not shown, as in `layout`: `name` or `name:width`. The width (1 to 4, web only) is what the card comes back with when the web layout editor shows it again; the editor writes it for you. A card in both lists is hidden |

```ini
[ui]
theme = dark
density = wall
preset = server
kpis = problems, internet, lan, cpu, ram, disk, temp
layout = attention:2, exposure:2, webapps, firewall, system, containers:2
hidden = sessions, docker_disk
```

`kpis = none` shows no KPI row at all, on the console and on the web (the space is given to the cards), as `hidden = none` hides nothing; a blank `kpis =` sets nothing and the preset's row is used. A browser's own choice (cookie, `?ui=`) still comes first.

Values are not case sensitive; lists are separated by commas (or semicolons, or blanks, and may continue on indented lines). An unknown name, a repeat, a width
outside 1-4 or more than 8 KPIs is reported on stderr and that item is skipped (a width is held to 1-4; the first 8 KPIs are used). If nothing valid is left in
`kpis` or `layout`, the preset's own is used.

**Presets.** A preset is a layout, a KPI row and some hidden cards (`hidden` empty: all are shown):

| Preset | `layout` | `kpis` | `hidden` |
|---|---|---|---|
| `default` | attention:2, exposure:2, webapps, firewall, system, containers, databases, boot, network_traffic, sessions, tailscale, docker_disk, disks | problems, internet, lan, beyond, cpu, ram, disk, temp | — |
| `security` | attention:2, exposure:2, firewall, webapps, databases, sessions, tailscale, containers, system, boot, disks, network_traffic, docker_disk | problems, internet, lan, beyond, containers, health | — |
| `server` | attention:2, system:2, containers, databases, disks, docker_disk, boot, exposure:2, webapps, firewall, network_traffic, sessions, tailscale | problems, cpu, ram, disk, temp, containers, load, health | — |
| `desktop` | attention:2, system:2, disks, network_traffic, sessions, exposure:2, firewall, boot | problems, cpu, ram, disk, temp, ai | containers, databases, webapps, tailscale, docker_disk |

The order of the `default` layout is the one of `[dashboard] sections`, so a `config.ini` that already sets it keeps its order with no `[ui]` section at all. The
other presets bring their own order, and `[dashboard] sections` does not change them; an explicit `[ui] layout` overrides any of them.

**On the console.**

- *Header and KPI row.* The first line is the tab bar, ` host │ [1 Overview]  2 Map  3 CPU  4 Health  5 AI │ 14:13:20 … ✖ 6 PROBLEMS`: the current screen is in
  reverse video and in brackets, a screen switched off in `[features]` is left out (the digits keep their meaning) and a narrow console gets
  `[1·Ov] 2·Map 3·CPU 4·Hlth 5·AI`. Under it, from 30 rows up (or on any height when `kpis` is set), the KPI row: a symbol, a name and a value for each
  KPI, the last ones dropped when the line is too narrow. A KPI whose data is missing shows `?`, never a reassuring value. Every screen has both lines.
- *Section titles* carry the state of their card when it is not fine: `── ✖ EXPOSURE ──`, `── ! FIREWALL ──`, `── ? DATABASE ──`.
- *Order and visibility of the overview's cards.* With none of `layout`, `hidden`, `preset` and `order` set, the order is `[dashboard] sections`, exactly as
  before. With any of them, the cards are `layout` (then the ones it leaves out), without the `hidden` ones. The cards stay in that order, which is `order = fixed`;
  **`order = severity` has to be written** (on the web too, when a `layout` is set from this file, the browser or a link; there the preset's own layout, or `hidden` and `preset` alone, still move by severity): the cards with the worst state come first (✖ and down, then !, then ?, then the fine ones),
  `attention` stays on top when the layout puts it first, and the cards of one state keep their order. The order depends only on the cards' states, so a card moves
  when its own state changes (or another's does), never because a number changed.
- *Theme.* `light` and `high-contrast` change the colours, never the symbols or the text; `NO_COLOR` leaves bold and reverse video only.

**Who wins.** For each key, the first of these that sets it: a `?ui=` value in one URL (a bookmark, a kiosk link), the `nuc_ui` cookie of that browser (set by the
settings page of the new web interface, which also shows where every value comes from), this section, the preset, the built-in default. The console has no
URL and no cookie: it reads this section, then the preset, then the default. The server never writes
the browser's choices anywhere. The settings page has an **Export** button that gives you a `[ui]` block like the one above, to paste here.

## `[console]` — font and screen blanking of the Linux console (both off by default)

Linux only (macOS and Windows ignore it). `src/ttyprep.py` applies it as root each time `nuc-console.service` starts (`ExecStartPre=-+`), on the terminal the dashboard draws on (`NUC_CONSOLE_VT`). Apply a change with `sudo systemctl restart nuc-console`. A missing `setfont` or `setterm`, an unknown font or an error never stops the dashboard: the step is skipped and the reason is in `journalctl -u nuc-console`.

| Key | Default | Meaning |
|---|---|---|
| `font` | empty | A font for `setfont`, e.g. `Lat15-TerminusBold32x16` (Terminus 16x32: readable on a full-HD monitor; Debian/Ubuntu: `sudo apt install console-terminus`). Empty: the font stays as it is. Letters, digits and `_ . + -` only |
| `blank_minutes` | `0` | Minutes without a key before the screen goes black and the monitor sleeps (`setterm --blank` and `--powerdown`), 0–60; `0` = never: after a number, the next start of the service takes the timer back and wakes a black screen. The header already moves every 10 minutes against burn-in; this is what lets the monitor rest |
| `wake_on_problem` | `yes` | With `blank_minutes` above 0: a problem that was not on the previous frame (not accepted, not muted), or one whose severity rose, wakes the blanked screen (`TIOCL_UNBLANKSCREEN` on the console the dashboard owns, no root needed), at most once per 30 s; the first frame after a start never wakes it. `no`: only a key does. Linux console only; read when the dashboard starts, so apply a change with `sudo systemctl restart nuc-console`. The Telegram notifier is separate |

Why not the kernel parameter `consoleblank=` (and `dpkg-reconfigure console-setup`)? Both need a bootloader or system-wide change and a reboot, apply to every console and not only the dashboard's, and one wrong edit of the kernel command line can stop the machine booting: the installer never does it. The cost of this way: it is set when the service starts (a reboot or restart re-applies it) and only on the dashboard's terminal. If you want the kernel way anyway, add `consoleblank=600` to `GRUB_CMDLINE_LINUX_DEFAULT` yourself. **Check once** that the monitor really sleeps with the dashboard redrawing every couple of seconds: output to the console can wake a blanked screen on some kernels; if it does, use the monitor's own sleep timer.

## `[display]` — the dashboard on macOS and Windows

macOS and Windows have no text console to take over. The installers start the web view (read-only; only the AI page has buttons) on **127.0.0.1 only**
(not reachable from the network) and show it the way you choose. Linux ignores this section (it uses the console).

| Key | Default | Meaning |
|---|---|---|
| `mode` | `browser` | How the dashboard opens, at install and at every login. `browser`: a normal window of your default browser. `fullscreen` (or `kiosk`): full screen as a wall display (big type, one screen, lists cut to three rows, scrolling every `[dashboard] rotate_seconds`). `none`: never by itself (a machine without a monitor). The **nuc-console** shortcut (Windows Start menu, macOS Applications) opens it again any time. The installers apply it: run them again after a change, or choose with `install-windows.cmd -Display fullscreen` / `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh` (that writes this key; a plain re-install keeps it) |
| `zoom` | `100` | Text size in percent, 50–200. Bigger text = fewer columns, re-laid out (no sideways scrolling). In a browser window **nothing is left out**: every section and every item, the page scrolls; full screen is the wall display: lists are cut to three rows with a "+N more". The **A− / A+** links at the bottom of the page change it while you look |
| `browser` | `auto` | The browser of the full-screen window. `auto`: Microsoft Edge, then Google Chrome, then Firefox (Windows: Firefox cannot start full screen, press F11; with none of them the default browser opens a normal window, F11 again); Chrome, Edge, Brave, Chromium, else Safari (macOS: press Ctrl+Cmd+F once). Or the full path of a Chromium-based browser |

The full-screen window opens the shell's wall display (`/?ui=1.dw&kiosk=1`: wall density, scrolling one screen every `[dashboard] rotate_seconds` seconds, [WEB.md](WEB.md#wall-and-kiosk)) and a normal window opens `/`.

The full-screen window is a plain browser window with a profile of its own (never your tabs or logins), not a locked kiosk:
**Alt+F4** (Cmd+Q) closes it until the next login, **F11** (Ctrl+Cmd+F) leaves full screen, Alt+Tab reaches the other windows.
Its grid follows the monitor's shape (64 rows; 16:9 → the 3-column layout) or `[dashboard] columns` × `rows` when set.
The page has nothing to click but A− / A+, the refresh − / + and the views: it refreshes and rotates by itself.

## `[webapps]` — the web apps you expect

```ini
[webapps]
ethibid = 8180, 8543
admin-console = 9443
```

`name = port[, port…]`. Listed apps appear in **WEB APPS** as active (with how far they are reachable: local, tailnet, LAN, Internet) or as **DOWN (expected)** when nothing listens. The ports are an *intended exposure*: they stop counting as "Docker port bypassing ufw" (database ports are never masked). Other web listeners found on the machine are listed as "not declared".

## `[expose]` — how far each service may reach

```ini
[expose]
shop-db = local      # a container, compose service or project, process, systemd unit, database name or kind, or a [webapps] name
n8n     = tailnet
8080    = lan        # or a port: 8080, 8080/udp
```

`name or port = local | tailnet | lan | internet` (any case; `tailscale` = `tailnet`, `localhost` and `loopback` = `local`, `public` = `internet`): the **widest** reach you intend. When the real reach, as the EXPOSURE section computes it, goes beyond that, ATTENTION raises **over-exposed** (an error that lists each service, its port and `LAN > local`), the matrix and the compact overview show a red `beyond config.ini: local` on that row (a grey `expected: LAN` when it is within), and the map port carries a "declared reach" fact and an error. A name matches a container (`shop-db` also matches `shop-db-1`, its compose service and its compose project), a process or systemd unit, a database name or kind (`postgres`), or a `[webapps]` name; for a Funnel or Serve entry it is whatever listens on the backend, so `n8n = tailnet` catches an n8n that is published on the Internet. A port key (`8080`, or `8080/udp`; tcp otherwise) matches what listens on it. When several keys match one service the most restrictive wins; an unknown firewall verdict counts as open. A name that matches nothing this machine knows (a typo) raises **expose-unmatched**, a warning; ports are never checked.

`[expose]` only adds alarms: `db-open-lan`, `docker-bypass` and the port baseline are never silenced by it. `over-exposed` can be accepted like any ATTENTION item (`nuc-console-accept --problem over-exposed --reason "…"`); a new service going beyond makes it reappear. A bad value or a port that cannot exist is reported on stderr and only that line is skipped. Never write a port as `:8080`: configparser refuses a key that starts with `:` and the **whole file** falls back to the defaults. Containers with `network_mode: host` listen as plain host processes, so declare them by process, unit or port; a broken `config.ini` (such a key, or any file that cannot be read) now raises the **config-unreadable** error in ATTENTION instead of silently dropping `[expose]`.

## `[backups]` — backups that must stay fresh (none by default)

```ini
[backups]
nightly-db = /srv/backups/db-*.sql.gz, 2
photos     = /srv/backups/photos, 7
```

`name = /absolute/path-or-glob[, max_age_days]`. In `config.ini` the section ships commented out (remove the `#` before `[backups]` and the lines you want); with no line there is no check and no alarm. The path is **absolute** (`~` is not expanded: the collector runs as root, so there is no home to expand it to) and has no `..`; a glob (`*`, `?`, `[…]`) is expanded by the collector. The collector looks at the **newest** match, file or folder, and keeps only its name, modification time and size: it never reads a file. A match is looked at as itself (a symbolic link is not followed), and a match reached through a link that leads out of the folder the pattern starts in is ignored. A folder's date is the folder's own: it changes when something is added to it, so point at the files inside (`/srv/backups/photos/*`) when you want the newest file's date.

- **backup-stale** (warning): the newest match is older than `max_age_days` (1-3650, default 2).
- **backup-missing** (error): nothing matches (the backup never ran, the disk is not mounted, or the path is wrong).

Neither can be muted (a mute would turn a guard you configured into a silent failure; remove the line to stop the check); both can be accepted with a reason. Only the administrator edits the section: the settings page does not (a path is read as root). The collector reads it at start: `sudo systemctl restart nuc-console-collector` after a change. A line the reader refuses (relative path, `~`, `..`, a bad age) is said on stderr (`journalctl -u nuc-console-collector`) and skipped.

### Host hygiene: reboot, updates, clock, inodes

Four more alarms come from the collector's boot data (every 5 minutes) and need `[features] boot = yes` (inodes: `disks = yes` too); there is nothing to configure:

| Alarm | Level | When | Mute |
|---|---|---|---|
| `reboot-required` | warning | `/run/reboot-required` exists, or the running kernel (`uname -r`) is older than the newest `/boot/vmlinuz-*` of the same flavour (`7.0.0-34-generic` against `7.0.0-38-generic`); the kernel test needs both to be readable | yes |
| `updates-pending` | warning | `apt-get -s dist-upgrade` (a simulation: no network, nothing changes; run at most once an hour) lists upgrades from a `-security` suite. Only the counts are kept, never package names. Debian and Ubuntu; elsewhere the check is simply absent | no |
| `clock-unsynced` | warning | `timedatectl show` says `NTPSynchronized=no`. Without `timedatectl` (no systemd, a container) nothing is said | no |
| `disk-inodes` | warning at 90 % of a filesystem's inodes used, error at 97 % | the filesystems of the DISKS card; those that report no fixed count (btrfs, xfs: `f_files` 0) are left out. Linux and macOS | yes |

**Windows `reboot-required`**: the SYSTEM collector reads three places of the registry (`winreg`, no PowerShell, key names and whether a value is empty only): `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired` (key exists), `...\Component Based Servicing\RebootPending` (key exists) and `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager` value `PendingFileRenameOperations` (not empty). The alarm names which signals said yes (Windows Update, servicing, files to replace). A signal that cannot be read counts as unknown, never as "no restart needed". The last signal is also left by some installers and security software, so it can stay on until the next restart. Mutable like on Linux.

`updates-pending` and `clock-unsynced` touch security (unpatched software, certificates and tokens that depend on the time) and cannot be muted. These checks are Linux only, except `disk-inodes` (macOS too) and `reboot-required`, which Windows has in its own form (below); Windows has no `updates-pending`, `clock-unsynced` or `disk-inodes`, and macOS has no `reboot-required`, `updates-pending` or `clock-unsynced` (the tools that would answer, `softwareupdate`, `sntp`, `systemsetup`, are not run).

## `[web]` — web view: read-only, except the AI page's buttons (off by default)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The only network listener of the dashboard (the optional AI model server listens on 127.0.0.1 only, [AI.md](AI.md)); every page is read-only except the buttons of the AI page, which `[ai] web_actions = no` locks (macOS/Windows: with `enabled = no` the installers still run it on 127.0.0.1 for `[display]`; a portable run always runs it on 127.0.0.1 and ignores this section's `enabled`, `bind`, `port` and `token_file`). Details and threat model: [WEB.md](WEB.md) |
| `bind` | `127.0.0.1` | Anything else **requires** `token_file` (the service refuses to start otherwise) |
| `port` | `8787` | |
| `token_file` | empty | File with a secret (16+ chars of `A-Za-z0-9._~-`), mode 0600, owned by root or `nuc-console` (Windows: keep it in `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write). Never put the token in `config.ini` (world-readable) |
| `allowed_hosts` | empty | Extra `Host` names accepted when no token is set (DNS-rebinding guard); `localhost`, `127.0.0.1`, the bind address, the hostname and `*.ts.net` always are |
| `metrics` | `yes` | `/metrics`: the figures of `/api/v1/overview` as Prometheus text, under the same token, `Host` and bind rules as `/api/v1/*` (it adds no data); `no` makes the route a 404. Applies at the next start. [WEB.md](WEB.md#metrics) |
| `settings_actions` | `no` | Opt-in, and a lock. `yes`: the settings page of an installation may change the presentation keys, in `settings.ini` (below), **only when `token_file` is set** (without a token any local user could use the page); `no` (the default, and what a value that is not yes/no counts as): it only shows, and what it chose before counts for nothing. A portable run does not use it |
| `refresh_seconds` | — | Older place of `[dashboard] refresh_seconds`: still read (1–10) for the web pages while `[dashboard]` has none. Use `[dashboard]` |

### The settings overlay: an installation's settings page

`config.ini` is root's, so the web view never writes it. What its settings page changes is kept in **`settings.ini`** (an INI file with the same sections and keys), in the AI folder:
Linux `/var/lib/nuc-console/ai/settings.ini`, macOS `/Library/Application Support/nuc-console/ai/settings.ini`, Windows `%ProgramData%\nuc-console\ai\settings.ini`
(`$NUC_CONSOLE_SETTINGS` names another file). The renderer, the web view and the collector read `config.ini` and then lay it over:

| Allowed in the overlay | Rule |
|---|---|
| `[features]` | **off only.** `no` switches a section off; `yes` is ignored: a section that `config.ini` switches off stays off and only the administrator switches it on, so the overlay can make root's collector run less, never more |
| `[dashboard]`, `[ui]`, `[display] zoom` | any value the dashboard takes; it wins over `config.ini` |

Everything else in the file is ignored and logged once: `[web]`, `[telegram]`, `[ai]`, `[expose]`, `[webapps]`, `[backups]`, `[console]`, `[display] mode` and `browser`, and every lock. The page marks each
overlaid value *set from this page* and has **Reset to config.ini** (a value equal to `config.ini`'s is not kept). A file that is a link, is over 16 KB, is writable by group or others,
or belongs to someone other than root or the folder's owner counts for nothing. To undo everything as the administrator: `sudo rm /var/lib/nuc-console/ai/settings.ini` (the collector
notices within 10 seconds); `[web] settings_actions = no` (the default) stops the page from using it at all. Switching `exposure`, `firewall`, `fail2ban`, `databases` or `tailscale` off through it raises the ATTENTION problem `feature-hidden`, which cannot be accepted.
## `[alerts]` — alarms you may mute

```ini
[alerts]
mute = journal-errors, thermal
```

| Key | Default | Meaning |
|---|---|---|
| `mute` | empty | ATTENTION alarms to silence, by id (`nuc-console-problems` prints the ids). A **closed** list: `journal-errors`, `thermal`, `throttling`, `container-exited`, `unhealthy-container`, `collector-boot`, `reboot-required`, `disk-inodes`, `container-no-restart`, `containers-not-back`, `display-stuck`, `oom-killed`, `memory-pressure`, `nic-resets`, `ai-down`. A muted alarm leaves the ATTENTION list, the **Problems** figure and the Telegram messages, and ATTENTION says `· N muted` (the web folds the list under it); `nuc-console-problems` lists them under a `muted` heading. The collectors keep producing the data: only what is shown is muted. Applies at once on the console and the web (the notifier reads it within 30 seconds) |

Security and integrity alarms can never be muted: `updates-pending`, `clock-unsynced`, `backup-stale`, `backup-missing`; ports and the baseline (`port-new`, `port-changed`, `port-gone`, `baseline-*`, `port-compare-suspended`), the firewall (`ufw-*`, `firewall-*`), exposure (`db-open-lan`, `docker-bypass`, `funnel-public`, `over-exposed`, `expose-unmatched`), the data those judgements read (`collector-net`, `collector-containers`, `stale-*`, `net-sections`, `config-unreadable`) the alerting itself (`telegram-*`) and the posture and availability checks (`service-down`, `ssh-*`, `cert-*`, `watch-config`). A name that is not on the list above, a security id included, is ignored, said on stderr (`journalctl -u nuc-console`) and raised in ATTENTION as `mute-ignored`; the rest of the line still applies. A new alarm is not mutable until it is added to `nuc_config.MUTABLE_ALERTS` on purpose.

**Muting is not accepting.** *Accept* (`sudo nuc-console-accept --problem <id> --reason "…"`) is a root command with a written reason, kept in `accepted.json`, tied to the alarm's severity and text, so a worse situation shows up again; the alarm stays in the list, dimmed. *Mute* is a line of this file for the alarm ids above: no reason, no root, nothing in `accepted.json`, and it hides the alarm whatever it says until you remove the line.

## `[containers]` — containers that stay down after a reboot

```ini
[containers]
restart_ignore = job-runner, *-test-*
```

| Key | Default | Meaning |
|---|---|---|
| `restart_ignore` | empty | Containers the `container-no-restart` alarm skips: names or shell-style globs (`*-test-*`), comma-separated, matched without regard to case. For one-shot jobs and test stacks that are meant to stay down after a reboot. Applies at once |

Two operational alarms (both can be muted with `[alerts] mute`) come from the boot collector's look at Docker, every 5 minutes, and need `[features] boot` and `containers`:

- `container-no-restart` (warning): a **running** container whose restart policy is `no` (or empty) will not come back after a reboot or a Docker restart. One alarm, with up to three names and the count. Fix: `docker update --restart unless-stopped <name>` (compose: `restart: unless-stopped`), or list it in `restart_ignore`.
- `containers-not-back` (error): in the first 20 minutes after a boot, containers that were running with a restart policy (`always`, `unless-stopped`, `on-failure`) before the reboot and are not running now. The collector keeps their **names only** (no image, environment or command line) in `running.json` in its own state folder (`/var/lib/nuc-console` on Linux), rewritten at every look; at a new boot that list becomes the "before" list for the whole boot. A container stopped by hand earlier was not in the list and is not flagged (one stopped in the 5 minutes before the reboot still is: that is the look's resolution). It clears when they run again, and in any case 20 minutes after the boot. If Docker is not installed, or the file cannot be written, there is no alarm and no error.

## `[thresholds]` — where a reading becomes a problem

```ini
[thresholds]
memory_pressure_pct = 10
commit_pressure_pct = 90
```

| Key | Default | Meaning |
|---|---|---|
| `memory_pressure_pct` | `10` | 1-100. The kernel's memory pressure (PSI, `some avg60` of `/proc/pressure/memory`, percent of the last minute in which some process waited for memory) above this raises `memory-pressure` (warning). Read with the temperatures, so `[features] thermal = no` switches it off too. Without PSI (Linux before 4.20, macOS, Windows) there is no problem and no error |
| `commit_pressure_pct` | `90` | 1-100. **Windows only.** The commit charge (memory promised to processes) at or above this percent of the commit limit (RAM plus page files; `GetPerformanceInfo`) raises `memory-pressure` (warning) with Windows wording. It is a different measure from the Linux key above, so the two are separate. Read with the temperatures (`[features] thermal = no` switches it off); where it cannot be read there is no problem and no error. Ignored on Linux and macOS |

**Kernel-log alarms (Linux)** have fixed windows, no keys: the root collector reads this boot's kernel log (`journalctl -k -b`, at most 5000 candidate lines, 20 s) and keeps only a count and the time of the newest line per class, never a line. `display-stuck` (error) clears when no graphics-driver timeout is newer than 30 minutes; `disk-io-errors` and `memory-errors` (errors; hardware, so they can be neither muted nor accepted) and `nic-resets` (warning, from 3 resets or link-downs) look at the last 24 hours; `oom-killed` (warning) lasts for the boot and names the killed process, not its command line. Where a kernel log is not read (macOS, Windows) these sections are "not available on this OS", not errors. On Windows the matching events go to the history and the Health page instead (no alarm): the display driver reset (`Display` 4101), disk and file-system errors (`disk` 7, 11, 51, 153; `Ntfs` 55, 140) as hardware events, an unexpected shutdown (`EventLog` 6008) and a blue screen (`BugCheck` 1001) as unexpected shutdowns. Only the source name and the number are read, never the message.

## `[services]` — units that must be running (Linux)

```ini
[services]
must_run = docker, ssh, tailscaled, fail2ban
```

| Key | Default | Meaning |
|---|---|---|
| `must_run` | empty (off) | Systemd unit names, comma-separated, up to 20 (letters, digits and `@ . _ : -`, not starting with `-`; `docker` means `docker.service`). The root collector asks `systemctl is-active -- <unit>` for each, every 5 minutes (with the BOOT section: `[features] boot = no` stops it). One that is not running is the ATTENTION error `service-down`, naming the unit and its state (`inactive`, `failed`, `not-found`, `unknown`). The same 30 s budget applies. A name that is not a valid unit name is skipped and said (`watch-config`); so is `must_run` set while `systemctl` is missing or the check failed. Not on Windows or macOS: there is no such check there and the key does nothing |

Why it exists: `failed-units` only sees units that *failed*. A service that was stopped by hand, never started or removed (fail2ban or sshd after an upgrade) is not failed, so it is invisible there. Like `failed-units`, `service-down` cannot be muted (it is the sign that a protection is missing); accept it with a reason if the unit is down on purpose.

## `[tls]` — certificates about to expire

```ini
[tls]
watch = nas = 192.168.1.20:443, router = 10.0.0.1:8443, internal = /etc/ssl/certs/internal.pem
warn_days = 21
```

| Key | Default | Meaning |
|---|---|---|
| `watch` | empty (nothing watched) | Comma-separated `name = host:port` or `name = /absolute/path/cert.pem` (on Windows also `C:\path`), up to 10; the collector stops starting new checks after 30 s of a pass and the rest is `watch-config` ("not checked this pass"), never fine. `host` must be an **IP address** (or `localhost`) on loopback, a private network (10/8, 172.16/12, 192.168/16, fc00::/7), Tailscale (100.64/10) or link-local; a name or a public address is refused and said (`watch-config`): nuc-console makes no other outbound connection than Telegram's. The collector connects with a 3 s limit, **without verifying the certificate** (the point is the end date of whatever is served, self-signed LAN certificates included), and keeps only the entry's name and the end date. A file is the first certificate of a PEM file of at most 64 KB. A name is letters, digits and `. _ -`; a path cannot contain a comma. Works on Linux, macOS and Windows |
| `warn_days` | `21` | 1-365. A certificate with fewer days left is the ATTENTION warning `cert-expiring`; one that has ended is the error `cert-expired`; one that could not be read is `cert-unreadable` (expiry unknown, never shown as fine) |

Both lists are read from `config.ini` only: the settings overlay of an installation cannot set them (root runs what they say).

## `sshd` audit (no key)

On Linux the root collector runs `sshd -T` (every 5 minutes, with the BOOT section) and keeps six effective settings (the five plus `KbdInteractiveAuthentication`), values only; `sshd -T` shows the global values, a `Match` block can override them. Nothing to configure; a machine without `sshd`, or where `sshd -T` fails, simply has no result (never a pass). `ssh-root-login` (warning: `PermitRootLogin yes`), `ssh-password-auth` (warning: `PasswordAuthentication yes`, or `KbdInteractiveAuthentication yes`, the PAM password route; an **error** when the exposure data says port 22 or the `sshd` process is reachable from the Internet, or beyond what you declared under `[expose]`) and `ssh-empty-passwords` (error: `PermitEmptyPasswords yes`). Many LANs choose passwords: accept the alarm with a reason (`sudo nuc-console-accept --problem ssh-password-auth --reason "..."`); it is tied to its severity, so it comes back if port 22 becomes reachable from further away. They cannot be muted.

## `[telegram]` — alerts on your phone (off by default)

```ini
[telegram]
enabled = no
username = your_telegram_name
detail = titles
resolved = yes
web_actions = yes
```

New and resolved ATTENTION problems, sent to **one** Telegram user by a bot you create yourself (free). It only sends: no listener, no webhook, it never reads messages, no commands. Set it up on the web view's **Telegram page** (settings › Telegram page) or with `sudo nuc-console-telegram --setup`. Set-up in three steps, what leaves the machine and the threat model: [TELEGRAM.md](TELEGRAM.md).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | Switch it on or off with `sudo nuc-console-telegram --on` / `--off` (they edit this line and start or stop the service). Nothing is sent until the chat is paired (`--setup`) |
| `username` | empty | Your Telegram `@username` (5–32 letters, digits, `_`; the `@` is optional): the only person who gets the messages. `sudo nuc-console-telegram --setup` asks for it and writes it here |
| `detail` | `titles` | `titles`: only the title of each problem and the host name leave the machine ("Container unhealthy"). `full`: the text too, with container names and ports; it is then stored by Telegram |
| `resolved` | `yes` | Also send a message when a problem goes away |
| `web_actions` | `yes` | The web view's Telegram page may pair, switch on and off and send a test; `no`: it only shows (a post is refused with `403`). What the page chooses is kept in the notifier's `web.json`, never here: it turns on what this file leaves off, and the paired `@username` replaces `username`; `enabled = yes` here cannot be switched off from the page. The command line (`--setup`, `--on`, `--off`, `--forget`) writes the page's choice back here and deletes `web.json` ([TELEGRAM.md](TELEGRAM.md#from-the-web-view)) |

The bot **token** is never in `config.ini` (it is world-readable): it lives in the notifier's own folder (`/var/lib/nuc-console-notify`, Windows `%ProgramData%\nuc-console\notify\private`), readable only by the notifier's own account (Linux `nuc-console-notify`, macOS `_nuc-console`, Windows NETWORK SERVICE), never by the web view's.

## `[hosts]` — other nuc-console machines to watch (none by default)

One line per peer: `name = URL [token=PATH] [pin=sha256:HEX] [scope=summary|views|full] [alert=yes] [insecure=yes]`. The separator is the **space**
(`config.ini` reads `;` as the start of a comment). It is read by the fleet client, `peers.py`, which polls each peer with one `GET` and keeps what it saw in a
cache (the poller as a service comes with the next step; today `nuc-console-fleet once` does one round). Nothing here changes what the host itself shows.

```ini
[hosts]
nuc-b = https://nuc-b.example.ts.net
nas   = http://100.64.1.7:8787 scope=views alert=yes
lab   = https://192.168.1.20:8443 pin=sha256:3f1c...e9 token=/etc/nuc-console/peers/lab.token
```

| Part | Meaning |
|---|---|
| name | `a-z`, `0-9`, `-`; 1 to 32 characters, starting with a letter or digit (`fleet` is taken). The name of the cache file and of the token file |
| URL | `http://` or `https://`, a DNS name or an IP address in its plain spelling (`127.1`, `2130706433`, `0x7f.1` are refused), an optional port 1-65535 and an optional path prefix. No user name, no query, no fragment, no spaces or quotes |
| `token=PATH` | the file with the token the peer gave this host (`[web] peer_tokens_file` on the peer). Absolute path. Default `/etc/nuc-console/peers/<name>.token` (Windows `%ProgramData%\nuc-console\peers`, a portable run `<data>/peers`). 0600, owned by the account that polls (or root), not a link, in a folder others cannot write to |
| `pin=sha256:HEX` | only on an `https://` URL (on `http://` the row is refused). For `https://` with a certificate no CA signed: the SHA-256 of the peer's certificate (the DER of the leaf), 64 hex digits. It is checked **instead of** the chain and the name; a different certificate makes the peer `untrusted`, there is no fallback to the CAs |
| `scope=` | `summary` (default): the status line only. `views`: also the overview, cpu, health, map and events documents, for the screens that show a peer in full (read-only, opaque, size-capped). `full`: the same, for a 2.5.x peer that has no scope tokens yet and is read with its ordinary viewer token (marked with a warning) |
| `alert=yes` | this peer is meant to raise an alarm when it goes silent (used by the alarm step; no effect yet) |
| `insecure=yes` | allow plain `http://` to a private (RFC 1918 / ULA) address that is not a Tailscale one. The cache marks the peer `insecure` so that the dashboard can say the token travels in the clear. Never allows plain http to a public address |

Rules that are not options: at most **16** peers; a row that is wrong is ignored and a line on standard error says which (the token's path never is printed with its content);
`http://` is only for Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) unless `insecure=yes`; a public address is `https://` only;
link-local (169.254.0.0/16 with the cloud metadata address, `fe80::/10`), multicast, unspecified and reserved addresses are never contacted, and loopback is not either.
`[hosts]` is **not** in the settings overlay and is never written by the settings page, not even in a portable run: edit `config.ini`.
The poll interval is 15 s (never under 5 s); a peer that fails is retried after 5 s, 10 s, 20 s ... up to 5 minutes. See [SECURITY.md](../SECURITY.md#the-peer-client) for what the client does and refuses.

## `[ai]` — optional local model for the HEALTH screen (off by default)

What it is, how to choose a model for your hardware, the commands and the security rules: **[AI.md](AI.md)**.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | Use a local model to turn the HEALTH findings into advice and to answer questions. It only reads and suggests: it never runs anything. The AI screen (`[features] ai`) shows with `no` as well: it is where you choose. `nuc-console-ai setup` never switches it on for you. **`yes` always wins**: the AI page and screen cannot switch it off (they say "on by config.ini"); with `no` they can turn it on, and what they chose is kept in `web.json` in the AI folder ([AI.md](AI.md#from-the-browser-and-the-console)) |
| `endpoint` | `http://127.0.0.1:11434/v1` | Any OpenAI-compatible server: an Ollama you run yourself (this default), llama.cpp server, LM Studio... (`nuc-console-ai setup` installs nuc-console's own Ollama, and offers to write `http://127.0.0.1:8080/v1`, or the `--port` you gave; the AI page's server writes its own endpoint in `web.json`) |
| `model` | empty | The model name the server knows. What `nuc-console-ai setup` (the first model named) or `use` writes is a catalog id, e.g. `qwen3-8b`, which is the name its server answers to; for Ollama e.g. `qwen3:8b`. Also the model `serve` starts when none is named. Empty: the server's first model is used |
| `gpu` | `auto` | `auto`: the server that `nuc-console-ai serve` and the AI page start uses the GPU when Ollama finds one that holds the model (all of it, or some layers: it measures the free video memory itself); `no`: the GPUs are hidden from it, it runs on the CPU (on a Mac Metal cannot be hidden: no effect there). Only for the server nuc-console starts: a server you run yourself decides for itself. An installed service keeps what it was installed with: after changing `gpu` run `sudo nuc-console-ai serve --install-service` again |
| `allow_remote` | `no` | An endpoint that is not on this machine is refused unless `yes`: it would receive this machine's history |
| `timeout_s` | `120` | Seconds a generation may take (10–600) |
| `web_actions` | `yes` | `yes`: the AI page of the web view and the AI screen of the console may act: set a model up (download it, start its server), turn the advisor on and off, delete the downloaded files, ask questions, as the unprivileged account that runs them. The same access as viewing the page: loopback, or the token; everyone who can open the page can press the buttons. What they choose goes in `web.json` in the AI folder, never in `config.ini`. `no`: the lock for an admin who wants the web view strictly read-only: the page and the screen only show ("locked by config.ini"), every post is refused with 403 and `web.json` counts for nothing. [WEB.md](WEB.md#the-ai-pages-buttons) |
| `autostart` | `yes` | The model server the AI page starts is a child of the web view, so it ends whenever the web view restarts (an update, a reboot). `yes`: when the web view (or the desktop app, or a portable run) starts and the AI is on with a model chosen (`web.json`), it starts that server again about 5 s later, with the checks of the page's own start (server and model installed, the model fits, 127.0.0.1 only, low priority), unless one already answers at the endpoint; once, and once more after 60 s if that failed, never after you pressed *Turn AI off*. `no`: only the buttons start it. Applies at the next start of the web view. The console never starts a server. [AI.md](AI.md#after-a-restart) |
| `daily` | `no` | `yes`: the collector asks for one digest of the last 7 days a day, at low priority, shown on the HEALTH screen ([AI.md](AI.md#the-daily-digest)) |

## Commands

Windows: the same commands without `sudo`, from an **administrator** prompt for `nuc-console-accept`; they are on the system PATH after the install.
In a portable folder `nuc-console-accept` and `nuc-console-problems` are not used (they read an installation): `./run.sh --accept` (`run.cmd -Accept`) does the accepting.
Windows: the same commands without `sudo`, from an **administrator** prompt for `nuc-console-accept` and `nuc-console-ai`; they are on the system PATH after the install.

| Command | |
|---|---|
| `nuc-console-update [--check] [--yes] [--installed]` | update to the latest GitHub release when you run it (`sudo` for an installed one; Windows: `-Check` `-Yes` `-Installed`; a portable folder: its own `bin/nuc-console-update`). Never automatic; keeps `config.ini`: [INSTALL.md](INSTALL.md#update) |
| `./run.sh [--console \| --web] [--port N] [--no-open]`, `run.cmd [-Port N] [-NoOpen]` | run the dashboard from the extracted folder without installing it; `./run.sh --accept [--problem <id> --reason "…" \| --forget <id>]` (`run.cmd -Accept`) accepts like `nuc-console-accept`: [PORTABLE.md](PORTABLE.md) |
| `sudo nuc-console-config migrate [--dry-run] [--yes]` | bring an old, long `config.ini` to the shipped layout, keeping your settings (a copy is kept): [Upgrading config.ini](#upgrading-configini) |
| `nuc-console-fleet list \| test NAME \| once \| forget NAME` | the `[hosts]` peers: `list` shows them and the last state in the cache, `test NAME` polls one now and says the state and the reason in words (never the token), `once` polls all and writes the cache, `forget NAME` drops the remembered `host_id` of a peer (the way to accept a replaced machine). The token files belong to the poller's account: `sudo -u nuc-console-peers nuc-console-fleet test NAME` |
| `nuc-console-problems [--json]` | every current ATTENTION item with id, why it matters and how to fix it (no root); the ones silenced by `[alerts] mute` come last, under `muted` |
| `sudo nuc-console-accept` | accept the current set of exposed ports, and on Linux the accounts, SSH keys, cron files and enabled units, as the baseline (their alarms) |
| `sudo nuc-console-accept --problem <id> --reason "…"` | mark a known ATTENTION item as accepted: hidden from the list, counted as "N accepted"; tied to its current severity and text, so a worse situation reappears. Port changes are not accepted this way |
| `sudo nuc-console-accept --forget <id>` | undo it |
| `sudo nuc-console-telegram --setup` | pair the Telegram notifier: asks the bot token (from @BotFather) and your @username, prints a `t.me/…` link to open on the phone (Windows: `nuc-console-telegram.cmd`, administrator prompt) |
| `sudo nuc-console-telegram --on` / `--off` | switch the notifications on or off (`[telegram] enabled`) |
| `nuc-console-telegram --status [--json]` | on or off, paired or not, last message sent, last error (no root on Linux and macOS; Windows: administrator prompt) |
| `sudo nuc-console-telegram --test` / `--forget` | send a test message / forget the token and the paired chat |
| `python3 /opt/nuc-console/render.py --once --demo` | preview with synthetic data (add `--cols N --rows N`, `--color`; `--demo-os windows` or `darwin` for those collectors) |
| `render.py --once --view events` | the EVENTS timeline once (`--age 1h\|24h\|7d`, `--kind KIND`, `--select N`, `--details`, `--demo`, `--demo-health none`); needs `[features] health = yes` to have anything to list (it says so when not) |
| `render.py --once --view map` / `--view cpu` / `--view health` / `--view ai` | the MAP, the CPU, the HEALTH or the AI screen once, for a quick look over SSH (`--demo`, `--cols`, `--rows`, `--color`; MAP: `--expand all`, `--select TEXT`, `--details`; CPU: `--sort mem`, `--select PID`, `--details`; HEALTH: `--period 1\|7\|30`, `--select TEXT`, `--details`; AI: `--select TEXT`, `--details`, `--demo-os windows\|darwin`); `--view start`: the screen `[ui] start_view` opens at, and the overview without it |
| `nuc-console-ai models` | what this machine can run: hardware and a verdict per model (fits the GPU, GPU+CPU, fits RAM, slows the PC, too big), no root; `status` (is it installed, does it answer) also needs none. `/usr/local/sbin/nuc-console-ai` if your PATH lacks the folder |
| `sudo nuc-console-ai setup [MODEL ...]` | download the local model server (Ollama, SHA-256 checked) and the models you name (none: the recommended one), once. Also `use MODEL`, `serve [--cpu] [--install-service]`, `remove [MODEL]` ([AI.md](AI.md#the-commands)) |
| `nuc-console-ask "question"`, `nuc-console-ask advise [--days N]`, `nuc-console-ask status` | ask the local model about this machine, get advice on the HEALTH findings, check the server (read-only, no root; needs `[ai] enabled = yes`) |
| `render.py --open` | the dashboard in a normal window of the default browser (what `browser` mode runs at login). With `[web] token_file`: the token goes in the address if this user can read the file, else the page is written to a file as `--kiosk --file` does ([WEB.md](WEB.md)) |
| `render.py --kiosk` | the full-screen window on the local web view (macOS/Windows; `--file` writes a local page instead, also on a Linux desktop: `--html FILE`, `--no-browser`) |

Install-time options: Linux `install.sh` reads `NUC_CONSOLE_VT` (virtual terminal, default 1) and `NUC_CONSOLE_TZ` (time zone), and a re-install keeps them.
macOS: `NUC_CONSOLE_DISPLAY=browser|fullscreen|none`. Windows: `install-windows.cmd -Display browser|fullscreen|none` (`-NoDisplay` = `none`), `-PythonZip <file>` (offline), `-Uninstall`.
