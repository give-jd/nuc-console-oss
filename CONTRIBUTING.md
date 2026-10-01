# Contributing

Thanks for helping. The project is small on purpose: **Python standard library only, one file per process, no build step.**

## Development

```bash
git clone <this repository> && cd nuc-console
python3 -m unittest discover -s tests            # must pass on Python 3.8+, on Linux, macOS and Windows (CI runs all three)
python3 src/render.py --once --demo --cols 200 --rows 50
python3 src/render.py --once --demo --demo-os windows --cols 200 --rows 50   # the screen as the Windows (or darwin) collector writes it
python3 src/render.py --once --demo --view ai --cols 200 --rows 50           # the AI screen: three invented machines (--demo-os windows|darwin for the others)
python3 src/render.py --once --demo --view ai --select qwen3-8b --details    # the details of one model: why, licence, the commands
shellcheck install.sh install-macos.sh scripts/*.sh bin/nuc-console-{accept,problems,ai,ask}   # if you touch shell
```

- Every change needs a test. Parsers get fixtures (see `tests/test_nuc_console.py`); **use documentation addresses** (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `100.64.0.0/10`, `*.example.ts.net`) and fake values built at runtime for anything secret-looking.
- Never add a dependency. If you need one, the answer is almost certainly a stdlib function or fewer features.
- Keep Python 3.8 compatibility (no `match`, no `X | Y` types, no `str.removeprefix`).
- Platform code lives in `collect_darwin.py` / `collect_windows.py` (collector), `hostinfo.py` / `winapi.py` (renderer metrics):
  parsers are pure functions with fixtures, so they are tested on every OS; the classes `OnWindows` / `OnMacOS` in
  `tests/test_platforms.py` exercise the real system calls on their own OS. Text output of system tools is localised on Windows:
  use the API or PowerShell objects (`ConvertTo-Json`), never `netstat`/`netsh` text.
- The AI screen reads the hardware in `aihw.py` the same way: the commands (`nvidia-smi`, `sysctl`, `vm_stat`, `system_profiler`) and the files
  (`/proc`, `/sys`) go through two injectable functions, so each OS has fixtures in the tests; fixed argument lists, a short time limit, never a
  shell, and whatever cannot be read goes to the notes instead of being guessed. The thresholds of the five verdicts are named constants at
  the top of that file; change one together with the table in `docs/AI.md`.
- The collector must **fail per section** (one broken command must not blank the others) and treat missing tools as `Absent`, not as errors.
- Anything that can be wrong must show `?` / "unknown", never a reassuring green.
- JavaScript: the web view has none except `src/graphjs.py` (the MAP's graph view). Keep it that way; `tests/test_graphjs.py` lists
  what that script may not do (build markup, eval, network, globals...). Its hash goes into the page's CSP automatically.
- Test layouts at several sizes: `--cols 79 --rows 24`, `120x33`, `200x50`, `226x50`. The MAP screen: `--view map` (with `--expand all|fit|N`, `--select TEXT`, `--details`, `--only`). The CPU screen: `--view cpu` (with `--sort cpu|mem|time|pid|user`, `--select NAME|PID`, `--details`). The HEALTH screen: `--view health` (with `--period 1|7|30`, `--select TEXT`, `--details`; `--demo-health little|none` for a machine with 5 hours of history or none). The AI screen: `--view ai` (with `--select TEXT`, `--details`; the demo has an NVIDIA box, a small Windows laptop and an Apple-silicon Mac). `--demo-os windows|darwin` for their data.

## Most wanted

1. **Translations.** All on-screen strings are English and live in `src/render.py`. A small translation layer (a dict of message keys, `NUC_CONSOLE_LANG` / `[dashboard] language`) would let other languages be added without touching the layout code. Keep strings within the widths the tables allot them, and discuss the approach in an issue first.
2. nftables-native and firewalld support in the exposure logic; macOS `pf` rules; verifying macOS code signatures for the Application Firewall.
3. Other container runtimes (podman).
4. More sensors (AMD/ARM thermal, macOS, Windows), multiple NVMe.

## Pull requests

Small, focused, with tests. Describe the *why*. Do not include secrets, real hostnames, real IP addresses or machine-specific paths in code, tests, docs or screenshots (use `--demo`).

## Pinning the AI manifest

`nuc-console-ai setup` downloads only what `RUNTIME` and `MODELS` in `src/aisetup.py` pin, and refuses while a value is `None`. A
release must fill them, from the sources, never from memory or from a web page:

1. On a machine with network access: `python3 src/aisetup.py pins`. It asks the GitHub API for the llamafile release (asset digest and size)
   and the Hugging Face API for every model (the repository's current commit, and the file's LFS SHA-256 and size at that commit) and prints one
   line per entry. An `ERROR` line means the repository or the file name in the code is wrong or moved: fix the entry, do not guess.
2. Check the output: the licence it prints must be Apache-2.0 or MIT (`ALLOWED_LICENSES`; the tests refuse others); the file must be the
   Q4_K_M GGUF you meant (or the model's own quantisation, noted in its entry); the size should be near its `approx_mb`, which only feeds the
   advice before and after pinning. Change `approx_mb` if it is off by more than a few percent.
3. Paste `revision`, `sha256` and `size` into the entries (the `version` and `url` of the runtime too, when you move to a newer llamafile, and
   then check its `args` against `llamafile --help` of that version). A model's `revision` is a 40-hex commit, never `main`.
4. `python3 -m unittest discover -s tests`, then try it for real on each OS you can reach: `setup`, `serve`, `status`, a question with
   `nuc-console-ask`, and `nuc-console-ai remove`. A new model or a new runtime is a new pin in a new release; nothing updates by itself.
5. Keep `docs/AI.md` (the table of models: names, approximate sizes, notes) in step with `MODELS`.

## Regenerating the README screenshots

```bash
python3 src/render.py --once --demo --color --cols 226 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · overview, 3-column layout (demo data)" > docs/img/overview.svg
python3 src/render.py --once --demo --color --cols 120 --rows 40 | python3 tools/ansi2svg.py --title "nuc-console · 120×40 console, single column (demo data)" > docs/img/compact.svg
python3 src/render.py --once --demo --color --view map --expand fit --select shop-api --details --cols 200 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · MAP: who reaches what, and what is behind it (demo data)" > docs/img/map.svg
# the CPU screen (docs/img/cpu.png) the same way: http://127.0.0.1:8799/?view=cpu&sel=<a pid>&pause=1, window 1760x940
# the HEALTH page (docs/img/health.png): http://127.0.0.1:8799/?view=health&sel=mem-leak%3Anode&pause=1, window 1760x840
# the AI page: http://127.0.0.1:8799/?view=ai&sel=qwen3-8b&pause=1 (the demo web view shows the invented machines)
# the graph view is a browser page: run the demo web view and take a screenshot with any Chromium-based browser
python3 src/web.py --demo --port 8799 &   # then:
chromium --headless --hide-scrollbars --window-size=1600,1000 --screenshot=docs/img/graph.png "http://127.0.0.1:8799/?view=map&as=graph&sel=<key of shop-api-1>"
kill %1
```
