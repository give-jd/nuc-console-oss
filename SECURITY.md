# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include the version/commit, what you did, and what you saw.
You can expect an acknowledgement within a few days. Only the latest release is supported.

## Threat model

nuc-console has two parts with different privilege:

| Part | Runs as | Reads | Writes |
|---|---|---|---|
| `collector.py` | **root** (systemd, `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`) | output of `docker`, `ss`, `ufw`, `iptables`, `fail2ban-client`, `tailscale`, `systemd-analyze`, `systemctl`, `journalctl`, `nsenter` (`ss` inside each running container's network namespace, for the MAP; `[features] map = no` stops it), `/proc/<pid>/cgroup` of listening processes | `/run/nuc-console/*.json` (0644, atomic rename) |
| `render.py` | unprivileged user `nuc-console` (`NoNewPrivileges`, `ProtectSystem=strict`) | `/proc`, `/sys`, the JSON files, `/var/lib/nuc-console/baseline.json` | the tty only |

On **macOS** and **Windows** the split is the same:

| Part | Runs as | Notes |
|---|---|---|
| `collector.py` | macOS: root (LaunchDaemon) · Windows: SYSTEM (scheduled task) | Apple's tools run as root only from SIP-protected folders; third-party tools (`docker`, `tailscale`) run **as the user who owns them** or the user at the console, never as root (a user-writable binary must not become root code). On Windows only `%SystemRoot%` and `%ProgramFiles%` are searched, never the working directory |
| `web.py` | macOS: `_nuc-console` · Windows: LOCAL SERVICE | **on by default there, on 127.0.0.1 only** (`--local`): it is how the dashboard is shown. Not reachable from the network; any local user or program can read it, like the state files. `[display] mode = none` and `[web] enabled = no` turn it off. With `[web] enabled = yes` it runs as configured there, as on Linux |
| `render.py --open` / `--kiosk` | the logged-in user | at login: opens that page in the default browser, or full screen in a browser window with a profile of its own |
| Python | macOS: a root-owned python.org or Command Line Tools Python (never Homebrew's) · Windows: a private embeddable Python in `%ProgramFiles%` | the installers pin the python.org files by SHA-256 and check their signature; the Windows `._pth` file makes it ignore `PYTHONPATH` and see only its own library and the code |
| State | macOS: `/var/run/nuc-console` · Windows: `%ProgramData%\nuc-console` | Windows: the installer replaces the inherited ACL so only SYSTEM and Administrators can write (a user could otherwise fake the state or edit what SYSTEM reads) |

Design rules you can audit in the code:

- **No network exposure by default.** The collector and the tty renderer open no socket. The read-only web view (`web.py`; Linux: off by default; macOS/Windows: on, bound to 127.0.0.1 only, as the dashboard) is the only listener: GET only, no JavaScript, strict CSP, loopback unless a token is configured (it refuses to start otherwise), token compared in constant time and read from a 0600 file; see [docs/WEB.md](docs/WEB.md).
- **No shell, no user-controlled command lines.** Commands are fixed argument lists run with `subprocess.run([...])` (no `shell=True`) and a fixed `PATH`; the only variable arguments are container IDs/PIDs obtained from Docker itself. Windows PowerShell receives fixed scripts as `-EncodedCommand` (no quoting, no user input).
- **Untrusted text is sanitised.** Container names, process names, journal lines etc. can contain terminal escape sequences; everything shown passes through `safe()` which strips control characters.
- **Secrets are never stored or displayed.** To find which containers use a database, the collector checks whether container environment variable *names/values reference the DB's hostname*; it keeps only the match result, never the values (`env_uses`). Tests assert this.
- **Fail-open for alarms.** Missing or unparsable data is reported as unknown (`?`) and treated as exposed, never as "OK".
- **Malformed state files cannot crash the dashboard** (a crash would leave a black screen); they produce an error block instead.
- **Only the standard library is used**: no third-party code to audit or to be supply-chain-compromised.

Things to be aware of (by design):

- The JSON state files are **world-readable** so the unprivileged renderer can read them. They contain your topology (ports, container names, which container or process talks to which, the IPs of clients seen connected and of the hosts your services connect to) but no secrets. Do not run this on a multi-user machine where local users must not see that.
- **The monitor itself shows your topology** to anyone who can see the screen.
- The `docker` group is root-equivalent; that is why only the root collector talks to Docker.
- `scripts/enable-ufw.sh` and `scripts/rebind-all-dbs.sh` are optional helpers that change your firewall/containers. Read them and use `--dry-run` first. They are never run by `install.sh`.

## Verifying a release

Releases are built by `.github/workflows/release.yml` when a tag `vX.Y.Z` is pushed: it checks that the tag is `VERSION` in
`src/nuc_config.py`, runs the tests, builds the archives with `tools/build_release.py` and signs a build provenance for each one
(GitHub artifact attestation, minted by the workflow itself: no signing key is stored anywhere). The only secret it uses is the
built-in `GITHUB_TOKEN`. The Windows archives carry the python.org embeddable Python, checked against the same SHA-256 the installer pins.

```bash
sha256sum -c SHA256SUMS                                                  # the archives are the ones listed (macOS: shasum -a 256 -c)
gh attestation verify nuc-console-X.Y.Z-linux.tar.gz --repo give-jd/nuc-console-oss   # built by that workflow, from that repository
```

The Linux and macOS archives are reproducible: `python3 tools/build_release.py --version X.Y.Z --out dist` on the tag gives the same
bytes (sorted entries, the commit time as modification time, no user names; the compressed stream also depends on the zlib of the Python
that builds it, 3.12 in CI). The Windows archives need the two Python zips: `--python-zips DIR`, see `--list-python`.

The installers keep what they download and trust it only after checking it again: Windows `%ProgramData%\nuc-console\cache` (write access
only for SYSTEM and Administrators, like the rest of that folder), macOS `/Library/Caches/nuc-console` (root-owned: a copy that is not root's
0644 file in a root-owned 0755 folder is replaced, never used). Both compare the SHA-256 pinned in the script; macOS also checks the signature.

The repository is scanned with `gitleaks` (history + tree), `trufflehog` and `semgrep`; the test-suite includes checks that secrets in container environments are never emitted. Run the same tools yourself before trusting any build.
