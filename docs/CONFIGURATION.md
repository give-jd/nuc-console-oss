# Configuration reference

Everything is optional: a missing file, a missing key or a broken value means the default shown here, and a broken file never stops the dashboard (the problem goes to the journal).

| | |
|---|---|
| File | Linux and macOS: `/etc/nuc-console/config.ini` · Windows: `%ProgramData%\nuc-console\config.ini` (created on first install, **never overwritten**; UTF-8) |
| Reference copy | `config.ini.dist` next to it, refreshed on every install: `diff /etc/nuc-console/config.ini{,.dist}` shows the options added since you copied it |
| Apply | Linux: `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web` · macOS: run `sudo ./install.sh` again · Windows: run `install-windows.cmd` again (the installers restart everything and keep `config.ini`) |
| Override | `NUC_CONSOLE_CONFIG=<path>` (config file), `NUC_CONSOLE_MODE` (`overview`/`rotate`) |

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
| `cpu` | the **CPU** screen: per-core load and frequency, temperatures, top processes, like htop (console key `c`, web `cpu` link) | `/proc`, `/sys` (Linux); system calls and `ps` (macOS); Windows API. Temperatures on macOS/Windows: the collector (`powermetrics`; WMI, LibreHardwareMonitor/OpenHardwareMonitor if installed) |
| `map` | the **MAP** screen: who reaches what and what is behind it, navigable (console keys `m`/`Tab`, web `map` link) | `docker inspect`, `ss`, `nsenter … ss` inside every running container (Linux); host sockets (macOS/Windows) |

## `[dashboard]` — layout

| Key | Default | Meaning |
|---|---|---|
| `mode` | `overview` | `overview`: one screen, no keyboard needed. `rotate`: 3 pages (System, Network & firewall, Boot), keys `1`-`3` jump to a page |
| `sections` | by priority | Fixed on-screen order, top-left to bottom-right. Names: `attention, exposure, webapps, firewall, system, containers, databases, boot, network_traffic, sessions, tailscale, docker_disk, disks`. Names you leave out keep their default place at the end |
| `columns`, `rows` | `0` | Layout size in characters; `0` = the real console size. Never larger than the real console. Use it when elements run off the screen (e.g. `columns = 235`, `rows = 65`) |
| `spacing` | `1` | A blank line under each section title. If something would be cut, the layout is first retried without it: complete content beats spacing |
| `details` | `yes` | The overview cuts a list only when it really does not fit; those sections then get **Details** pages showing everything, rotating on the monitor (it has no keyboard). `no`: never rotate |
| `overview_seconds` | `45` | How long the overview stays before the Details pages (10-600) |
| `cpu_in_rotation` | `no` | `yes`: the CPU screen joins the pages the monitor rotates through (a monitor with no keyboard) |
| `map_in_rotation` | `no` | `yes`: the MAP, expanded as far as it fits, joins the pages the monitor rotates through (a monitor with no keyboard). The interactive MAP is always one key / one click away |
| `rotate_seconds` | `15` | How long each page stays in `rotate` mode and each Details page (3-600) |
| `refresh_seconds` | `2` | Seconds between two redraws, **1–10** (smaller or larger values are clamped): the console, the full-screen window and the browser pages, where the **− / +** links next to "refresh every" change it while you look. Faster = livelier CPU and traffic bars, a little more CPU |

Sections fill the columns in the given order and never back-fill, so a line more or less in one block does not move the others. Per-core CPU bars are always one per core, except on tiny consoles (the last two fitting levels).

## `[display]` — the dashboard on macOS and Windows

macOS and Windows have no text console to take over. The installers start the read-only web view on **127.0.0.1 only**
(not reachable from the network) and show it the way you choose. Linux ignores this section (it uses the console).

| Key | Default | Meaning |
|---|---|---|
| `mode` | `browser` | How the dashboard opens, at install and at every login. `browser`: a normal window of your default browser. `fullscreen` (or `kiosk`): full screen, overview and Details pages taking turns. `none`: never by itself (a machine without a monitor). The **nuc-console** shortcut (Windows Start menu, macOS Applications) opens it again any time. The installers apply it: run them again after a change, or choose with `install-windows.cmd -Display fullscreen` / `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh` (that writes this key; a plain re-install keeps it) |
| `zoom` | `100` | Text size in percent, 50–200. Bigger text = fewer columns, re-laid out (no sideways scrolling). In a browser window **nothing is left out**: every section and every item, the page scrolls; full screen shows what does not fit on the rotating Details pages. The **A− / A+** links at the bottom of the page change it while you look |
| `browser` | `auto` | The browser of the full-screen window. `auto`: Microsoft Edge, then Google Chrome (Windows); Chrome, Edge, Brave, Chromium, else Safari (macOS: press Ctrl+Cmd+F once). Or the full path of a Chromium-based browser |

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

`[expose]` only adds alarms: `db-open-lan`, `docker-bypass` and the port baseline are never silenced by it. `over-exposed` can be accepted like any ATTENTION item (`nuc-console-accept --problem over-exposed --reason "…"`); a new service going beyond makes it reappear. A bad value or a port that cannot exist is reported on stderr and only that line is skipped. Never write a port as `:8080`: configparser refuses a key that starts with `:` and the **whole file** falls back to the defaults.

## `[web]` — read-only web view (off by default)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The only network listener of the project (macOS/Windows: with `enabled = no` the installers still run it on 127.0.0.1 for `[display]`). Details and threat model: [WEB.md](WEB.md) |
| `bind` | `127.0.0.1` | Anything else **requires** `token_file` (the service refuses to start otherwise) |
| `port` | `8787` | |
| `token_file` | empty | File with a secret (16+ chars of `A-Za-z0-9._~-`), mode 0600, owned by root or `nuc-console` (Windows: keep it in `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write). Never put the token in `config.ini` (world-readable) |
| `allowed_hosts` | empty | Extra `Host` names accepted when no token is set (DNS-rebinding guard); `localhost`, `127.0.0.1`, the bind address, the hostname and `*.ts.net` always are |
| `columns`, `rows` | `200`, `60` | Layout of the page (`?cols=100` for compact, `?full=1` for the overview plus every Details page) |
| `refresh_seconds` | — | Older place of `[dashboard] refresh_seconds`: still read (1–10) for the web pages while `[dashboard]` has none. Use `[dashboard]` |

## Commands

Windows: the same commands without `sudo`, from an **administrator** prompt for `nuc-console-accept`; they are on the system PATH after the install.

| Command | |
|---|---|
| `nuc-console-problems [--json]` | every current ATTENTION item with id, why it matters and how to fix it (no root) |
| `sudo nuc-console-accept` | accept the current set of exposed ports as the baseline (port alarms) |
| `sudo nuc-console-accept --problem <id> --reason "…"` | mark a known ATTENTION item as accepted: hidden from the list, counted as "N accepted"; tied to its current severity and text, so a worse situation reappears. Port changes are not accepted this way |
| `sudo nuc-console-accept --forget <id>` | undo it |
| `python3 /opt/nuc-console/render.py --once --demo` | preview with synthetic data (add `--cols N --rows N`, `--color`; `--demo-os windows` or `darwin` for those collectors) |
| `render.py --once --view map` / `--view cpu` | the MAP or the CPU screen once, for a quick look over SSH (`--demo`, `--cols`, `--rows`, `--color`; MAP: `--expand all`, `--select TEXT`, `--details`; CPU: `--sort mem`, `--select PID`, `--details`) |
| `render.py --open` | the dashboard in a normal window of the default browser (what `browser` mode runs at login) |
| `render.py --kiosk` | the full-screen window on the local web view (macOS/Windows; `--file` writes a local page instead, also on a Linux desktop: `--html FILE`, `--no-browser`) |

Install-time options: Linux `install.sh` reads `NUC_CONSOLE_VT` (virtual terminal, default 1) and `NUC_CONSOLE_TZ` (time zone), and a re-install keeps them.
macOS: `NUC_CONSOLE_DISPLAY=browser|fullscreen|none`. Windows: `install-windows.cmd -Display browser|fullscreen|none` (`-NoDisplay` = `none`), `-PythonZip <file>` (offline), `-Uninstall`.
