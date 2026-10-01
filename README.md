<div align="center">

# 🖥️ nuc-console

**Turn the forgotten monitor of your headless Linux box — or Mac, or Windows PC — into a live security & health board.**<br>
Linux: no X11, no browser · macOS/Windows: one full-screen local page · no dependencies · one screen · ~0.5 % of a CPU core

[![License: MIT](https://img.shields.io/badge/license-MIT-3fb950?style=flat-square)](LICENSE)
[![Release](https://img.shields.io/github/v/release/give-jd/nuc-console-oss?style=flat-square&color=58a6ff)](https://github.com/give-jd/nuc-console-oss/releases)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776ab?style=flat-square&logo=python&logoColor=white)](#requirements)
[![Linux + systemd](https://img.shields.io/badge/linux-systemd-fcc624?style=flat-square&logo=linux&logoColor=black)](#requirements)
[![macOS 11+](https://img.shields.io/badge/macOS-11%2B-999999?style=flat-square&logo=apple&logoColor=white)](docs/INSTALL.md#macos)
[![Windows 10/11](https://img.shields.io/badge/windows-10%20%7C%2011-0078d4?style=flat-square)](docs/INSTALL.md#windows)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-8957e5?style=flat-square)](#security)

[**Quick start**](#quick-start) · [**Install guide**](docs/INSTALL.md) · [**Configuration**](#configuration) · [**How it works**](#how-it-works) · [**Security**](SECURITY.md)

<a href="docs/img/overview.svg"><img src="docs/img/overview.svg" alt="nuc-console on a wide console: three-column overview with synthetic demo data" width="100%"></a>

<sub>The real screen on a 226-column console (3-column layout), rendered from <code>--demo</code> synthetic data. Click to zoom.</sub>

</div>

## ✨ At a glance

| | |
|---|---|
| 🌐 **Exposure by reach** | every listener classified as *local / LAN / Tailscale / Internet (Funnel)*, corrected by the real firewall |
| 🧱 **Firewall truth** | ufw, iptables, `DOCKER-USER`, fail2ban — flags *Docker ports that bypass ufw* and *Tailscale accepted before ufw* |
| 🧾 **Problem inventory** | every ATTENTION item has a stable id, an explanation and a fix: `nuc-console-problems` lists them, `sudo nuc-console-accept --problem <id> --reason "…"` marks a known one (dimmed, not counted) |
| 🕸️ **Web apps** | WEB APPS section: the apps you declared (active, or DOWN when expected but not listening) and the web listeners found on their own, with how far each is reachable |
| 🚨 **Port alarms** | a baseline of exposed ports; any new, changed or vanished port turns the banner red |
| 🗺️ **Map** | who reaches what, and *what is behind it*: zone → open port → process or container → what that one uses (`LAN → :8080 → shop-web → shop-api → shop-db`), what breaks if something is down, compose stacks, outbound connections. Every link says how it is known (*seen* / *declared* / *same network*); navigable: open, close, expand all, details of any node, problems only (console `m`/`Tab`, web **map** link) |
| 🐳 **Containers & databases** | per-stack health, real published ports, *who actually connects* to each DB (seen inside its network namespace) |
| 🌡️ **Health** | boot time and slowest units, failed units, journal errors, CPU/NVMe temperature, thermal throttling, disks, traffic |
| 🔒 **Least privilege** | small root collector + unprivileged renderer, stdlib only, nothing reachable from the network (macOS/Windows: the page is on 127.0.0.1 only) |
| 🖥️ **Linux, macOS, Windows** | one command each; on macOS and Windows the same screen in your browser or full screen at login (your choice, text size A− / A+), and the exposure is judged by the **Application Firewall** / **Windows Firewall** per program ([install guide](docs/INSTALL.md)) |
| 🎛️ **Configurable** | switch every section on/off, **fixed and reorderable section order**, single screen or rotating pages, pin the layout size, refresh every 1–10 s |
| 🌍 **Read-only web view** | optional: the same screen in a browser over Tailscale/LAN ([docs/WEB.md](docs/WEB.md)); off by default, token or loopback only |
| 🔍 **Nothing hidden** | what the overview cuts ("… +N more") is shown in full on rotating **Details** pages (no keyboard needed) and in the web view (`/?full=1`) |
| 🧪 **Try it without root** | `python3 src/render.py --once --demo` |

<details>
<summary><b>Smaller consoles: the layout adapts</b> (120×33, single column)</summary>
<br>
<img src="docs/img/compact.svg" alt="nuc-console on a 120x33 console, single column, demo data" width="100%">
</details>

<details>
<summary><b>The MAP: who reaches what, and what is behind it</b> (200×46, details of a container open)</summary>
<br>
<img src="docs/img/map.svg" alt="nuc-console MAP screen: zones, open ports and the containers behind them as a tree, with the details pane of one container, demo data" width="100%">
</details>

## Why

A home server or NUC with a monitor attached usually shows a login prompt nobody reads. This turns it into a
"glanceable" security and health board, built around one question that `htop` and Grafana don't answer:

> *Which of my services can actually be reached from where — and did that change since I last looked?*

- **Exposure by reach, not by bind address.** Every listener is classified as local / LAN / Tailscale / Internet (Funnel),
  then corrected by the real firewall: `ufw` rules, `DOCKER-USER`, and the two famous surprises — **Docker-published ports bypass ufw** and
  **`tailscale0` is accepted before ufw** — are detected and flagged.
- **Port alarms.** A baseline of the exposed ports is stored on install; a new, changed or vanished port raises a red banner until you accept it (`sudo nuc-console-accept`).
- **Honest about missing data.** Unreadable or missing sections show `?` and are treated as open, never as "OK". A tool that isn't installed is reported as such, not as an error.
- **Databases.** Finds postgres/redis/mysql/mongo/… containers, shows their *real* published ports and which containers/hosts actually connect (seen inside the container's network namespace, so Docker's DNAT can't hide external clients).
- **Map.** The sections above say *which* ports are open; the MAP follows each one to what is behind it — `LAN → :8080 → shop-web → shop-api → shop-db`
  reads "the LAN reaches the database through web and api", `LAN → :5432 → shop-db` reads "the database is open on the LAN". The IMPACT branch
  answers the other question: this is down, what depends on it? Links are *seen* (a live connection, inside each container's network namespace),
  *declared* (compose `depends_on`, a container named in another's environment, service dependencies) or *same network*, and drawn differently.
- **Least privilege.** A small root *collector* runs the privileged commands and writes JSON to `/run`; the *renderer* that owns the tty runs as an unprivileged user and only reads `/proc`, `/sys` and that JSON. No network listener unless you opt in to the read-only web view (macOS/Windows show the dashboard through it, on 127.0.0.1 only).
- **Adaptive layout.** One screen from 79×24 up to 4K consoles: 1 column → 2 (≥200 cols) → 3 (≥225 cols), dropping detail before dropping sections.

## Requirements

| | |
|---|---|
| OS | **Linux** with systemd (Debian/Ubuntu/Fedora/Arch… anything with `systemd`, `/proc`, `/sys`) · **macOS** 11 or newer · **Windows** 10/11 or Server 2019+ (64-bit x86 or ARM) |
| Python | 3.8 or newer, standard library only. Linux: the system's `python3`. macOS: a python.org or Command Line Tools Python, installed from python.org (hash-checked) if missing. Windows: a private copy of the official embeddable Python, shipped in the release ZIP (or downloaded once, hash-checked, and kept for the next update) |
| Root | only for the installers and the collector service (Windows: Administrator, the collector runs as SYSTEM) |
| Optional tools | Linux: `docker`, `ss` (iproute2), `ufw`, `iptables`, `fail2ban-client`, `tailscale`, `systemd-analyze`, `journalctl`, `nsenter`. macOS/Windows: Docker Desktop (or OrbStack), Tailscale. Each one that is missing simply disables its section — nothing crashes |
| Display | Linux: a virtual terminal. macOS/Windows: it opens at every login, your choice how — a normal browser window (default) or full screen (Alt+F4 / Cmd+Q closes it); text size with **A− / A+** |

## Quick start

```bash
git clone <this repository> nuc-console && cd nuc-console
python3 src/render.py --once --demo --cols 200 --rows 50    # try it: synthetic data, no root, no install
python3 -m unittest discover -s tests                        # optional: run the test-suite
sudo ./install.sh                                            # Linux: install + switch tty1 to the dashboard now
sudo ./install.sh                                            # macOS: the same command (launchd, full-screen browser at login)
```

Without git: every [release](https://github.com/give-jd/nuc-console-oss/releases/latest) has `nuc-console-X.Y.Z-linux.tar.gz`, `-macos.tar.gz`,
`-windows-x64.zip` and `-windows-arm64.zip`, built and attested by CI, with a `SHA256SUMS` file. Linux/macOS: extract, `sudo ./install.sh`.
Windows: extract the ZIP, double-click **`install-windows.cmd`** (it asks for administrator rights); the ZIP carries the Python it needs,
so it installs offline. Nothing is ever downloaded twice: the installers keep what they fetched and reuse it on the next install or update.
Preview a macOS or Windows screen anywhere with `--demo-os darwin` / `--demo-os windows`.

Full guide (VT choice, time zone, font, upgrade, uninstall, troubleshooting): **[docs/INSTALL.md](docs/INSTALL.md)**.

## Configuration

`/etc/nuc-console/config.ini` (created on first install, never overwritten). Everything defaults to *on*:

```ini
[features]
containers = yes      databases = yes     exposure = yes      firewall = yes
fail2ban   = yes      tailscale = yes     boot     = yes      docker_disk = yes
network_traffic = yes sessions  = yes     disks    = yes      thermal  = yes
webapps  = yes      map      = yes

[dashboard]
mode = overview       # overview (one screen, no keyboard) | rotate (3 pages, keys 1-3)
rotate_seconds = 15
refresh_seconds = 2   # redraw every 1-10 s: console, full-screen window and browser pages (- / + on the page)
# sections = attention, exposure, webapps, firewall, system, containers, databases, boot, network_traffic, sessions, tailscale, docker_disk, disks
#            ^ fixed on-screen order (this is the default, by priority); columns fill left to right, never back-filled
columns = 0           # 0 = real console size; set e.g. 235 if elements run off the screen
rows = 0              # e.g. 65 if the bottom lines are cut by the monitor
spacing = 1           # a blank line under each section title (0 = compact)
details = yes         # pages with everything the overview cuts ("… +N more"), rotating on the monitor
overview_seconds = 45
map_in_rotation = no  # yes: the MAP joins the rotating pages too (a monitor with no keyboard)

[webapps]             # apps you EXPECT to be reachable: shown as active or DOWN; not a Docker-bypass problem
ethibid = 8180, 8543

[web]                 # optional read-only web view, see docs/WEB.md
enabled = no
```

A disabled section is not drawn, raises no alarm, and — for the collector-side ones — **its commands are never run as root**.
Apply with `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web`. After an upgrade, `diff /etc/nuc-console/config.ini{,.dist}` shows the options added since you copied the file.
**Full reference of every key, default and command: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)** (commented example: [config/config.ini](config/config.ini)).

## ATTENTION: inventory, analysis, accepting

The ATTENTION list is generated from the current state, and every item has a stable id:

```bash
nuc-console-problems            # all current items with why it matters and how to fix it (add --json for scripts)
sudo nuc-console-accept --problem docker-bypass --reason "ethibid web app, exposed on purpose"
sudo nuc-console-accept --forget docker-bypass
```

Accepted items are dimmed ("N accepted" under ATTENTION) and no longer count in the header. The list lives in `/var/lib/nuc-console/accepted.json`; a missing or broken file accepts nothing.
For web apps you expose on purpose, declare them under `[webapps]` instead: they appear in WEB APPS and stop counting as "Docker port bypassing ufw".

## How it works

```
  root, systemd ─ collector.py ─ docker · ss · ufw · iptables · fail2ban · tailscale · systemd-analyze · journalctl · nsenter
                        │ JSON (0644, atomic rename)            /run/nuc-console/{containers,net,boot}.json
                        ▼
  unprivileged  ─ render.py ──── /proc · /sys · the JSON ──────► ANSI on /dev/tty1 (redraw in place, no flicker)
```

- `nuc-console.service` owns the VT (`TTYPath=/dev/tty1`), `Restart=always`. `install.sh` masks `getty@tty1` so nothing draws over it; login stays on **tty2** (Ctrl+Alt+F2) and SSH.
- **macOS / Windows**: the collector is a LaunchDaemon (root) / a scheduled task (SYSTEM) that reads sockets, the OS firewall and services
  with native tools (`lsof`, `socketfilterfw`, `launchctl` / the IP helper API and PowerShell) and judges every listening port against the
  firewall *per program*. The read-only web view (unprivileged) serves the same screen on **127.0.0.1 only**, and at every login it opens
  in a normal browser window (or full screen: `[display] mode = fullscreen`); a *nuc-console* shortcut reopens it. Same layout, same alarms.
- Exposure rules and the design principles behind the layout: [docs/DESIGN.md](docs/DESIGN.md). Hardening the things it reports: [docs/HARDENING.md](docs/HARDENING.md).

### Cost

Measured on a 14-thread x86 mini-PC: renderer (2 s refresh, the default; 240×67) **≈0.5 % of one core, ~14 MB RSS**; the root collector runs its commands every 10–30 s (< 0.5 s CPU in total). Per-container memory is read from cgroup files, not `docker stats` (2 s per call).

## Web view (optional)

Want the screen in a browser? `[web] enabled = yes`, then `tailscale serve --bg 8787`. Read-only, no JavaScript, binds to loopback unless you give it a token.
Setup and threat model: **[docs/WEB.md](docs/WEB.md)**. Config editing from the web is deliberately not offered.

## Security

It reads sensitive-looking facts (ports, container names) and runs privileged commands, so it is built defensively: fixed command lines (no shell, no user input), output sanitised against terminal-escape injection, secrets in container env are matched but **never stored or shown**, state files are world-readable on purpose (no secrets inside) and written atomically. The monitor itself shows your topology to anyone in the room — keep that in mind.
Read the threat model and how to report a vulnerability in **[SECURITY.md](SECURITY.md)**.

## Limitations

- Linux: the exposure logic knows `ufw`, `iptables`/`ts-input` and Docker; **nftables-native** or firewalld rulesets are not interpreted (shown as unknown `?`).
- macOS/Windows: no thermal sensors, fail2ban or "who connects" inside containers (Docker Desktop runs them in a VM); Windows Firewall rules from Group Policy, port keywords (RPC…) and macOS `pf` rules show as unknown `?`. Details: [docs/INSTALL.md](docs/INSTALL.md#macos).
- Non-systemd Linux (OpenRC, runit…) and the BSDs are not supported.
- Docker-published ports are assumed TCP; `tailscaled` ephemeral ports (≥32768 except 41641) are ignored; one NVMe sensor is read.
- Connections shorter than the 30 s sampling window are not seen by the database "who connects" view nor by the MAP (which remembers what it saw for 24 h).
- MAP on macOS/Windows: the containers' own connections are inside Docker Desktop's VM, so container-to-container links are *declared* or *same network* only.

## Contributing · License

See [CONTRIBUTING.md](CONTRIBUTING.md). Released under the [MIT License](LICENSE). Copyright © 2026 [Gi.Ve Group S.r.l.](https://givegroup.it)
