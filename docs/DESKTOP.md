# The desktop app

nuc-console in a window of its own, with an icon in the tray (the menu bar on macOS) and *start at login*: install a package, open
it, and the dashboard is there. It is the [portable run](PORTABLE.md) inside an app: the same collector, web view and live app
([docs/WEB.md](WEB.md#the-live-app)), with its data in your user folder instead of `./data`.

| System | Package | Install |
|---|---|---|
| Windows, Intel/AMD | `nuc-console-desktop-X.Y.Z-windows-x64.msi` (for every user, asks for administrator rights) or `...-windows-x64-setup.exe` (for you, no administrator) | double-click it |
| Windows on ARM | `nuc-console-desktop-X.Y.Z-windows-arm64-setup.exe` | double-click it |
| macOS, Apple silicon | `nuc-console-desktop-X.Y.Z-macos-arm64.dmg` | open it, drag **nuc-console** to Applications |
| macOS, Intel | `nuc-console-desktop-X.Y.Z-macos-x86_64.dmg` | the same |
| Debian, Ubuntu... (x86-64, arm64) | `nuc-console-desktop-X.Y.Z-linux-x86_64.deb`, `...-linux-arm64.deb` | `sudo apt install ./nuc-console-desktop-*.deb` |
| Fedora, openSUSE... (x86-64, arm64) | `nuc-console-desktop-X.Y.Z-linux-x86_64.rpm`, `...-linux-arm64.rpm` | `sudo dnf install ./nuc-console-desktop-*.rpm` |
| any Linux, x86-64 | `nuc-console-desktop-X.Y.Z-linux-x86_64.AppImage` | `chmod +x` it and run it |

They are on every [release](https://github.com/give-jd/nuc-console-oss/releases/latest), next to the archives, with
`SHA256SUMS-desktop` and their build provenance: `sha256sum --ignore-missing -c SHA256SUMS-desktop` and
`gh attestation verify <package> --repo give-jd/nuc-console-oss` check them as [the archives](INSTALL.md#check-it) are checked.
Every package carries the core and its Python: nothing else is downloaded or installed.

## Not signed yet: the first start

The packages are not signed with a publisher's certificate yet ([docs/ROADMAP.md](ROADMAP.md#later-signing-and-stores)), so the
system warns the first time:

- **Windows**: SmartScreen says *Windows protected your PC*: **More info**, then **Run anyway** (once).
- **macOS**: *"nuc-console" cannot be opened because Apple cannot check it*. Open **System Settings > Privacy & Security**, and at
  the bottom **Open Anyway** (once). If macOS says the app *is damaged*, it is the quarantine of the download:
  `xattr -dr com.apple.quarantine /Applications/nuc-console.app` (the app has an ad-hoc signature, not Apple's).
- **Linux**: no warning.

## Using it

- **The window** shows the live app. Closing it hides it: the collector keeps running (the history, the alarms). Open it again from
  the tray icon, or by starting the app again.
- **The tray icon**: *Open nuc-console*, *Open in the browser* (the same dashboard, in your browser), *Start at login* (a tick: it
  then starts without the window), *Quit nuc-console*. On macOS it is in the menu bar; the Dock icon opens the window too.
- **Quitting** stops the collector and the web view it started. `nuc-console --quit` (Windows: `nuc-console.exe --quit`) quits the
  one that runs, from a terminal or a script.
- Links to anything but the dashboard (the docs, a project page) open in your browser, never in the window.
- One app per user: starting it again shows the window of the one that runs.
- **Settings** (the gear at the top right): the look of the dashboard; **Screens and sections**, where each `[features]` switch (the Health
  screen, the containers, the firewall...) is turned on or off in the app's `config.ini`, without a restart; and **config.ini**, every other key of
  that file, section by section, each with what it does, the values it takes and when a change applies, and a **Save** per section
  ([WEB.md](WEB.md#the-settings-pages-configini)).
- **Phone alerts**: ⚙ settings › *Phone alerts* › *Set up Telegram alerts* walks you through a Telegram bot of your own (create it, pair it,
  press Start, send a test); the app starts the notifier itself ([TELEGRAM.md](TELEGRAM.md#in-the-desktop-app-and-a-portable-run)).

On GNOME without the AppIndicator extension (Fedora's default) there is no tray icon: start the app again to show the window, and
quit it with `nuc-console --quit`. Ubuntu, KDE, Xfce, Cinnamon and the others show it.

**What it sees.** The app runs as you, like the portable run without `sudo`: what needs root or administrator rights (the firewall,
other users' processes, containers on Linux, the services on Windows) shows as missing on the screen. For everything, install the
service version instead ([docs/INSTALL.md](INSTALL.md)); the two do not share their data.

## Where things are

| | Linux | macOS | Windows |
|---|---|---|---|
| The app | `/usr/bin/nuc-console`, the core in `/usr/lib/nuc-console/core` (AppImage: inside the file) | `/Applications/nuc-console.app` (the core in `Contents/Resources/core`) | `C:\Program Files\nuc-console` (`.msi`) or `%LOCALAPPDATA%\nuc-console` (setup `.exe`), the core in `core\` |
| Its data: `config.ini`, state, baseline, history, logs | `~/.local/share/io.github.give-jd.nuc-console` | `~/Library/Application Support/io.github.give-jd.nuc-console` | `%LOCALAPPDATA%\io.github.give-jd.nuc-console` |

The data folder is the portable run's `./data`: `config.ini` is copied there once (your edits stay), `config.ini.dist` beside it
shows the new options of each version, and `logs/` holds `collector.log`, `web.log`, `notify.log` (the Telegram notifier) and `desktop.log` (what the core printed, and
what the app said). Uninstalling the app leaves it; delete it by hand to start afresh.

The advice on screen names the portable commands (`./run.sh --accept`, `run.cmd -Accept`): in the app they are the core's, run with
the data folder, for example on Linux
`NUC_CONSOLE_DATA=~/.local/share/io.github.give-jd.nuc-console /usr/lib/nuc-console/core/run.sh --accept`
(macOS: `.../nuc-console.app/Contents/Resources/core/run.sh`; Windows: `$env:NUC_CONSOLE_DATA = "$env:LOCALAPPDATA\io.github.give-jd.nuc-console"`,
then `& "C:\Program Files\nuc-console\core\run.cmd" -Accept`). The baseline of the port alarms is accepted by itself once the collector has
written a complete snapshot, as in the portable run.

## How it works

The app is built with [Tauri 2](https://tauri.app) (`desktop/`): a small native program around the system's web view (WebView2 on
Windows, WebKit on macOS and Linux), with no browser of its own.

1. It opens its window on a start page of its own (`desktop/ui`), and starts the core: `core/run.sh --web --no-open` (Windows:
   `core\run.ps1 -NoOpen`, through the PowerShell of `System32`), with `NUC_CONSOLE_DATA` set to the data folder above. The core is
   the release archive of the same system and processor, Python included, unpacked into the package; it writes only to the data folder.
2. `run.sh` / `run.ps1` start the collector and the web view on `127.0.0.1` and a free port, and print the address; the app reads
   it and loads `/app` in the window. The view asks for a token: the core made one in the data folder (`web.token`, 0600, see
   [PORTABLE.md](PORTABLE.md#the-access-token)), the app reads that file and opens `/app?token=…` in its window, where the view moves it
   into a cookie. *Open in the browser* does not put it on a command line (other users could list that): it opens `open-app.html` in the data
   folder (0600), a page that forwards to the view with the token. To get a new token, quit the app, delete `web.token` and start it again.
3. If the core does not come up, the start page says why, with its last lines and where its logs are. If an earlier run of the app
   ended without stopping it (a crash, a kill), the app finds that core by its address in `logs/web.log` and uses it, but only if the pid in
   `portable.pid` is a process running this app's own `run.sh` (`run.ps1`); otherwise it does not adopt it and the start page says the core is
   already running.
4. Quitting sends the core SIGTERM (`run.sh` then stops all it started; a core that does not end in 10 seconds is killed with its
   process group); on Windows the process tree of `run.ps1` is ended.

The window may show only the start page and the core: any other address is opened in the browser, and no page can call into the
app (it declares no capability and exposes no API to its pages). The dashboard's own rules stay as they are: the web view listens
on `127.0.0.1` only, with the CSP, the CSRF tokens and the checks of [docs/WEB.md](WEB.md) and [SECURITY.md](../SECURITY.md).

Options of the program: `--hidden` (start without the window: what *start at login* uses), `--quit`, and `--smoke-test` for CI (no
window: start the core, ask it for `/app` and `/api/v1/summary`, stop it, exit 0 when both answered).

## Building it

The packages are built by `.github/workflows/desktop.yml` on a runner of each system and processor (on pull requests that touch the
app, by hand, and from `release.yml` on every tag, which attaches them to the release):

1. the release archive of the target (`tools/build_release.py`), turned into the app's `core/` by `tools/desktop_core.py --archive`:
   the links of the Linux and macOS Python become copies (the bundlers copy files), and on Windows the embeddable Python is unpacked
   the way `run.ps1` does it on its first run, after its SHA-256 is checked against the pin, so that the core never writes into its
   own folder;
2. `cargo tauri build` with the version of `src/nuc_config.py` (`--config version.json`);
3. `tools/desktop_core.py --collect`: the packages, named `nuc-console-desktop-X.Y.Z-<target>.<ext>`;
4. the smoke test: each package installed (the `.deb` with apt, the `.msi` with msiexec, the setup `.exe` silently), mounted (the
   `.dmg`, whose ad-hoc signature is verified) or run (the AppImage), its core asked `--which-python` (the package's own Python) and
   `--problems`, then the app started with `--smoke-test`.

On your machine: Rust ([rustup](https://rustup.rs)), the Tauri command line (`cargo install tauri-cli --version "^2" --locked`) and, on
Linux, WebKitGTK and the tray library (Debian/Ubuntu: `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev
libssl-dev`). Then:

```bash
python3 tools/desktop_core.py --checkout       # core/ from this checkout (no Python of its own: it uses the machine's, as a clone does)
python3 tools/desktop_core.py --archive dist/nuc-console-X.Y.Z-linux-x86_64.tar.gz   # or a release archive, as the packages have it
cd desktop/src-tauri
cargo test                                     # the app's own tests
cargo tauri dev                                # the app, from this checkout
cargo tauri build --bundles deb                # a package (appimage, rpm; app, dmg on macOS; msi, nsis on Windows)
```

`core/` is never committed. The icons are drawn by `python3 tools/desktop_icons.py` (standard library; the same bytes every time,
and `tests/test_desktop.py` checks them). The crates are Tauri's (`tauri`, its autostart, opener and single-instance plugins) and
`libc` on Linux and macOS; nothing else.

## Adopting an earlier core

When the app finds a core that an earlier run left (see *How it works*, point 3), it takes its pid from `portable.pid` and its address from
`logs/web.log`. Before it adopts that pid, and again before it sends it SIGTERM on quit, it checks that the process is the core: it must run
this app's own `core/run.sh` (Linux: `/proc/<pid>/cmdline`; macOS: `ps`; Windows: the command line from `Get-CimInstance`, through the
PowerShell of `System32`). A pid of 0 or 1 is never signalled (`kill(0)` would reach the app's own process group). A pid that went to another
process, or that cannot be looked at, is left alone. A core started from another copy of the app (another path) is not this app's to stop.
Not covered: the check reads the command line, which whoever can run a process as you can imitate; it is there against a stale pid, not against
you.

## What comes next

- **Updates from inside the app**, from the GitHub releases: Tauri's updater checks a signature of its own, whose key belongs in the
  repository's secrets. Until then, install the new package over the old one (the data folder stays).
- **Signing**: a code-signing certificate on Windows and an Apple Developer ID with notarization on macOS, so that the warnings
  above go away ([docs/ROADMAP.md](ROADMAP.md#later-signing-and-stores)).
