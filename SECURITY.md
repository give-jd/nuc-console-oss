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
| `render.py` | unprivileged user `nuc-console` (`NoNewPrivileges`, `ProtectSystem=strict`) | `/proc`, `/sys`, the JSON files, `/var/lib/nuc-console/baseline.json` | the tty, and `/var/lib/nuc-console/ai` (`ReadWritePaths`: the local model's files, `web.json`, and the model server the AI screen starts as its child) |
| `web.py` (optional) | the same user, its own hardened unit | the same state | `/var/lib/nuc-console/ai` only (`ReadWritePaths`), and only for the AI page's buttons ([below](#the-web-view-is-not-purely-read-only)) |
| `notify.py` (optional Telegram notifier, **off by default**) | unprivileged user `nuc-console-notify`, not the web view's `nuc-console`, so the web view cannot read the token (`UMask=0077`, `NoNewPrivileges`, `ProtectSystem=strict`, empty `CapabilityBoundingSet`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`; no listener, no socket activation) | the same state files as `render.py`, plus **the bot token** and the paired chat in `/var/lib/nuc-console-notify` (folder 0711, files 0600: only `nuc-console-notify` and root can read them; never in `config.ini`, logs or `status.json`) | that folder (`sent.json`, `status.json`) and, **outbound only, HTTPS to `api.telegram.org`**: the host name and the problem **titles** (with `detail = full`, also container names and ports). It never reads incoming messages and has no commands; see [docs/TELEGRAM.md](docs/TELEGRAM.md) |

On **macOS** and **Windows** the split is the same:

| Part | Runs as | Notes |
|---|---|---|
| `collector.py` | macOS: root (LaunchDaemon) · Windows: SYSTEM (scheduled task) | Apple's tools run as root only from SIP-protected folders (this includes `/usr/bin/powermetrics` for the CPU temperature); third-party tools (`docker`, `tailscale`, and `smctemp` / `osx-cpu-temp` when installed) run **as the user who owns them** or the user at the console, never as root (a user-writable binary must not become root code). On Windows only `%SystemRoot%` and `%ProgramFiles%` are searched, never the working directory |
| `web.py` | macOS: `_nuc-console` · Windows: LOCAL SERVICE | **on by default there, on 127.0.0.1 only** (`--local`): it is how the dashboard is shown. Not reachable from the network; any local user or program can read it, like the state files, and use the AI page's buttons (they write only the AI folder: macOS `/Library/Application Support/nuc-console/ai`, owned by `_nuc-console`; Windows `%ProgramData%\nuc-console\ai`, where LOCAL SERVICE may modify). `[display] mode = none` and `[web] enabled = no` turn it off. With `[web] enabled = yes` it runs as configured there, as on Linux |
| `render.py --open` / `--kiosk` | the logged-in user | at login: opens that page in the default browser, or full screen in a browser window with a profile of its own |
| Python | every release archive carries its own: Linux and macOS a python-build-standalone CPython in `python/` (an install copies it to `/opt/nuc-console/python`, root-owned), Windows a private embeddable Python in `%ProgramFiles%` (a portable run: in the `python\` folder of the extracted ZIP). An install from a clone: Linux the system's `python3`; macOS a root-owned python.org or Command Line Tools Python (never Homebrew's) | the bundled Pythons are pinned by SHA-256 (and size) and checked when the archive is built; the installers of a clone pin the python.org files by SHA-256 and check their signature; the Windows `._pth` file makes it ignore `PYTHONPATH` and see only its own library and the code. [The Python in the archives](#the-python-in-the-archives) |
| State | macOS: `/var/run/nuc-console` · Windows: `%ProgramData%\nuc-console` | Windows: the installer replaces the inherited ACL so only SYSTEM and Administrators can write (a user could otherwise fake the state or edit what SYSTEM reads) |
| `notify.py` (optional, off by default) | macOS: `_nuc-console` (LaunchDaemon; the macOS web view is on 127.0.0.1 only) · Windows: NETWORK SERVICE (scheduled task), not the web view's LOCAL SERVICE | outbound HTTPS to `api.telegram.org` only, as on Linux. The bot token: macOS `/var/lib/nuc-console-notify` (0711, files 0600, owned by `_nuc-console`, `Umask` 0077) · Windows `%ProgramData%\nuc-console\notify\private` (ACL: SYSTEM, Administrators and NETWORK SERVICE only; the folder above holds `status.json` only). The uninstallers delete it first |

Design rules you can audit in the code:

- **No network exposure by default.** The collector and the tty renderer open no socket. The web view (`web.py`; Linux: off by default; macOS/Windows: on, bound to 127.0.0.1 only, as the dashboard) is the only listener that is part of the dashboard (the optional AI model server below listens on 127.0.0.1 only, when you start it): GET and HEAD only, except the forms of the AI page ([below](#the-web-view-is-not-purely-read-only)), no JavaScript except one inline script on the MAP's graph view (pinned by its SHA-256 in that page's CSP; it can open no connection), strict CSP, loopback unless a token is configured (it refuses to start otherwise), token compared in constant time and read from a 0600 file; see [docs/WEB.md](docs/WEB.md). The optional Telegram notifier (`notify.py`, off by default) is not a listener either: it makes outbound HTTPS connections to `api.telegram.org` only, never listens, never reads incoming messages and accepts no commands (only `--setup`, run by an administrator, asks Telegram once for the `/start` message carrying its one-time code); see [docs/TELEGRAM.md](docs/TELEGRAM.md).
- **The new web shell's appearance cookie** (`[ui] web = app` or `?app=1`, off by default). `/?set=` stores one choice of look (theme, density, preset, order, start view, key figures, layout) in the `nuc_ui` cookie: `HttpOnly; SameSite=Strict`, at most 256 bytes, parsed by a strict grammar that ignores what it does not know, never echoed raw into a page. It stores nothing on the machine and changes nothing but how the page looks. The redirect after it is rebuilt from validated view parameters (no open redirect), and a request the browser marks cross-site is refused. The shell's pages carry the same strict CSP, with `style-src 'self'` added for its one style sheet (`/s/app.<sha8>.css`, immutable, behind the same `Host` and token checks); it is still read-only, and the AI page's forms and CSRF are unchanged. See [docs/WEB.md](docs/WEB.md#the-new-shell-preview-opt-in).
- <a id="the-web-view-is-not-purely-read-only"></a>**The web view is not purely read-only: the AI page has buttons** (`[ai] web_actions`, default `yes`; `no` is the lock). What they can do, and nothing else: download the pinned llamafile runtime and one of the pinned models into the AI folder, start the model server (the pinned runtime, on 127.0.0.1) as a child of the web service and stop it, write `web.json` there (on/off, a catalog model id, the loopback endpoint of that server), delete those files, and relay a question or an "advice now" to the local model. **By whom:** anyone who may open the page: with loopback + `tailscale serve` your whole tailnet, with a token its holders, on macOS/Windows every local user and program; there is no second login, so if that is too many, set `web_actions = no`. **Protections:** a per-process random CSRF token in every form (`hmac.compare_digest`); `Origin`/`Referer`, when present, must be the request's own host and port and `Sec-Fetch-Site` same-origin (or none); `application/x-www-form-urlencoded` only, 4 KB at most, 20 fields; a model id must be in the catalog and a number a number, so nothing from a request reaches a path, a command line or a shell; Post/Redirect/Get; the strict CSP with `form-action 'self'` on that one page and `form-action 'none'` everywhere else, still no script; one job at a time, a lock file between the web view and the console, a disk-space check before a download. They never write `config.ini` (root's), and `web.json` is only read by an account that owns it or root owns it, never by root, with a loopback endpoint only. **What it costs:** the web account (and the tty account, which is the same user) can write the AI folder, so it can replace the runtime and the models in it: `nuc-console-ai serve --install-service` hashes them again before it installs a service that runs them as another account; and the web unit has `MemoryMax=85%`/`TasksMax=512` instead of 256M/64 because the model server is a child in its cgroup (the rest of its sandbox is unchanged). Details: [docs/AI.md](docs/AI.md#from-the-browser-and-the-console), [docs/WEB.md](docs/WEB.md#the-ai-pages-buttons).
- **No shell, no user-controlled command lines.** Commands are fixed argument lists run with `subprocess.run([...])` (no `shell=True`) and a fixed `PATH`; the only variable arguments are container IDs/PIDs obtained from Docker itself. Windows PowerShell receives fixed scripts as `-EncodedCommand` (no quoting, no user input).
- **The history keeps names and counts, not text.** With `[features] health` on, the collector writes `history.db` (SQLite,
  world-readable like the state files): per app CPU and memory per hour, events (crash, hang, OOM, restart, failed service,
  unexpected shutdown, hardware error, failed logins as a count) and log messages reduced to *templates* (numbers, ids, IP
  addresses, paths, quoted strings and every value after `=` / `:` replaced), so a secret in a log line is not stored. It runs
  `journalctl -o json` with fixed fields (Linux; the cursor it passes back is validated), `docker ps` / `docker inspect` with
  validated ids, a fixed PowerShell script over the System and Application event logs that reads event ids and names, never
  the message text (Windows), and reads the first 4 KB of crash reports in `DiagnosticReports`, regular files only, no links
  followed (macOS, including the users' folders). The IP addresses and user names of failed logins are never stored.
- **The optional local model analyses and never acts.** Off by default (`[ai] enabled = no`; the AI screen, `[features] ai`, reads
  the hardware with fixed commands and no shell, as the unprivileged user, and sets the model up when you click: see the bullet above). The advisor talks only to an endpoint on this machine
  (it refuses any other unless `[ai] allow_remote = yes`, and connects only to the addresses it checked); the model server that
  `nuc-console-ai serve` starts binds `127.0.0.1` and has no option to bind anything else. What the model receives is the HEALTH
  findings as JSON: names, counts and log *templates*, never log lines or command lines. Names are data, never instructions (they
  travel inside the JSON only; the model's text is stripped of escape sequences and control characters and capped before it is shown;
  every answer is marked "AI, check before acting"). To answer questions it may pick one of six fixed read-only queries: arguments
  validated against whitelists and ranges, bound parameters, a read-only connection, three at most. It has no tool that runs a command,
  writes a file or changes a setting. Nothing of it runs in the root collector, and a page never starts a generation by itself (only a click on Ask or advice now does, through the same limits). Details and the
  model list: [docs/AI.md](docs/AI.md).
- **One downloaded program, pinned.** `nuc-console-ai setup`, when you run it, or a model's button on the AI page, when you click it, downloads the llamafile runtime and the model files you name
  from GitHub and Hugging Face: HTTPS only (a redirect to `http://` is refused), a size and a SHA-256 written in the code (a model
  from a commit, not a branch), a temporary name until the check passes, no automatic update, permissive licences only. It refuses to
  download anything that is not pinned yet (the runtime and the twelve models are pinned in this release). The server `serve --install-service` installs runs as its own unprivileged account at low priority (Linux: a systemd unit with a sandbox and a memory cap; macOS: a
  hidden `_nuc-console-ai` account under launchd; Windows: LOCAL SERVICE); the one a button starts is a child of the web view (or the console), under that account and its unit.
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
- **With the Telegram notifier on, what it sends leaves the machine** and is stored by Telegram (bot chats are not end-to-end encrypted). By default that is the host name and the problem *titles* ("Container unhealthy"); `detail = full` adds container names and ports. Leave it off, or keep `detail = titles`, if even that must not leave. Whoever holds the bot token can send messages as your bot: it is only in `/var/lib/nuc-console-notify` (Windows: `%ProgramData%\nuc-console\notify\private`), never in `config.ini`; `nuc-console-telegram --forget` deletes it and `/revoke` in @BotFather kills it.
- A local model's advice can be wrong, or steered by a name or a log message that someone else controls (an app, a container, a
  service): it is a hint to check, not an instruction, and it is marked as such. With a GPU backend loaded, llamafile's own
  system-call sandbox cannot be used (the GPU drivers need device access), and on Linux the systemd unit has to let the service see the
  GPU (`PrivateDevices=no`, the `render` and `video` groups); the rest of the unit's sandbox stays. `[ai] gpu = no` keeps the server on the
  CPU, with `PrivateDevices=yes`.
- The `docker` group is root-equivalent; that is why only the root collector talks to Docker.
- `scripts/enable-ufw.sh` and `scripts/rebind-all-dbs.sh` are optional helpers that change your firewall/containers. Read them and use `--dry-run` first. They are never run by `install.sh`.

## Verifying a release

Releases are built by `.github/workflows/release.yml` when a tag `vX.Y.Z` is pushed, and by nothing else. On a fresh runner it:

1. checks that the tag is `vX.Y.Z` and that it is `VERSION` in `src/nuc_config.py`;
2. runs the unit tests;
3. downloads the six Pythons the archives carry (four python-build-standalone tarballs for Linux and macOS, the two python.org embeddable zips for Windows) and
   checks each against its pin (`tools/python-pins.json`, `install-windows.ps1`); it stops there while a pin is missing: there is no archive without a pinned Python;
4. builds the six archives with `tools/build_release.py` (which checks every Python again, size and SHA-256), builds them a second time and compares the two byte for
   byte, checks `SHA256SUMS`, and runs the Linux x86-64 archive's own Python once;
5. runs every archive, in read-only jobs, on a runner of its own system and processor (Linux and macOS: x86-64 and ARM, Windows: x64 and ARM64) with no Python set up
   for it: it is checked against `SHA256SUMS`, unpacked, and its `run.sh --which-python` (`run.cmd -WhichPython`) must name the Python inside it, `--problems` must
   exit 0 and a `--once --demo` render must work. Nothing below runs unless all six passed;
6. signs a build provenance for every archive and for `SHA256SUMS` (a GitHub artifact attestation, SLSA build provenance minted by the workflow
   itself: no signing key is stored anywhere);
7. creates the GitHub release with the archives and `SHA256SUMS`.

What it is allowed to do: the only secret it uses is the built-in `GITHUB_TOKEN` (no personal token, no signing key, no upload credential). The
token is read-only (`contents: read`) except for the one job, which also gets `contents: write` (to create the release), `id-token: write` and
`attestations: write` (for the provenance), and nothing more. Every action it uses is pinned by commit SHA, with the release it stands for in a comment
(`tests.yml`, which only runs the tests with read-only rights and no secret, uses version tags). Started by hand (*Actions › release › Run
workflow*) it is a dry run: the same checks and archives, kept as a workflow artifact for seven days, with no provenance signed and no release created.

```bash
sha256sum --ignore-missing -c SHA256SUMS      # the archives are the ones listed (macOS: grep the line of your archive | shasum -a 256 -c -)
gh attestation verify nuc-console-X.Y.Z-linux-x86_64.tar.gz --repo give-jd/nuc-console-oss   # built by a workflow of that repository
```

`SHA256SUMS` says the file is whole and is the one listed; it comes from the same release, so it does not protect against a release published by
someone who can publish releases on this repository. The attestation says that a workflow of this repository produced exactly this file, which is
what protects against a swapped archive. Neither is a signature by a person, and nothing in the archives is code-signed (no Authenticode,
no notarization): the scripts are only as trustworthy as these two checks and the repository itself. `SHA256SUMS` can be verified with `gh attestation verify` too.

The archives are reproducible: `python3 tools/build_release.py --version X.Y.Z --out dist --python-dir DIR` on the tag gives the same
bytes (sorted entries, the commit time as modification time, no user names; the compressed stream also depends on the zlib of the Python
that builds it, 3.12 in CI). `DIR` holds the six Pythons: `--list-python` prints the file, SHA-256 and URL of each.

### The Python in the archives

Every archive carries the Python it runs with, so that a machine needs no Python and no network to use it. What is bundled, where it comes from and how it is pinned:

| Archives | What | Where it comes from | Pinned in | Verified |
|---|---|---|---|---|
| Linux and macOS, `x86_64` and `arm64` | a CPython from **python-build-standalone** (`install_only_stripped`: relocatable, stripped), already unpacked in `python/` | [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) releases on GitHub, the file of the archive's target (`x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`, `aarch64-apple-darwin`, `x86_64-apple-darwin`) | `tools/python-pins.json`: Python version, release tag, file name, SHA-256 and size of each tarball | the release workflow checks each download against the pin, `tools/build_release.py` checks it again (size and SHA-256) and refuses to build otherwise |
| Windows, `x64` and `arm64` | the official **embeddable Python** from python.org, as the zip it is published as (`python\python-<version>-embed-<arch>.zip`) | python.org | `$PyVersion` and `$PyBuilds` in `install-windows.ps1` (the hashes were checked against python.org's Sigstore signatures when they were pinned) | the same two checks, and `install-windows.ps1` and `run.ps1` check the zip again before they unpack it and accept `python.exe` only with a valid Authenticode signature of the Python Software Foundation |

How the pins get their values: the `python-pins` job of `.github/workflows/ai-pins.yml` (`tools/python_pins.py`) reads the latest python-build-standalone release on a
runner with network access and prints the newest stable 3.13.x (3.12.x if there is none) for the four targets: file name, size and SHA-256 (the release's `SHA256SUMS`,
which must agree with the digest GitHub computed for the asset, else it stops), downloads the files to check the bytes against those numbers, and shows the
JSON in the job summary. A maintainer pastes it into `tools/python-pins.json` in a reviewed commit; a value is never typed from memory, and a `null` there
(not pinned yet) stops the build and the release workflow. Changing the Python is that one commit; the next release carries the new one. Nothing is downloaded
or updated by the program: the Python of a release is the one that was pinned when it was built.

What the build does with a python-build-standalone tarball: it unpacks it into `python/` of the archive, keeping exec bits, symbolic links (relative, inside `python/`:
`python/bin/python3 -> python3.13`) and the file times (the `.pyc` files of the standard library may record them), with no owner. It refuses a tarball with a
member outside `python/`, an absolute path or `..`, a link that is absolute or leaves the tree, an entry below a link, or a device. The archive is then covered by
`SHA256SUMS` and by the build provenance like everything else in it, and a build from the same tag and the same pins is byte-identical.

At run time: `run.sh` and the installers use that Python (Linux: the system's `python3` first when it is 3.8+, for an install). An installer copies it to
`/opt/nuc-console/python`, owned by root and not writable by others, because the collector runs it as root; `run.sh` run as root refuses a `python/` that is a link or
that others can write to, before it runs it. It is unpacked from the archive as it is, nothing about it is modified, and nothing else on the system uses it (it
has no `site-packages` of ours, and no pip is run). Neither the Python nor anything else in the archives is code-signed by this project: on macOS the Python is
ad-hoc signed by python-build-standalone, and a browser download is quarantined until `xattr -dr com.apple.quarantine <folder>` (the installer clears the mark from its
copy). On Windows the embeddable Python is private to nuc-console and its `._pth` file makes it ignore `PYTHONPATH` and see only its own library and the code.

### Download caches

The installers and the updater keep what they download and trust it only after checking it again: Windows `%ProgramData%\nuc-console\cache` (write access
only for SYSTEM and Administrators, like the rest of that folder), macOS `/Library/Caches/nuc-console` (root-owned: a copy that is not root's
0644 file in a root-owned 0755 folder is replaced, never used), Linux `/var/cache/nuc-console` (the updater only; root-owned). The installers compare the SHA-256
pinned in the script; macOS also checks the signature. The updater compares the SHA-256 that `SHA256SUMS` lists.

The repository is scanned with `gitleaks` (history + tree), `trufflehog` and `semgrep`; the test-suite includes checks that secrets in container environments are never emitted. Run the same tools yourself before trusting any build.

## Portable mode and the updater

`run.sh` / `run.cmd` run everything as the user who starts them (as root only if you use `sudo` / *Run as administrator*: then keep the folder
yours alone; `run.sh` refuses a folder others can write to, `run.ps1` does not check), write only inside `./data`, and listen on `127.0.0.1` with no token: nothing but
the same machine can connect (`[web]` settings are ignored there).

`nuc-console-update` runs only when you start it (the program itself never downloads anything; the only other downloads are the one-time Python of an installer run from a clone: the release archives carry theirs). What it does and checks:

- it talks to `api.github.com` and to the assets of the release on `github.com`, over HTTPS only (a redirect too), with a size limit; it runs nothing it
  downloaded before the checks below have passed;
- the archive must have the SHA-256 that `SHA256SUMS` of the same release lists: a mismatch deletes it and stops;
- when `gh` is installed and logged in it runs `gh attestation verify` on the archive (`--repo give-jd/nuc-console-oss`) and a failure stops the update;
  without `gh`, or without a login, it says that the provenance was not checked and goes on. It does not verify the attestation of `SHA256SUMS` itself;
- the `VERSION` inside the archive must be the release's; a member with an absolute path or `..` is refused, and so is a device or a hard link in a `.tar.gz`
  and a symbolic link that is absolute or leaves the archive's folder (the links of the bundled Python stay inside `python/`); tar members are unpacked as 0755 or 0644
  only: no setuid;
- it asks for the archive of this system **and processor** (`linux-x86_64`, `linux-arm64`, `macos-arm64`, `macos-x86_64`, `windows-x64`, `windows-arm64`): a processor
  without one, or a release that lacks it, is refused with a message, never a guess at another archive;
- as root it refuses a cache folder that is not root's alone, runs `gh` only when root owns it and nobody else can write it, and starts Python with `-I`
  (the `PYTHON*` variables and the user's site are ignored); in a portable folder it refuses to run as root unless root owns the folder;
- an installed one is then updated by the installer of that release, exactly as if you ran it yourself (`sudo`, or an administrator prompt: the updater never calls `sudo`);
  a portable folder has its code replaced and keeps `data/` and `cache/`.

As with any `SHA256SUMS`, this protects against a damaged or swapped download, not against a malicious release: the attestation, which `gh` checks, is what says who
built it. Details: [docs/PORTABLE.md](docs/PORTABLE.md), [docs/INSTALL.md](docs/INSTALL.md#update).
