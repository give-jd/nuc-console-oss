# Installation guide

Download the archive of your system and processor, check it, extract it and run one command. **The archive has everything it needs, Python
included: no Python on the machine and no network are needed.** To upgrade, run `nuc-console-update` (or the same command again); to
remove it, add `--uninstall` / `-Uninstall`. To try it without installing anything: [PORTABLE.md](PORTABLE.md).

| System | Command | The monitor shows |
|---|---|---|
| **Linux** (systemd) | `sudo ./install.sh` | the text console (a virtual terminal): sections 1–8 below |
| **macOS** 11+ | `sudo ./install.sh` (hands over to `install-macos.sh`) | your browser, or a full-screen window at login: [macOS](#macos) |
| **Windows** 10/11, Server 2019+ | double-click `install-windows.cmd` | your browser, or a full-screen window at login: [Windows](#windows) |

[Download a release](#download-a-release) · [Linux](#linux) · [macOS](#macos) · [Windows](#windows) · [Update](#update)

# Download a release

Every [release](https://github.com/give-jd/nuc-console-oss/releases/latest) has one archive per system **and processor** and a `SHA256SUMS`
file. The release workflow builds them from the tag, not on anyone's machine, and signs a build provenance for each one
([how, and what the checks prove](../SECURITY.md#verifying-a-release)).

## Which file

`X.Y.Z` is the release number. Pick the row of your system and of your processor (`uname -m` on Linux and macOS: `x86_64` is Intel/AMD,
`aarch64` or `arm64` is ARM):

| Your system | Download | The Python inside |
|---|---|---|
| Linux, Intel/AMD 64-bit (`x86_64`) | `nuc-console-X.Y.Z-linux-x86_64.tar.gz` | `python/`: a CPython from python-build-standalone, already unpacked |
| Linux, ARM 64-bit (`aarch64`: Raspberry Pi 4/5 with a 64-bit system, ARM servers) | `nuc-console-X.Y.Z-linux-arm64.tar.gz` | the same, for ARM |
| macOS, Apple Silicon (M1 and later) | `nuc-console-X.Y.Z-macos-arm64.tar.gz` | the same, for Apple silicon |
| macOS, Intel | `nuc-console-X.Y.Z-macos-x86_64.tar.gz` | the same, for Intel |
| Windows 10/11, Server 2019+, Intel or AMD (64-bit) | `nuc-console-X.Y.Z-windows-x64.zip` | `python\`: the official embeddable Python, unpacked at the first run or install |
| Windows on ARM | `nuc-console-X.Y.Z-windows-arm64.zip` | the same, with the ARM64 Python |
| every system | `SHA256SUMS` | the SHA-256 of each archive |

Windows: *Settings › System › About › System type* says which one you have. 32-bit Windows is not supported, nor 32-bit Linux or ARM
(`armv7l`, a 32-bit Raspberry Pi OS): there is no archive for them, and the updater says so. The Linux archives are for glibc systems (Debian,
Ubuntu, Fedora, Arch, Raspberry Pi OS 64-bit...); on Alpine (musl) use a clone and the system's `python3`. An archive of the wrong processor does not run: `run.sh`
and the installers say `the Python in python/ does not run here` and name the archive to take.

**Everything is in the archive.** One folder, `nuc-console-X.Y.Z/`, holds what is needed to install and to run it: `src/`, `bin/`, `config/`, the service
files of that system (`systemd/` or `launchd/`), `scripts/` (Linux and macOS), the installer and the portable launcher of that system, `docs/`,
`README.md`, `LICENSE`, `SECURITY.md`, and **the Python it runs with** (`python/`). So it works on a machine that has no Python and no Internet
access: download it elsewhere, copy it over, extract, run. It does not hold the tests or the development tools: `git clone` gives those too
(a clone has no Python of its own: Linux and macOS use the machine's `python3`, the Windows installer downloads one once, see [Windows](#windows)).
Where the Python comes from and how it is checked: [SECURITY.md](../SECURITY.md#the-python-in-the-archives).

## Check it

Do this before you extract or run anything, in the folder where you saved the files.

```bash
sha256sum --ignore-missing -c SHA256SUMS                                          # Linux: checks every archive that is here
grep ' nuc-console-X.Y.Z-macos-arm64.tar.gz$' SHA256SUMS | shasum -a 256 -c -   # macOS: the line of your archive
gh attestation verify nuc-console-X.Y.Z-linux-x86_64.tar.gz --repo give-jd/nuc-console-oss
```

```powershell
Get-FileHash -Algorithm SHA256 .\nuc-console-X.Y.Z-windows-x64.zip           # the same hash as its line in SHA256SUMS (case does not matter)
gh attestation verify .\nuc-console-X.Y.Z-windows-x64.zip --repo give-jd/nuc-console-oss
```

- `SHA256SUMS` says the file is whole and is the one listed. It comes from the same release page, so it does not protect against a
  release that someone else published.
- `gh attestation verify` (the [GitHub CLI](https://cli.github.com/), logged in with `gh auth login`) says the file was built by this repository's release
  workflow, from a tag of this repository. That is what protects against a swapped archive. Neither check is a signature by a person, and nothing in
  the archives is code-signed (Windows may ask you to confirm a file that comes from the Internet).

## Install from the archive

Linux and macOS, from SSH or another terminal (Linux: not from the console the dashboard will take over):

```bash
tar xzf nuc-console-X.Y.Z-linux-x86_64.tar.gz    # your file: -linux-arm64, -macos-arm64 or -macos-x86_64
cd nuc-console-X.Y.Z
sudo ./install.sh
```

The archive's Python is used like this. **Linux**: the system's `/usr/bin/python3` when it is 3.8 or newer (updated by your distribution), else the one in
`python/`, which `install.sh` copies to `/opt/nuc-console/python` (owned by root, readable by the service users) and which the systemd units and the commands
then run. **macOS**: the one in `python/` is always used, copied to `/opt/nuc-console/python`; nothing is downloaded. Re-running the installer, or an update,
replaces it with the one of the new release; it is removed with the rest by `--uninstall`.

Windows: extract the ZIP (right-click › *Extract All*, or `Expand-Archive nuc-console-X.Y.Z-windows-x64.zip .`), double-click **`install-windows.cmd`**
and accept the administrator prompt. The ZIP carries the Python the installer needs, in `python\` next to it, so **the install downloads nothing
and works on a PC with no Internet access**: the installer checks that file against the SHA-256 it pins, and python.exe against the Python Software Foundation's
signature, before it uses it.

**No installer of an archive downloads anything**, on any system. Only a clone (no `python/`) can: the macOS installer then downloads the official python.org
package if the Mac has no Python 3.8+ of its own (hash and signature checked), and the Windows installer downloads its Python once. What each installer
does, its options, and what it keeps for the next time: [Linux](#linux), [macOS](#macos), [Windows](#windows). The extracted folder is not used after the
install: delete it, or keep it to run `./run.sh` / `run.cmd` without installing ([PORTABLE.md](PORTABLE.md)).

macOS may refuse to start programs that come from a browser download (they are "quarantined"). If the installer says `the Python in python/ does not run on
this Mac`, take the archive that matches `uname -m` and run `xattr -dr com.apple.quarantine nuc-console-X.Y.Z` on the extracted folder; extracting
with `tar` in a terminal does not mark the files.

# Linux

## 1. Check the prerequisites

```bash
python3 --version          # 3.8 or newer: only needed from a clone; the release archive carries its own (a system older than that uses it)
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

From the extracted [archive](#download-a-release), or from a clone:

```bash
git clone <this repository> nuc-console && cd nuc-console     # a clone; an archive is extracted instead
python3 src/render.py --once --demo --cols 200 --rows 50      # synthetic data
python3 src/render.py --once --cols 120 --rows 33              # your machine (sections that need root show "collector not running")
python3 -m unittest discover -s tests                          # test-suite (a clone: the archives do not hold the tests)
```

`./run.sh` runs the whole dashboard from that folder without installing it: [PORTABLE.md](PORTABLE.md).

## 3. Install

**Run it from SSH or another terminal, not from the console it will take over.**

```bash
sudo ./install.sh          # in the folder of the extracted archive (or of the clone)
```

What it does (all idempotent, re-run it to upgrade):

1. creates the system user `nuc-console` (no shell, no home);
2. picks the Python: `/usr/bin/python3` if it is 3.8 or newer, else the one in `python/` of the release archive (a clone has none: it then stops and
   says to install `python3`), which is copied to `/opt/nuc-console/python` (root-owned) and which the units and the commands then run;
3. copies the code to `/opt/nuc-console`, the units to `/etc/systemd/system`, the commands `nuc-console-accept`, `nuc-console-update` and `nuc-console-ai` to `/usr/local/sbin` and `nuc-console-problems` and `nuc-console-ask` to `/usr/local/bin` (the last two are for the optional AI model, step 7: nothing is downloaded or started) and gives the folder `/var/lib/nuc-console/ai` to the `nuc-console` account, so that the AI page and the AI screen can set a model up (step 7);
4. writes `/etc/nuc-console/config.ini` **only if it does not exist**;
5. starts the root collector, **masks `getty@tty<N>`** and starts the dashboard on that terminal;
6. waits for the first fresh collector snapshot and stores the **port baseline** (only if none exists).

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
Want them on your phone? The installer also sets up the optional Telegram notifier (`nuc-console-notify.service`, user `nuc-console-notify`, idle until you run `sudo nuc-console-telegram --setup`): [TELEGRAM.md](TELEGRAM.md).

### Port baseline

The first install stores the set of ports reachable from outside as the *expected* state. Afterwards a new, changed
or vanished port shows a red banner. After an intended change: `sudo nuc-console-accept` (it refuses stale or partial data).

## 6. Console font and screen blanking (optional)

The default VT font is small on a full-HD monitor. On Debian/Ubuntu: `sudo dpkg-reconfigure console-setup` (choose Terminus 16×32),
or `setfont Lat15-TerminusBold32x16` for a one-off test. To let the monitor sleep, add `consoleblank=600` (seconds) to the kernel command line.

## 7. Optional: a local AI model

The AI screen (console key `5`) and the **AI** page of the web view (`http://127.0.0.1:8787/?view=ai`, the **ai** link at the bottom) already tell
you which local models this machine can run: RAM, GPU and GPU memory, one verdict per model. They are also where you set one up and let it
explain the HEALTH findings and answer questions ([AI.md](AI.md) has the choice, the GPU notes and the security rules). **You only choose a model**:
press **use this model** on the page (or `Enter` then `u` on the screen); the model server (Ollama, the build of this system, SHA-256 checked) and the model
are downloaded with a progress bar, the server is started on 127.0.0.1 and the AI is turned on. The **AI on / off** button at the top (console key `e`) starts and stops it, and
the chat below it answers as soon as the server does. The files go to the folder the page shows (`/var/lib/nuc-console/ai`, a model is 0.5 to 19 GB).

The same from a terminal (as before; `nuc-console-ai` and `nuc-console-ask` are unchanged):

```bash
nuc-console-ai models                         # the same table in a terminal; no root (/usr/local/sbin/nuc-console-ai if your PATH lacks sbin)
sudo nuc-console-ai setup                     # the recommended model (or: setup qwen3-8b qwen3-4b); downloads once, SHA-256 checked
sudo nuc-console-ai serve --install-service   # a service on 127.0.0.1 only, on the GPU when one holds the model (or: nuc-console-ai serve, in the foreground)
# set [ai] enabled = yes in /etc/nuc-console/config.ini, then:
nuc-console-ask advise
```

`install.sh` itself downloads nothing and starts no model server; the buttons and `setup` need the network once and download only what this release
pins (the server's build of each system and the models' names). The service serves every installed model: `sudo nuc-console-ai use qwen3-4b` switches the
advisor to another one without a restart; after changing `[ai] gpu`, run `sudo nuc-console-ai serve --install-service` again: the service keeps what it was
installed with. On Linux the server's build is a `.tar.zst`: Python 3.14 reads it, an older Python needs the `zstd` tool (`apt install zstd`). On a machine with a GPU the unit it writes lets the service see the GPU (`PrivateDevices=no`, the `render` and `video` groups).

What the installer changes for the buttons: `/var/lib/nuc-console/ai` belongs to `nuc-console` (the account of the web view and of the console), both units may
write there (`ReadWritePaths=-/var/lib/nuc-console/ai`), and the web unit has `TasksMax=512` and `MemoryMax=85%` because the model server it starts lives in its
cgroup and stops with it (it runs on the CPU: the web unit has no GPU access; for the GPU use `serve --install-service`). Nothing else of the web view becomes writable. To make the page and the screen read-only again, set `[ai] web_actions = no`
in `/etc/nuc-console/config.ini` (then they show the command to run instead). A model server the buttons started stops when the web view
or the console stops; the one `serve --install-service` installs is a separate service and stays.

## 8. Update, uninstall

```bash
sudo nuc-console-update                # update to the latest release (see Update below; keeps config.ini, baseline, VT and time zone)
git pull && sudo ./install.sh          # update a clone
sudo ./install.sh --uninstall          # restore the login on the terminal
```

Uninstall removes the commands and, if you installed it, the AI model service. It leaves `/etc/nuc-console`, `/var/lib/nuc-console` (baseline,
accepted problems, the HEALTH history `history.db`, and the AI runtime and models in `ai/`), the `nuc-console` and `nuc-console-notify` users and the `nuc-console-ai` account with its
state `/var/lib/nuc-console-ai` and, if you used `nuc-console-update`, its download cache
`/var/cache/nuc-console`; remove them by hand if you want. To give the disk of the AI files back first: `sudo nuc-console-ai remove`. It does delete `/var/lib/nuc-console-notify` (the Telegram notifier's token and paired chat).

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

macOS has no text console to take over, so the same screen is shown in a browser, from the web view that the
installer runs on **127.0.0.1 only** (not reachable from the network; read-only, except the buttons of the AI page, see [AI.md](AI.md)). You choose how ([display] `mode`):

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

From the extracted [archive](#download-a-release) (`nuc-console-X.Y.Z-macos-arm64.tar.gz` or `-macos-x86_64.tar.gz`) or from a clone:

```bash
git clone <this repository> nuc-console && cd nuc-console                  # a clone; an archive is extracted instead
python3 src/render.py --once --demo --demo-os darwin --cols 200 --rows 50   # optional preview (needs a python3)
sudo ./install.sh                                                          # install, start, open the dashboard now
```

`./run.sh` runs the dashboard from that folder without installing it: [PORTABLE.md](PORTABLE.md).

What it does (idempotent):

1. **Python**: from a release archive, the Python it carries (`python/`, a python-build-standalone CPython for this Mac's processor) is copied to
   `/opt/nuc-console/python`, owned by root (nobody else can write it), checked there and used: **nothing is downloaded** and the Mac needs no Python. From a
   clone (no `python/`) it uses a Python 3.8+ owned by the system (a python.org install, or Apple's with the Command Line Tools). If
   there is none it downloads the official python.org package (SHA-256 pinned, signature checked) and installs **only the
   framework**: no apps, no `/usr/local/bin` links, no shell profile changes. Homebrew's Python is never used: its files
   belong to a user, and the collector runs as root. The package is kept in `/Library/Caches/nuc-console` (root-owned) and reused
   by the next install if its hash and signature still check: it is never downloaded twice. It is downloaded to a temporary name
   there and gets its real name only after both checks.
2. Copies the code to `/opt/nuc-console`, `config.ini` to `/etc/nuc-console` (only if missing), and creates
   `/var/lib/nuc-console` (baseline) and `/var/log/nuc-console` (logs, rotated by newsyslog). Links `nuc-console-problems`,
   `nuc-console-accept` and `nuc-console-update` into `/usr/local/bin` and `/usr/local/sbin`, but only into folders root owns (else it says to run
   `/opt/nuc-console/bin/<command>` by its full path).
3. Starts the collector as a **LaunchDaemon** (root): `lsof`, the Application Firewall, `pfctl`, `launchctl`, Docker, Tailscale.
   Docker and Tailscale are run **as the user who owns them** (or the user at the screen), never as root.
4. Starts the web view as the hidden user `_nuc-console`: on 127.0.0.1, or as configured in `[web]` if you enabled it there.
   The optional Telegram notifier (`com.nuc-console.notify`, the same user, outbound only) is loaded too and idles until you run
   `sudo nuc-console-telegram --setup` ([TELEGRAM.md](TELEGRAM.md)); its token lives in `/var/lib/nuc-console-notify` (0711).
5. Adds `/Applications/nuc-console.webloc` and a **LaunchAgent** that opens the dashboard at every login, as the user: a normal
   window of the default browser (`browser`), or full screen (`fullscreen`: Chrome, Edge, Brave or Chromium if installed, else
   Safari: press Ctrl+Cmd+F once). It opens it right away for the user at the screen.
6. Stores the port baseline (only if missing).

The commands `nuc-console-problems`, `nuc-console-accept`, `nuc-console-ask` and `nuc-console-ai` are linked into `/usr/local/bin` and `/usr/local/sbin`
(only when root owns that folder; otherwise run them from `/opt/nuc-console/bin`). The last two are for the optional local AI model
([AI.md](AI.md): Metal uses the GPU of Apple silicon; `nuc-console-ai models` (no root), `sudo nuc-console-ai setup`, `serve --install-service`); nothing is downloaded or started.
The AI page of the web view and the AI screen set a model up with one choice (**use this model**; AI on / off; a chat): the installer makes
`/Library/Application Support/nuc-console/ai` the property of `_nuc-console` for them, and `[ai] web_actions = no` makes them read-only again.

Choose the mode: `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh` (it is written to `config.ini`; without it the file decides).

## A Mac used as a wall screen

- *System Settings › Users & Groups › Automatically log in as…* (not available with FileVault on): the dashboard comes back after a power cut.
- *System Settings › Lock Screen*: never turn the display off; *Energy*: prevent sleep.
- `sudo NUC_CONSOLE_DISPLAY=fullscreen ./install.sh`. **Cmd+Q** closes the window until the next login; **Ctrl+Cmd+F** leaves full screen.

## Update, uninstall

```bash
sudo nuc-console-update                # update to the latest release (see Update below; keeps config.ini, the baseline and the display mode)
git pull && sudo ./install.sh          # update a clone; or run install.sh from the new release archive
sudo ./install.sh --uninstall          # removes /opt/nuc-console, the launchd jobs and the three commands
```

It also removes the AI model service, if you installed it. `/etc/nuc-console`, `/var/lib/nuc-console` (with the HEALTH history, `history.db`), `/var/log/nuc-console`, the download cache `/Library/Caches/nuc-console`, the `_nuc-console` user, the `_nuc-console-ai` account and the AI runtime and models (`/Library/Application Support/nuc-console/ai`) are left in place; `/var/lib/nuc-console-notify` (the Telegram token) is deleted.

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

Windows has no text console to take over, so the same screen is shown in a browser, from the web view that the
installer runs on **127.0.0.1 only** (not reachable from the network; read-only, except the buttons of the AI page, see [AI.md](AI.md)). You choose how ([display] `mode`):

| `install-windows.cmd -Display` | |
|---|---|
| `browser` (default) | at install and at every logon it opens in a normal window of your browser (http://127.0.0.1:8787) |
| `fullscreen` (or `kiosk`) | at install and at every logon it opens full screen in Edge (or Chrome; Firefox opens a window to take full screen with **F11**; with none of them, your default browser), overview and Details pages taking turns. **Alt+F4** closes it, **F11** leaves full screen, Alt+Tab reaches the other windows |
| `none` | it never opens by itself (a machine without a monitor: `nuc-console-problems`) |

**Start › nuc-console** opens it again any time. A plain re-install (double-click) keeps the mode you chose.

**Text size**: the **A− / A+** links at the bottom of the page; the default is `[display] zoom`. Bigger text = fewer columns, never less
content: in a browser window every section stays and the page scrolls; full screen rotates what does not fit onto Details pages.
**Refresh**: the **− / +** links next to "refresh every" in the same bar, 1 to 10 seconds; the default is `[dashboard] refresh_seconds` (2).

## Install

1. Download `nuc-console-X.Y.Z-windows-x64.zip` (`-windows-arm64.zip` on a Windows on ARM PC) from the
   [latest release](https://github.com/give-jd/nuc-console-oss/releases/latest), [check it](#check-it) and extract it. The ZIP carries Python: the
   install needs no Internet access. (Or *Code › Download ZIP* / `git clone`: the installer then downloads Python once.)
2. Double-click **`install-windows.cmd`** and accept the administrator prompt.
   From an administrator prompt: `powershell -ExecutionPolicy Bypass -File install-windows.ps1`.

What it does (idempotent):

1. Puts a **private Python** (the official python.org *embeddable* build, SHA-256 pinned and checked for the Python
   Software Foundation's signature) and the code in `%ProgramFiles%\nuc-console`. Nothing else on the system uses it or is changed by it.
   The Python zip is taken from `python\` next to the installer (the release ZIP ships it there), else from the cache
   `%ProgramData%\nuc-console\cache`, else downloaded from python.org into that cache: see the options below.
2. Creates `%ProgramData%\nuc-console` (`config.ini` only if missing, `run`, `lib`, `logs`, `cache`): writable only by SYSTEM and
   Administrators, readable by users.
3. Registers scheduled tasks in the folder **`\nuc-console\`**: `collector` (SYSTEM, at startup, restarted if it stops),
   `web` (LOCAL SERVICE: on 127.0.0.1, or as configured in `[web]` if you enabled it there) and `display` (every user, at
   logon: opens the dashboard as that user, in the browser or full screen; not with `none`). The optional Telegram notifier is a
   task too, `notify` (NETWORK SERVICE, outbound only): it idles until you run `nuc-console-telegram.cmd --setup` ([TELEGRAM.md](TELEGRAM.md));
   its token lives in `%ProgramData%\nuc-console\notify\private`, which only SYSTEM, Administrators and NETWORK SERVICE can open.
4. Adds **Start › nuc-console** and `%ProgramFiles%\nuc-console\bin` to the system PATH: `nuc-console-problems`,
   `nuc-console-accept` (administrator prompt), `nuc-console-update` and, for the optional local AI model ([AI.md](AI.md)), `nuc-console-ai` (administrator
   prompt to install, switch or serve; `models` and `status` need none) and `nuc-console-ask`. Nothing is downloaded or started until you run them or
   choose a model on the AI page of the web view (**use this model**; LOCAL SERVICE may modify `ai\` (and its log folder), nothing else of the data;
   `[ai] web_actions = no` makes the page read-only).
5. Waits for the first snapshot, stores the port baseline (only if missing) and opens the dashboard.

Options: `-Display browser|fullscreen|none` (written to `config.ini`; without it the file decides; `-NoDisplay` = `none`),
`-PythonZip <file>` (the `python-3.14.8-embed-amd64.zip` you downloaded yourself, checked against the same hash; it wins over everything below).

Python is never downloaded twice. The installer looks for the embeddable zip, with the SHA-256 it pins, in this order, and takes
the first one that matches: `python\` next to `install-windows.ps1`, then the folder of the script itself, then the cache
`%ProgramData%\nuc-console\cache` (same access rules as `%ProgramData%\nuc-console`: only SYSTEM and Administrators write there).
Only if none has it, it downloads it from python.org to a temporary name in the cache and gives it its real name after the hash check,
so the next install or update finds it. An already installed, unchanged Python is not touched at all.

## A PC used as a wall screen

- Automatic sign-in after a restart: Sysinternals **Autologon**; *Settings › Accounts › Sign-in options* to skip the lock screen.
- *Settings › System › Power*: never turn off the screen, never sleep.
- `install-windows.cmd -Display fullscreen`. **Alt+F4** closes the window until the next logon; **F11** leaves full screen;
  Alt+Tab reaches the other windows. Back to the browser only: `install-windows.cmd -Display browser`.

## Update, uninstall

`nuc-console-update` updates to the latest release (see [Update](#update); it keeps `config.ini`, the baseline and the display mode). Running
`install-windows.cmd` again, from a newer ZIP, does the same by hand. `install-windows.cmd -Uninstall` removes the tasks (the AI model's too, if you installed it), `%ProgramFiles%\nuc-console`
(with the commands) and the PATH entry; `%ProgramData%\nuc-console` (with `config.ini`, the baseline, the logs, the HEALTH history
`lib\history.db`, the download cache `cache\` and the AI runtime and models in `ai\`) is left in place, except its `notify` folder (the Telegram token), which is deleted. An update keeps the AI model's task.

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
| Nothing opens at logon | `[display] mode` is not `none`?; task `display`; `%LOCALAPPDATA%\nuc-console\display.log`; full screen needs Edge or Chrome (or `[display] browser`); with Firefox or no supported browser the window is not full screen until **F11** |
| Docker "not installed" or "not responding" | Docker Desktop must be running (it runs in a user's session) |
| Many `?` in EXPOSURE | rules the evaluation cannot read with certainty: see the NOTE column; `Get-NetFirewallRule` shows them |


# Update

nuc-console never updates itself: there is no timer, no service and no check at start-up. The only things that download anything are this
updater, when you run it, and an installer that has to fetch a Python (once). You run the updater when you decide to.

```bash
nuc-console-update --check        # only say whether a newer release exists: changes nothing, needs no root
sudo nuc-console-update           # ask, then update
sudo nuc-console-update --yes     # do not ask
```

| Option | Windows | |
|---|---|---|
| `--check` | `-Check` | only report: `already at X.Y.Z`, or `update available: X.Y.Z -> A.B.C (archive name)`. The exit code is 0 either way: read the message |
| `--yes`, `-y` | `-Yes` | do not ask `update nuc-console X -> Y? [y/N]` (without a terminal it refuses to ask: add `--yes`) |
| `--installed` | `-Installed` | update the installed nuc-console even when you run the command from an extracted folder |

On Windows run `nuc-console-update` (or `nuc-console-update.cmd`) from a new prompt, as the install put it on the PATH. It asks for
administrator rights itself, like `install-windows.cmd` (a new window opens and waits for a key at the end), except for `-Check`. On Linux and macOS it
never calls `sudo` itself: if it needs root it says so. On macOS, if `/usr/local/sbin` is not root's, use `sudo /opt/nuc-console/bin/nuc-console-update`.

## Which one is updated

| You run | It updates | Needs | Replaced | Kept | Cache |
|---|---|---|---|---|---|
| `bin/nuc-console-update` in a folder that has `run.sh` / `run.cmd` (a [portable](PORTABLE.md) folder) | that folder | nothing: no root, no administrator (quit it first) | `src/`, `bin/`, `docs/`, `config/`, the launchers, ... of the folder, and the `python/` of a Linux or macOS archive (links included) | `data/` (config, state, baseline, logs) and `cache/` | `<folder>/cache` |
| `nuc-console-update` on Linux | `/opt/nuc-console`, by running the new release's `install.sh` | `sudo` | the code, the units and the commands | `/etc/nuc-console/config.ini`, the baseline, the VT and the time zone | `/var/cache/nuc-console` |
| `nuc-console-update` on macOS | the same, through `install-macos.sh` | `sudo` | the same | `config.ini`, the baseline, the display mode | `/Library/Caches/nuc-console` |
| `nuc-console-update` on Windows | `%ProgramFiles%\nuc-console`, by running the new release's `install-windows.ps1` | Administrator | the code, the tasks and the commands | `%ProgramData%\nuc-console\config.ini`, the baseline, the display mode | `%ProgramData%\nuc-console\cache` |

An installed one is updated by the installer of the new release, so it is the same as extracting that archive and running the installer
yourself: the notes of [Linux](#linux), [macOS](#macos) and [Windows](#windows) apply (Linux: run it from SSH or another
terminal, not from the console the dashboard takes over; the services restart). Each install also refreshes `config.ini.dist` next to your
`config.ini`, which is never touched. If the installer fails, the update says so, and running it again repeats the install from the same cache.
An installation made before the first release that has the updater has none: update that one once by hand (`sudo ./install.sh` from the new
archive), and from then on `nuc-console-update` is there.

## What it does

1. Asks `api.github.com` (HTTPS) for the latest release: the one GitHub calls *latest*, never a draft or a pre-release. It compares the number with
   the `VERSION` you have, as numbers (`1.10.0` is newer than `1.9.9`). If you are up to date, or ahead, it says so and stops: it never downgrades.
2. Asks you (unless `--yes`).
3. Downloads `SHA256SUMS` (every time: it is what the archive is checked against) and the archive of this system **and processor** (`linux-x86_64`,
   `linux-arm64`, `macos-arm64`, `macos-x86_64`, `windows-x64`, `windows-arm64`: the one of the Python that runs the updater) into the
   **cache**. A processor with no archive (32-bit, RISC-V...) is refused with a message that lists what exists; so is a release that lacks the archive of yours. A file that is already in the cache with the SHA-256 that `SHA256SUMS` lists is **not downloaded again**; a missing, half or
   damaged one is.
4. Checks the archive against `SHA256SUMS`. A mismatch deletes it and nothing is installed.
5. If `gh` (GitHub CLI) is installed and logged in, runs `gh attestation verify <archive> --repo give-jd/nuc-console-oss`; a failure stops the
   update. Without `gh`, or without a login, it says that the provenance was not checked and goes on.
6. Unpacks the archive into a temporary folder of the cache (an absolute path or `..` in it is refused, and so is a device or a hard link in a `.tar.gz`
   and a symbolic link that is absolute or leaves the folder: the links of the bundled Python, such as `python/bin/python3`, stay inside it; the
   `VERSION` inside must be the release's) and installs from there: the installer of the new release, or, for a portable folder, the replacement of its files.
7. Deletes the unpacked folder and the archives of older releases from the cache.

Downloads are HTTPS only (a redirect too), from `github.com` assets, with a size limit, and nothing downloaded is run before the checks have passed.
What these checks prove and what they do not: [SECURITY.md](../SECURITY.md#verifying-a-release).

## The cache

Nothing is downloaded twice: the updater, the Windows installer (the Python zip) and the macOS installer (the python.org package, a clone only) all keep what they
fetched, check it again before every use, and reuse it. Only the newest release's archive and `SHA256SUMS` are kept (older archives are deleted after
an update; an archive is 20 to 40 MB, it carries its Python); the Python zip or package is kept for the next install. Delete the folder whenever you like: the next update fetches again.

An installed one's cache is root's (Windows: only SYSTEM and Administrators can write there). As root the updater refuses a cache folder that is a link or that
anyone else can write to, runs `gh` only if root owns it and nobody else can write it, and ignores the `PYTHON*` environment variables. Uninstalling leaves the cache in place
(Linux `/var/cache/nuc-console`, macOS `/Library/Caches/nuc-console`, Windows `%ProgramData%\nuc-console\cache`); a portable folder's is in the folder.
