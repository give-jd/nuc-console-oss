# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include the version/commit, what you did, and what you saw.
You can expect an acknowledgement within a few days. Only the latest release is supported.

## Threat model

nuc-console has two parts with different privilege:

| Part | Runs as | Reads | Writes |
|---|---|---|---|
| `collector.py` | **root** (systemd, `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`) | output of `docker`, `ss`, `ufw`, `iptables`, `fail2ban-client`, `tailscale`, `systemd-analyze`, `journalctl`, `nsenter` | `/run/nuc-console/*.json` (0644, atomic rename) |
| `render.py` | unprivileged user `nuc-console` (`NoNewPrivileges`, `ProtectSystem=strict`) | `/proc`, `/sys`, the JSON files, `/var/lib/nuc-console/baseline.json` | the tty only |

Design rules you can audit in the code:

- **No network exposure by default.** The collector and the tty renderer open no socket. The optional read-only web view (`web.py`, off by default) is the only listener: GET only, no JavaScript, strict CSP, loopback unless a token is configured (it refuses to start otherwise), token compared in constant time and read from a 0600 file; see [docs/WEB.md](docs/WEB.md).
- **No shell, no user-controlled command lines.** Commands are fixed argument lists run with `subprocess.run([...])` (no `shell=True`) and a fixed `PATH`; the only variable arguments are container IDs/PIDs obtained from Docker itself.
- **Untrusted text is sanitised.** Container names, process names, journal lines etc. can contain terminal escape sequences; everything shown passes through `safe()` which strips control characters.
- **Secrets are never stored or displayed.** To find which containers use a database, the collector checks whether container environment variable *names/values reference the DB's hostname*; it keeps only the match result, never the values (`env_uses`). Tests assert this.
- **Fail-open for alarms.** Missing or unparsable data is reported as unknown (`?`) and treated as exposed, never as "OK".
- **Malformed state files cannot crash the dashboard** (a crash would leave a black screen); they produce an error block instead.
- **Only the standard library is used**: no third-party code to audit or to be supply-chain-compromised.

Things to be aware of (by design):

- The JSON state files are **world-readable** so the unprivileged renderer can read them. They contain your topology (ports, container names, IPs of clients seen on databases) but no secrets. Do not run this on a multi-user machine where local users must not see that.
- **The monitor itself shows your topology** to anyone who can see the screen.
- The `docker` group is root-equivalent; that is why only the root collector talks to Docker.
- `scripts/enable-ufw.sh` and `scripts/rebind-all-dbs.sh` are optional helpers that change your firewall/containers. Read them and use `--dry-run` first. They are never run by `install.sh`.

## Verifying a release

The repository is scanned with `gitleaks` (history + tree), `trufflehog` and `semgrep`; the test-suite includes checks that secrets in container environments are never emitted. Run the same tools yourself before trusting any build.
