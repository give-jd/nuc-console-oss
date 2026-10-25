# Web view

The same screen as the monitor, in a browser: over your LAN, your Tailscale tailnet or a VPN. **Read-only, opt-in, GET (and HEAD) only.** What every screen shows is also a read-only **data API**, JSON and a live stream ([below](#the-data-api)), and **`/app`** draws the screens from it in the browser, live, without a reload ([below](#the-live-app)). The web view is **the shell** (cards, key figures, five screens; [below](#the-shell-the-web-interface)). The older **classic** pages (the console's text turned into HTML, once kept as `[ui] web = classic` / `?app=0`) are gone; a config file that still says `web = classic` loads, and the key is ignored. No JavaScript is needed, and four exceptions are boxed in: the **AI page** (`/?view=ai`) has buttons (forms that POST to `/ai/...`: choose a model,
switch the AI on or off, delete, ask: [below](#the-ai-pages-buttons)), the **Telegram page** (`/?view=telegram`) has buttons too (pair the notifier with your bot, switch it on or off, send a test: [below](#the-telegram-pages-buttons)), the shell carries three small first-party scripts per page (four in all, with the layout editor's), pinned by their hashes ([below](#the-shells-scripts)), and the MAP's graph view one small script of its own, pinned the same way ([below](#the-graph-views-script)). Configuration is *not* editable from the web on purpose (see below), except in a portable run and in the desktop app, whose `config.ini` is your own file: there the settings page turns the `[features]` switches on and off ([below](#the-settings-pages-switches)) and edits the other keys ([below](#the-settings-pages-configini)), all but the locks; an installation's page changes presentation keys only, in [an overlay file](#an-installations-settings-overlay) laid over `config.ini` (opt-in: `[web] settings_actions = yes` and a `[web] token_file`); `[ai] web_actions = no` makes the AI page read-only too, `[telegram] web_actions = no` the Telegram page.

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
Behind a proxy that speaks HTTPS to the browser and says so (`X-Forwarded-Proto: https`, as `tailscale serve` does) the cookies (`nuc_token`, `nuc_ui`) also carry `Secure`;
over plain http, which includes the loopback address, they do not (a browser drops a `Secure` cookie that arrives over http). The header can only make a cookie
stricter, so any peer may send it.
The view you asked for stays: `/?token=<the token>&view=map` lands on the MAP, `…&view=cpu&sort=mem` on the CPU page sorted by memory. The redirect
rebuilds the address from the parameters the page understands, with their values checked; anything else is dropped, and the token never comes back.
Scripts can send `Authorization: Bearer <token>`. The token lives in a file, never in `config.ini` (world-readable).
Plain HTTP on a LAN sends the token in clear text: prefer `tailscale serve` or a TLS reverse proxy for anything you don't fully trust.

A non-loopback listener shows up as a **new exposed port** in the dashboard's own alarms until you accept it (`sudo nuc-console-accept`): that is intended.

## Hardening built in

- **DNS-rebinding guard**: without a token, requests whose `Host` is not `localhost`, `127.0.0.1`, `::1`, the bind address, the hostname, a `*.ts.net` name or one listed in `[web] allowed_hosts` get `421`.
- **Connection cap** (32) and a **15 s total deadline per request**: a slow client cannot hold threads. A stream of the data API is the one request the deadline does not end: at most 8 are open at once (more get `503`), each ends after 5 minutes, and a client that stops reading is dropped by the socket's 10 s timeout ([below](#the-data-api)). The unit adds `ProtectSystem=strict` with one writable place, `ReadWritePaths=-/var/lib/nuc-console/ai` (the AI folder), `TasksMax=512`, `MemoryMax=85%`, `PrivateDevices`, `RestrictNamespaces` and more. (`TasksMax` and `MemoryMax` were 64 and 256M before the AI page could start a model server: it is a child of this service and lives in its cgroup; 85% of the RAM is the line the page's "too big" is drawn at.)
- Page cache: layout widths are rounded to steps of 20, so at most a dozen distinct renders exist and a burst costs one render per size.
- Render errors go to the journal, never to the page.

## What it serves

**The shell is what `/` serves** ([below](#the-shell-the-web-interface)). The query parameters that name a view (`view=`, `sort=`, `sel=`, `period=`, `all=`, `as=graph` ...) are the ones of the table; an old link with `app=`, `cols=`, `rows=`, `fit=` or `full=` still works, and those are ignored.

| Path | |
|---|---|
| `/` | the overview, as the shell ([below](#the-shell-the-web-interface)); auto-refresh by `<meta refresh>` (inside `<noscript>`) every `[dashboard] refresh_seconds` |
| `/?zoom=150` | text size in % (50–200, the **A− / A+** links); default `[display] zoom` |
| `/?rotate=N` | on the wall display (`kiosk=1` with the wall density) the seconds between two one-screen scrolls (3–600; default `[dashboard] rotate_seconds`) |
| `/?view=map` | the **MAP** (the **map** link in the bottom bar): every row is a link. ▸/▾ opens or closes a branch, a name shows its details pane. The whole state is in the URL, so a view can be bookmarked: `open=`/`shut=` the branches opened/closed by hand (row keys), `all=1` everything open, `sel=` the row whose details are shown, `only=1` problems only, `pause=1` no reload while you read. Off with `[features] map = no` |
| `/?view=map&as=graph` | the MAP as a **graph**: circles and lines, like Obsidian's graph view (the **tree \| graph** switch on the MAP page). Zones, ports, processes, containers and remote addresses are circles coloured by state and sized by how connected they are; seen links are solid, declared dashed, same-network dotted. A click on a circle selects it (details pane, its neighbours highlighted); `local=1` / `local=2` show only it and what is 1 or 2 links away, `only=1` the paths to a problem, `ext=0` hides remote addresses, `stacks=1` adds the compose projects, `z=50`…`300` zooms without a script. With the script: drag a circle (it stays where you put it, double-click to release it), drag the background to pan, mouse wheel or pinch to zoom, `+` `-` `0` and arrows on the keyboard; the view survives the page's refresh. At most 400 circles are drawn ("+N more" says what is left out) |
| `/?view=cpu` | the **CPU** screen (the **cpu** link in the bottom bar): processor, per-core load, frequency and temperature, processes. `sort=mem` / `time` / `pid` / `user` (CPU% by default), `sel=<pid>` the details of one process (each row is a link). Process names only, never command lines. Off with `[features] cpu = no` |
| `/?view=health` | the **HEALTH** page (the **health** link in the bottom bar): the findings over the history kept by the collector, top CPU and memory apps per day, events, noisy and new log templates, disks, hot hours, boots. `period=1` / `7` / `30` (days, 7 by default), `sel=<finding id>` the details and fix of one finding (each finding is a link), `pause=1` no reload. Names and counts only ([HEALTH.md](HEALTH.md)). In a narrow window or on the wall density the tables of top CPU and memory, logs and disks drop their least important columns (the `wprio` of each `Col`, hidden by container queries on the panel) instead of scrolling sideways. Off with `[features] health = no` |
| `/?view=ai` | the **AI** page (the **ai** link in the bottom bar): the AI switch and what it is doing (a download with its progress, the model server starting), the chat, the hardware found (RAM, GPU memory) and, for each local model, whether it fits (GPU, GPU+CPU, RAM, slow, too big), a rough speed, the commands of the command line and a **use this model** button; `sel=<model id>` its details, `pause=1` no reload, `confirm=on\|delete\|delete-all\|clear` the question the page asks before it does that, `prompt=<id>` the whole prompt the model was sent for that answer of the chat. It has forms (below) unless `[ai] web_actions = no` locks it ([AI.md](AI.md)). It reloads by itself only while something runs (a download, a start, an answer), every 2 s, so that a question being typed is not lost. Off with `[features] ai = no` |
| `/?refresh=5` | reload every 5 s (1–10, the **− / +** links in the bottom bar); default `[dashboard] refresh_seconds` |
| `/?view=events` | the **EVENTS** page (the **events** link in the bottom bar): what happened and when, newest first, from the history the collector keeps: crashes, out-of-memory kills, restarts, failed services, unexpected shutdowns, hardware errors, throttling, low disks, failed logins, and **problems** that appeared or cleared (`problem`: the id of the alarm and `opened (error)` / `cleared (error)`, nothing else of it). Made for the morning after: the monitor went black and the box was restarted, what did it say, and when? `age=1h` / `24h` (default) / `7d`, `kind=` one of the kinds (the **kind** links; the key `f` moves to the next), `pause=1` no reload. Times are UTC. **download CSV** / **download JSON** (`?view=events&format=csv\|json`, the same filters, up to 500 events): a CSV cell that starts with `=`, `+`, `-` or `@` gets a quote in front of it, so a spreadsheet never takes it for a formula; a history that is off or missing gives a 404 with the reason, not an empty file. On a phone (600 px and less) each event is a block of its own, nothing scrolls sideways. With `[features] health = no` the page says the history is off ([HEALTH.md](HEALTH.md#events-the-timeline)) |
| `/?view=hosts` | the **Hosts** page (tab `6`; [below](#hosts)), only while `[hosts]` has entries and `[features] fleet` is not `no`; otherwise it is an unknown view and the page is the overview. `host=<id>` one configured peer in detail (an id that is not configured: the list), `go=<id>` a redirect to that peer's own address (not a page) |
| `/healthz` | `ok` (no data) |
| `/api/v1/...` | the **data API**, with either interface: every screen as JSON, and as a stream of Server-Sent Events ([below](#the-data-api)) |
| `/metrics` | **Prometheus text** of the overview's figures, same access as the API ([below](#metrics)); `[web] metrics = no` makes it a `404` |
| `/app` | the **live app**: every screen drawn in the browser from the data API and kept up to date by its stream ([below](#the-live-app)) |

Everything else is 404; any method but GET and HEAD is 405 (`Allow: GET, HEAD`), except the `POST` of the AI page's and the Telegram page's forms (below).
HEAD is answered like GET (same status, same headers, `Content-Length` included, the same token and `Host` checks), without the body.
Security headers, on every response: strict CSP (`default-src 'none'`), `no-store`, `nosniff`, `frame-ancestors 'none'`, `no-referrer`,
`Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Opener-Policy: same-origin`; the AI page differs in two, see below.
No access log (URLs may carry a token).

## The shell (the web interface)

The web view is a shell made of cards, and the only web interface: the older classic pages (the console's text turned into HTML) and the switches that
chose them, `[ui] web = classic` and `?app=0`, are gone. A `config.ini` that still has a `web` key in `[ui]` loads; the key is ignored and reported once on
stderr (`[ui] web is gone: the app shell is the only web interface`).
Every control is a link or a form, so it works with scripts off, and the page then reloads by `<meta refresh>` (inside `<noscript>`). With scripts on, three small inline ones refresh it in place and add keys and instant preferences ([below](#the-shells-scripts)).

- **Top bar**: the host, the status pill (✔ ALL OK, ! warnings, ✖ problems, with its symbol), the five screens as tabs (keys 1–5; badges for the
  Map's problems, the Health findings and the AI), the clock, `?` (the keys, `#help`, shown by the browser's `:target`) and ⚙ (the settings).
- **Key figures** under it (`[ui] kpis`, the settings page): one tile each, a link to its card; a source that cannot be read is `?`, never green.
  A banner says so when a collector is not running.
- **Overview**: a grid of cards (12 / 6 / 1 columns by the window's width), in the order of `[ui] order` (by severity, or fixed) and the layout.
  The grid packs **densely**: its rows are a half em high and each card spans the rows the server estimates its height needs (the class `rN`, from the
  lines and table rows of its body: `htmlview.est_rows`), and `grid-auto-flow: dense` lets a later card fill the hole a taller one left, so a short card
  is not left alone beside a long one. The price is the reading order: a card may sit above one that comes earlier in the order (the order of the markup,
  of the keyboard and of the layout is unchanged). The rows are `auto`, so an estimate that is too low makes the rows taller instead of overlapping the next
  card, and one that is too high leaves a small hole. The layout editor (`?edit=1`) and a single card (`?card=`) are not packed: they stay in strict order.
  On a one-column window nothing spans. A card holds the text the console draws for that section (colours by the theme); a card that hid items says so and links to `/?card=<id>`, the card in full.
  **Edit layout** (footer, and Appearance in the settings) opens the layout editor ([below](#edit-the-layout)).
- **Map, CPU, Health, AI**: their existing pages, inside the same frame (their own controls in a bar above them). The CPU, Health and AI screens are drawn natively from the components the console draws too (no `<pre>`). The AI screen (`?view=ai`): the switch as a pill with what it is doing and a progress bar for a download (a moving bar when its size is not known yet), the chat (shown when it can be used or has something to show: the exchanges in a box that scrolls, its newest in sight, the question box, each answer with "AI, check before acting", "advice now", **Clear chat**) with **MODEL USAGE** beside it (the models loaded and where, the CPU and memory of the model server, how busy each GPU is; [AI.md](AI.md#from-the-browser-and-the-console)), the hardware and the status as labelled values, the models as a table (rows the keys move over with Enter to select, the less important columns dropped on a narrow screen, the verdict a pill with its symbol, a **use this model** button per row, the selected one's details beside it with **delete its files**), the folder and **delete everything**, and the question that waits (**Yes** / **No**). Every button is a POST form (same endpoints, field names and CSRF token) and carries `data-key` from the keymap (`e` on/off, `c` cancel, `u` use, `x` delete, `X` delete all, `y` / `n` the answers). When `[ai] web_actions = no` locks it, it shows a notice and has no form and no button.
  The **CPU** page is built of components, not text: key figures, a meter per logical CPU, the temperatures, and the process table, whose column heads are
  links (`p m t n u` do the same by keyboard) and whose rows are links to `sel=`; with a process selected its details sit beside the table (below it in a narrow window).
  The **Map** (tree view) is built of components too: the figures and the arrows' legend, a segmented control of links (`e` expand all, `c` collapse all, `p` problems
  only, and the graph view), the tree as a list whose rows are links (a row selects it, the selected one deselects it; its ▸ ▾ mark opens or closes the branch; the state
  is a symbol and a class, the depth an indent), and the selected node's details beside the tree with a link that closes them (the pane is sticky: it stays in sight while a long tree scrolls; in a narrow window it is drawn right under the selected row instead, a copy that CSS shows in place of the pane). j / k and the
  arrows move over the rows, Enter follows. The graph view (`as=graph`) is its own drawing.
- **Footer**: refresh − / +, pause, Edit layout, A− / A+ (`z50` … `z200`), theme and density as links, "read-only · AI actions".
- **Blocks**: the top bar (`__top`), the key figures (`__kpis`) and every card are elements with `data-card` and a `data-rev` that changes when,
  and only when, their HTML does. The Map, CPU, Health and AI pages are one block (`__view`, the bar above them and their body). The refresh
  script replaces the blocks whose `data-rev` changed, nothing else.

| Path | |
|---|---|
| `/` | the shell (every view above takes it: `/?view=cpu`) |
| `/?card=<id>` | one card of the overview in full (a section id of `[dashboard] sections`) |
| `/?edit=1` | the **layout editor**: the overview in edit mode ([below](#edit-the-layout)); Only on the overview: on another view it is ignored |
| `/?view=settings` | **Appearance** (theme, density, preset, layout with its **Edit layout** link, order, start view, key figures: each choice a link; the theme and the density switch in place, and the chosen one stays a link so you can come back to it; with 8 key figures chosen the others are dimmed until you untick one), **Export** (the `[ui]` block for `config.ini`, and the cookie value), **Screens and sections** (every `[features]` switch, on or off: in a portable run or the desktop app a button turns each one on or off, [below](#the-settings-pages-switches); in an installation a button switches a section off, into [the overlay](#an-installations-settings-overlay), and `config.ini` alone switches one on), **config.ini** (every other key of the file, a section at a time: its value, what it does, the values it takes, its default and when a change applies; a form per section in a portable run or the desktop app, [below](#the-settings-pages-configini); in an installation forms for `[dashboard]`, `[ui]` and `[display] zoom` only, the rest read-only), **Phone alerts** (are the Telegram alerts on and do they reach a phone, and a button to the Telegram page, which walks you through the setup and the test), **About this machine** (read-only: the version and how to update, installed or portable with the folders, the web access, the display mode and zoom, Telegram, `[ai] web_actions`, errors in `config.ini`). Each value says where it comes from |
| `/?view=telegram` | the **Telegram page** (linked from the settings' About): whether the alerts are on and reach someone, the notifier's last message and error, **Switch on** / **Switch off**, **Send a test**, and the pairing (the steps, the bot token and your @username, then the link to press Start on, with **Cancel**). Forms unless `[telegram] web_actions = no`, a portable run, or a notifier that does not take requests ([below](#the-telegram-pages-buttons)). It reloads by itself only while a pairing waits or an answer is due. `&open=1`: a redirect to the `t.me` link of the pairing that waits (the page's *Open in Telegram*), else back to the page |
| `/?set=<field>&back=<view>` | stores one choice and redirects: `<field>` is one field of the cookie grammar (`tl` light, `dw` wall, `pv` server, `kpb_in_la` ...; `reset` forgets all), `back` the query of the view to return to. Anything invalid is `400`; the redirect is rebuilt from the validated view parameters, never from the text given, so it always stays on this server. A request marked cross-site by the browser (`Sec-Fetch-Site`) is `403`. The layout editor's links are fields too (`euat`: move ATTENTION one place earlier; [below](#edit-the-layout)) |
| `/?set=<field>&frag=1` | what the preferences script sends: the same cookie, but the answer is `204` (no redirect, no body) with `Set-Cookie` and `X-Nuc-Prefs: <the canonical cookie string>`, which the script keeps in `localStorage`. `<field>` may also be that whole string (`1.tl.dw`): the script sends it back, once in a while, when the browser sent no cookie. Same checks as above |
| `<any shell page>&frag=1` | the **fragment** of that page ([below](#the-fragment-endpoint)) |
| `/?ui=<string>` | the same grammar for this URL only (a bookmark, a kiosk link); an invalid string is ignored |
| `/s/app.<sha8>.css` | the style sheet (themes `dark`, `light`, `high-contrast`, `auto` by the system's own settings and contrast, forced colours; densities `wall`, `desk`, `compact`). The name carries the first 8 digits of its SHA-256: `Cache-Control: private, max-age=31536000, immutable`, `nosniff`; an unknown name or hash is `404`; the same `Host` and token checks as the pages |

**The cookie.** `nuc_ui` (`HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000`) holds the grammar of [CONFIGURATION.md](CONFIGURATION.md#ui--look-and-layout-of-the-screens)
(`1.tl.dw.pv`), at most 256 bytes; the server validates and canonicalises it on every request and ignores a value that is wrong, a field at a time.
Only the browser keeps it: the server writes nothing for the interface. Pages vary by it (`Vary: Cookie`) and the cache key holds the canonical preferences.
The pages have a strict CSP (`default-src 'none'`), with `style-src 'self'` and no `'unsafe-inline'` (the page loads its style sheet from `/s/`
and carries no `<style>` element and no `style=` attribute; the graph's size per zoom step is a class of `<html>`, `gz<NN>` / `gzfit`, that the sheet
sizes; the scripts only set CSSOM properties, never the `style` attribute), and the policy for their scripts, composed per page by `web.page_csp()` ([below](#the-shells-scripts)).

| page | `style-src` |
|---|---|
| every page (overview, settings, layout editor, MAP tree and graph, CPU, HEALTH, AI, wall) | `'self'` |

### The shell's scripts

Four first-party scripts, each an inline `<script>` at the end of the shell pages that use it (`src/webjs.py`; ASCII, strict mode, no library, no global, ~28 KB together; a page carries three of them).
They only ever **GET**: nothing they do sends a form, and the AI page's POSTs stay native forms. Every control still works without them.

| Script | What it does |
|---|---|
| `REFRESH_JS` | polls the page's fragment every `refresh` seconds and replaces the blocks whose `data-rev` changed, keeping the focus, the open `<details>` and the scroll. It waits while the tab is hidden, while the page is paused and while you type or select; a failed poll backs off (2 s to 60 s) and the banner under the top bar says **stale since HH:MM:SS** (the numbers are never shown as fresh); a lost session (`401`) says *session expired*. A fragment that is not on its allowlists makes it reload the page, at most once in 15 seconds. The pause link becomes a toggle that does not navigate |
| `KEYS_JS` | a key press clicks the link or button the server marked with `data-key` (the keymap is the server's, the script has none: `1`–`5`, `Z`, `?`, `Escape`, ...); arrows, `j`/`k`, PageUp/PageDown, Home/End move over the rows of a list (`data-row`) |
| `PREFS_JS` | a click on a theme or density link is sent to the server in the background (`/?set=…&frag=1`, `204`) and applied at once, without a reload; if that fails the link is followed as it is. It keeps the preferences string in `localStorage` and the **Copy** button of the settings page copies the `[ui]` block. It never touches `document.cookie` (the cookie is `HttpOnly`) |
| `BUILDER_JS` | only on the layout editor page (`?edit=1`), in place of `REFRESH_JS`: drag a card to move it, drag its right edge to resize it, a keyboard path, and each change is saved at once ([below](#edit-the-layout)). It moves the cards the server drew and builds no markup; it keeps nothing in the browser |

### Wall and kiosk

`/?ui=1.dw&kiosk=1` is the shell as a wall display: the **wall** density (big type, lists cut to three rows with a "+N more", no small print) and `kiosk=1`.
Only that combination (not the settings page, not the editor) changes the page:

- `main[data-rotate="N"]`: `REFRESH_JS` scrolls one screen (90 % of the window) every `N` seconds, back to the top at the end, and waits while someone is at the
  page (a click, a key or the wheel pauses it for `3 N` seconds), while paused and while the tab is hidden; with `prefers-reduced-motion` it jumps instead of gliding.
  `N` is `[dashboard] rotate_seconds` (default 15), or `&rotate=N` (3 to 600) in the URL. The partial refresh goes on while it scrolls and keeps the scroll.
- Burn-in: the `shift-N` class of `<html>` (the top bar moves one character, three positions) changes every ten minutes on the server, and a wall page reloads itself
  at every ten-minute mark so that the new class arrives; the scroll position is kept across that reload (`sessionStorage`, this tab only, read back only
  after a reload), so the wall does not start again from the top.
- Nothing is drawn that needs a mouse: the "… the whole card" link, the "show all" link and the "fix" of a problem are gone, the "+N more" is plain text and the
  other links of a card are plain text too.
- The footer holds only what a wall can use: the refresh interval, `read-only`, the hint of how to close the window (`Alt+F4 closes · F11 leaves full screen`;
  macOS: `Cmd+Q`) and the time of the last update; no pause, edit, size, theme or density links.
- Without JavaScript the `<noscript>` meta refresh still reloads the page every refresh interval; it shows the first screen (nothing scrolls; there is no server-side paging).

The display opens it: `render.py --kiosk` (`[display] mode = fullscreen`) starts the full-screen window on
`http://127.0.0.1:<port>/?ui=1.dw&kiosk=1` and `render.py --open` (a normal window) on `/`, with the token as before. The fallback page written to a file
(`--file`, or a `[web] token_file` this user cannot read) is a page the console draws itself, not the web view.

### Edit the layout

`Edit layout` (the overview's footer, or Appearance in the settings) opens `/?edit=1`: the overview as a grid of **previews** (the card
bodies cannot be clicked here), in the order of the layout and never by severity, with a bar above it (**Done**, **Reset layout** and a short help)
and these buttons in every card:

| Button | What it does |
|---|---|
| ↑ / ↓ | the card one place earlier / later |
| − / + | one column narrower / wider: `s1` to `s4`, a quarter to the whole width of the grid (shown as 1/4 … 4/4; on a narrow window the grid has fewer columns and the widths collapse, but the layout keeps them) |
| ✕ | hide the card. A hidden card stays in the grid, dimmed and marked "hidden", with a **show** button; showing it puts it last, as wide as it was (the cookie keeps a hidden card's width: `dbx` is 1 wide, `db3x` 3 wide; its place is not kept) |

**Without JavaScript** every button is a link. A click is one `GET /?set=e<step><card>&back=…` (for example `/?set=edat&…`: ATTENTION later): the
server applies that step to the layout in force (the cookie's, else `config.ini`'s, else the preset's), stores the **whole** layout in the `nuc_ui`
cookie (`l<card><1-4>[x]_…`, the other preferences untouched) and redirects back to the editor. The steps are `u` earlier, `d` later, `s` narrower,
`g` wider, `h` hide, `w` show, and `ereset` drops the layout and the hidden list (the preset's cards show again). A step that changes nothing (the
first card up, the narrowest card narrower) does not touch the cookie; an unknown card or a malformed step is `400`; the cookie stays a valid,
canonical string of at most 256 bytes, always. The pure functions are `prefs.move`, `resize`, `hide`, `show` and `reset` over a layout, and
`prefs.apply_edit` for a cookie.

**With JavaScript** (`BUILDER_JS`) the same buttons change the page at once and the new layout is sent as `GET /?set=l…&frag=1` (`204`), one request after
the other. If the server says anything but `204` the page goes back to the last layout it kept and says so. On top:

- **Drag** a card by its ⠿ handle onto another card to move it; drag the ↔ handle on its right edge sideways to resize it (it snaps to 1–4 quarters of the
  grid). `Esc` during a drag puts everything back. The handles appear only when the script runs.
- **Keyboard**: Tab to a card, `Space` grabs it, arrows move it one place, `+` and `−` resize it, `x` hides or shows it, `Space` drops it, `Esc` puts it back
  where it was. The `#live` region announces each step (`exposure: position 3 of 12, width 2`). The help (`?`) lists the keys of the editor.

**The order.** While a layout is in force that somebody stated (the cookie, `?ui=` or `[ui] layout` in `config.ini`) the cards stay in it, as if
`order = fixed`, unless `order = severity` is set explicitly (by the Order setting, `?ui=` or `config.ini`): the editor says so on its page, and so does the
Order setting. The preset's own layout is not one: it moves by severity as before. **Reset layout** gives the preset's back, with its order. The layout is **per browser**: to make it everyone's default copy the **Export** block of the settings into
`config.ini` (`layout =` and `hidden =` are in it).

The editor page does not reload by itself and has no pause link: a page that moves under your hand is no editor. It carries `KEYS_JS`,
`PREFS_JS` and `BUILDER_JS`; the policy is in the table below.

**The CSP of each page** is built by one function, `web.page_csp(scripts, shell, forms)`, from what the page carries:

| Page | `script-src` | `connect-src` | `require-trusted-types-for 'script'; trusted-types nuc-frag` | `form-action` |
|---|---|---|---|---|
| shell pages | the hashes of `REFRESH_JS`, `KEYS_JS`, `PREFS_JS` | `'self'` | yes | `'none'` |
| shell edit page (`?edit=1`) | the hashes of `KEYS_JS`, `PREFS_JS`, `BUILDER_JS` | `'self'` | no (no script there parses markup) | `'none'` |
| shell AI page | the same three as the shell pages | `'self'` | yes | `'self'` (not when locked) |
| shell settings page | the same three as the shell pages | `'self'` | yes | `'self'` in a portable run (its `[features]` switches) and in an installation that can use the overlay, else `'none'` |
| shell map graph | the same three as the shell pages and the graph script's | `'self'` | yes | `'none'` |
| the live app (`/app`) | the hashes of `APP_JS`, `KEYS_JS`, `PREFS_JS` | `'self'` | `require-trusted-types-for 'script'; trusted-types 'none'` (it parses no markup at all) | `'self'` |

Always `default-src 'none'; base-uri 'none'; frame-ancestors 'none'`. The scripts are inline and pinned (a hash source is honoured reliably by Safari
and Firefox only for inline scripts). To verify a page: take each `<script>…</script>` body of the page, hash its UTF-8 bytes and compare:

```sh
curl -s 'http://127.0.0.1:8787/' | python3 -c 'import sys,re,hashlib,base64
for s in re.findall(r"<script>(.*?)</script>", sys.stdin.read(), re.S): print("sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode())'
curl -sI 'http://127.0.0.1:8787/' | grep -i '^content-security-policy'
```

The two lists are the same. `tests/test_webshell.py` checks that for every shell page, and `tests/jsrules.py` / `tests/test_webjs.py` what the scripts may do.

The markup tests are static; `tools/browser_check.py` (the `browser` job of the `tests` workflow, `python3 tools/browser_check.py --out shots` locally) loads every page of the shell in a real headless Chrome and fails when the browser reports a CSP or Trusted Types violation or a script error, or when the page lacks its landmarks.

### The fragment endpoint

`GET <shell page URL>&frag=1` (the page's own address, which `main[data-frag]` says) answers:

- `200`, `Content-Type: text/html`, `X-Nuc-Fragment: 1`, `ETag: "…"` (a hash of the blocks), `Vary: Cookie`, `Cache-Control: no-store`,
  `Content-Security-Policy: default-src 'none'`; the body is the page's blocks, in the page's order, byte for byte as in the full page, with nothing else
  (no `<html>`, no script, no style);
- `304` when `If-None-Match` carries the current tag;
- `401` without the token, `421` for an unknown `Host`: the same checks as the page. `HEAD` works.

The edit page ignores `frag`. The refresh script parses the fragment inertly and takes over only the tags and attributes of two allowlists
(`webjs.FRAG_TAGS`, `webjs.FRAG_ATTRS`: no script, style, link, image, frame, `on…`, `style`; `href` only `/?…` or `#`; `action` only a path of this server);
a test parses every fragment and checks the same lists.

## The data API

What the screens show, as JSON, for a program or a front end (the [live app](#the-live-app) is one): `GET /api/v1/<view>` (`overview`, `cpu`,
`health`, `map`, `ai`, `events`, `telegram`, `summary`), and the same document again each time it changes, as Server-Sent Events:
`GET /api/v1/stream?view=<view>`. It changes nothing; the buttons of the AI
and Telegram pages stay their forms (below), which a client posts as a page does.

| Path | What |
|---|---|
| `/api/v1` | what there is: the API version, nuc-console's, the refresh interval, the views whose feature is on |
| `/api/v1/overview` | `status` (the pill), `kpis` (every key figure), `cards` (every card whose feature is on, built in full: nothing hidden), `layout` (the cards `config.ini`'s `[ui]` shows, in order, with their widths), `problems` (each with its id, state, text, the cards it belongs to, and the catalog's title, why, fix and accept command; how many were accepted, and which; how many were muted by `[alerts] mute`, and which) |
| `/api/v1/cpu?sort=&sel=` | the CPU screen (`sort` cpu, mem, time, pid or user; `sel` a pid: its details) |
| `/api/v1/health?period=&sel=` | the HEALTH screen (`period` 1, 7 or 30 days; `sel` a finding's id) |
| `/api/v1/events?limit=&since=&age=&kind=` | the EVENTS timeline: `state` (`ok`, `off` when `[features] health = no`, `none` when there is no history yet, `error`) with `msg` saying why there are no events, `total` (how many match), `count`, and `events` (newest first: `id`, `ts`, `time`, `kind`, `subject`, `detail`, `n` how many times, `source`), plus the page's `nodes`. `limit` 1 to 500 (default 100), `since` seconds since the epoch, `age` `1h`, `24h` or `7d` (not with `since`; none of the two: the last 24 hours, as the page; `since=0` is everything kept, 90 days), `kind` one of the history's kinds. **Strictly checked**: a value that is not valid, blank or repeated is a `400` with `{"error": "limit: a number from 1 to 500"}` (the answer never echoes what was sent), on the stream too. Same token, Host and ETag rules as the other views |
| `/api/v1/map?sel=&open=&shut=&all=&only=` | the MAP as a tree (the parameters of the page), `node` (the selected row's node) and `counts` |
| `/api/v1/ai?sel=&confirm=&prompt=` | the AI screen, `engine` (the state of the downloads, the server, the model (`model`: loaded, loading, not loaded...) and the chat, without the prompts: what the page shows) and `csrf`; `prompt=<id>` puts the whole prompt of that answer in the nodes (`Prompt`) |
| `/api/v1/telegram` | `engine` (the pairing and the notifier's state: never the bot's token), `csrf` and the `actions` its buttons post to |
| `/api/v1/summary` | what every page shows above its screen: `host`, `status`, `kpis`, `stale` (a collector's data is old or missing) with the `restart` command, the number of `problems` and the tabs' `badges` (`map`: its problems, `health`: errors and warnings, `ai`: on, down or unknown); since 2.6: `host_id` (16 hex digits from the collector's `host_id` file, `null` when there is none), `version`, `os` (`linux`, `darwin` or `windows`), `counts` (`{"err","warn","unknown"}`: the ATTENTION problems shown, by the status pill's rule: severity 2 or 3 is `err`, 1 is `warn`, any other is `unknown`; accepted and muted ones are not counted; the three add up to `problems`) and `worst` (up to 3 catalog titles, most severe first, never a problem's own text; titles only because the text can name containers, ports or addresses). `host` is `[host] name` when set. The new fields are additive (`api` stays 1): a 2.5.x server has none of them, so a client must treat each as optional and a missing one as unknown, never as fine |
| `/api/v1/hosts` | only while the fleet is on ([below](#hosts)): `self` (`name`, `host_id`, `version`, `os`), `hosts` (per configured peer: `id`, `name`, `url`, `state`, `status`, `counts` (`err`, `warn`, `unknown`, or `null` when the peer does not say), `worst` (up to 3 titles), `age_s`, `skew_s`, `version`, `api`, `scope`, `insecure`, `identity`, `error`) and `fleet` (`state`: the worst; `poller`: `running`, `stale` or `absent`). Built from the poller's cache folder only. The viewer's token or cookie; a peer token is `403`. Off: `404` like any unknown view |
| `/api/v1/stream?view=<view>&…` | Server-Sent Events: each message is that view's document (the same parameters). `view` may be given up to 3 times (a screen and the summary): each message is then one of them, its `view` says which |

**A document** is `{"api": 1, "view": …, "rev": …, "at": …, …}`: `at` is when the data was read (seconds since the epoch), `rev` a hash of the
rest. The same data has the same `rev`: it is the document's `ETag` (`If-None-Match` gets `304`) and the id of its event. Screens are
**components**, the ones the console and the pages draw (`src/ui.py`): `{"type": "Card", "id": …, "title": …, "state": …, "body": [...]}`,
`{"type": "Span", "text": …, "tone": …}`, a `Table` with its `cols` and `rows`, a `Bar` with its `frac` and `state`... the classes and their
fields, less what only the console draws (widths, gaps, heights, indents). A state is one of `ok`, `warn`, `err`, `down`, `unknown`, `info`
and a value that could not be read is `unknown`, never `ok`, as on the screens. The links inside (`href`) are the pages' URLs. A new field
may appear in version 1; one that changes meaning or goes is version 2.

**The stream** sends `retry: 3000`, then the document at once (unless the request's `Last-Event-ID` is already its `rev`), then again each
time its `rev` changes (it looks every refresh interval, every second for `ai` and `telegram`), and a `: keepalive` comment after 15 s of
nothing. It ends after 5 minutes and the browser's `EventSource` reconnects by itself with the last id; 8 streams at most at once (`503`
with `Retry-After` after that). The shell's own scripts do not use it: they keep polling the fragment endpoint.

**Access**: exactly the page's. Without a token the `Host` guard (`421`); with one the token as a header (`Authorization: Bearer`), the
`nuc_token` cookie or `?token=` (no redirect: a program asked), else `401`. A request a browser marks as coming from another site
(`Sec-Fetch-Site` other than `same-origin` or `none`) gets `403`, and the answers carry the headers of every page (`no-store`, `nosniff`,
`Cross-Origin-Resource-Policy: same-origin`, a CSP of `default-src 'none'`) and no CORS header: no page of another site can read them. The AI
and Telegram documents carry the CSRF token their buttons need, as their pages do (none when `web_actions = no` locks them). Errors are JSON
too: `{"error": "..."}` (`404` for an unknown view or one whose feature is off, `500` when the data could not be read: the detail goes to
the journal). Anything but GET and HEAD: `405`.

```bash
curl -s http://127.0.0.1:8787/api/v1/overview | python3 -m json.tool | head
curl -sN http://127.0.0.1:8787/api/v1/stream?view=cpu         # one document per refresh, until you stop it
curl -s -H "Authorization: Bearer $(sudo cat /etc/nuc-console/web.token)" http://box.example.ts.net:8787/api/v1/health
```

## Hosts

With `[hosts]` in `config.ini` (peers to watch, [CONFIGURATION.md](CONFIGURATION.md#hosts--other-nuc-console-machines-to-watch-none-by-default)) and `[features] fleet = yes` (the default) the shell gets a sixth tab, **6 Hosts**, a page `/?view=hosts` and a document
`/api/v1/hosts`; the host name in the top bar becomes a link to the page. With no `[hosts]`, or `fleet = no`, none of it exists: no tab, no link, no section in the settings, `/api/v1/hosts` is a `404` ("no such view") and `view=hosts` is an unknown view (the overview).

**This web view never talks to another host.** It reads the folder the poller writes ([Fleet cache](#fleet-cache): files only, treated as another account's input) and nothing else: no connection, no name lookup, no token. The
page and the document are as old as the poller's last round (15 s by default); when the poller does not run, or has stopped, the page says so and shows nothing as fine.

**The page.** A header with the fleet's state and one **card per host, worst first**: the state as a symbol and a word (`✖ unreachable`, `✖ untrusted`, `! warn`, `? stale`, `✔ ok`: never the colour alone), the host's name and version, its `err`/`warn` counts
(`? problems not reported` for a 2.5.x peer, which does not say), how old the data is ("5 min ago"), the connection (`answering`, or why not), the worst problem titles (three at most) and **Open on NAME**. The rank is: `unreachable`, `untrusted`, `unauthorized`,
`incompatible`, `err` and `down`, then `warn`, then `stale` and `unknown`, then `ok`. The fleet's state is the worst of them, and is never `ok` while a host is old, unreachable or not known (or the poller is not running). Banners say what needs a word:
a 2.5.x peer read through a token with full access (`scope=full`: update it); a clock more than 60 s from this one; a peer whose identity changed (`nuc-console-fleet forget NAME`); plain http on a local network (`insecure`); the poller not running
(`sudo systemctl start nuc-console-peers`, or `python3 src/peers.py serve` in a portable run); two machines with the same display name (give each its own `[host] name`). `host=<id>` (the card's **details** link) shows one host with everything the cache holds, less the secrets
(no token, pin or path is ever in the document or the page). The page refreshes with the shell's own poll of the fragment endpoint, like the other views; the live app (`/app`) does not list Hosts: the shell page is the one.

**Switching host is a navigation.** **Open on NAME** is `/?view=hosts&go=<id>`: the server answers `302` to the address configured for that peer (rebuilt from its parsed parts: scheme, host, port, path; never userinfo, query or fragment), and the browser opens the other
host's own origin, where its own pages ask for its own token (one cookie per origin). `id` must match `[a-z0-9][a-z0-9-]{0,31}` **and** be a configured peer; anything else (unknown, a URL, `..`, too long) is a `404` with no `Location`; a URL is never taken from the request, so it
cannot be an open redirect. A link on another site (`Sec-Fetch-Site` not `same-origin`/`none`) goes back to the page instead. No proxy, no CORS, no cross-origin `fetch`, `connect-src 'self'` as ever. The desktop app opens the other address in the system browser (as for every address that is not its own).

**Mobile first.** One column up to about 480 px, two from there, three from about 900 px; every control (the tab's host link, **Open on**, **details**) is at least 44 px high and the button spans the card on a phone; words wrap, nothing scrolls sideways;
no table. It uses the shell's theme tokens (dark, light, high contrast), no inline style and no script of its own.

The settings page has a read-only **Hosts** section: the configured peers, their address, the token scope, whether a pin is set, the last state and the command to try one (`nuc-console-fleet test NAME`). No form, not even in a portable run.

**Alarms about the peers.** ATTENTION carries `peer-down` (a peer with `alert=yes` silent for 3 polls and 60 s), `fleet-poller-down`, `peer-insecure` and `peer-identity`, built from the same cache; none of them is in the `/api/v1/hosts` document's fields (the Hosts page already shows each peer's state). Rules and what can be muted: [CONFIGURATION.md](CONFIGURATION.md#alarms-about-the-peers).

## Peer tokens

A peer token lets **another nuc-console host** watch this one (the multi-host view) without being able to use this one: the viewer token opens the whole
view, AI buttons and Telegram pairing included, so it is never given to another host. A peer token reads a few documents and can do nothing else.

```ini
[web]
token_file       = /etc/nuc-console/web.token
peer_tokens_file = /etc/nuc-console/peer.tokens
```

`peer_tokens_file` (empty by default: off, nothing changes) is read as `token_file` is (mode 0600, owned by root or the service user; also refused when it is
a link, not a regular file, over 16 KB or not UTF-8: then no peer token works). One entry per line, white space between the three fields, `#` starts a comment:

```
# label     scope     token (32+ characters of A-Z a-z 0-9 . _ ~ -)
nuc-b       summary   9f2c...
nas         views     71ab...
```

The label is `[a-z0-9][a-z0-9-]{0,31}`, the scope `summary` or `views`, at most 16 entries. A line that is not one (or repeats a label, a token or the viewer token) is
ignored with one line in the service log that gives its number and why, never its text. The file is read again when it changes: **to revoke a token, delete its
line**. It needs a viewer token (`token_file`); a portable run and the desktop app do not read it. The settings page lists each label, scope and when it was last
used (read-only; there is no access log, by design). Make a token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.

The peer sends `Authorization: Bearer <token>`: **only** that (a cookie or `?token=` with a peer token is `401`), **only** `GET` or `HEAD`, **only** these paths:

| Path | `summary` | `views` |
|---|---|---|
| `/api/v1` (the index), `/api/v1/summary` | yes | yes |
| `/api/v1/overview`, `/api/v1/cpu`, `/api/v1/health`, `/api/v1/map`, `/api/v1/events` | `403` | yes |
| `/api/v1/ai`, `/api/v1/telegram` (they carry the forms' CSRF token) | `403` | `403` |
| `/api/v1/stream`, `/api/v1/hosts`, any other or unknown `/api/v1/...` (also with a trailing or doubled `/`) | `403` | `403` |
| `/metrics`, the pages (`/`, `/app`, `/setup`, `?view=...`, `?set=`, `/s/...`) | `403` | `403` |
| `POST`, `PUT`, `DELETE`, `PATCH`, `OPTIONS` anywhere | `403` | `403` |
| `/healthz` (no token needed by anyone), a path the server has not (before any token) | as for everyone | as for everyone |

Scope `views` exposes the topology of the machine as the viewer sees it: listening ports, firewall and session findings, container names, the restart command string and the problem titles. Give a `views` token only to hosts you trust with that picture; `summary` shows the status, the figures and the problem titles only.

A refused request is `403` with a fixed text: no CSRF token, no cookie (a peer token never gets `Set-Cookie`, so it cannot become a viewer), no hint of what exists. The documents
are the viewer's own (same builders, same rev and ETag), with the same parameters and the same `Sec-Fetch-Site` rule; answers carry `Cache-Control: no-store`. A wrong token is
the `401` of a wrong viewer token (this server has no lockout or rate limit for either). Tokens are compared with `hmac.compare_digest` against every entry.

## Metrics

`GET /metrics` (and `HEAD`) gives the figures of `/api/v1/overview` as Prometheus text exposition, version 0.0.4
(`Content-Type: text/plain; version=0.0.4; charset=utf-8`). It is built from the same frame as the overview (at most once per half refresh
interval), adds no data the API does not give, opens no listener and starts no thread. **Access is the API's, line for line**: without a token the
`Host` guard (`421`); with one `Authorization: Bearer <token>`, the `nuc_token` cookie or `?token=` else `401`; `403` for a request another site
makes; `405` for anything but GET and HEAD; a bind other than loopback needs the token at start. `[web] metrics = no` turns the route into a `404`.

A figure that could not be read is left out, never 0. Label values are escaped as the format asks (`\\`, `\"`, `\n`).

| Metric | Labels | Meaning |
|---|---|---|
| `nuc_console_build_info` | `version` | always 1 |
| `nuc_console_up` | | 1 while the view answers |
| `nuc_console_collector_age_seconds` | `collector` (`network`, `containers`, `boot`) | seconds since that collector wrote its state |
| `nuc_console_problems` | `severity` (`warning`, `error`, `change`) | problems in the list |
| `nuc_console_problem_active` | `id` | 1 per active, unmuted problem id (at most 200) |
| `nuc_console_problems_muted`, `nuc_console_problems_accepted` | | counts |
| `nuc_console_cpu_usage_ratio` | | mean of the threads, 0 to 1 |
| `nuc_console_load1`, `_load5`, `_load15` | | load average |
| `nuc_console_memory_used_bytes`, `_memory_total_bytes` | | RAM (used = total less available) |
| `nuc_console_filesystem_used_bytes`, `_filesystem_total_bytes` | `mount` | the filesystems the DISKS card lists (at most 64) |
| `nuc_console_temperature_celsius` | | hottest of the CPU and NVMe sensors |
| `nuc_console_uptime_seconds` | | |
| `nuc_console_network_receive_bytes_total`, `_network_transmit_bytes_total` | `interface` | counters since boot, as the traffic card shows |
| `nuc_console_containers` | `state` (`running`, `unhealthy`, `exited`) | counts, never names |
| `nuc_console_exposed_ports` | `reach` (`local`, `tailnet`, `lan`, `internet`) | listening ports by widest reach |
| `nuc_console_firewall_active` | | 1 on, 0 off; unknown: no series |

Prometheus, with the token in a file (mode 0600) next to the scraper:

```yaml
scrape_configs:
  - job_name: nuc-console
    metrics_path: /metrics
    authorization:            # sends "Authorization: Bearer <file content>"
      type: Bearer
      credentials_file: /etc/prometheus/nuc-console.token
    static_configs:
      - targets: ["box.example.ts.net:8787"]
```

On loopback with no token, drop the `authorization` block. Any scraper that can send the `Authorization: Bearer` header (or `?token=` in the
URL, which logs and proxies may keep: prefer the header) reads it; nothing here depends on Prometheus or Grafana.

A label value is cut at 64 characters (the cards cut theirs shorter), so two mounts that begin alike are one series; with more than 64 interfaces the busiest 64 are kept. `nuc_console_collector_age_seconds` is the only freshness signal: the figures of a collector that stopped keep their last values, exactly as in `/api/v1/*`, so alert on that age (and on a missing series) rather than on the value alone. The age itself is not in the JSON API.

## The live app

`/app` is the web view as an app: one page that draws every screen in the browser from the [data API](#the-data-api)'s documents and keeps it
up to date from its stream. Nothing reloads: a new document is drawn and morphed into the page, so what did not change stays the same element
(the focus, a `<details>` you opened, the scroll, the question you are typing); the tabs, the column heads, a row, a period, a model are
followed inside the page (`history.pushState`, the back button works), and the buttons of the AI screen are posted in the background, their
effect arriving with the stream. It looks exactly like the shell: it draws the same components into the same markup (`src/appjs.py` is
`htmlview.html` in the browser, and `tools/browser_check.py` checks, screen by screen, that the two give the same markup), with the same style
sheet, themes, densities, keys and help. The shell has a **live app** link in its footer, and the app a **shell** one.

| Path | What |
|---|---|
| `/app` | the overview: the key figures and every card, in the order your layout gives them |
| `/app?view=cpu&sort=&sel=`, `?view=health&period=&sel=`, `?view=events&age=&kind=`, `?view=map&sel=&open=&shut=&all=&only=`, `?view=ai&sel=&confirm=&prompt=` | the other screens, with the parameters of their pages |
| `/app?…&live=0` | the page as it is now, with no stream (a snapshot: a screenshot, the browser check) |

The settings, the Telegram page, the MAP as a graph, a card in full and the layout editor are the shell's: a link to them leaves the app. The
app needs JavaScript (without it the page says so and links to the shell) and one of the 8 streams the server allows (when none is free it
asks for the documents every refresh interval instead). The page carries what it needs to draw at once: the settings (`<script
type="application/json" id="cfg">`: the refresh, the screens, your layout, order and key figures) and the first documents (`id="doc"`), with
`<`, `>` and `&` escaped so that nothing in them can end the element.

**Security**: the access is the pages' (the `Host` guard, or the token, which `?token=` moves into the cookie as on `/`), the forms are the AI
and Telegram pages' with their CSRF token, and the app's script obeys the rules of the shell's scripts with four exceptions, each replaced by a
narrower rule (`tests/jsrules.py`, policy `app`): it creates elements, but only with `createElement` / `createElementNS` from a fixed list of
tags (no `script`, `style`, `iframe`, `object`, `img`...) and only attributes from a fixed list (no `on…`, `style`, `src`), text only as text,
never markup (`innerHTML`, `DOMParser` stay banned, and the page's CSP has `trusted-types 'none'`: no policy can hand markup to the parser);
it opens one `EventSource`, behind a door that only opens `/api/v1/stream`; it fetches only `/api/v1/…` and posts only to `/ai/…` and
`/telegram/…` (asking for JSON, never following a redirect: the `to` of the answer is drawn in the page only when it is one of its own screens); and
it changes the address only to `/app…` with `history`. Another screen starts at its top; the same screen with other parameters (a model selected,
the prompt of an answer shown, a question asked first) keeps the page where it was and brings what it opened into sight.

## The AI page's buttons

`/?view=ai` is the one page with forms. They do what [AI.md](AI.md#from-the-browser-and-the-console) describes; here is how they are guarded.

| POST | Fields | Does |
|---|---|---|
| `/ai/use` | `model` | the one action: download what is missing (the model server: pinned, SHA-256 checked; the model, pulled through it), start the server, load the model, turn the advisor on with it |
| `/ai/on` | (`model`, `confirm=yes`) | AI on with the model chosen before; with none chosen it redirects to a question naming the recommended model and its size, and only `confirm=yes` with that `model` goes on |
| `/ai/off` | | stops the model server this process started, turns the advisor off |
| `/ai/cancel` | | stops the download or the start that runs (what was fetched is kept) |
| `/ai/delete` | `model`, `confirm=yes` | deletes that model's files; without `confirm=yes` it only redirects to the question |
| `/ai/delete-all` | `confirm=yes` | deletes the model server and every model; the same |
| `/ai/ask` | `q` (500 characters at most) | a question, answered by the model in the background |
| `/ai/advise` | `days` = 1, 7 or 30 | advice on the HEALTH findings of that period, written now |
| `/ai/clear` | `confirm=yes` | deletes the chat's questions, answers and prompts, and `chat.json` (an answer still being written stays); without `confirm=yes` it only redirects to the question |
| `/ai/load` | | loads the model of the server started here into memory again (Ollama lets it go after 5 minutes without a question) |

Every one answers **303** to `/?view=ai...` (Post/Redirect/Get: a reload never posts twice); the work runs in a background thread, the page shows it. A
post that asks for `Accept: application/json` (the live app's script) gets `200` and `{"to": "/?view=ai..."}`, the same address, after the same
checks: the app draws it in the page (a question asked first, `confirm=`, shows at once) instead of following a redirect. Every form
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
- **CSP**: the AI page has `form-action 'self'` (its forms post to this server and nowhere else), and so do the Telegram page and a portable run's
  settings page for theirs; every other page, every error and every redirect has `form-action 'none'`. There is still no script on the AI page, and `default-src 'none'`.
- **The page after a post** is never an old cached one (a post clears the page cache); like every page it shows this process's state, the same for every viewer.

## The setup guide

`/setup?step=N` (N = 1-5; anything else is the nearest step) is the first-run wizard, in the shell and in the desktop app: plain links, no script, one URL per step.
`/?view=setup&step=N` is the same page. It is described in [WIZARD.md](WIZARD.md). Until the guide is finished or skipped, the shell and the live app carry a banner
**New here? Take the 2-minute tour** with a **Dismiss** button; the footer and the settings page link to the guide for good.

| Route | |
|---|---|
| `GET /setup?step=N` | step N (`wizard.clamp`). Same access as every page (Host check, or the token), `form-action 'self'` (the Finish, Skip and step 4's buttons), `Referrer-Policy: same-origin` |
| `POST /setup/finish`, `POST /setup/skip` | write the marker `setup.json` (next to `settings.ini` in the AI folder; a portable run's own `ai` folder) and redirect (`303`) to `/`, or to `/app` with `to=app`. They change no setting. The same guard as every form: token, CSRF token, `Origin` / `Referer` / `Sec-Fetch-Site`, `application/x-www-form-urlencoded`, 4 KB |
| `POST /settings/feature`, `/settings/config` with `to=setup` | step 4's forms are the settings page's own actions (the allowlist `nuc_config.OVERLAY_ALLOW`, the opt-in `[web] settings_actions` and `token_file`); `to=setup` only changes where the redirect goes (`/setup?step=4`). A step 4 on an installation without the overlay has no form: it shows the lines to put in `config.ini` |

## The settings page's switches

In a portable run (`run.sh`, `run.cmd`) and in the desktop app, which is one, `config.ini` is your own file in your own folder and the web view runs as
you: the settings page's **Screens and sections** turn each `[features]` switch on or off. The forms follow the AI page's rules above: the same access as
viewing, the per-process CSRF token, `Origin` / `Referer` / `Sec-Fetch-Site`, a form body of at most 4 KB and 20 fields, Post/Redirect/Get (`303` to
`/?view=settings#features`), `form-action 'self'` and `Referrer-Policy: same-origin` on that page only when it has them.

| POST | Fields | Does |
|---|---|---|
| `/settings/feature` | `name` (one of the `[features]` keys), `on` = `yes` or `no` | writes `name = yes` or `no` in `[features]` of that `config.ini`: only that value changes, every other line and comment stays as it is, the file keeps its permissions. The screens follow at once; the collector reads the switches again within 10 seconds (a loop switched on starts, one switched off ends). Switching the AI screen off also stops the model server the web view started |

In an installation a button switches a section **off**, in [the overlay](#an-installations-settings-overlay) (`config.ini` stays the administrator's; a section it switches off can only be switched on there; **Reset to config.ini** takes a choice back). With `[web] settings_actions = no` (the default), without a `token_file`, or when the overlay's folder is not this account's to write, the section only shows the switches and a post is refused with `403` (see [below](#why-there-is-no-configuration-editor)).

## The settings page's config.ini

Below the switches, **config.ini** lists every other key of the file (`src/confedit.py` has them all: what each does, the values it takes, its default and
when a change applies), one block per section: `[dashboard]`, `[ui]`, `[display]`, `[web]`, `[telegram]`, `[ai]`, and `[webapps]` and `[expose]` as a
text of `name = value` lines. In a portable run and the desktop app each block is a form with its own **Save**; the rules are the switches' (same access as
viewing, CSRF token, `Origin` / `Referer` / `Sec-Fetch-Site`, 4 KB and 20 fields, Post/Redirect/Get to `/?view=settings#cfg-<section>`).

| POST | Fields | Does |
|---|---|---|
| `/settings/config` | `section`, and the keys of that section (`text` for `[webapps]` and `[expose]`) | checks each value against what the key takes (a choice, a whole number in its range, a name or an address of its pattern, one line, no `#` or `;`); writes the keys whose value differs from the file's, and removes the `[ui]` keys left empty and the `[webapps]` / `[expose]` lines taken away; every other line and comment stays, the file keeps its permissions. The new file is written aside and read back the way the dashboard reads it: if that has a complaint about the section that the old file did not, the post is refused and the old file stays. Then the web view reads the file again: most keys apply at once, and the page says which ones wait for the next start |

- **Not from the page**: the locks, `[ai] web_actions`, `[telegram] web_actions` and `[ai] allow_remote` (so no page can let the machine's history go to another
  machine), and the keys that only an installation reads (`[web] enabled`, `bind`, `port`, `token_file`, `[display] mode` and `browser`, one of which is a
  program to run): they are shown with their value and why, and a value posted for them is ignored. `[features]` is the switches above.
- **A refused post** writes nothing and comes back to the section, open, with every reason and what was typed. A file that cannot be read is not edited.
- In an installation the block works through [the overlay](#an-installations-settings-overlay): `[dashboard]`, `[ui]` and `[display] zoom` have their forms (a key set from the page is marked, with **Reset to config.ini**), every other section is shown read-only with the command that applies `config.ini`. When the overlay cannot be used (`[web] settings_actions` not `yes`, no `[web] token_file`, or the folder is not this account's to write) the whole block is read-only and a post is refused with `403`.

## An installation's settings overlay

An installation's `config.ini` is root's and the web view is unprivileged, so the page does not write it (and there is no helper that does). Like the AI and Telegram pages it writes a
file of its own, **`settings.ini`** in the AI folder (`/var/lib/nuc-console/ai`; macOS `/Library/Application Support/nuc-console/ai`; Windows `%ProgramData%\nuc-console\ai`), and
`nuc_config.load()` lays it over `config.ini` for the renderer, the web view and the collector ([CONFIGURATION.md](CONFIGURATION.md#the-settings-overlay-an-installations-settings-page) has the allowlist).

| POST | Fields | Does |
|---|---|---|
| `/settings/feature` | `name`, `on` | `no`: `name = no` in the overlay (only when `config.ini` has it on); `yes`: removes that entry. A feature that `config.ini` switches off cannot be switched on here (`400`) |
| `/settings/config` | `section`, the section's keys | the same checks as a portable run's save; the values that differ from `config.ini`'s go in the overlay, an empty `[ui]` value or one equal to `config.ini`'s drops its entry. A section or key outside the allowlist is refused whole and nothing is written |
| `/settings/reset` | `reset` = `section.key`, or `*` | removes that entry (all of them) from the overlay: `config.ini` is in force again |

The guards are the other pages': the token (or loopback and a known `Host`), the CSRF token, `Origin` / `Referer` / `Sec-Fetch-Site`, 4 KB. The file is written aside, mode 0640, created new
(never through a link) and moved into place; the new file is read back with the dashboard's own reader before it replaces the old one. The threat note is in
[SECURITY.md](../SECURITY.md#the-installations-settings-overlay). An administrator resets everything with `sudo rm` of the file.

**Opt-in, behind a token.** `[web] settings_actions` is `no` by default, and even with `yes` the page writes only when a `token_file` is configured and the request carries it: with no token any local user can read the page, take the CSRF token and post. Without one the page says why it is read-only. Switching `exposure`, `firewall`, `fail2ban`, `databases` or `tailscale` off this way raises the ATTENTION problem `feature-hidden` (not acceptable, gone when reset).

**The Telegram notifier ignores the overlay by design**: it is another account and cannot read the 0640 file (it logs one calm line per process). It keeps running with `config.ini` alone, so `feature-hidden` shows on the dashboard but is not sent to Telegram.

## The Telegram page's buttons

`/?view=telegram` pairs the Telegram notifier with your own bot and drives it ([TELEGRAM.md](TELEGRAM.md#from-the-web-view)). The forms follow the
AI page's rules above, one by one: the same access as viewing, the per-process CSRF token, `Origin` / `Referer` / `Sec-Fetch-Site`, a form body of
at most 4 KB and 20 fields, Post/Redirect/Get (`303` to `/?view=telegram#tg`), `form-action 'self'` and `Referrer-Policy: same-origin` on that page only.

| POST | Fields | Does |
|---|---|---|
| `/telegram/pair` | `token`, `username` | checks the token's format and asks Telegram who the bot is (`getMe`), shows the link with a one-time code and waits for the Start of that @username (10 minutes, in a thread); then hands the pairing to the notifier |
| `/telegram/cancel` | | stops the pairing that waits: nothing is paired |
| `/telegram/on`, `/telegram/off` | | asks the notifier to switch on or off (off: the paired chat is told first; `enabled = yes` in `config.ini` cannot be switched off here) |
| `/telegram/test` | | asks the notifier to send a test message |

- **`[telegram] web_actions = no`** (default `yes`): the page shows "locked by config.ini" and has no form, a post is refused with `403`, and the notifier reads no request.
- **The token**: checked against its format, sent to `api.telegram.org` only, never written into a page, a log, a redirect or a file of the web view, and held in
  memory only while the pairing runs. It reaches the notifier as a request file in its inbox, which the web view's account can create but never list or read back
  (Linux 2730 and `SupplementaryGroups=nuc-console-notify`, Windows `W` for LOCAL SERVICE); the notifier deletes it once read.
- **Values**: a username is 5–32 letters, digits or `_`; nothing from a request reaches a path, a command line or a shell; the request files are named by the
  engine (`r-<milliseconds>-<random>.json`, created, never replaced).
- **`/?view=telegram&open=1`** redirects only to the engine's own link (`https://t.me/<bot>?start=<code>`, checked against a pattern), and only for a request the
  browser does not mark cross-site; anything else goes back to the page. It exists so that the link survives the page's refresh, whose fragments may only
  carry links of this server.
- The answers are the notifier's (`status.json`); a notifier that does not answer within a minute is said, never guessed.

## The graph view's script

The MAP's graph view carries one small inline script (`src/graphjs.py`, ~16 KB, no library) for dragging, zooming and panning. On the
graph page it comes with the shell's own scripts ([above](#the-shells-scripts)). It is boxed in:

- its SHA-256 is in that page's CSP (`script-src 'sha256-…'`, next to the shell's scripts'): no other script, inline or
  loaded, can run;
- it only moves what the server drew: it never builds HTML from data and uses no network API (`tests/test_graphjs.py`); the page's CSP allows
  connections to this server only; it keeps the view (zoom, pan, circles
  placed by hand) in `sessionStorage`;
- the page works without it: every circle is a link, zoom has links, and the page then refreshes by `<meta refresh>`
  (inside `<noscript>`; with the script, the script reloads the page itself, never while you are dragging);
- `tests/test_graphjs.py` rejects any change that adds markup building, `eval`, timers with strings, network or storage
  APIs, globals, or anything outside the page's own elements.

## Why there is no configuration editor

`config.ini` is owned by root; the web process is unprivileged. Writing it from the browser would require either a root process listening on the network
or a privileged helper — a large jump in risk for a file you change a few times a year. Edit it over SSH, then `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web`.
What an installation's settings page can change is limited to presentation and is kept apart, in [an overlay](#an-installations-settings-overlay) laid over `config.ini`.
A portable run and the desktop app are the one exception ([the switches](#the-settings-pages-switches) and [the config.ini block](#the-settings-pages-configini)):
there `config.ini` is the file of the account the web view runs as, so writing it is no jump in privilege; the locks stay for the file.
(The shell's [layout editor](#edit-the-layout) changes only the `nuc_ui` cookie of your browser, never `config.ini`; the settings page's Export block is what you paste there.
The Telegram page does not write `config.ini` either: it asks the notifier, an unprivileged service, which keeps the page's choice in its own `web.json`.)

## Fleet cache

On Linux the poller is the service `nuc-console-peers` (account `nuc-console-peers`, cache `/run/nuc-console-peers` readable by everybody, state `/var/lib/nuc-console-peers`; [INSTALL.md](INSTALL.md#fleet-poller)); it exists only while `[hosts]` has a peer. A portable run starts it with the launcher.

The fleet client (`src/peers.py`, `nuc-console-fleet once`) writes what it saw of the `[hosts]` peers into a folder of its own: Linux `/run/nuc-console-peers`,
macOS `/var/run/nuc-console-peers`, Windows `%ProgramData%\nuc-console\run\peers`, a portable run `<data>/peers-cache` (`NUC_CONSOLE_PEERS_CACHE` overrides it). The web view and the
console only **read** this folder; they never open a socket to a peer and never hold a peer's token. Two JSON files per peer, written atomically (a file aside, then rename), mode 0644: `<name>.json`, the summary entry and a small index of its documents (what a page reads every few seconds), and `<name>.views.json`, the documents themselves (`{"v": 1, "views": {...}}`, only for scope `views`/`full`, written before `<name>.json` and removed when there are none). A name ending `.views.json` is never a peer. `<name>.json`:

```json
{"v": 1, "name": "nuc-b", "url_display": "https://nuc-b.example.ts.net", "ts": 1792925628, "state": "ok", "error": "",
 "summary": {"host": "nuc-b", "host_id": "0123456789abcdef", "version": "2.6.0", "os": "linux", "api": 1, "state": "ok", "status_text": "ALL OK",
             "counts": {"err": 0, "warn": 0, "unknown": 0}, "worst": [], "stale": false, "at": 1792925627.4, "problems": 0},
 "view_index": {"overview": {"size": 48213, "ts": 1792925628}},
 "scope": "summary", "warn": ["legacy-peer"], "insecure": false, "identity": "ok", "last_ok": 1792925628, "skew_s": 0, "age_s": 1}
```

`state` is one of `ok`, `warn`, `err`, `down`, `unknown`, `info` (what the peer reports) or `unreachable`, `unauthorized` (401/403 or no usable token file), `incompatible` (an API newer than
this client reads, or no such page), `untrusted` (pin or certificate not accepted, or the `host_id` changed), `stale` (the peer says its data is old, or its own `at` is more than 2 minutes behind its own `Date`).
`error` is empty or a word of `peers.REASONS` (`timeout`, `connect`, `redirect`, `too-large`, `pin-mismatch`, ...): never text from the peer. `summary` holds only the fields the client whitelists, each with
its type and length (strings at most 120 characters, no control character); `view_index` names the documents in `<name>.views.json` (the overview, cpu, health, map and events documents, only for `scope=views` or `full`) with their size and time; the documents are opaque JSON, at most 2 MB each, nested at most 40 deep, keys named like `csrf` or `token` removed.
`warn` is a list of words: `legacy-peer` (a 2.5.x peer: no `host_id` or `counts`), `full-scope-token`, `clock-skew` (the peer's `Date` differs from this clock by more than 60 s; `skew_s` is the difference, the age of the data is `Date` minus the peer's `at`, never this clock against `at`),
`insecure-transport`, `showing-old-data` (the peer is unreachable: the last summary and documents are kept, `last_ok` says when), `view-failed:<name>`, `views-dropped`. `identity` is `changed` when the peer's `host_id`,
remembered at the first contact in the file `known` of the persistent state folder (`/var/lib/nuc-console-peers`, Windows `%ProgramData%\\nuc-console\\peers-state`, portable `<data>/peers-state`; `NUC_CONSOLE_PEERS_STATE` overrides it; like `known_hosts`), is not the one that answered: the peer is then `untrusted` and its data is dropped.
To accept a replaced machine, run `nuc-console-fleet forget NAME`. The cache folder holds a `.owner` marker: the poller refuses a non-empty folder without it and removes only the peers' files that `fleet.json` listed.

`fleet.json` is the index: `{"v": 1, "ts": ..., "worst": "<state>", "peers": [{"name", "state"}]}`; the worst state orders `ok = info < warn < unknown = stale = incompatible < unauthorized = untrusted < unreachable = err < down`.
A reader uses `peers.read_cache(folder, views=False)`, which reads only the `<name>.json` files (the entry has no `views` key), and `peers.read_cache(folder, views=True)` or `peers.read_views(folder, name)` for the documents of one peer (`{document: tree}`, `{}` when there are none); both treat the folder as another account's: no link, regular files only, not writable by group or others, owned by root, itself or the folder's owner, at most 12 MB (64 KB for `fleet.json`), every field rebuilt with its type,
text cleaned again, and an `ok`, `info`, `warn` or `unknown` entry older than 2 minutes returned as `stale` (`cache-old`). Whether the poller runs at all is `fleet["age_s"]` (the Hosts page says `running` up to 2 minutes, then `stale`; no `fleet.json` is `absent`). A peer that cannot be read is skipped and counted in `skipped`, never shown as ok.

## Threat model in one paragraph

The page shows your topology (ports, container names, client IPs seen on databases, and on the map which service talks to which), exactly like the monitor. With loopback + `tailscale serve` only your tailnet can read it.
With a token, anyone holding the token can. It cannot change the machine, with one exception on the AI page: whoever can open it can set up the local model
(download the pinned files into the AI folder, start the model server on 127.0.0.1, turn the advisor on and off, delete those files) and ask it questions, as the
unprivileged web account, within the unit's sandbox; `[ai] web_actions = no` removes that. And on the Telegram page whoever can open it can pair the
notifier with their own bot, switch it on or off and send a test; the chat paired until then is told of a new pairing and of *off*, and
`[telegram] web_actions = no` removes that too. A [peer token](#peer-tokens) opens none of that: it can only `GET` the few read-only documents of its scope. Rendering is cached for half the refresh interval per layout size.
The graph view's script runs in your browser and cannot send anything anywhere (the CSP forbids connections). The shell's scripts may connect
to this server only (`connect-src 'self'`), and the page's only way to put markup into the DOM is Trusted Types' one policy, whose input is checked against an allowlist first.

## macOS and Windows

There the web view **is** the dashboard: the installers run it as the hidden user `_nuc-console` (macOS LaunchDaemon) or as
LOCAL SERVICE (Windows scheduled task `\nuc-console\web`), with `--local`: while `[web] enabled = no` it listens on **127.0.0.1
only**, without a token unless `[web] token_file` is set (nothing on the network can reach it), whatever `bind` says. Set `[web] enabled = yes` (and a token for a
non-loopback `bind`) to reach it from other devices as described above, then run the installer again. `[display] mode = none`
with `[web] enabled = no` runs no web view at all.
Windows has no mode bits: keep `token_file` inside `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write
(and limit who can read the file with an ACL if other people use the machine).

With a token, what opens the dashboard at login (`render.py --open`, `[display] mode = browser`) puts it in the address as `?token=` when the
logged-in user can read the token file (the web view moves it into a cookie, as above; for a moment it is in the browser's command line,
which other users of the machine can list). When that user cannot read it (the usual case on macOS, where the file is the service user's),
the dashboard is shown from a page written to a file instead, as the full-screen window does, and `display.log` says why.

### The loopback view and the other accounts of the machine

Loopback keeps the network out; it does not separate the accounts of one machine. What does, depends on how the view runs:

- **A portable run and the desktop app** ([PORTABLE.md](PORTABLE.md#the-access-token)): the view asks for a **token of its own**, `data/web.token` in
  the data folder: `secrets.token_urlsafe`, mode 0600 (Windows: an ACL for the user), created at the first start, kept between starts (the browser
  and the app hold it as a cookie), regenerated by deleting the file, never in `config.ini`, a log, a page or `/api/`. It is checked as any token is
  (cookie `HttpOnly; SameSite=Strict`, `Authorization: Bearer`, `?token=` moved into the cookie; constant-time compare), so another account that
  can reach the port gets `401` and no CSRF token. The launchers read the file and never put the token on a command line (they open a 0600 page,
  `data/open.html` / `open-app.html`, that forwards to the view). `[web] token_file` is still ignored there. `--demo` has none.
- **A Linux installation** (and any installation where `[web] token_file` is set): the token you configured, as above. `token_file` wins everywhere:
  an installation on macOS or Windows with `[web] enabled = no` now also asks for it when the file is set.
- **An installation on macOS or Windows without `token_file`: no token.** The web view runs as a service account (`_nuc-console`, LOCAL SERVICE) and
  the browser is opened by the logged-in user: a token file that user can read is a file every other local user can read as well (macOS and Windows
  have no per-user group the installer could rely on), so a generated token would protect nothing. The `Host` check (DNS rebinding) and
  `Origin` / `Sec-Fetch-Site` / the CSRF token (other sites in your browser) defend; **not defended:** another local account or program, which can
  read the CSRF token from the page and post it back to `/ai/*` (the model, the chat) and `/telegram/pair` (pair the alerts with its own bot,
  switch them off). The settings page refuses posts there (`403`).

What to do on a shared Mac or Windows PC:

- `[ai] web_actions = no` and `[telegram] web_actions = no`: the two pages only show, and no page can unlock them (locks of `config.ini`).
- `[web] enabled = yes` with a `token_file` (and the installer run again) makes the installed view ask for the token like on Linux; then
  `render.py --open` / `--kiosk` fall back on a page written to a file when the logged-in user cannot read the token (the usual case on macOS).
- Or use a portable run / the desktop app under your own account, which has its own token.
- Not a mitigation: `allow_remote`, which is the AI endpoint's key (`[ai] allow_remote`), nothing to do with who may open the view.

The token does not defend against **you**, **root** or a program that runs as you (they can read the file), and it adds nothing against another user
with root.

A portable run ([PORTABLE.md](PORTABLE.md): `./run.sh --web` (the default on macOS) or `run.cmd`) starts the web view the same way on any system: 127.0.0.1 only, behind
the folder's own token ([above](#the-loopback-view-and-the-other-accounts-of-the-machine)), a free port (or `--port`), whatever `[web]` says; it stops with the run.
