# Portable run

Run nuc-console from the extracted folder, without installing it. No service and no scheduled task is created, no system
setting changes, nothing listens beyond `127.0.0.1`, and everything it writes is in `data/` of that folder. It runs when you start
it and stops when you quit it. To install it instead: [INSTALL.md](INSTALL.md). To update a folder: [Update a portable folder](#update-a-portable-folder).

## Run it

Download the archive of your system **and processor** from the [latest release](https://github.com/give-jd/nuc-console-oss/releases/latest)
([which one, and how to check it](INSTALL.md#download-a-release): `linux-x86_64`, `linux-arm64`, `macos-arm64`, `macos-x86_64`, `windows-x64`,
`windows-arm64`) and extract it. **It carries its own Python: nothing else has to be installed on the machine and no network is needed** (download,
unpack, run). Or `git clone` the repository, which needs a Python 3.8+ on Linux and macOS. In that folder:

| System | Command | What you get |
|---|---|---|
| Linux | `./run.sh` | the dashboard in this terminal (`q` or Ctrl+C quits) |
| Linux, in a browser | `./run.sh --web` | the dashboard in your browser (it opens it when there is a desktop, else it prints the address) |
| macOS | `./run.sh` | the dashboard in your browser (`./run.sh --console` for the terminal) |
| Windows | double-click `run.cmd` | the dashboard in your default browser |

Options (Windows: `run.cmd` takes the PowerShell spelling):

| `run.sh` | `run.cmd` | |
|---|---|---|
| `--console` | | the dashboard in this terminal: the default on Linux; needs a terminal (`--console needs a terminal: use --web`) |
| `--web` | (always) | the dashboard in your browser, the default on macOS and Windows |
| `--port N` | `-Port N` | the port of the browser view; a free one by default. The address is printed: `dashboard on http://127.0.0.1:PORT/?fit=1` |
| `--no-open` | `-NoOpen` | only print the address, do not open a browser |
| `--accept` | `-Accept` | accept the ports exposed now as the baseline of the port alarms, then exit: [Port alarms](#port-alarms) |
| `-h`, `--help` | | the usage |

Only one copy runs per folder: a second start says `already running` and exits. On Linux and macOS, `PYTHON=/path/to/python3 ./run.sh`
chooses the Python (otherwise: [Python](#python)).

## What it does, and where

It starts, as **you**:

1. the collector (`src/collector.py`), in the background: it writes its snapshots to `data/run/`;
2. the view: the terminal screen (`src/render.py`), or the browser view (`src/web.py --local`) on `127.0.0.1`, behind a token of its own (nothing but
   this machine can connect, and of its accounts only you: [the token](#the-access-token)), and opens your default browser on it (`open` on macOS, `xdg-open` on Linux when there is a desktop, `Start-Process` on Windows);
3. until the baseline of the port alarms exists, a small helper that tries to create it ([Port alarms](#port-alarms));
4. the Telegram notifier (`src/notify.py`): beside the browser view it takes the Telegram page's requests (⚙ settings › *Phone alerts*: pair your
   own bot, send a test, [TELEGRAM.md](TELEGRAM.md#in-the-desktop-app-and-a-portable-run)); in the terminal it only sends, and while the alerts are
   off it ends at once.

Everything it writes:

| Where | What |
|---|---|
| `data/config.ini` | your configuration: copied from `config/config.ini` the first time, never overwritten |
| `data/config.ini.dist` | the newest defaults, refreshed at every start: `diff data/config.ini data/config.ini.dist` lists the options added by a newer version |
| `data/run/` | the collector's snapshots (`net.json`, `containers.json`, `boot.json`, and `sensors.json` on macOS and Windows) |
| `data/lib/` | `baseline.json` (the port alarms) and `accepted.json` (the problems you accepted) |
| `data/notify/` | the Telegram notifier's: the bot token and the paired chat (0600, only your account), `status.json`, the page's requests in `inbox/` |
| `data/logs/` | `collector.log`, `web.log` (the view in the browser), `notify.log` (the Telegram notifier), `baseline.log`; a `collector.log` over 1 MB is kept once as `.1` at the next start |
| `data/web.token` | the access token of the browser view (0600, only your account; Windows: an ACL for your account), made at the first start and kept: [the token](#the-access-token) |
| `data/open.html` | what the browser is given to open: a page that sends it to the view with the token (0600). Same secret as `web.token`, rewritten at every start |
| `data/portable.pid` | the process ID of the running copy; removed when it stops |
| `python\` (Windows ZIP) | the Python, unpacked once on the first run (see [Python](#python)); on Linux and macOS `python/` is already unpacked in the archive and is only read |
| `cache/` | what `nuc-console-update` downloaded; only exists once you used it |

On Linux and macOS `data/` is private to you (mode 0700, files 0600): the snapshots hold your topology. On Windows it has the
permissions of the folder it is in.

Nothing is installed and nothing is written outside this folder, apart from what the programs it starts do on their own: your browser,
and the temporary files of the system and of PowerShell. Python runs with `-B`: no `__pycache__` is written. The launcher does this by setting
`NUC_CONSOLE_HOME` to `data/`: the collector, the screen and the web view keep their config, state and baseline there
([CONFIGURATION.md](CONFIGURATION.md#portable-run)).

## Without root, with root

As a normal user (Windows: without *Run as administrator*) it still runs; what needs more rights shows less, and the screen says so: the
firewall, containers, other users' processes and services. Start `sudo ./run.sh` (Windows: right-click `run.cmd` › *Run as administrator*)
to see everything. Then **every** part runs with those rights, so keep that folder yours alone:

- Linux and macOS: as root, `run.sh` refuses a `src/`, `data/` (or one of its folders) that is a link or writable by others, and says what to fix.
- Windows: `run.ps1` does not check this. Run it as Administrator only from a folder that only Administrators can write.
- `data/` created by a `sudo` run belongs to root: a later run as yourself says `data is not writable by you`, and `sudo chown -R "$USER" data` fixes it.

## The access token

The browser view is on `127.0.0.1` only, but other accounts of the same machine can reach `127.0.0.1` too. So it asks for a token, made for this
folder: `data/web.token` (`secrets.token_urlsafe`, 0600; Windows: an ACL for your account only), created at the first start, **kept** between
starts (your browser and the desktop app hold it as a cookie) and never in `config.ini`, in a log or in a page. `run.sh` / `run.cmd` do not put it
on the browser's command line (any user can list that): they open `data/open.html` (0600), a page that forwards to the view with the token, which
moves it into an `HttpOnly; SameSite=Strict` cookie. The address they print has no token, so a browser you open by hand on it gets `401`: open
`data/open.html` instead. For a script: `Authorization: Bearer <the contents of data/web.token>`. A new token: stop the copy, delete `data/web.token`,
start again (the browser is sent a new cookie by `open.html`). `--demo` and an installation (`[web] token_file`) are not this mode.
What it does not do: it does not stop **you**, **root**, or a program that runs as you (they read the file), and it does not encrypt anything
(the traffic is loopback http). A portable folder on a shared disk where others can read `data/` gives the token away: keep it yours.

## Python

- **Linux and macOS archives**: the Python is in `python/` (`python/bin/python3`), a python-build-standalone CPython for the processor of the archive,
  already unpacked: `./run.sh` uses it first, so the machine needs no Python. `$PYTHON` overrides it if you set it. If it does not run here (the archive of
  another processor or system: `run.sh` says `the Python in .../python does not run here: is this the archive for Linux x86_64?`; on macOS the files of a
  browser download are blocked until `xattr -dr com.apple.quarantine <folder>`), the Python of the machine is looked for instead: on macOS the newest
  python.org Python, then Apple's `/usr/bin/python3` (only when the Command Line Tools are installed); on Linux `/usr/bin/python3`; then the
  first `python3` on the PATH (Python 3.8 or newer, standard library only). A **clone** has no `python/`: it uses those. As root (`sudo ./run.sh`) it refuses a
  `python/` that is a link or that others can write to, before it runs anything from it. Nothing is ever downloaded.
- **Windows ZIP**: the official embeddable Python is in `python\` (the x64 ZIP carries the x64 one, the ARM64 ZIP the ARM64 one). The first run checks its
  SHA-256 against the pin in `install-windows.ps1` and python.exe's signature (Python Software Foundation), unpacks it once into `python\`, and reuses it afterwards. If it
  cannot be used (`warning: ... not used`), or in a source checkout that has none, the run falls back to a Python 3.8+ of the machine (`py -3`, `python`, `python3`).

## Port alarms

The baseline of the exposed ports is created once the collector has written its first **complete** snapshot. A collector without root
(Windows: Administrator) cannot see everything, so the snapshot may be incomplete and the baseline refused (`baseline not created`, in
`data/logs/baseline.log`). It is tried every few seconds for a couple of minutes; after that either start it as root once, or from another
terminal accept the state of right now:

```bash
./run.sh --accept                                          # Windows: run.cmd -Accept
./run.sh --accept --problem ID --reason "why it is fine"   # a known item of the ATTENTION list
./run.sh --accept --forget ID                              # undo it
```

`--accept` refuses stale or incomplete data. Port changes are accepted with the baseline, not with `--problem`. `nuc-console-accept` and
`nuc-console-problems` in `bin/` belong to an installation (they read `/opt/nuc-console`), not to this folder. To list the ids of the ATTENTION items of a portable run:
`NUC_CONSOLE_HOME=data python3 src/render.py --problems` (Windows, in `cmd`: `set NUC_CONSOLE_HOME=data` and `python\python.exe -B src\render.py --problems`).

## Configuration

Edit `data/config.ini` ([CONFIGURATION.md](CONFIGURATION.md) lists every key), then quit and start it again; or do it from the web view's settings page:
**Screens and sections** turns the `[features]` switches on and off (the collector picks a change up within 10 seconds), and **config.ini** has every other
key, section by section, with what it does, the values it takes and when a change applies (most at once; [WEB.md](WEB.md#the-settings-pages-configini)).
Everything applies except the
parts that decide how an installation shows itself: `[web] enabled`, `bind`, `port` and `token_file` are ignored (the view is always on
`127.0.0.1`, on the port you give or a free one, behind the folder's own token) and so are `[display] mode` and `browser` (the script opens your default browser itself;
`--no-open` stops it). `[display] zoom` still sets the text size.

## Stopping

Ctrl+C, `q` in the terminal view, or closing the terminal or the window stops **everything** it started. On Windows the collector and
the web view are in a job object, so Windows ends them with the window even when it is closed hard. A copy that is killed without
a chance to clean up (`kill -9`, a power cut) leaves `data/portable.pid` behind. The next start ignores it unless a process with that
ID exists; if it says `already running` and nothing runs, delete the file.

## Update a portable folder

```bash
bin/nuc-console-update --check        # only say whether a newer release exists
bin/nuc-console-update                # ask, then update
bin/nuc-console-update --yes          # do not ask
```

The archive of your **processor** is taken (`x86_64` or `arm64`: the one of the Python that runs the updater; a processor without an archive is refused). Windows: `bin\nuc-console-update.cmd`, with `-Check` and `-Yes`. It needs no rights: run it as the user who owns the folder (as root, on
Linux and macOS, it refuses a folder root does not own). It refuses while a copy is running from the folder: quit it first. Run it when you decide to; it is never automatic.

It downloads and checks the archive of your system as described in [Update](INSTALL.md#update), into `cache/` of this folder (a file
already there with the right SHA-256 is not downloaded again), and then:

- replaces the files the new release ships: `src/`, `bin/`, `docs/`, `config/`, `scripts/`, the launchers, installers, `README.md`...; a file the new
  release no longer has is deleted from those folders; on Linux and macOS the new release's `python/` replaces the old one (links stay links, a file of the old
  Python that the new one lacks is deleted, files that did not change are not written again); on Windows, when the release has another Python, its zip replaces the old one in `python\` and is unpacked at the next run.
  The files are first written next to their targets and then renamed over them, so a full disk stops it before anything is replaced;
- keeps `data/` (your config, state, baseline, accepted problems, logs) and `cache/`, untouched. `data/config.ini` is never replaced; the
  next start refreshes `config.ini.dist`;
- says `updated to X.Y.Z: start it again with run.sh / run.cmd`.

Files you changed outside `data/` are overwritten, and files you added are left alone. A `git clone` updates with `git pull`: run the
updater only in a folder extracted from an archive. To update an *installed* nuc-console from an extracted folder: `sudo ./bin/nuc-console-update --installed`.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| `already running (pid N): quit it first` | a copy runs from this folder; if it does not, delete `data/portable.pid` |
| `--console needs a terminal: use --web` | there is no terminal (a script, a pipe): use `./run.sh --web` |
| `the Python in .../python does not run here` | the archive is for another processor or system: take the one that matches `uname -m` ([which file](INSTALL.md#which-file)); macOS: `xattr -dr com.apple.quarantine <folder>` |
| `Python 3.8 or newer not found` | a clone: Linux: install `python3`; macOS: python.org or `xcode-select --install`; Windows: use the release ZIP. The release archives carry their own |
| `the web view did not start` | the end of `data/logs/web.log` is printed; if you gave `--port`, that port may be taken: try without it |
| the screen says the collector is not running, or the data is old | `data/logs/collector.log` (Windows: `the collector stopped (see ...)` is printed) |
| `baseline not created` | [Port alarms](#port-alarms): `./run.sh --accept`, or start it as root once |
| a section shows less than on an installation | it needs root or Administrator: [Without root, with root](#without-root-with-root) |

## Security notes

Nothing in this mode runs with more rights than you give it, and it opens no listener beyond `127.0.0.1`. The updater only runs when you start
it; it checks what it downloads against `SHA256SUMS` and, when `gh` is installed and logged in, against the build attestation (an installed update refuses to go on without it unless `--allow-unattested`; a portable folder does not): what that proves and
what it does not is in [SECURITY.md](../SECURITY.md#verifying-a-release).
