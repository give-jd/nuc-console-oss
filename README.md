<div align="center">

# 🖥️ nuc-console

**Turn the forgotten monitor of your headless Linux box into a live security & health board.**<br>
No X11 · no browser · no dependencies · one screen · ~0.5 % of a CPU core

[![License: MIT](https://img.shields.io/badge/license-MIT-3fb950?style=flat-square)](LICENSE)
[![Release](https://img.shields.io/github/v/release/give-jd/nuc-console-oss?style=flat-square&color=58a6ff)](https://github.com/give-jd/nuc-console-oss/releases)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776ab?style=flat-square&logo=python&logoColor=white)](#requirements)
[![Linux + systemd](https://img.shields.io/badge/linux-systemd-fcc624?style=flat-square&logo=linux&logoColor=black)](#requirements)
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
| 🚨 **Port alarms** | a baseline of exposed ports; any new, changed or vanished port turns the banner red |
| 🐳 **Containers & databases** | per-stack health, real published ports, *who actually connects* to each DB (seen inside its network namespace) |
| 🌡️ **Health** | boot time and slowest units, failed units, journal errors, CPU/NVMe temperature, thermal throttling, disks, traffic |
| 🔒 **Least privilege** | small root collector + unprivileged renderer, stdlib only, no network listener |
| 🎛️ **Configurable** | switch every section on/off, single screen or rotating pages, pin the layout size |
| 🌍 **Read-only web view** | optional: the same screen in a browser over Tailscale/LAN ([docs/WEB.md](docs/WEB.md)); off by default, token or loopback only |
| 🧪 **Try it without root** | `python3 src/render.py --once --demo` |

<details>
<summary><b>Smaller consoles: the layout adapts</b> (120×33, single column)</summary>
<br>
<img src="docs/img/compact.svg" alt="nuc-console on a 120x33 console, single column, demo data" width="100%">
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
- **Least privilege.** A small root *collector* runs the privileged commands and writes JSON to `/run`; the *renderer* that owns the tty runs as an unprivileged user and only reads `/proc`, `/sys` and that JSON. No network listener unless you opt in to the read-only web view.
- **Adaptive layout.** One screen from 79×24 up to 4K consoles: 1 column → 2 (≥200 cols) → 3 (≥225 cols), dropping detail before dropping sections.

## Requirements

| | |
|---|---|
| OS | Linux with **systemd** (Debian/Ubuntu/Fedora/Arch… anything with `systemd`, `/proc`, `/sys`) |
| Python | 3.8 or newer (tests run on 3.8 – 3.13), standard library only |
| Root | only for `install.sh` and the collector service |
| Optional tools | `docker`, `ss` (iproute2), `ufw`, `iptables`, `fail2ban-client`, `tailscale`, `systemd-analyze`, `journalctl`, `nsenter`. Each one that is missing simply disables its section — nothing crashes |

## Quick start

```bash
git clone <this repository> nuc-console && cd nuc-console
python3 src/render.py --once --demo --cols 200 --rows 50    # try it: synthetic data, no root, no install
python3 -m unittest discover -s tests                        # optional: run the test-suite
sudo ./install.sh                                            # install + switch tty1 to the dashboard now
```

Full guide (VT choice, time zone, font, upgrade, uninstall, troubleshooting): **[docs/INSTALL.md](docs/INSTALL.md)**.

## Configuration

`/etc/nuc-console/config.ini` (created on first install, never overwritten). Everything defaults to *on*:

```ini
[features]
containers = yes      databases = yes     exposure = yes      firewall = yes
fail2ban   = yes      tailscale = yes     boot     = yes      docker_disk = yes
network_traffic = yes sessions  = yes     disks    = yes      thermal  = yes

[dashboard]
mode = overview       # overview (one screen, no keyboard) | rotate (3 pages, keys 1-3)
rotate_seconds = 15
# sections = attention, exposure, firewall, system, containers, databases, boot, ...   (fixed on-screen order)
columns = 0           # 0 = real console size; set e.g. 235 if elements run off the screen
rows = 0
```

A disabled section is not drawn, raises no alarm, and — for the collector-side ones — **its commands are never run as root**.
Apply with `sudo systemctl restart nuc-console nuc-console-collector`. Environment overrides: `NUC_CONSOLE_CONFIG`, `NUC_CONSOLE_MODE`.
Details in [config/config.ini](config/config.ini).

## How it works

```
  root, systemd ─ collector.py ─ docker · ss · ufw · iptables · fail2ban · tailscale · systemd-analyze · journalctl · nsenter
                        │ JSON (0644, atomic rename)            /run/nuc-console/{containers,net,boot}.json
                        ▼
  unprivileged  ─ render.py ──── /proc · /sys · the JSON ──────► ANSI on /dev/tty1 (redraw in place, no flicker)
```

- `nuc-console.service` owns the VT (`TTYPath=/dev/tty1`), `Restart=always`. `install.sh` masks `getty@tty1` so nothing draws over it; login stays on **tty2** (Ctrl+Alt+F2) and SSH.
- Exposure rules and the design principles behind the layout: [docs/DESIGN.md](docs/DESIGN.md). Hardening the things it reports: [docs/HARDENING.md](docs/HARDENING.md).

### Cost

Measured on a 14-thread x86 mini-PC: renderer (2 s refresh, 240×67) **≈0.5 % of one core, ~14 MB RSS**; the root collector runs its commands every 10–30 s (< 0.5 s CPU in total). Per-container memory is read from cgroup files, not `docker stats` (2 s per call).

## Web view (optional)

Want the screen in a browser? `[web] enabled = yes`, then `tailscale serve --bg 8787`. Read-only, no JavaScript, binds to loopback unless you give it a token.
Setup and threat model: **[docs/WEB.md](docs/WEB.md)**. Config editing from the web is deliberately not offered.

## Security

It reads sensitive-looking facts (ports, container names) and runs privileged commands, so it is built defensively: fixed command lines (no shell, no user input), output sanitised against terminal-escape injection, secrets in container env are matched but **never stored or shown**, state files are world-readable on purpose (no secrets inside) and written atomically. The monitor itself shows your topology to anyone in the room — keep that in mind.
Read the threat model and how to report a vulnerability in **[SECURITY.md](SECURITY.md)**.

## Limitations

- Linux + systemd only. The exposure logic knows `ufw`, `iptables`/`ts-input` and Docker; **nftables-native** or firewalld rulesets are not interpreted (shown as unknown `?`).
- Docker-published ports are assumed TCP; `tailscaled` ephemeral ports (≥32768 except 41641) are ignored; one NVMe sensor is read.
- Connections shorter than the 30 s sampling window are not seen by the database "who connects" view.

## Contributing · License

See [CONTRIBUTING.md](CONTRIBUTING.md). Released under the [MIT License](LICENSE).
