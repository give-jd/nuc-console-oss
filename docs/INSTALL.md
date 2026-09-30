# Installation guide

## 1. Check the prerequisites

```bash
python3 --version          # 3.8 or newer
systemctl --version        # systemd is required
ls /proc /sys >/dev/null && echo ok
```

The dashboard draws on a Linux **virtual terminal** (the text console of the physical monitor). It works on a headless
box with a monitor attached (mini-PC, NUC, Raspberry Pi with HDMI, a server with a KVM…). It does **not** work over SSH
(use `--once`/`--demo` there to preview).

Optional tools — install only what you want to see; every missing one just disables its section:

| Section | Needs |
|---|---|
| exposure, databases, sessions | `ss` (package `iproute2`) |
| containers, databases, docker_disk | `docker`, plus `nsenter` (`util-linux`) for "who connects to the DB" |
| firewall | `ufw` and/or `iptables` |
| fail2ban | `fail2ban-client` |
| tailscale | `tailscale` |
| boot | `systemd-analyze`, `journalctl` |

## 2. Preview without installing (no root)

```bash
git clone <this repository> nuc-console && cd nuc-console
python3 src/render.py --once --demo --cols 200 --rows 50      # synthetic data
python3 src/render.py --once --cols 120 --rows 33              # your machine (sections that need root show "collector not running")
python3 -m unittest discover -s tests                          # test-suite
```

## 3. Install

**Run it from SSH or another terminal, not from the console it will take over.**

```bash
sudo ./install.sh
```

What it does (all idempotent, re-run it to upgrade):

1. creates the system user `nuc-console` (no shell, no home);
2. copies the code to `/opt/nuc-console`, the units to `/etc/systemd/system`, the helper `nuc-console-accept` to `/usr/local/sbin`;
3. writes `/etc/nuc-console/config.ini` **only if it does not exist**;
4. starts the root collector, **masks `getty@tty<N>`** and starts the dashboard on that terminal;
5. waits for the first fresh collector snapshot and stores the **port baseline** (only if none exists).

Options (environment variables; a re-install without options keeps the previous choice):

| Variable | Default | Meaning |
|---|---|---|
| `NUC_CONSOLE_VT` | `1` | virtual terminal to draw on. On machines with a desktop use a free one (e.g. `3`: GDM uses 1 and 2) |
| `NUC_CONSOLE_TZ` | system zone | time zone of the clock, e.g. `Europe/Rome` |

```bash
sudo NUC_CONSOLE_VT=3 NUC_CONSOLE_TZ=Europe/Berlin ./install.sh
```

The login prompt of that terminal disappears while the service runs. Use **Ctrl+Alt+F2** (another VT) or SSH to log in.

## 4. Configure

Edit `/etc/nuc-console/config.ini` (see [config/config.ini](../config/config.ini) for every key), then:

```bash
sudo systemctl restart nuc-console nuc-console-collector
```

Examples — a machine without Docker and without Tailscale, single screen, no thermal info:

```ini
[features]
containers = no
databases = no
docker_disk = no
tailscale = no
thermal = no
```

## 5. Port alarms

The first install stores the set of ports reachable from outside as the *expected* state. Afterwards a new, changed
or vanished port shows a red banner. After an intended change: `sudo nuc-console-accept` (it refuses stale or partial data).

## 6. Console font and screen blanking (optional)

The default VT font is small on a full-HD monitor. On Debian/Ubuntu: `sudo dpkg-reconfigure console-setup` (choose Terminus 16×32),
or `setfont Lat15-TerminusBold32x16` for a one-off test. To let the monitor sleep, add `consoleblank=600` (seconds) to the kernel command line.

## 7. Update, uninstall

```bash
git pull && sudo ./install.sh          # update (keeps config.ini, baseline, VT and time zone)
sudo ./install.sh --uninstall          # restore the login on the terminal
```

Uninstall leaves `/etc/nuc-console`, `/var/lib/nuc-console` and the `nuc-console` user; remove them by hand if you want.

## Troubleshooting

| Symptom | Check |
|---|---|
| Black screen | `journalctl -u nuc-console -u nuc-console-collector -n 50`; the units restart forever, so look at the log, not `systemctl status` |
| "collector not running" banners | `systemctl status nuc-console-collector`; `ls -l /run/nuc-console/` |
| A section says "not installed on this machine" | the tool is missing (see table in step 1) |
| A section says "disabled in config.ini" | you turned it off in `[features]` |
| Login prompt still visible | `systemctl is-enabled getty@tty1` must say `masked`; the VT in `local.conf` must match the one on the monitor |
| Wrong layout | the real console size is logged: `journalctl -u nuc-console \| grep console` |
| Time is in UTC | reinstall with `NUC_CONSOLE_TZ=Your/Zone` |
