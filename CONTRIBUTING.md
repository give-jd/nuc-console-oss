# Contributing

Thanks for helping. The project is small on purpose: **Python standard library only, one file per process, no build step.**

## Development

```bash
git clone <this repository> && cd nuc-console
python3 -m unittest discover -s tests            # must pass on Python 3.8+, on Linux, macOS and Windows (CI runs all three)
python3 src/render.py --once --demo --cols 200 --rows 50
python3 src/render.py --once --demo --demo-os windows --cols 200 --rows 50   # the screen as the Windows (or darwin) collector writes it
shellcheck install.sh install-macos.sh run.sh scripts/*.sh bin/nuc-console-accept bin/nuc-console-problems bin/nuc-console-update   # if you touch shell
```

- Every change needs a test. Parsers get fixtures (see `tests/test_nuc_console.py`); **use documentation addresses** (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `100.64.0.0/10`, `*.example.ts.net`) and fake values built at runtime for anything secret-looking.
- Never add a dependency. If you need one, the answer is almost certainly a stdlib function or fewer features.
- Keep Python 3.8 compatibility (no `match`, no `X | Y` types, no `str.removeprefix`).
- Platform code lives in `collect_darwin.py` / `collect_windows.py` (collector), `hostinfo.py` / `winapi.py` (renderer metrics):
  parsers are pure functions with fixtures, so they are tested on every OS; the classes `OnWindows` / `OnMacOS` in
  `tests/test_platforms.py` exercise the real system calls on their own OS. Text output of system tools is localised on Windows:
  use the API or PowerShell objects (`ConvertTo-Json`), never `netstat`/`netsh` text.
- The portable run (`run.sh`, `run.cmd` + `run.ps1`) and the updater (`bin/nuc-console-update` and its `.cmd` / `.ps1`) are thin wrappers: the logic that can be
  wrong (versions, choosing the asset, `SHA256SUMS`, the cache, unpacking, replacing a portable folder) is `src/update.py`, pure functions tested without a network in
  `tests/test_portable.py`. They are never started by anything but the user: no timer, nothing at start-up.
- The collector must **fail per section** (one broken command must not blank the others) and treat missing tools as `Absent`, not as errors.
- Anything that can be wrong must show `?` / "unknown", never a reassuring green.
- JavaScript: the web view has none except `src/graphjs.py` (the MAP's graph view). Keep it that way; `tests/test_graphjs.py` lists
  what that script may not do (build markup, eval, network, globals...). Its hash goes into the page's CSP automatically.
- Test layouts at several sizes: `--cols 79 --rows 24`, `120x33`, `200x50`, `226x50`. The MAP screen: `--view map` (with `--expand all|fit|N`, `--select TEXT`, `--details`, `--only`). The CPU screen: `--view cpu` (with `--sort cpu|mem|time|pid|user`, `--select NAME|PID`, `--details`). The HEALTH screen: `--view health` (with `--period 1|7|30`, `--select TEXT`, `--details`; `--demo-health little|none` for a machine with 5 hours of history or none). `--demo-os windows|darwin` for their data.

## Most wanted

1. **Translations.** All on-screen strings are English and live in `src/render.py`. A small translation layer (a dict of message keys, `NUC_CONSOLE_LANG` / `[dashboard] language`) would let other languages be added without touching the layout code. Keep strings within the widths the tables allot them, and discuss the approach in an issue first.
2. nftables-native and firewalld support in the exposure logic; macOS `pf` rules; verifying macOS code signatures for the Application Firewall.
3. Other container runtimes (podman).
4. More sensors (AMD/ARM thermal, macOS, Windows), multiple NVMe.

## Pull requests

Small, focused, with tests. Describe the *why*. Do not include secrets, real hostnames, real IP addresses or machine-specific paths in code, tests, docs or screenshots (use `--demo`).

## Releasing

A release is a tag. Everything else is done by `.github/workflows/release.yml`, so nobody builds or uploads archives by hand.

1. **Version.** Set `VERSION` in `src/nuc_config.py` to `X.Y.Z` (the number the tag will have, without the `v`), commit and push. It is the only place
   the number lives: the workflow, `tools/build_release.py` and `nuc-console-update` (which compares it with the latest release) read it from there.
2. **Dry run (optional).** *Actions › release › Run workflow*: choose the branch (or tag) to build and type the tag you are about to create (`vX.Y.Z`).
   It runs the same checks and builds the same archives, which you can download from the run for seven days; it signs no provenance and creates no release.
3. **Tag.** `git tag -a vX.Y.Z -m "nuc-console X.Y.Z" && git push origin vX.Y.Z`. Only tags of the form `vX.Y.Z` work (no `-rc1`). The workflow then:
   - **refuses a tag that is not `VERSION`** (and one that is not `vX.Y.Z`);
   - runs the unit tests;
   - downloads the two embeddable Pythons for the Windows archives and checks them against the SHA-256 pinned in `install-windows.ps1`;
   - builds the four archives and `SHA256SUMS` with `tools/build_release.py`, builds them again and checks that the second build is byte-identical and
     that `SHA256SUMS` matches;
   - attests every archive and `SHA256SUMS` (build provenance), and creates the release `nuc-console X.Y.Z` with them, with generated notes.
   The release is public as soon as the workflow ends, and from then on it is the *latest* one that `nuc-console-update` offers.
4. **Locally**, to look at what would ship: `python3 tools/build_release.py --version X.Y.Z --out dist [--python-zips DIR]`. Without `--python-zips` only
   the Linux and macOS archives are built (it says so); `--list-python` prints the file, SHA-256 and URL of the Pythons to download for the Windows ones.
   The version must be `VERSION`, and the build warns when the tree has uncommitted changes (the archives are of the files git tracks).

What goes in an archive is computed from the files git tracks, so a new file is shipped without touching the script: everything except `tests/`, `tools/`,
`CONTRIBUTING.md` and dotfiles (`.github/`); `systemd/` only in the Linux archive, `launchd/` and `install-macos.sh` only in the macOS one, `*.cmd` / `*.bat` /
`*.ps1` only in the Windows ones, shell scripts and `scripts/` not in the Windows ones. Name and place a new file accordingly. The rules are in the docstring of
`tools/build_release.py` and tested in `tests/test_release.py`.

The Python the Windows installer uses is pinned in one place, `$PyVersion` / `$PyBuilds` in `install-windows.ps1`: the installer, `run.ps1`, the build script and the
workflow all read it from there. Changing it is a normal commit; the next release carries the new Python. The actions of the workflow are pinned by commit SHA: to
update one, change the SHA and the version in the comment together.

## Regenerating the README screenshots

```bash
python3 src/render.py --once --demo --color --cols 226 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · overview, 3-column layout (demo data)" > docs/img/overview.svg
python3 src/render.py --once --demo --color --cols 120 --rows 40 | python3 tools/ansi2svg.py --title "nuc-console · 120×40 console, single column (demo data)" > docs/img/compact.svg
python3 src/render.py --once --demo --color --view map --expand fit --select shop-api --details --cols 200 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · MAP: who reaches what, and what is behind it (demo data)" > docs/img/map.svg
# the CPU screen (docs/img/cpu.png) the same way: http://127.0.0.1:8799/?view=cpu&sel=<a pid>&pause=1, window 1760x940
# the HEALTH page (docs/img/health.png): http://127.0.0.1:8799/?view=health&sel=mem-leak%3Anode&pause=1, window 1760x840
# the graph view is a browser page: run the demo web view and take a screenshot with any Chromium-based browser
python3 src/web.py --demo --port 8799 &   # then:
chromium --headless --hide-scrollbars --window-size=1600,1000 --screenshot=docs/img/graph.png "http://127.0.0.1:8799/?view=map&as=graph&sel=<key of shop-api-1>"
kill %1
```
