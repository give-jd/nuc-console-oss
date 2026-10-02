# Contributing

Thanks for helping. The project is small on purpose: **Python standard library only, one entry script per process, no build step.** The modules an entry script imports are flat files in `src/` (the installers copy `src/*.py`): add one as a new file next to the others, never a package or a folder. [docs/DESIGN.md](docs/DESIGN.md#code-layout) says which module holds what.

## Development

```bash
git clone <this repository> && cd nuc-console
python3 -m unittest discover -s tests            # must pass on Python 3.8+, on Linux, macOS and Windows (CI runs all three, once per commit of a PR and on main;
                                                 # a newer commit on the same PR cancels the older run)
python3 src/render.py --once --demo --cols 200 --rows 50
python3 src/render.py --once --demo --demo-os windows --cols 200 --rows 50   # the screen as the Windows (or darwin) collector writes it
python3 src/render.py --once --demo --view ai --cols 200 --rows 50           # the AI screen: three invented machines (--demo-os windows|darwin for the others)
python3 src/render.py --once --demo --view ai --select qwen3-8b --details    # the details of one model: why, licence, the commands
python3 src/web.py --demo --port 8796                                        # the web view with the AI page's buttons, simulated: nothing is downloaded or started
python3 -m unittest tests.test_golden            # every screen and page, byte for byte (see "Golden outputs and the render benchmark")
python3 tools/bench_render.py                    # the CPU a frame costs, per view and size (--json, --compare before.json)
shellcheck install.sh install-macos.sh run.sh scripts/*.sh bin/nuc-console-{accept,problems,update,ai,ask}   # if you touch shell
```

- Every change needs a test. Parsers get fixtures (see `tests/test_nuc_console.py`); **use documentation addresses** (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `100.64.0.0/10`, `*.example.ts.net`) and fake values built at runtime for anything secret-looking.
- Never add a dependency. If you need one, the answer is almost certainly a stdlib function or fewer features.
- Keep Python 3.8 compatibility (no `match`, no `X | Y` types, no `str.removeprefix`).
- Colours: a colour that means a state (fine, attention, problem, unknown, a note) is a token of `src/ui.py`, drawn with `ui.sgr(token)` (`ansi.py` does it for the primitives); a raw SGR code is for what means nothing but itself. Every state also has a symbol: colour is never the only signal.
- Configuration: read it through `nuc_config.current()`, the one dict of the process (`render.CFG` is that object); only code that needs the file read again calls `nuc_config.load()`.
- Platform code lives in `collect_darwin.py` / `collect_windows.py` (collector), `hostinfo.py` / `winapi.py` (renderer metrics):
  parsers are pure functions with fixtures, so they are tested on every OS; the classes `OnWindows` / `OnMacOS` in
  `tests/test_platforms.py` exercise the real system calls on their own OS. Text output of system tools is localised on Windows:
  use the API or PowerShell objects (`ConvertTo-Json`), never `netstat`/`netsh` text.
- The portable run (`run.sh`, `run.cmd` + `run.ps1`) and the updater (`bin/nuc-console-update` and its `.cmd` / `.ps1`) are thin wrappers: the logic that can be
  wrong (versions, choosing the asset, `SHA256SUMS`, the cache, unpacking, replacing a portable folder) is `src/update.py`, pure functions tested without a network in
  `tests/test_portable.py`. They are never started by anything but the user: no timer, nothing at start-up.
- The AI screen reads the hardware in `aihw.py` the same way: the commands (`nvidia-smi`, `sysctl`, `vm_stat`, `system_profiler`) and the files
  (`/proc`, `/sys`) go through two injectable functions, so each OS has fixtures in the tests; fixed argument lists, a short time limit, never a
  shell, and whatever cannot be read goes to the notes instead of being guessed. The thresholds of the five verdicts and the memory
  bandwidths behind the speed estimates are named constants at the top of that file; change one together with the tables in `docs/AI.md`.
- What the AI page and the AI screen do (choose a model, AI on / off, delete, chat) is one engine, `src/aiweb.py`, used by both: the jobs (download, start, delete)
  and the chat answer run in daemon threads and the screens only read `snapshot()`; nothing there may block a page or a key. Put new behaviour in the engine and its tests
  (`tests/test_ai_web_actions.py`: a fake download server, fake runtimes that are shell scripts, a fake OpenAI server; no sleeps, no network, nothing real is downloaded),
  never in `web.py` or `render.py`, which only turn a request or a key into a call and the snapshot into markup or text. The POST rules (CSRF token,
  Origin/Referer, 4 KB, ids from the catalog, no JavaScript, the CSP) are in `docs/WEB.md`; a new button follows them and gets a test in `WebSecurity`. `--demo` simulates every action.
- The collector must **fail per section** (one broken command must not blank the others) and treat missing tools as `Absent`, not as errors.
- Anything that can be wrong must show `?` / "unknown", never a reassuring green.
- JavaScript: the web view has none except `src/graphjs.py` (the MAP's graph view). Keep it that way; `tests/test_graphjs.py` lists
  what that script may not do (build markup, eval, network, globals...). Its hash goes into the page's CSP automatically.
- Test layouts at several sizes: `--cols 79 --rows 24`, `120x33`, `200x50`, `226x50`. The MAP screen: `--view map` (with `--expand all|fit|N`, `--select TEXT`, `--details`, `--only`). The CPU screen: `--view cpu` (with `--sort cpu|mem|time|pid|user`, `--select NAME|PID`, `--details`). The HEALTH screen: `--view health` (with `--period 1|7|30`, `--select TEXT`, `--details`; `--demo-health little|none` for a machine with 5 hours of history or none). The AI screen: `--view ai` (with `--select TEXT`, `--details`; the demo has an NVIDIA box, a small Windows laptop and an Apple-silicon Mac). `--demo-os windows|darwin` for their data.

## Golden outputs and the render benchmark

`tests/golden/` holds what every screen writes, byte for byte, so that a refactor that is meant to change nothing can prove it. The console files are
`render.py --once --demo --color` raw (the ANSI colours and the `ESC[K CR LF` that ends each line of a frame included): the overview at 79x24, 120x33,
200x50 and 226x50 with `[dashboard] spacing` 0 and 1, `--demo-os windows` and `darwin`, the rotating pages and the Details pages, the MAP, CPU, HEALTH
and AI screens (sorted, with a selection and its details), and the rotation with those screens in it. The web files are the pages `web.Server` builds:
the dashboard (compact, `full=1`, `rotate=1`), CPU, the MAP as a tree and as a graph, HEALTH and AI, one tag per line outside `<pre>`.
`python3 -m unittest tests.test_golden` (about 2 s) compares each case and prints the first lines of the diff of the ones that differ.

`tests/golden.py` renders them in a `FrozenWorld`: a clock that stands still (UTC, whatever `TZ` says), the host name `demo-host`, the default
configuration (not the `config.ini` of the machine), the commands the advice names in their Linux words, and a host that always reads the same (CPUs,
RAM, disk, load, uptime, temperatures, network counters). The files are therefore the same on Linux, macOS and Windows, on any number of CPUs and on every
Python from 3.8 (the world also adds floats the old way and gives the graph's layout an exact `abs()`, because Python 3.12 and the C libraries of the
three systems do not round alike). If the renderer starts to read something else from the machine, fake it there, or the test fails somewhere else.
A golden test that passes on your machine and fails on one OS in CI has found a dependency on that OS: fix the code, not the file.

When a change is meant to alter what is drawn, regenerate and read the result:

```bash
NUC_GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden   # rewrites the files that differ, deletes those no case owns
git diff tests/golden                                        # this is the change the user will see: review it before you commit it
```

Commit the regenerated files with the code and say in the message why they changed; the reviewer reads the diff too. Never regenerate just to turn a
red test green. A new screen, size or option is a new case in `_cases()` of `tests/golden.py`. The files are `-text` in `.gitattributes` (a frame ends its
lines with CR LF on purpose): do not let an editor or a checkout convert them.

`tools/bench_render.py` times the same renders: the CPU milliseconds one frame costs (`time.process_time`, the fastest of a few batches) for the overview
at the four sizes, MAP, CPU, HEALTH, AI and the web pages. The console redraws every second or two on a machine that is meant to idle, so a refactor stays
within 1.2 times of the baseline:

```bash
python3 tools/bench_render.py --json > before.json    # on the commit before your change
python3 tools/bench_render.py --compare before.json   # after it: the ratios; exit status 1 if one view is more than 1.2 times slower
```

Compare runs on one machine and one Python, one after the other: the figures do not travel, and `before.json` is not committed.

## Most wanted

1. **Translations.** All on-screen strings are English and live in `src/render.py`, with the console primitives in `src/ansi.py`, the text helpers and the colour tokens in `src/ui.py` and the notes of the exposure rows in `src/exposure.py`. A small translation layer (a dict of message keys, `NUC_CONSOLE_LANG` / `[dashboard] language`) would let other languages be added without touching the layout code. Keep strings within the widths the tables allot them, and discuss the approach in an issue first.
2. nftables-native and firewalld support in the exposure logic; macOS `pf` rules; verifying macOS code signatures for the Application Firewall.
3. Other container runtimes (podman).
4. More sensors (AMD/ARM thermal, macOS, Windows), multiple NVMe.

## Pull requests

Small, focused, with tests. Describe the *why*. Do not include secrets, real hostnames, real IP addresses or machine-specific paths in code, tests, docs or screenshots (use `--demo`).

## Releasing

A release is a tag. Everything else is done by `.github/workflows/release.yml`, so nobody builds or uploads archives by hand.

1. **Version.** Set `VERSION` in `src/nuc_config.py` to `X.Y.Z` (the number the tag will have, without the `v`), commit and push. It is the only place
   the number lives: the workflow, `tools/build_release.py` and `nuc-console-update` (which compares it with the latest release) read it from there.
   **The Pythons** the archives carry must be pinned (see *Pinning the Pythons of the archives* below): the workflow stops while `tools/python-pins.json` holds `null`.
2. **Dry run (optional).** *Actions › release › Run workflow*: choose the branch (or tag) to build and type the tag you are about to create (`vX.Y.Z`).
   It runs the same checks, builds the same archives and smoke-tests them (see below), and you can download the archives from the run for seven days; it signs no provenance
   and creates no release.
3. **Tag.** `git tag -a vX.Y.Z -m "nuc-console X.Y.Z" && git push origin vX.Y.Z`. Only tags of the form `vX.Y.Z` work (no `-rc1`). The workflow then:
   - **refuses a tag that is not `VERSION`** (and one that is not `vX.Y.Z`);
   - runs the unit tests;
   - downloads the six Pythons the archives carry (four python-build-standalone tarballs for Linux and macOS, two embeddable zips for Windows) and checks each
     against its pin (`tools/python-pins.json`, `install-windows.ps1`);
   - builds the six archives (`linux-x86_64`, `linux-arm64`, `macos-arm64`, `macos-x86_64`, `windows-x64`, `windows-arm64`) and `SHA256SUMS` with
     `tools/build_release.py`, builds them again and checks that the second build is byte-identical and that `SHA256SUMS` matches, and runs the Linux x86-64 archive
     with its own Python;
   - **smoke-tests every archive** (jobs `smoke-unix` and `smoke-windows`, read-only) on a runner of its own system and processor, with no `setup-python` and no checkout, so the only
     Python it can use is the one it carries: `ubuntu-latest` (`linux-x86_64`), `ubuntu-24.04-arm` (`linux-arm64`), `macos-latest` (`macos-arm64`), `macos-15-intel`
     (`macos-x86_64`), `windows-latest` (`windows-x64`) and `windows-11-arm` (`windows-arm64`). Each job takes the archive from the build, checks it against `SHA256SUMS`
     (the file the release publishes), unpacks it and runs it: `./run.sh --which-python` (`run.cmd -WhichPython`) must print the Python inside the archive (`python/`), that
     Python must run and sit in the archive, `./run.sh --problems` (`run.cmd -Problems`) must exit 0, and `render.py --once --demo`, run by that Python, must draw the demo screen.
     The release job needs all six: **a broken archive is never published**. On Windows the archive is unpacked under Program Files, because the runner is an administrator and
     `run.ps1` refuses, as one, a folder that ordinary users can write to. `macos-15-intel` is the last Intel image GitHub hosts (announced until August 2027): when it goes,
     remove its entry from the matrix and say here that `macos-x86_64` is not run in CI (`tests/test_release.py` names the six entries);
   - attests every archive and `SHA256SUMS` (build provenance), and creates the release `nuc-console X.Y.Z` with them, with generated notes.
   The release is public as soon as the workflow ends, and from then on it is the *latest* one that `nuc-console-update` offers.
4. **Locally**, to look at what would ship: `python3 tools/build_release.py --version X.Y.Z --out dist --python-dir DIR`. `--list-python` prints the file, SHA-256 and URL
   of the six Pythons to download into `DIR` (it refuses while the pins are `null`); there is no build without them, because every archive carries its Python, and a
   file in `DIR` with another size or SHA-256 than its pin is refused. The version must be `VERSION`, and the build warns when the tree has uncommitted changes
   (the archives are of the files git tracks).

What goes in an archive is computed from the files git tracks, so a new file is shipped without touching the script: everything except `tests/`, `tools/`,
`CONTRIBUTING.md` and dotfiles (`.github/`); `systemd/` only in the Linux archive, `launchd/` and `install-macos.sh` only in the macOS one, `*.cmd` / `*.bat` /
`*.ps1` only in the Windows ones, shell scripts and `scripts/` not in the Windows ones. Name and place a new file accordingly. The rules are in the docstring of
`tools/build_release.py` and tested in `tests/test_release.py`.

The Python of the Windows archives is pinned in one place, `$PyVersion` / `$PyBuilds` in `install-windows.ps1`: the installer, `run.ps1`, the build script and the
workflow all read it from there. Changing it is a normal commit; the next release carries the new Python. The actions of the workflow are pinned by commit SHA: to
update one, change the SHA and the version in the comment together.

### Pinning the Pythons of the archives (Linux and macOS)

The Linux and macOS archives carry a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) CPython (`install_only_stripped`, one tarball
per target). Its version, release tag, file names, SHA-256 and sizes are pinned in `tools/python-pins.json`, the one source of truth: the build script, the release workflow and the
tests read it. While a value in it is `null` (a fresh repository, or after you reset it) `tools/build_release.py` and the release workflow **refuse** to build. To pin, or to
move to a newer Python:

1. On GitHub: *Actions › ai-pins › Run workflow* (a branch is fine). The job `python-pins` runs `tools/python_pins.py` on a runner with network access. It reads the latest
   python-build-standalone release and takes the newest stable **3.13.x** that has all four targets (`x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`,
   `aarch64-apple-darwin`, `x86_64-apple-darwin`; 3.12.x if there is no complete 3.13), and prints in its log and in the job summary, for each: file name, size, SHA-256 (the
   release's `SHA256SUMS`, which must agree with the asset digest) and the ready-to-paste JSON. It also downloads the four files, checks bytes against numbers, reads them the way the build does
   (`python/` folder, links, exec bits) and runs the Linux x86-64 one. Locally the same: `python3 tools/python_pins.py` (needs network access; `--series 3.12`, `--release TAG`, `--download DIR`).
2. Paste the JSON over `tools/python-pins.json`, read the diff (the file names must carry the Python version and the release tag you expect) and commit it. A pull request that changes the
   file runs the same job, which then compares the pinned values with the release that holds them and reports any difference.
3. `python3 -m unittest discover -s tests`, then a dry run of *Actions › release* on the branch: it downloads the six Pythons, checks them against the pins, builds the archives
   twice and compares them, runs the Linux one, and smoke-tests all six archives on their own runners. Download the artifact `dist` for a look, and try `./run.sh` on a real
   machine if you can: the smoke test does not start the dashboard.

A new Python is a new pin in a new release: nothing updates by itself, and nothing in the archives downloads a Python.

## Pinning the AI manifest

`nuc-console-ai setup` downloads only what `RUNTIME` and `MODELS` in `src/aisetup.py` pin, and refuses while a value is `None`. A
release must fill them, from the sources, never from memory or from a web page. (A new model is an entry in `MODELS`, kept in rank order,
`rank` 1..n with the best first: `params_b`, and `active_b` for a mixture of experts, `layers` (the blocks `--gpu-layers` counts), `ctx_max`,
`approx_mb` (the file, in MB), `ram_mb`, a one-line ASCII `notes`, and the `repo` and `file` it comes from; `pins` then finds the rest.)

1. On a machine with network access: `python3 src/aisetup.py pins`. It asks the GitHub API for the llamafile release (asset digest and size)
   and the Hugging Face API for every model (the repository's current commit, and the file's LFS SHA-256 and size at that commit) and prints one
   line per entry. An `ERROR` line means the repository or the file name in the code is wrong or moved: fix the entry, do not guess.
2. Check the output: the licence it prints must be Apache-2.0 or MIT (`ALLOWED_LICENSES`; the tests refuse others); the file must be the
   Q4_K_M GGUF you meant (or the model's own quantisation, noted in its entry); the size should be near its `approx_mb`, which only feeds the
   advice before and after pinning. Change `approx_mb` if it is off by more than a few percent.
3. Paste `revision`, `sha256` and `size` into the entries (for the runtime `sha256` and `size`, and its `version` and `url` when you move to a newer
   llamafile). Then check the flags `serve_argv` passes (`--server --host --port -m -a -t -c`, `--gpu auto -ngl N` or
   `--gpu disable`, and the runtime's `args`) against `llamafile --help` of that version, and that the models that need a recent llama.cpp
   (SmolLM3, gpt-oss) load with it. A model's `revision` is a 40-hex commit, never `main`.
4. `python3 -m unittest discover -s tests`, then try it for real on each OS you can reach: `setup`, `serve`, `status`, a question with
   `nuc-console-ask`, and `nuc-console-ai remove`. A new model or a new runtime is a new pin in a new release; nothing updates by itself.
5. Keep `docs/AI.md` in step with the code: the table of models (ids, names, parameters, approximate sizes, needs, context, notes) with `MODELS`
   (`python3 src/aisetup.py models` prints the numbers), the verdicts and the speed table with the constants at the top of `src/aihw.py`.

## Regenerating the README screenshots

```bash
python3 src/render.py --once --demo --color --cols 226 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · overview, 3-column layout (demo data)" > docs/img/overview.svg
python3 src/render.py --once --demo --color --cols 120 --rows 40 | python3 tools/ansi2svg.py --title "nuc-console · 120×40 console, single column (demo data)" > docs/img/compact.svg
python3 src/render.py --once --demo --color --view map --expand fit --select shop-api --details --cols 200 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · MAP: who reaches what, and what is behind it (demo data)" > docs/img/map.svg
# the CPU screen (docs/img/cpu.png) the same way: http://127.0.0.1:8799/?view=cpu&sel=<a pid>&pause=1, window 1760x940
# the HEALTH page (docs/img/health.png): http://127.0.0.1:8799/?view=health&sel=mem-leak%3Anode&pause=1, window 1760x840
# the AI page (docs/img/ai.png): the demo web view shows the invented machines and simulates the buttons:
#   python3 src/web.py --demo --port 8796 & PID=$!   # then window 1760x900: http://127.0.0.1:8796/?view=ai&pause=1 ; kill $PID
# the graph view is a browser page: run the demo web view and take a screenshot with any Chromium-based browser
python3 src/web.py --demo --port 8799 &   # then:
chromium --headless --hide-scrollbars --window-size=1600,1000 --screenshot=docs/img/graph.png "http://127.0.0.1:8799/?view=map&as=graph&sel=<key of shop-api-1>"
kill %1
```
