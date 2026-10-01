# Portable mode and updating

Two things for people who do not want (or do not yet want) an installation: run nuc-console from the extracted folder, and
update it when a new release is out. Neither installs a service, neither runs by itself.

## Run it without installing

Extract the archive of your system from the [latest release](https://github.com/give-jd/nuc-console-oss/releases/latest) (or `git clone`
the repository) and run, in that folder:

| System | Command | What you get |
|---|---|---|
| Linux | `./run.sh` | the dashboard in this terminal (`q` or Ctrl+C quits) |
| Linux, no terminal screen | `./run.sh --web` | the dashboard in your browser (it opens it when there is a desktop) |
| macOS | `./run.sh` | the dashboard in your browser (`./run.sh --console` for the terminal) |
| Windows | double-click `run.cmd` | the dashboard in your default browser |

Options: `--port N` (`-Port N` on Windows) picks the port of the browser view, a free one by default; `--no-open` (`-NoOpen`) only
prints the address. Ctrl+C, `q` in the terminal view, or closing the window stops **everything** it started: no process is left.
Only one copy runs per folder.

What it does, and does not do:

- Everything it writes is in `data/` of that folder: `config.ini` (copied from `config/` the first time; your edits are never
  overwritten, `config.ini.dist` always has the newest defaults to diff against), the state, the port-alarm baseline, `logs/`.
  Nothing is written anywhere else, nothing is installed, no service or scheduled task is created, no system setting changes.
- It starts the collector in the background, as **you**, and the browser view on `127.0.0.1` only, with no token (nothing but this
  machine can connect). `[web] bind` in `config.ini` is ignored in portable mode.
- Without root (Windows: without Administrator) it still runs: the sections that need more (firewall, containers, other
  users' processes, ...) show what is missing. `sudo ./run.sh` (Windows: right-click `run.cmd`, *Run as administrator*) shows everything,
  and then every part runs with those rights, so keep that folder yours alone (as root, `run.sh` refuses a folder others can write to).
- **Python**: Linux and macOS use the system's Python 3.8+ (`PYTHON=/path/to/python3 ./run.sh` to choose one). The Windows ZIP carries the
  official embeddable Python in `python\`: the first run checks its SHA-256 against the pin in `install-windows.ps1` and its
  signature, unpacks it once into `python\`, and reuses it afterwards; a source checkout without it uses `py -3` / `python`.
- **Port alarms**: the baseline is created on the first complete snapshot of the collector. A collector without root cannot see
  everything, so it may refuse (it tries for a couple of minutes): then run `./run.sh --accept` (`run.cmd -Accept`) from another terminal,
  or start it as root once. `./run.sh --accept --problem ID --reason "why"` accepts a known item of the ATTENTION list.

## Update

```bash
bin/nuc-console-update --check        # only say whether a newer release exists
bin/nuc-console-update                # ask, then update
bin/nuc-console-update --yes          # do not ask
```

Windows: `bin\nuc-console-update.cmd` with `-Check`, `-Yes`. Run it when you decide to; there is no timer, no service, no check at
start-up, and nothing else in nuc-console talks to the Internet.

It reads the version of what you have, asks `api.github.com` (HTTPS only) for the latest release and compares the numbers
(`1.10.0` is newer than `1.9.9`). When you are up to date it says `already at X.Y.Z` and stops. When there is a newer release:

1. it downloads the archive of this system (Windows: x64 or ARM64) and `SHA256SUMS` into the **cache**;
2. a file already in the cache whose SHA-256 is right is **not downloaded again** (a corrupt or half file is replaced);
3. the archive's SHA-256 must be the one in `SHA256SUMS`, else it is deleted and nothing is installed;
4. if `gh` (GitHub CLI) is installed and logged in it runs `gh attestation verify <archive> --repo give-jd/nuc-console-oss`, and
   **refuses on failure**; without `gh`, or without a login, it says that the provenance was not checked, and goes on;
5. it unpacks into a temporary folder of the cache (links, devices and paths that escape are refused; the version inside must be
   the release's) and installs from there.

| Mode | Which one | Needs | What is replaced | Cache |
|---|---|---|---|---|
| Portable | the `bin/` of an extracted folder that has `run.sh` / `run.cmd` | nothing (stop it first) | `src/`, `bin/`, `docs/`, `config/`, ... of that folder; **`data/` (your config, baseline, logs) and `cache/` stay** | `<folder>/cache` |
| Installed, Linux | `/opt/nuc-console` | `sudo` | the new release's `install.sh` runs: it keeps `/etc/nuc-console/config.ini`, the baseline, the VT and the time zone | `/var/cache/nuc-console` |
| Installed, macOS | `/opt/nuc-console` | `sudo` | the same, through `install-macos.sh` | `/Library/Caches/nuc-console` |
| Installed, Windows | `%ProgramFiles%\nuc-console` | Administrator: `nuc-console-update.cmd` asks for it, like `install-windows.cmd` | `install-windows.ps1` of the new release: it keeps `%ProgramData%\nuc-console\config.ini` and the baseline | `%ProgramData%\nuc-console\cache` |

`--installed` (`-Installed`) updates the installed one even when you run the command from an extracted folder:
`sudo ./bin/nuc-console-update --installed`. The updater never calls `sudo` itself: if it needs root it says so.
The caches are root's (Windows: SYSTEM and Administrators) and are checked again before use; only the archive of the newest release is
kept. Delete a cache folder whenever you like.

## Security notes

- The logic of the updater is `src/update.py` (standard library only); the shell and PowerShell wrappers only ask GitHub and start the
  installer after the Python that checked the archive has ended. It is the same code on every system and it is tested without a network.
- It trusts GitHub's release for the file list and the hashes, and the build attestation (`gh`) for who built it: `SHA256SUMS`
  protects against a damaged or swapped download, not against a release that was published by someone else; for that, install `gh`
  or check the attestation yourself ([SECURITY.md](../SECURITY.md#verifying-a-release)).
- Downloads: HTTPS only (redirects too), `github.com` assets only, size-limited. Nothing is run from the network.
- As root, the updater refuses a cache folder that is not root's, runs `gh` only when root owns it, and ignores `PYTHON*` variables.
