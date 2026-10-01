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
| `collector.py` | macOS: root (LaunchDaemon) · Windows: SYSTEM (scheduled task) | Apple's tools run as root only from SIP-protected folders (this includes `/usr/bin/powermetrics` for the CPU temperature); third-party tools (`docker`, `tailscale`, and `smctemp` / `osx-cpu-temp` when installed) run **as the user who owns them** or the user at the console, never as root (a user-writable binary must not become root code). On Windows only `%SystemRoot%` and `%ProgramFiles%` are searched, never the working directory |
| `web.py` | macOS: `_nuc-console` · Windows: LOCAL SERVICE | **on by default there, on 127.0.0.1 only** (`--local`): it is how the dashboard is shown. Not reachable from the network; any local user or program can read it, like the state files. `[display] mode = none` and `[web] enabled = no` turn it off. With `[web] enabled = yes` it runs as configured there, as on Linux |
| `render.py --open` / `--kiosk` | the logged-in user | at login: opens that page in the default browser, or full screen in a browser window with a profile of its own |
| Python | macOS: a root-owned python.org or Command Line Tools Python (never Homebrew's) · Windows: a private embeddable Python in `%ProgramFiles%` | the installers pin the python.org files by SHA-256 and check their signature; the Windows `._pth` file makes it ignore `PYTHONPATH` and see only its own library and the code |
| State | macOS: `/var/run/nuc-console` · Windows: `%ProgramData%\nuc-console` | Windows: the installer replaces the inherited ACL so only SYSTEM and Administrators can write (a user could otherwise fake the state or edit what SYSTEM reads) |

Design rules you can audit in the code:

- **No network exposure by default.** The collector and the tty renderer open no socket. The read-only web view (`web.py`; Linux: off by default; macOS/Windows: on, bound to 127.0.0.1 only, as the dashboard) is the only listener that is part of the dashboard (the optional AI model server below listens on 127.0.0.1 only, when you start it): GET only, no JavaScript except one inline script on the MAP's graph view (pinned by its SHA-256 in that page's CSP; it can open no connection), strict CSP, loopback unless a token is configured (it refuses to start otherwise), token compared in constant time and read from a 0600 file; see [docs/WEB.md](docs/WEB.md).
- **No shell, no user-controlled command lines.** Commands are fixed argument lists run with `subprocess.run([...])` (no `shell=True`) and a fixed `PATH`; the only variable arguments are container IDs/PIDs obtained from Docker itself. Windows PowerShell receives fixed scripts as `-EncodedCommand` (no quoting, no user input).
- **The history keeps names and counts, not text.** With `[features] health` on, the collector writes `history.db` (SQLite,
  world-readable like the state files): per app CPU and memory per hour, events (crash, hang, OOM, restart, failed service,
  unexpected shutdown, hardware error, failed logins as a count) and log messages reduced to *templates* (numbers, ids, IP
  addresses, paths, quoted strings and every value after `=` / `:` replaced), so a secret in a log line is not stored. It runs
  `journalctl -o json` with fixed fields (Linux; the cursor it passes back is validated), `docker ps` / `docker inspect` with
  validated ids, a fixed PowerShell script over the System and Application event logs that reads event ids and names, never
  the message text (Windows), and reads the first 4 KB of crash reports in `DiagnosticReports`, regular files only, no links
  followed (macOS, including the users' folders). The IP addresses and user names of failed logins are never stored.
- **The optional local model analyses and never acts.** Off by default (`[ai] enabled = no`; the AI screen, `[features] ai`, only reads
  the hardware with fixed commands and no shell, as the unprivileged user). The advisor talks only to an endpoint on this machine
  (it refuses any other unless `[ai] allow_remote = yes`, and connects only to the addresses it checked); the model server that
  `nuc-console-ai serve` starts binds `127.0.0.1` and has no option to bind anything else. What the model receives is the HEALTH
  findings as JSON: names, counts and log *templates*, never log lines or command lines. Names are data, never instructions (they
  travel inside the JSON only; the model's text is stripped of escape sequences and control characters and capped before it is shown;
  every answer is marked "AI, check before acting"). To answer questions it may pick one of six fixed read-only queries: arguments
  validated against whitelists and ranges, bound parameters, a read-only connection, three at most. It has no tool that runs a command,
  writes a file or changes a setting. Nothing of it runs in the root collector, and a page never starts a generation. Details and the
  model list: [docs/AI.md](docs/AI.md).
- **One downloaded program, pinned.** `nuc-console-ai setup`, when you run it, downloads the llamafile runtime and a model file
  from GitHub and Hugging Face: HTTPS only (a redirect to `http://` is refused), a size and a SHA-256 written in the code (a model
  from a commit, not a branch), a temporary name until the check passes, no automatic update, permissive licences only. It refuses to
  download anything that is not pinned yet. The server runs as its own unprivileged account at low priority (systemd unit with a
  sandbox and a memory cap on Linux).
- **Process names, never command lines.** The CPU screen lists processes by name (and PID, user, CPU, memory): their arguments can hold passwords or tokens, so they are never read or shown. On Windows the collector reads CPU sensors through WMI (LibreHardwareMonitor / OpenHardwareMonitor namespaces, ACPI thermal zones) with a fixed PowerShell script.
- **Untrusted text is sanitised.** Container names, process names, journal lines etc. can contain terminal escape sequences; everything shown passes through `safe()` which strips control characters.
- **Secrets are never stored or displayed.** To find which containers use a database, the collector checks whether container environment variable *names/values reference the DB's hostname*; it keeps only the match result, never the values (`env_uses`). Tests assert this.
- **Fail-open for alarms.** Missing or unparsable data is reported as unknown (`?`) and treated as exposed, never as "OK".
- **Malformed state files cannot crash the dashboard** (a crash would leave a black screen); they produce an error block instead.
- **Only the standard library is used**: no third-party code to audit or to be supply-chain-compromised. The one exception is the optional
  AI model server above (llamafile and a model file, pinned by SHA-256, started by you, never by the collector).

Things to be aware of (by design):

- The JSON state files are **world-readable** so the unprivileged renderer can read them. They contain your topology (ports, container names, which container or process talks to which, the IPs of clients seen connected and of the hosts your services connect to) but no secrets. Do not run this on a multi-user machine where local users must not see that.
- **The monitor itself shows your topology** to anyone who can see the screen.
- A local model's advice can be wrong, or steered by a name or a log message that someone else controls (an app, a container, a
  service): it is a hint to check, not an instruction, and it is marked as such. With a GPU backend loaded, llamafile's own
  system-call sandbox cannot be used (the GPU drivers need device access); `[ai] gpu = no` keeps the server on the CPU.
- The `docker` group is root-equivalent; that is why only the root collector talks to Docker.
- `scripts/enable-ufw.sh` and `scripts/rebind-all-dbs.sh` are optional helpers that change your firewall/containers. Read them and use `--dry-run` first. They are never run by `install.sh`.

## Verifying a release

The repository is scanned with `gitleaks` (history + tree), `trufflehog` and `semgrep`; the test-suite includes checks that secrets in container environments are never emitted. Run the same tools yourself before trusting any build.
