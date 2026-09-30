# nuc-console

**A full-screen status dashboard for the physical monitor of a headless Linux box — no X11, no browser, no dependencies.**

It replaces the login prompt on `tty1` with a live one-screen summary: what is listening and *who can reach it*
(this machine / LAN / Tailscale / Internet), firewall state, containers, databases and who talks to them, boot
health, temperatures and throttling, disks, network traffic. Plain Python 3 standard library, ANSI text, ~0.5 % of one core.

```
 demo-host │ Overview │ 19:06:32                                                       ✖ 5 PROBLEMS
── ATTENTION ────────────────────────────────────────────────────────────────────────────────
   ✖ 1 DB/broker open on LAN
   ! 1 container exited with an error
   ! 2 Docker ports bypassing ufw (DOCKER-USER empty)
   ! 1 service public on the Internet (Funnel :8444)
── EXPOSURE ─────────────────────────────────────────────────────────────────────────────────
 Internet 1   LAN 3   tailnet only 0   local only 4   ⚠ 1 DB/broker on LAN
 ● 8444/t funnel /webhook → 127.0.0.1:5678/webhook  public on the Internet
 ⚠5432 shop-db-1  ·  8080 shop-web-1  ·  22 sshd
── FIREWALL ─────────────────────────────────────────────────────────────────────────────────
   ✔ ufw active
   ! DOCKER-USER empty: ports published by containers bypass ufw
   INPUT DROP · FORWARD DROP   ts-input ✔   fail2ban sshd:2   drop 1h 41
```
<sub>Synthetic data from `--demo`.</sub>

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
- **Least privilege.** A small root *collector* runs the privileged commands and writes JSON to `/run`; the *renderer* that owns the tty runs as an unprivileged user and only reads `/proc`, `/sys` and that JSON. No network listener anywhere.
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

## Security

It reads sensitive-looking facts (ports, container names) and runs privileged commands, so it is built defensively: fixed command lines (no shell, no user input), output sanitised against terminal-escape injection, secrets in container env are matched but **never stored or shown**, state files are world-readable on purpose (no secrets inside) and written atomically. The monitor itself shows your topology to anyone in the room — keep that in mind.
Read the threat model and how to report a vulnerability in **[SECURITY.md](SECURITY.md)**.

## Limitations

- Linux + systemd only. The exposure logic knows `ufw`, `iptables`/`ts-input` and Docker; **nftables-native** or firewalld rulesets are not interpreted (shown as unknown `?`).
- Docker-published ports are assumed TCP; `tailscaled` ephemeral ports (≥32768 except 41641) are ignored; one NVMe sensor is read.
- Connections shorter than the 30 s sampling window are not seen by the database "who connects" view.

## Contributing · License

See [CONTRIBUTING.md](CONTRIBUTING.md). Released under the [MIT License](LICENSE).
