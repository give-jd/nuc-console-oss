# Installation guide

One command on each system; re-run it to upgrade, add `--uninstall` / `-Uninstall` to remove it.

| System | Command | The monitor shows |
|---|---|---|
| **Linux** (systemd) | `sudo ./install.sh` | the text console (a virtual terminal): sections 1–8 below |
| **macOS** 11+ | `sudo ./install.sh` (hands over to `install-macos.sh`) | your browser, or a full-screen window at login: [macOS](#macos) |
| **Windows** 10/11, Server 2019+ | double-click `install-windows.cmd` | your browser, or a full-screen window at login: [Windows](#windows) |

# Linux

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
2. copies the code to `/opt/nuc-console`, the units to `/etc/systemd/system`, the commands `nuc-console-accept` and `nuc-console-ai` to `/usr/local/sbin` and `nuc-console-problems` and `nuc-console-ask` to `/usr/local/bin` (the last two are for the optional AI model, step 7: nothing is downloaded or started);
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

Every option is described in [CONFIGURATION.md](CONFIGURATION.md). Each install also refreshes `/etc/nuc-console/config.ini.dist` (your `config.ini` is never touched): `diff /etc/nuc-console/config.ini{,.dist}` lists the options added by a newer version.

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

## 5. Port alarms and problems

`nuc-console-problems` (no root) lists every current ATTENTION item with advice; `sudo nuc-console-accept --problem <id> --reason "…"` accepts a known one. Declare web apps you expose on purpose under `[webapps]` in `config.ini`.

### Port baseline

The first install stores the set of ports reachable from outside as the *expected* state. Afterwards a new, changed
or vanished port shows a red banner. After an intended change: `sudo nuc-console-accept` (it refuses stale or partial data).

## 6. Console font and screen blanking (optional)

The default VT font is small on a full-HD monitor. On Debian/Ubuntu: `sudo dpkg-reconfigure console-setup` (choose Terminus 16×32),
or `setfont Lat15-TerminusBold32x16` for a one-off test. To let the monitor sleep, add `consoleblank=600` (seconds) to the kernel command line.

## 7. Optional: a local AI model

The AI screen (console key `a`) already tells you which local models this machine can run: RAM, GPU and GPU memory, one verdict per
model. To install one and let it explain the HEALTH findings and answer questions ([AI.md](AI.md) has the choice, the GPU notes and the security rules):

```bash
sudo nuc-console-ai models                    # the same table in a terminal
sudo nuc-console-ai setup                     # the recommended model (or: setup qwen3-8b); downloads once, SHA-256 checked
sudo nuc-console-ai serve --install-service   # a service on 127.0.0.1 only (or: nuc-console-ai serve, in the foreground)
# set [ai] enabled = yes in /etc/nuc-console/config.ini, then:
nuc-console-ask --advise
```

`install.sh` itself downloads nothing and starts no model server. `setup` needs the network once and refuses to download what this
release does not pin yet. The files go to `/var/lib/nuc-console/ai` (a model is 0.4 to 19 GB).

## 8. Update, uninstall

```bash
git pull && sudo ./install.sh          # update (keeps config.ini, baseline, VT and time zone)
sudo ./install.sh --uninstall          # restore the login on the terminal
```

Uninstall removes the commands and, if you installed it, the AI model service. It leaves `/etc/nuc-console`, `/var/lib/nuc-console` (baseline,
accepted problems, the HEALTH history `history.db`, and the AI runtime and models in `ai/`) and the `nuc-console` user; remove them by hand if you want. To
give the disk of the AI files back first: `sudo nuc-console-ai remove`.

## Troubleshooting (Linux)

| Symptom | Check |
|---|---|
| Black screen | `journalctl -u nuc-console -u nuc-console-collector -n 50`; the units restart forever, so look at the log, not `systemctl status` |
| "collector not running" banners | `systemctl status nuc-console-collector`; `ls -l /run/nuc-console/` |
| A section says "not installed on this machine" | the tool is missing (see table in step 1) |
| A section says "disabled in config.ini" | you turned it off in `[features]` |
| Login prompt still visible | `systemctl is-enabled getty@tty1` must say `masked`; the VT in `local.conf` must match the one on the monitor |
| Wrong layout | the real console size is logged: `journalctl -u nuc-console \| grep console` |
| Time is in UTC | reinstall with `sudo NUC_CONSOLE_TZ=Your/Zone ./install.sh` (a re-install keeps the zone you chose; 1.1.0 had a bug that dropped it: upgrade to 1.1.1 first) |

# macOS

macOS has no text console to take over, so the same screen is shown in a browser, from the read-only web view that the
installer runs on **127.0.0.1 only** (not reachable from the network). You choose how ([display] `mode`):

| `NUC_CONSOLE_DISPLAY=` | |
|---|---|
| `browser` (default) | at install and at every login it opens in a normal window of your browser (http://127.0.0.1:8787) |
| `fullscreen` (or `kiosk`) | at install and at every login it opens full screen, overview and Details pages taking turns. **Cmd+Q** closes it, **Ctrl+Cmd+F** leaves full screen |
| `none` | it never opens by itself (a Mac without a monitor: `nuc-console-problems`) |

**Applications › nuc-console** (or Spotlight) opens it again any time. A plain re-install keeps the mode you chose.

**Text size**: the **A− / A+** links at the bottom of the page; the default is `[display] zoom`. Bigger text = fewer columns, never less
content: in a browser window every section stays and the page scrolls; full screen rotates what does not fit onto Details pages.
**Refresh**: the **− / +** links next to "refresh every" in the same bar, 1 to 10 seconds; the default is `[dashboard] refresh_seconds` (2).

## Install

```bash
git clone <this repository> nuc-console && cd nuc-console
python3 src/render.py --once --demo --demo-os darwin --cols 200 --rows 50   # optional preview (needs a python3)
sudo ./install.sh                                                          # install, start, open the dashboard now
```

What it does (idempotent):

1. **Python**: uses a Python 3.8+ owned by the system (a python.org install, or Apple's with the Command Line Tools). If
   there is none it downloads the official python.org package (SHA-256 pinned, signature checked) and installs **only the
   framework**: no apps, no `/usr/local/bin` links, no shell profile changes. Homebrew's Python is never used: its files
   belong to a user, and the collector runs as root.
2. Copies the code to `/opt/nuc-console`, `config.ini` to `/etc/nuc-console` (only if missing), and creates
   `/var/lib/nuc-console` (baseline) and `/var/log/nuc-console` (logs, rotated by newsyslog).
3. Starts the collector as a **LaunchDaemon** (root): `lsof`, the Application Firewall, `pfctl`, `launchctl`, Docker, Tailscale.
   Docker and Tailscale are run **as the user who owns them** (or the user at the screen), never as root.
4. Starts the web view as the hidden user `_nuc-console`: on 127.0.0.1, or as configured in `[web]` if you enabled it there.
5. Adds `/Applications/nuc-console.webloc` and a **LaunchAgent** that opens the dashboard at every login, as the user: a normal
   window of the default browser (`browser`), or full screen (`fullscreen`: Chrome, Edge, Brave or Chromium if installed, else
   Safari: press Ctrl+Cmd+F once). It opens it right away for the user at the screen.
6. Stores the port baseline (only if missing).

The commands `nuc-console-problems`, `nuc-console-accept`, `nuc-console-ask` and `nuc-console-ai` are linked into `/usr/local/bin` and `/usr/local/sbin`
(only when root owns that folder; otherwise run them from `/opt/nuc-console/bin`). The last two are for the optional local AI model
([AI.md](AI.md): Metal uses the GPU of Apple silicon; `sudo nuc-console-ai models`, `setup`, `serve --install-service`); nothing is downloaded or started.

Choose the mode: `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh` (it is written to `config.ini`; without it the file decides).

## A Mac used as a wall screen

- *System Settings › Users & Groups › Automatically log in as…* (not available with FileVault on): the dashboard comes back after a power cut.
- *System Settings › Lock Screen*: never turn the display off; *Energy*: prevent sleep.
- `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh`. **Cmd+Q** closes the window until the next login; **Ctrl+Cmd+F** leaves full screen.

## Update, uninstall

```bash
git pull && sudo ./install.sh          # update (keeps config.ini and the baseline)
sudo ./install.sh --uninstall          # removes /opt/nuc-console and the launchd jobs
```

It also removes the AI model service, if you installed it. `/etc/nuc-console`, `/var/lib/nuc-console` (with the HEALTH history, `history.db`), `/var/log/nuc-console`, the `_nuc-console` user and the AI runtime and models (`/Library/Application Support/nuc-console/ai`) are left in place.

## What is different from Linux

| | |
|---|---|
| Firewall | the **Application Firewall** works per program: a port is *open* when its program is allowed (Apple's own programs are, by default), *blocked* when it is blocked, `?` when macOS would ask or decide on the signature. With the firewall off every listener is open to the LAN. `pf` rules of your own are not interpreted (`?`) |
| Docker | Docker Desktop / OrbStack: published ports belong to the Docker backend program, judged like any other; "who connects" inside the containers is not visible (they live in a VM) |
| Boot | failed launch daemons and the third-party ones; no boot time, slowest units or system log |
| CPU temperature | read by the collector (root) with Apple's `powermetrics`: the CPU die temperature on Intel Macs, the thermal pressure level (Nominal…) and the per-cluster frequencies on Apple Silicon. Apple Silicon has no temperature without a helper: install [smctemp](https://github.com/narugit/smctemp) or osx-cpu-temp and the collector uses it (run as the user who owns it, never as root) |
| Not available | throttling counters, load-based CPU frequency on Apple Silicon, fail2ban, ufw/iptables |

## Troubleshooting (macOS)

| Symptom | Check |
|---|---|
| "collector not running" | `sudo launchctl print system/com.nuc-console.collector`; `/var/log/nuc-console/collector.log` |
| The page does not open | `curl http://127.0.0.1:8787/healthz`; `sudo launchctl print system/com.nuc-console.web`; `/var/log/nuc-console/web.log` |
| Nothing opens at login | `[display] mode` is not `none`?; `launchctl print gui/$(id -u)/com.nuc-console.display`; `~/Library/Application Support/nuc-console/display.log` |
| Safari, not full screen | install Chrome/Edge, or set `[display] browser` to a browser's path |
| Docker or Tailscale "not installed" | the collector looks in `/usr/local/bin`, `/opt/homebrew/bin` and the apps in `/Applications`; someone must be logged in at the console |

# Windows

Windows has no text console to take over, so the same screen is shown in a browser, from the read-only web view that the
installer runs on **127.0.0.1 only** (not reachable from the network). You choose how ([display] `mode`):

| `install-windows.cmd -Display` | |
|---|---|
| `browser` (default) | at install and at every logon it opens in a normal window of your browser (http://127.0.0.1:8787) |
| `fullscreen` (or `kiosk`) | at install and at every logon it opens full screen in Edge, overview and Details pages taking turns. **Alt+F4** closes it, **F11** leaves full screen, Alt+Tab reaches the other windows |
| `none` | it never opens by itself (a machine without a monitor: `nuc-console-problems`) |

**Start › nuc-console** opens it again any time. A plain re-install (double-click) keeps the mode you chose.

**Text size**: the **A− / A+** links at the bottom of the page; the default is `[display] zoom`. Bigger text = fewer columns, never less
content: in a browser window every section stays and the page scrolls; full screen rotates what does not fit onto Details pages.
**Refresh**: the **− / +** links next to "refresh every" in the same bar, 1 to 10 seconds; the default is `[dashboard] refresh_seconds` (2).

## Install

1. Download the repository (*Code › Download ZIP*) and extract it, or `git clone` it.
2. Double-click **`install-windows.cmd`** and accept the administrator prompt.
   From an administrator prompt: `powershell -ExecutionPolicy Bypass -File install-windows.ps1`.

What it does (idempotent):

1. Puts a **private Python** (the official python.org *embeddable* build, SHA-256 pinned and checked for the Python
   Software Foundation's signature) and the code in `%ProgramFiles%\nuc-console`. Nothing else on the system uses it or is changed by it.
2. Creates `%ProgramData%\nuc-console` (`config.ini` only if missing, `run`, `lib`, `logs`): writable only by SYSTEM and
   Administrators, readable by users.
3. Registers scheduled tasks in the folder **`\nuc-console\`**: `collector` (SYSTEM, at startup, restarted if it stops),
   `web` (LOCAL SERVICE: on 127.0.0.1, or as configured in `[web]` if you enabled it there) and `display` (every user, at
   logon: opens the dashboard as that user, in the browser or full screen; not with `none`).
4. Adds **Start › nuc-console** and `%ProgramFiles%\nuc-console\bin` to the system PATH: `nuc-console-problems`,
   `nuc-console-accept` (administrator prompt) and, for the optional local AI model ([AI.md](AI.md)), `nuc-console-ai` (administrator
   prompt) and `nuc-console-ask`. Nothing is downloaded or started until you run them.
5. Waits for the first snapshot, stores the port baseline (only if missing) and opens the dashboard.

Options: `-Display browser|fullscreen|none` (written to `config.ini`; without it the file decides; `-NoDisplay` = `none`),
`-PythonZip <file>` (offline: the `python-3.14.8-embed-amd64.zip` you downloaded yourself, checked against the same hash).
Python is downloaded only the first time: a re-install reuses it.

## A PC used as a wall screen

- Automatic sign-in after a restart: Sysinternals **Autologon**; *Settings › Accounts › Sign-in options* to skip the lock screen.
- *Settings › System › Power*: never turn off the screen, never sleep.
- `install-windows.cmd -Display fullscreen`. **Alt+F4** closes the window until the next logon; **F11** leaves full screen;
  Alt+Tab reaches the other windows. Back to the browser only: `install-windows.cmd -Display browser`.

## Update, uninstall

Run `install-windows.cmd` again to update (it keeps `config.ini` and the baseline). `install-windows.cmd -Uninstall` removes
the tasks (the AI model's too, if you installed it), `%ProgramFiles%\nuc-console` and the PATH entry; `%ProgramData%\nuc-console` (config, state, the HEALTH history `lib\history.db` and the AI runtime and models in `ai\`) is left in place. An update keeps the AI model's task.

## What is different from Linux

| | |
|---|---|
| Firewall | **Windows Firewall** per network profile (Domain, Private, Public): for each listening port the collector (as SYSTEM, so it knows the program behind every socket) applies the enabled inbound rules: protocol, port, program, service; a block rule wins; `LocalSubnet` = open to the LAN. Port keywords (RPC…), rules bound to an interface or authenticated (IPsec) peers, and rules from Group Policy show as `?` (treated as open) |
| Docker | Docker Desktop: published ports belong to `com.docker.backend`, judged like any other program; "who connects" inside the containers is not visible (they live in a VM) |
| Boot | boot time (Diagnostics-Performance log), automatic services stopped with an error, System event log since boot |
| Sessions | console and Remote Desktop users; ssh and RDP clients |
| CPU temperature | Windows has no standard sensor API: the collector (SYSTEM) reads [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) or OpenHardwareMonitor when one of them is running (per-core temperatures), else the ACPI thermal zones (one value, often missing). Without them the CPU screen shows `?` |
| Not available | load average, throttling counters, slowest units, fail2ban, ufw/iptables |

## Troubleshooting (Windows)

| Symptom | Check |
|---|---|
| "collector not running" | Task Scheduler › `nuc-console` › `collector` (Last Run Result); `%ProgramData%\nuc-console\logs\collector.log` |
| The page does not open | task `web`; `%ProgramData%\nuc-console\logs\web.log`; http://127.0.0.1:8787/healthz must answer `ok` |
| Nothing opens at logon | `[display] mode` is not `none`?; task `display`; `%LOCALAPPDATA%\nuc-console\display.log`; full screen needs Edge (or `[display] browser`) |
| Docker "not installed" or "not responding" | Docker Desktop must be running (it runs in a user's session) |
| Many `?` in EXPOSURE | rules the evaluation cannot read with certainty: see the NOTE column; `Get-NetFirewallRule` shows them |

