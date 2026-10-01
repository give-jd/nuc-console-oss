# Web view

The same screen as the monitor, in a browser: over your LAN, your Tailscale tailnet or a VPN. **Read-only, opt-in, no API, GET only, no
JavaScript** — with two exceptions, each boxed in: the **AI page** (`/?view=ai`) has buttons (forms that POST to `/ai/...`: choose a model,
switch the AI on or off, delete, ask: [below](#the-ai-pages-buttons)), and the MAP's graph view carries one small script, pinned by its hash
(see below). Configuration is *not* editable from the web on purpose (see below); `[ai] web_actions = no` makes the AI page read-only too.

It is a separate service (`nuc-console-web`, unprivileged user, hardened unit). It is **off** until you enable it:
until then, no port is opened by this project.

## Enable it (recommended: loopback + `tailscale serve`)

```ini
# /etc/nuc-console/config.ini
[web]
enabled = yes
# bind = 127.0.0.1   (default)
# port = 8787
```

```bash
sudo ./install.sh        # or: sudo systemctl enable --now nuc-console-web
tailscale serve --bg 8787   # HTTPS on your tailnet name, authenticated by Tailscale
```

The service listens on `127.0.0.1` only; `tailscale serve` exposes it to *your tailnet members* over HTTPS. Nothing is published to the LAN or the Internet
(do **not** use `tailscale funnel`).

## Other ways to reach it

Binding to any address other than loopback **requires a token**; without one the service refuses to start (fail closed):

```bash
openssl rand -hex 24 | sudo install -m 0600 -o nuc-console -g nuc-console /dev/stdin /etc/nuc-console/web.token
```

```ini
[web]
enabled = yes
bind = 192.168.0.10        # the LAN address (or your Tailscale IP)
token_file = /etc/nuc-console/web.token
```

The token must be 16+ characters from `A-Z a-z 0-9 . _ ~ -` and the file must be mode 0600 owned by root or `nuc-console`: otherwise the service refuses to start.

Open `http://192.168.0.10:8787/?token=<the token>` once: the token is moved into an `HttpOnly; SameSite=Strict` cookie and the URL is cleaned.
Scripts can send `Authorization: Bearer <token>`. The token lives in a file, never in `config.ini` (world-readable).
Plain HTTP on a LAN sends the token in clear text: prefer `tailscale serve` or a TLS reverse proxy for anything you don't fully trust.

A non-loopback listener shows up as a **new exposed port** in the dashboard's own alarms until you accept it (`sudo nuc-console-accept`): that is intended.

## Hardening built in

- **DNS-rebinding guard**: without a token, requests whose `Host` is not `localhost`, `127.0.0.1`, `::1`, the bind address, the hostname, a `*.ts.net` name or one listed in `[web] allowed_hosts` get `421`.
- **Connection cap** (32) and a **15 s total deadline per request**: a slow client cannot hold threads. The unit adds `ProtectSystem=strict` with one writable place, `ReadWritePaths=-/var/lib/nuc-console/ai` (the AI folder), `TasksMax=512`, `MemoryMax=85%`, `PrivateDevices`, `RestrictNamespaces` and more. (`TasksMax` and `MemoryMax` were 64 and 256M before the AI page could start a model server: it is a child of this service and lives in its cgroup; 85% of the RAM is the line the page's "too big" is drawn at.)
- Page cache: layout widths are rounded to steps of 20, so at most a dozen distinct renders exist and a burst costs one render per size.
- Render errors go to the journal, never to the page.

## What it serves

| Path | |
|---|---|
| `/` | the overview screen as HTML (`?cols=100` compact, `?cols=200` wide; 60–300), auto-refresh by `<meta refresh>` every `[dashboard] refresh_seconds` |
| `/?full=1` | the overview **plus every Details page**: everything the overview cuts ("… +N more"), stacked |
| `/?zoom=150` | text size in % (50–200, the **A− / A+** links); default `[display] zoom` |
| `/?fit=1` | the text fills the window width: a bigger zoom means fewer columns, re-laid out; every section and item is shown and the page scrolls. With `rows=` it fills the height instead (one screen, like the console) |
| `/?rotate=1` | overview and Details pages take turns, as on the console (the full-screen window uses it) |
| `/?view=map` | the **MAP** (the **map** link in the bottom bar): every row is a link. ▸/▾ opens or closes a branch, a name shows its details pane. The whole state is in the URL, so a view can be bookmarked: `open=`/`shut=` the branches opened/closed by hand (row keys), `all=1` everything open, `sel=` the row whose details are shown, `only=1` problems only, `pause=1` no reload while you read. Off with `[features] map = no` |
| `/?view=map&as=graph` | the MAP as a **graph**: circles and lines, like Obsidian's graph view (the **tree \| graph** switch on the MAP page). Zones, ports, processes, containers and remote addresses are circles coloured by state and sized by how connected they are; seen links are solid, declared dashed, same-network dotted. A click on a circle selects it (details pane, its neighbours highlighted); `local=1` / `local=2` show only it and what is 1 or 2 links away, `only=1` the paths to a problem, `ext=0` hides remote addresses, `stacks=1` adds the compose projects, `z=50`…`300` zooms without a script. With the script: drag a circle (it stays where you put it, double-click to release it), drag the background to pan, mouse wheel or pinch to zoom, `+` `-` `0` and arrows on the keyboard; the view survives the page's refresh. At most 400 circles are drawn ("+N more" says what is left out) |
| `/?view=cpu` | the **CPU** screen (the **cpu** link in the bottom bar): processor, per-core load, frequency and temperature, processes. `sort=mem` / `time` / `pid` / `user` (CPU% by default), `sel=<pid>` the details of one process (each row is a link). Process names only, never command lines. Off with `[features] cpu = no` |
| `/?view=health` | the **HEALTH** page (the **health** link in the bottom bar): the findings over the history kept by the collector, top CPU and memory apps per day, events, noisy and new log templates, disks, hot hours, boots. `period=1` / `7` / `30` (days, 7 by default), `sel=<finding id>` the details and fix of one finding (each finding is a link), `pause=1` no reload. Names and counts only ([HEALTH.md](HEALTH.md)). Off with `[features] health = no` |
| `/?view=ai` | the **AI** page (the **ai** link in the bottom bar): the AI switch and what it is doing (a download with its progress, the model server starting), the chat, the hardware found (RAM, GPU memory) and, for each local model, whether it fits (GPU, GPU+CPU, RAM, slow, too big), a rough speed, the commands of the command line and a **use this model** button; `sel=<model id>` its details, `pause=1` no reload, `confirm=on\|delete\|delete-all` the question the page asks before it does that. It has forms (below) unless `[ai] web_actions = no` locks it ([AI.md](AI.md)). It reloads by itself only while something runs (a download, a start, an answer), every 2 s, so that a question being typed is not lost. Off with `[features] ai = no` |
| `/?refresh=5` | reload every 5 s (1–10, the **− / +** links in the bottom bar); default `[dashboard] refresh_seconds` |
| `/healthz` | `ok` (no data) |

Everything else is 404; any method but GET is 405 (and `POST` of the AI page's forms, below). Security headers: strict CSP (`default-src 'none'`),
`no-store`, `nosniff`, `frame-ancestors 'none'`, `no-referrer`; the AI page differs in two, see below. No access log (URLs may carry a token).

## The AI page's buttons

`/?view=ai` is the one page with forms. They do what [AI.md](AI.md#from-the-browser-and-the-console) describes; here is how they are guarded.

| POST | Fields | Does |
|---|---|---|
| `/ai/use` | `model` | the one action: download what is missing (runtime, model: pinned, SHA-256 checked), start the model server, wait until it answers, turn the advisor on with it |
| `/ai/on` | (`model`, `confirm=yes`) | AI on with the model chosen before; with none chosen it redirects to a question naming the recommended model and its size, and only `confirm=yes` with that `model` goes on |
| `/ai/off` | | stops the model server this process started, turns the advisor off |
| `/ai/cancel` | | stops the download or the start that runs (what was fetched is kept) |
| `/ai/delete` | `model`, `confirm=yes` | deletes that model's files; without `confirm=yes` it only redirects to the question |
| `/ai/delete-all` | `confirm=yes` | deletes the runtime and every model; the same |
| `/ai/ask` | `q` (500 characters at most) | a question, answered by the model in the background |
| `/ai/advise` | `days` = 1, 7 or 30 | advice on the HEALTH findings of that period, written now |

Every one answers **303** to `/?view=ai...` (Post/Redirect/Get: a reload never posts twice); the work runs in a background thread, the page shows it. Every form
carries `csrf` (this process's random token) and `back` (the view to come back to: parsed again, never used as an address).

- **Same access as viewing**: no token = loopback and a known `Host` name (else `421`); with a token, that token as a bearer or cookie (else `401`). Behind
  `tailscale serve` that means everyone who can open the page can press the buttons: if that is more than you want, `[ai] web_actions = no`.
- **`[ai] web_actions = no`** (default `yes`): the page shows "locked by config.ini" and has no form, and a post is refused with `403`. `[features] ai = no`: `404`.
- **CSRF token**: per process, random, in a hidden field of every form, compared with `hmac.compare_digest`; wrong or missing: `403`.
- **Where a post comes from**: if a request has an `Origin` or a `Referer`, its host and port must be the `Host` the request was addressed to (`null`, another
  port, another name, a user name in front: `403`); `Sec-Fetch-Site` other than `same-origin` or `none`: `403`. The AI page is therefore sent with
  `Referrer-Policy: same-origin` (the other pages keep `no-referrer`: with it a browser sends `Origin: null` on a post, which would be refused).
- **Body**: `application/x-www-form-urlencoded` (else `415`), a `Content-Length` of at most **4 KB** (else `413`, not read; none or chunked: `400`), at most 20 fields.
- **Values**: a model id must be one the catalog has (else `400`), a number is digits; a question is cleaned and cut by the advisor; nothing from a request
  reaches a path, a command line or a shell, and the files and the server they act on are the pinned ones.
- **CSP**: the AI page has `form-action 'self'` (its forms post to this server and nowhere else); every other page, every error and every redirect has
  `form-action 'none'`. There is still no script on the AI page, and `default-src 'none'`.
- **The page after a post** is never an old cached one (a post clears the page cache); like every page it shows this process's state, the same for every viewer.

## The one script (graph view)

Every page is plain HTML except the MAP's graph view, which carries one small inline script (`src/graphjs.py`, ~16 KB, no
library) for dragging, zooming and panning. It is the only exception, and it is boxed in:

- its SHA-256 is in that page's CSP (`script-src 'sha256-…'`): no other script, inline or loaded, can run, and every other
  page keeps a CSP without `script-src`;
- it only moves what the server drew: it never builds HTML from data, and `default-src 'none'` (no `connect-src`) means it
  cannot open any connection or load anything; it keeps the view (zoom, pan, circles placed by hand) in `sessionStorage`;
- the page works without it: every circle is a link, zoom has links, and the page then refreshes by `<meta refresh>`
  (inside `<noscript>`; with the script, the script reloads the page itself, never while you are dragging);
- `tests/test_graphjs.py` rejects any change that adds markup building, `eval`, timers with strings, network or storage
  APIs, globals, or anything outside the page's own elements.

## Why there is no configuration editor

`config.ini` is owned by root; the web process is unprivileged. Writing it from the browser would require either a root process listening on the network
or a privileged helper — a large jump in risk for a file you change a few times a year. Edit it over SSH, then `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web`.

## Threat model in one paragraph

The page shows your topology (ports, container names, client IPs seen on databases, and on the map which service talks to which), exactly like the monitor. With loopback + `tailscale serve` only your tailnet can read it.
With a token, anyone holding the token can. It cannot change the machine, with one exception on the AI page: whoever can open it can set up the local model
(download the pinned files into the AI folder, start the model server on 127.0.0.1, turn the advisor on and off, delete those files) and ask it questions, as the
unprivileged web account, within the unit's sandbox; `[ai] web_actions = no` removes that. Rendering is cached for half the refresh interval per layout size.
The graph view's script runs in your browser and cannot send anything anywhere (the CSP forbids connections).

## macOS and Windows

There the web view **is** the dashboard: the installers run it as the hidden user `_nuc-console` (macOS LaunchDaemon) or as
LOCAL SERVICE (Windows scheduled task `\nuc-console\web`), with `--local`: while `[web] enabled = no` it listens on **127.0.0.1
only**, without a token (nothing on the network can reach it), whatever `bind` says. Set `[web] enabled = yes` (and a token for a
non-loopback `bind`) to reach it from other devices as described above, then run the installer again. `[display] mode = none`
with `[web] enabled = no` runs no web view at all.
Windows has no mode bits: keep `token_file` inside `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write
(and limit who can read the file with an ACL if other people use the machine).

A portable run ([PORTABLE.md](PORTABLE.md): `./run.sh --web` (the default on macOS) or `run.cmd`) starts the web view the same way on any system: 127.0.0.1 only, no
token, a free port (or `--port`), whatever `[web]` says; it stops with the run.
