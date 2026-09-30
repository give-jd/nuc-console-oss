# Contributing

Thanks for helping. The project is small on purpose: **Python standard library only, one file per process, no build step.**

## Development

```bash
git clone <this repository> && cd nuc-console
python3 -m unittest discover -s tests            # must pass on Python 3.8 - 3.13
python3 src/render.py --once --demo --cols 200 --rows 50
shellcheck install.sh scripts/*.sh bin/*         # if you touch shell
```

- Every change needs a test. Parsers get fixtures (see `tests/test_nuc_console.py`); **use documentation addresses** (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `100.64.0.0/10`, `*.example.ts.net`) and fake values built at runtime for anything secret-looking.
- Never add a dependency. If you need one, the answer is almost certainly a stdlib function or fewer features.
- Keep Python 3.8 compatibility (no `match`, no `X | Y` types, no `str.removeprefix`).
- The collector must **fail per section** (one broken command must not blank the others) and treat missing tools as `Absent`, not as errors.
- Anything that can be wrong must show `?` / "unknown", never a reassuring green.
- Test layouts at several sizes: `--cols 79 --rows 24`, `120x33`, `200x50`, `226x50`.

## Most wanted

1. **Translations.** All on-screen strings are English and live in `src/render.py`. A small translation layer (a dict of message keys, `NUC_CONSOLE_LANG` / `[dashboard] language`) would let other languages be added without touching the layout code. Keep strings within the widths the tables allot them, and discuss the approach in an issue first.
2. nftables-native and firewalld support in the exposure logic.
3. Other container runtimes (podman).
4. More sensors (AMD/ARM thermal), multiple NVMe.

## Pull requests

Small, focused, with tests. Describe the *why*. Do not include secrets, real hostnames, real IP addresses or machine-specific paths in code, tests, docs or screenshots (use `--demo`).

## Regenerating the README screenshots

```bash
python3 src/render.py --once --demo --color --cols 226 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console · overview, 3-column layout (demo data)" > docs/img/overview.svg
python3 src/render.py --once --demo --color --cols 120 --rows 40 | python3 tools/ansi2svg.py --title "nuc-console · 120×40 console, single column (demo data)" > docs/img/compact.svg
```
