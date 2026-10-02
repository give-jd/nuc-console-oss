# Configuration reference

Everything is optional: a missing file, a missing key or a broken value means the default shown here, and a broken file never stops the dashboard (the problem goes to the journal).

| | |
|---|---|
| File | Linux and macOS: `/etc/nuc-console/config.ini` · Windows: `%ProgramData%\nuc-console\config.ini` (created on first install, **never overwritten**; UTF-8) · [portable run](PORTABLE.md): `data/config.ini` in the extracted folder (copied from `config/config.ini` the first time, never overwritten) |
| Reference copy | `config.ini.dist` next to it, refreshed on every install, update and portable start: `diff /etc/nuc-console/config.ini{,.dist}` shows the options added since you copied it |
| Apply | Linux: `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web` · macOS: run `sudo ./install.sh` again · Windows: run `install-windows.cmd` again (the installers restart everything and keep `config.ini`) · portable: quit (Ctrl+C) and start `run.sh` / `run.cmd` again. `nuc-console-update` keeps your `config.ini` |
| Override | `NUC_CONSOLE_CONFIG=<path>` (config file), `NUC_CONSOLE_MODE` (`overview`/`rotate`), `NUC_CONSOLE_HOME=<folder>` ([portable run](#portable-run)) |

## Portable run

`run.sh` and `run.cmd` (see [PORTABLE.md](PORTABLE.md)) set `NUC_CONSOLE_HOME` to the `data/` folder next to them, and the collector, the screen and the web view
then keep everything there: the config as `data/config.ini`, the collector's snapshots in `data/run/`, the baseline and the accepted problems in `data/lib/`.
You do not normally set it yourself (set by hand, it also makes the web view always local, see below). `NUC_CONSOLE_CONFIG`, if set, wins over the file in that
folder; the launchers clear it, and the other `NUC_CONSOLE_*` path overrides (state, baseline, ...).

Every key applies as on an installation except the ones that decide how an installation shows itself: `[web] enabled`, `bind`, `port` and `token_file` are ignored (the
web view is on `127.0.0.1`, on the port you give with `--port` / `-Port` or a free one, with no token) and so are `[display] mode` and `browser`
(the launcher opens your default browser itself; `--no-open` / `-NoOpen` prints the address instead). `[display] zoom` still sets the text size.

## `[features]` — switch sections on or off

All default to `yes`. A disabled section is not drawn, raises no alarm and, for the collector-side ones, **its commands are never run as root**.

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
| `spacing` | `1` | A blank line under each section title. If something would be cut, the layout is first retried without it: complete content beats spacing |
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
> the table and under "On the console" below). **The web interface** (a shell of cards, [WEB.md](WEB.md#the-shell-the-default-web-interface)) is the default and reads this
> section; `web = classic` (or `?app=0` in one URL) serves the older classic pages instead, kept for one release as a fallback and then removed, and they ignore every other key here.
> A wrong value is reported on stderr and only that key is skipped, like everywhere else in this file.

Every key is optional. A key left out, blank or wrong means the default, **except** that `hidden =` left blank means "nothing is hidden".

| Key | Default | Meaning |
|---|---|---|
| `web` | `app` | Which web interface is served: `app` (the shell of cards) or `classic` (the older pages, kept for one release as a fallback and then removed). `?app=0` / `?app=1` choose for one URL |
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

## `[display]` — the dashboard on macOS and Windows

macOS and Windows have no text console to take over. The installers start the web view (read-only; only the AI page has buttons) on **127.0.0.1 only**
(not reachable from the network) and show it the way you choose. Linux ignores this section (it uses the console).

| Key | Default | Meaning |
|---|---|---|
| `mode` | `browser` | How the dashboard opens, at install and at every login. `browser`: a normal window of your default browser. `fullscreen` (or `kiosk`): full screen, overview and Details pages taking turns. `none`: never by itself (a machine without a monitor). The **nuc-console** shortcut (Windows Start menu, macOS Applications) opens it again any time. The installers apply it: run them again after a change, or choose with `install-windows.cmd -Display fullscreen` / `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh` (that writes this key; a plain re-install keeps it) |
| `zoom` | `100` | Text size in percent, 50–200. Bigger text = fewer columns, re-laid out (no sideways scrolling). In a browser window **nothing is left out**: every section and every item, the page scrolls; full screen shows what does not fit on the rotating Details pages. The **A− / A+** links at the bottom of the page change it while you look |
| `browser` | `auto` | The browser of the full-screen window. `auto`: Microsoft Edge, then Google Chrome, then Firefox (Windows: Firefox cannot start full screen, press F11; with none of them the default browser opens a normal window, F11 again); Chrome, Edge, Brave, Chromium, else Safari (macOS: press Ctrl+Cmd+F once). Or the full path of a Chromium-based browser |

By default (`[ui] web = app`) the full-screen window opens the shell's wall display (`/?app=1&ui=1.dw&kiosk=1`: wall density, scrolling one screen every `[dashboard] rotate_seconds` seconds, [WEB.md](WEB.md#wall-and-kiosk)) and a normal window opens `/?app=1`; with `web = classic` they open the classic page as before.

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

## `[web]` — web view: read-only, except the AI page's buttons (off by default)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The only network listener of the dashboard (the optional AI model server listens on 127.0.0.1 only, [AI.md](AI.md)); every page is read-only except the buttons of the AI page, which `[ai] web_actions = no` locks (macOS/Windows: with `enabled = no` the installers still run it on 127.0.0.1 for `[display]`; a portable run always runs it on 127.0.0.1 and ignores this section's `enabled`, `bind`, `port` and `token_file`). Details and threat model: [WEB.md](WEB.md) |
| `bind` | `127.0.0.1` | Anything else **requires** `token_file` (the service refuses to start otherwise) |
| `port` | `8787` | |
| `token_file` | empty | File with a secret (16+ chars of `A-Za-z0-9._~-`), mode 0600, owned by root or `nuc-console` (Windows: keep it in `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write). Never put the token in `config.ini` (world-readable) |
| `allowed_hosts` | empty | Extra `Host` names accepted when no token is set (DNS-rebinding guard); `localhost`, `127.0.0.1`, the bind address, the hostname and `*.ts.net` always are |
| `columns`, `rows` | `200`, `60` | Layout of the page (`?cols=100` for compact, `?full=1` for the overview plus every Details page) |
| `refresh_seconds` | — | Older place of `[dashboard] refresh_seconds`: still read (1–10) for the web pages while `[dashboard]` has none. Use `[dashboard]` |

## `[telegram]` — alerts on your phone (off by default)

```ini
[telegram]
enabled = no
username = your_telegram_name
detail = titles
resolved = yes
```

New and resolved ATTENTION problems, sent to **one** Telegram user by a bot you create yourself (free). It only sends: no listener, no webhook, it never reads messages, no commands. Set-up in three steps, what leaves the machine and the threat model: [TELEGRAM.md](TELEGRAM.md).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | Switch it on or off with `sudo nuc-console-telegram --on` / `--off` (they edit this line and start or stop the service). Nothing is sent until the chat is paired (`--setup`) |
| `username` | empty | Your Telegram `@username` (5–32 letters, digits, `_`; the `@` is optional): the only person who gets the messages. `sudo nuc-console-telegram --setup` asks for it and writes it here |
| `detail` | `titles` | `titles`: only the title of each problem and the host name leave the machine ("Container unhealthy"). `full`: the text too, with container names and ports; it is then stored by Telegram |
| `resolved` | `yes` | Also send a message when a problem goes away |

The bot **token** is never in `config.ini` (it is world-readable): it lives in the notifier's own folder (`/var/lib/nuc-console-notify`, Windows `%ProgramData%\nuc-console\notify\private`), readable only by the notifier's own account (Linux `nuc-console-notify`, macOS `_nuc-console`, Windows NETWORK SERVICE), never by the web view's.

## `[ai]` — optional local model for the HEALTH screen (off by default)

What it is, how to choose a model for your hardware, the commands and the security rules: **[AI.md](AI.md)**.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | Use a local model to turn the HEALTH findings into advice and to answer questions. It only reads and suggests: it never runs anything. The AI screen (`[features] ai`) shows with `no` as well: it is where you choose. `nuc-console-ai setup` never switches it on for you. **`yes` always wins**: the AI page and screen cannot switch it off (they say "on by config.ini"); with `no` they can turn it on, and what they chose is kept in `web.json` in the AI folder ([AI.md](AI.md#from-the-browser-and-the-console)) |
| `endpoint` | `http://127.0.0.1:11434/v1` | Any OpenAI-compatible server: Ollama (this default), llama.cpp server, LM Studio, llamafile (`nuc-console-ai setup` installs one, and offers to write `http://127.0.0.1:8080/v1`, or the `--port` you gave) |
| `model` | empty | The model name the server knows. What `nuc-console-ai setup` (the first model named) or `use` writes is a catalog id, e.g. `qwen3-8b`, which is the name its server answers to; for Ollama e.g. `qwen3:8b`. Also the model `serve` starts when none is named. Empty: the server's first model is used |
| `gpu` | `auto` | `auto`: `nuc-console-ai serve` puts the model on the GPU (all of it, or some layers) when the hardware advice says it fits there; `no`: the server it starts never uses the GPU (and the hardware is not even read for it). Only for the server that `nuc-console-ai` starts: Ollama and the others decide for themselves. An installed service keeps what it was installed with: after changing `gpu` (or `model`, with `use`) run `sudo nuc-console-ai serve --install-service` again |
| `allow_remote` | `no` | An endpoint that is not on this machine is refused unless `yes`: it would receive this machine's history |
| `timeout_s` | `120` | Seconds a generation may take (10–600) |
| `web_actions` | `yes` | `yes`: the AI page of the web view and the AI screen of the console may act: set a model up (download it, start its server), turn the advisor on and off, delete the downloaded files, ask questions, as the unprivileged account that runs them. The same access as viewing the page: loopback, or the token; everyone who can open the page can press the buttons. What they choose goes in `web.json` in the AI folder, never in `config.ini`. `no`: the lock for an admin who wants the web view strictly read-only: the page and the screen only show ("locked by config.ini"), every post is refused with 403 and `web.json` counts for nothing. [WEB.md](WEB.md#the-ai-pages-buttons) |
| `daily` | `no` | `yes`: the collector asks for one digest of the last 7 days a day, at low priority, shown on the HEALTH screen ([AI.md](AI.md#the-daily-digest)) |

## Commands

Windows: the same commands without `sudo`, from an **administrator** prompt for `nuc-console-accept`; they are on the system PATH after the install.
In a portable folder `nuc-console-accept` and `nuc-console-problems` are not used (they read an installation): `./run.sh --accept` (`run.cmd -Accept`) does the accepting.
Windows: the same commands without `sudo`, from an **administrator** prompt for `nuc-console-accept` and `nuc-console-ai`; they are on the system PATH after the install.

| Command | |
|---|---|
| `nuc-console-update [--check] [--yes] [--installed]` | update to the latest GitHub release when you run it (`sudo` for an installed one; Windows: `-Check` `-Yes` `-Installed`; a portable folder: its own `bin/nuc-console-update`). Never automatic; keeps `config.ini`: [INSTALL.md](INSTALL.md#update) |
| `./run.sh [--console \| --web] [--port N] [--no-open]`, `run.cmd [-Port N] [-NoOpen]` | run the dashboard from the extracted folder without installing it; `./run.sh --accept [--problem <id> --reason "…" \| --forget <id>]` (`run.cmd -Accept`) accepts like `nuc-console-accept`: [PORTABLE.md](PORTABLE.md) |
| `nuc-console-problems [--json]` | every current ATTENTION item with id, why it matters and how to fix it (no root) |
| `sudo nuc-console-accept` | accept the current set of exposed ports as the baseline (port alarms) |
| `sudo nuc-console-accept --problem <id> --reason "…"` | mark a known ATTENTION item as accepted: hidden from the list, counted as "N accepted"; tied to its current severity and text, so a worse situation reappears. Port changes are not accepted this way |
| `sudo nuc-console-accept --forget <id>` | undo it |
| `sudo nuc-console-telegram --setup` | pair the Telegram notifier: asks the bot token (from @BotFather) and your @username, prints a `t.me/…` link to open on the phone (Windows: `nuc-console-telegram.cmd`, administrator prompt) |
| `sudo nuc-console-telegram --on` / `--off` | switch the notifications on or off (`[telegram] enabled`) |
| `nuc-console-telegram --status [--json]` | on or off, paired or not, last message sent, last error (no root on Linux and macOS; Windows: administrator prompt) |
| `sudo nuc-console-telegram --test` / `--forget` | send a test message / forget the token and the paired chat |
| `python3 /opt/nuc-console/render.py --once --demo` | preview with synthetic data (add `--cols N --rows N`, `--color`; `--demo-os windows` or `darwin` for those collectors) |
| `render.py --once --view map` / `--view cpu` / `--view health` / `--view ai` | the MAP, the CPU, the HEALTH or the AI screen once, for a quick look over SSH (`--demo`, `--cols`, `--rows`, `--color`; MAP: `--expand all`, `--select TEXT`, `--details`; CPU: `--sort mem`, `--select PID`, `--details`; HEALTH: `--period 1\|7\|30`, `--select TEXT`, `--details`; AI: `--select TEXT`, `--details`, `--demo-os windows\|darwin`); `--view start`: the screen `[ui] start_view` opens at, and the overview without it |
| `nuc-console-ai models` | what this machine can run: hardware and a verdict per model (fits the GPU, GPU+CPU, fits RAM, slows the PC, too big), no root; `status` (is it installed, does it answer) also needs none. `/usr/local/sbin/nuc-console-ai` if your PATH lacks the folder |
| `sudo nuc-console-ai setup [MODEL ...]` | download the local model server's runtime and the models you name (none: the recommended one), once, SHA-256 checked. Also `use MODEL`, `serve [--gpu-layers N] [--install-service]`, `remove [MODEL]` ([AI.md](AI.md#the-commands)) |
| `nuc-console-ask "question"`, `nuc-console-ask advise [--days N]`, `nuc-console-ask status` | ask the local model about this machine, get advice on the HEALTH findings, check the server (read-only, no root; needs `[ai] enabled = yes`) |
| `render.py --open` | the dashboard in a normal window of the default browser (what `browser` mode runs at login). With `[web] token_file`: the token goes in the address if this user can read the file, else the page is written to a file as `--kiosk --file` does ([WEB.md](WEB.md)) |
| `render.py --kiosk` | the full-screen window on the local web view (macOS/Windows; `--file` writes a local page instead, also on a Linux desktop: `--html FILE`, `--no-browser`) |

Install-time options: Linux `install.sh` reads `NUC_CONSOLE_VT` (virtual terminal, default 1) and `NUC_CONSOLE_TZ` (time zone), and a re-install keeps them.
macOS: `NUC_CONSOLE_DISPLAY=browser|fullscreen|none`. Windows: `install-windows.cmd -Display browser|fullscreen|none` (`-NoDisplay` = `none`), `-PythonZip <file>` (offline), `-Uninstall`.
