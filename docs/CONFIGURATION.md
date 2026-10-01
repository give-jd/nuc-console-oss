# Configuration reference

Everything is optional: a missing file, a missing key or a broken value means the default shown here, and a broken file never stops the dashboard (the problem goes to the journal).

| | |
|---|---|
| File | `/etc/nuc-console/config.ini` (created on first install, **never overwritten**) |
| Reference copy | `/etc/nuc-console/config.ini.dist`, refreshed on every install: `diff /etc/nuc-console/config.ini{,.dist}` shows the options added since you copied it |
| Apply | `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web` |
| Override | `NUC_CONSOLE_CONFIG=<path>` (config file), `NUC_CONSOLE_MODE` (`overview`/`rotate`) |

## `[features]` — switch sections on or off

All default to `yes`. A disabled section is not drawn, raises no alarm and, for the collector-side ones, **its commands are never run as root**.

| Key | Section | Runs as root |
|---|---|---|
| `containers` | CONTAINER list and health | `docker` |
| `databases` | DATABASE: ports and who connects | `docker`, `ss`, `nsenter` |
| `exposure` | EXPOSURE, port alarms, Funnel (also the "DB open on LAN" alarm) | `ss`, `tailscale serve` |
| `webapps` | WEB APPS | — |
| `firewall` | FIREWALL: ufw, iptables, `DOCKER-USER`, drops | `ufw`, `iptables`, `journalctl` |
| `fail2ban` | jails and bans (drawn inside FIREWALL) | `fail2ban-client` |
| `tailscale` | TAILSCALE peers | `tailscale` |
| `boot` | BOOT: time, slowest units, failed units, journal | `systemd-analyze`, `journalctl` |
| `docker_disk` | DOCKER · DISK | `docker system df` |
| `network_traffic`, `sessions`, `disks`, `thermal` | the respective panels | — (reads `/proc`, `/sys`) |

## `[dashboard]` — layout

| Key | Default | Meaning |
|---|---|---|
| `mode` | `overview` | `overview`: one screen, no keyboard needed. `rotate`: 3 pages (System, Network & firewall, Boot), keys `1`-`3` jump to a page |
| `sections` | by priority | Fixed on-screen order, top-left to bottom-right. Names: `attention, exposure, webapps, firewall, system, containers, databases, boot, network_traffic, sessions, tailscale, docker_disk, disks`. Names you leave out keep their default place at the end |
| `columns`, `rows` | `0` | Layout size in characters; `0` = the real console size. Never larger than the real console. Use it when elements run off the screen (e.g. `columns = 235`, `rows = 65`) |
| `spacing` | `1` | A blank line under each section title. If something would be cut, the layout is first retried without it: complete content beats spacing |
| `details` | `yes` | The overview cuts a list only when it really does not fit; those sections then get **Details** pages showing everything, rotating on the monitor (it has no keyboard). `no`: never rotate |
| `overview_seconds` | `45` | How long the overview stays before the Details pages (10-600) |
| `rotate_seconds` | `15` | How long each page stays in `rotate` mode and each Details page (3-600) |

Sections fill the columns in the given order and never back-fill, so a line more or less in one block does not move the others. Per-core CPU bars are always one per core, except on tiny consoles (the last two fitting levels).

## `[webapps]` — the web apps you expect

```ini
[webapps]
ethibid = 8180, 8543
admin-console = 9443
```

`name = port[, port…]`. Listed apps appear in **WEB APPS** as active (with how far they are reachable: local, tailnet, LAN, Internet) or as **DOWN (expected)** when nothing listens. The ports are an *intended exposure*: they stop counting as "Docker port bypassing ufw" (database ports are never masked). Other web listeners found on the machine are listed as "not declared".

## `[web]` — read-only web view (off by default)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The only network listener of the project. Details and threat model: [WEB.md](WEB.md) |
| `bind` | `127.0.0.1` | Anything else **requires** `token_file` (the service refuses to start otherwise) |
| `port` | `8787` | |
| `token_file` | empty | File with a secret (16+ chars of `A-Za-z0-9._~-`), mode 0600, owned by root or `nuc-console`. Never put the token in `config.ini` (world-readable) |
| `allowed_hosts` | empty | Extra `Host` names accepted when no token is set (DNS-rebinding guard); `localhost`, `127.0.0.1`, the bind address, the hostname and `*.ts.net` always are |
| `columns`, `rows` | `200`, `60` | Layout of the page (`?cols=100` for compact, `?full=1` for the overview plus every Details page) |
| `refresh_seconds` | `5` | Page auto-refresh |

## Commands

| Command | |
|---|---|
| `nuc-console-problems [--json]` | every current ATTENTION item with id, why it matters and how to fix it (no root) |
| `sudo nuc-console-accept` | accept the current set of exposed ports as the baseline (port alarms) |
| `sudo nuc-console-accept --problem <id> --reason "…"` | mark a known ATTENTION item as accepted: hidden from the list, counted as "N accepted"; tied to its current severity and text, so a worse situation reappears. Port changes are not accepted this way |
| `sudo nuc-console-accept --forget <id>` | undo it |
| `python3 /opt/nuc-console/render.py --once --demo` | preview with synthetic data (add `--cols N --rows N`, `--color`) |

Install-time options (environment of `install.sh`): `NUC_CONSOLE_VT` (virtual terminal, default 1) and `NUC_CONSOLE_TZ` (time zone); a re-install keeps them.
