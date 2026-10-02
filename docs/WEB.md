# Web view

The same screen as the monitor, in a browser: over your LAN, your Tailscale tailnet or a VPN. **Read-only, opt-in, no API, GET (and HEAD) only.** The web view is **the shell** (cards, key figures, five screens; [below](#the-shell-the-default-web-interface)); the older **classic** pages (the console's text turned into HTML) are kept for one release as a fallback (`[ui] web = classic`, or `?app=0` for one URL) and will then be removed. No JavaScript is needed, and three exceptions are boxed in: the **AI page** (`/?view=ai`) has buttons (forms that POST to `/ai/...`: choose a model,
switch the AI on or off, delete, ask: [below](#the-ai-pages-buttons)), the shell carries three small first-party scripts per page (four in all, with the layout editor's), pinned by their hashes ([below](#the-shells-scripts)), and the MAP's graph view one small script of its own, pinned the same way ([below](#the-graph-views-script)). Configuration is *not* editable from the web on purpose (see below); `[ai] web_actions = no` makes the AI page read-only too.

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
The view you asked for stays: `/?token=<the token>&view=map` lands on the MAP, `…&view=cpu&sort=mem` on the CPU page sorted by memory. The redirect
rebuilds the address from the parameters the page understands, with their values checked; anything else is dropped, and the token never comes back.
Scripts can send `Authorization: Bearer <token>`. The token lives in a file, never in `config.ini` (world-readable).
Plain HTTP on a LAN sends the token in clear text: prefer `tailscale serve` or a TLS reverse proxy for anything you don't fully trust.

A non-loopback listener shows up as a **new exposed port** in the dashboard's own alarms until you accept it (`sudo nuc-console-accept`): that is intended.

## Hardening built in

- **DNS-rebinding guard**: without a token, requests whose `Host` is not `localhost`, `127.0.0.1`, `::1`, the bind address, the hostname, a `*.ts.net` name or one listed in `[web] allowed_hosts` get `421`.
- **Connection cap** (32) and a **15 s total deadline per request**: a slow client cannot hold threads. The unit adds `ProtectSystem=strict` with one writable place, `ReadWritePaths=-/var/lib/nuc-console/ai` (the AI folder), `TasksMax=512`, `MemoryMax=85%`, `PrivateDevices`, `RestrictNamespaces` and more. (`TasksMax` and `MemoryMax` were 64 and 256M before the AI page could start a model server: it is a child of this service and lives in its cgroup; 85% of the RAM is the line the page's "too big" is drawn at.)
- Page cache: layout widths are rounded to steps of 20, so at most a dozen distinct renders exist and a burst costs one render per size.
- Render errors go to the journal, never to the page.

## What it serves

**The shell is what `/` serves by default** ([below](#the-shell-the-default-web-interface)); this table lists the **classic** pages, which `[ui] web = classic` or `app=0` (`/?app=0&view=cpu`) still give for this release. The query parameters that name a view (`view=`, `sort=`, `sel=`, `period=`, `all=`, `as=graph` ...) work on both.

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

Everything else is 404; any method but GET and HEAD is 405 (`Allow: GET, HEAD`), except the `POST` of the AI page's forms (below).
HEAD is answered like GET (same status, same headers, `Content-Length` included, the same token and `Host` checks), without the body.
Security headers, on every response: strict CSP (`default-src 'none'`), `no-store`, `nosniff`, `frame-ancestors 'none'`, `no-referrer`,
`Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Opener-Policy: same-origin`; the AI page differs in two, see below.
No access log (URLs may carry a token).

## The shell (the default web interface)

The web view is a shell made of cards. It is **the default** (`[ui] web = app`). The classic pages, the older interface, are kept for one release as a
fallback and will be removed: `[ui] web = classic` in `config.ini` serves them for every page, `?app=0` for one URL (`?app=1` gives the shell back when
`web = classic`).
Every control is a link or a form, so it works with scripts off, and the page then reloads by `<meta refresh>` (inside `<noscript>`) like the
classic one. With scripts on, three small inline ones refresh it in place and add keys and instant preferences ([below](#the-shells-scripts)).

- **Top bar**: the host, the status pill (✔ ALL OK, ! warnings, ✖ problems, with its symbol), the five screens as tabs (keys 1–5; badges for the
  Map's problems, the Health findings and the AI), the clock, `?` (the keys, `#help`, shown by the browser's `:target`) and ⚙ (the settings).
- **Key figures** under it (`[ui] kpis`, the settings page): one tile each, a link to its card; a source that cannot be read is `?`, never green.
  A banner says so when a collector is not running.
- **Overview**: a grid of cards (12 / 6 / 1 columns by the window's width), in the order of `[ui] order` (by severity, or fixed) and the layout. A
  card holds the text the console draws for that section (colours by the theme); a card that hid items says so and links to `/?card=<id>`, the card in full.
  **Edit layout** (footer, and Appearance in the settings) opens the layout editor ([below](#edit-the-layout)).
- **Map, CPU, Health, AI**: their existing pages, inside the same frame (their own controls in a bar above them). The CPU, Health and AI screens are drawn natively from the components the console draws too (no `<pre>`). The AI screen (`?app=1&view=ai`): the switch as a pill with what it is doing and a progress bar for a download, the chat (the question box, each answer with "AI, check before acting", "advice now"), the hardware and the status as labelled values, the models as a table (the verdict a pill with its symbol, a **use this model** button per row, the selected one's details beside it with **delete its files**), the folder and **delete everything**, and the question that waits (**Yes** / **No**). Every button is the same POST form as on the classic page (same endpoints, field names and CSRF token) and carries `data-key` from the keymap (`e` on/off, `c` cancel, `u` use, `x` delete, `X` delete all, `y` / `n` the answers). When `[ai] web_actions = no` locks it, it shows a notice and has no form and no button. The classic page (`?app=0&view=ai`) is unchanged.
  The **CPU** page is built of components, not text: key figures, a meter per logical CPU, the temperatures, and the process table, whose column heads are
  links (`p m t n u` do the same by keyboard) and whose rows are links to `sel=`; with a process selected its details sit beside the table (below it in a narrow window).
  The **Map** (tree view) is built of components too: the figures and the arrows' legend, a segmented control of links (`e` expand all, `c` collapse all, `p` problems
  only, and the graph view), the tree as a list whose rows are links (a row selects it, the selected one deselects it; its ▸ ▾ mark opens or closes the branch; the state
  is a symbol and a class, the depth an indent), and the selected node's details beside the tree (below it in a narrow window) with a link that closes them. j / k and the
  arrows move over the rows, Enter follows. The graph view (`as=graph`) and the classic pages are as before.
- **Footer**: refresh − / +, pause, Edit layout, A− / A+ (`z50` … `z200`), theme and density as links, "read-only · AI actions".
- **Blocks**: the top bar (`__top`), the key figures (`__kpis`) and every card are elements with `data-card` and a `data-rev` that changes when,
  and only when, their HTML does. The Map, CPU, Health and AI pages are one block (`__view`, the bar above them and their body). The refresh
  script replaces the blocks whose `data-rev` changed, nothing else.

| Path | |
|---|---|
| `/` | the shell (every view above takes it: `/?view=cpu`); `?app=1` forces it when `[ui] web = classic`, `?app=0` gives the classic page |
| `/?card=<id>` | one card of the overview in full (a section id of `[dashboard] sections`) |
| `/?edit=1` | the **layout editor**: the overview in edit mode ([below](#edit-the-layout)); `/?edit=1` (`/?app=1&edit=1` when `[ui] web = classic`). Only on the overview: with `app=0` or on another view it is ignored |
| `/?view=settings` | **Appearance** (theme, density, preset, layout with its **Edit layout** link, order, start view, key figures: each choice a link), **Export** (the `[ui]` block for `config.ini`, and the cookie value), **About this machine** (read-only: the version and how to update, installed or portable with the folders, the web access, the display mode and zoom, Telegram, `[ai] web_actions`, errors in `config.ini`). Each value says where it comes from |
| `/?set=<field>&back=<view>` | stores one choice and redirects: `<field>` is one field of the cookie grammar (`tl` light, `dw` wall, `pv` server, `kpb_in_la` ...; `reset` forgets all), `back` the query of the view to return to. Anything invalid is `400`; the redirect is rebuilt from the validated view parameters, never from the text given, so it always stays on this server. A request marked cross-site by the browser (`Sec-Fetch-Site`) is `403`. The layout editor's links are fields too (`euat`: move ATTENTION one place earlier; [below](#edit-the-layout)) |
| `/?set=<field>&frag=1` | what the preferences script sends: the same cookie, but the answer is `204` (no redirect, no body) with `Set-Cookie` and `X-Nuc-Prefs: <the canonical cookie string>`, which the script keeps in `localStorage`. `<field>` may also be that whole string (`1.tl.dw`): the script sends it back, once in a while, when the browser sent no cookie. Same checks as above |
| `<any shell page>&frag=1` | the **fragment** of that page ([below](#the-fragment-endpoint)) |
| `/?ui=<string>` | the same grammar for this URL only (a bookmark, a kiosk link); an invalid string is ignored |
| `/s/app.<sha8>.css` | the style sheet (themes `dark`, `light`, `high-contrast`, `auto` by the system's own settings and contrast, forced colours; densities `wall`, `desk`, `compact`). The name carries the first 8 digits of its SHA-256: `Cache-Control: private, max-age=31536000, immutable`, `nosniff`; an unknown name or hash is `404`; the same `Host` and token checks as the pages |

**The cookie.** `nuc_ui` (`HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000`) holds the grammar of [CONFIGURATION.md](CONFIGURATION.md#ui--look-and-layout-of-the-screens)
(`1.tl.dw.pv`), at most 256 bytes; the server validates and canonicalises it on every request and ignores a value that is wrong, a field at a time.
Only the browser keeps it: the server writes nothing for the interface. Pages vary by it (`Vary: Cookie`) and the cache key holds the canonical preferences.
The shell's pages have the strict CSP of the classic ones, but with `style-src 'self'` and no `'unsafe-inline'` (the page loads its style sheet from `/s/`
and carries no `<style>` element and no `style=` attribute; the graph's size per zoom step is a class of `<html>`, `gz<NN>` / `gzfit`, that the sheet
sizes; the scripts only set CSSOM properties, never the `style` attribute), and the policy for their scripts, composed per page by `web.page_csp()` ([below](#the-shells-scripts)).

| page | `style-src` |
|---|---|
| shell (`?app=1`: overview, settings, layout editor, MAP tree and graph, CPU, HEALTH, AI, wall) | `'self'` |
| classic (`?app=0`) | `'unsafe-inline'` (each page has its own `<style>`) |

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

`/?app=1&ui=1.dw&kiosk=1` is the shell as a wall display: the **wall** density (big type, lists cut to three rows with a "+N more", no small print) and `kiosk=1`.
Only that combination (not the settings page, not the editor) changes the page:

- `main[data-rotate="N"]`: `REFRESH_JS` scrolls one screen (90 % of the window) every `N` seconds, back to the top at the end, and waits while someone is at the
  page (a click, a key or the wheel pauses it for `3 N` seconds), while paused and while the tab is hidden; with `prefers-reduced-motion` it jumps instead of gliding.
  `N` is `[dashboard] rotate_seconds` (default 15), or `&rotate=N` (3 to 600) in the URL. The partial refresh goes on while it scrolls and keeps the scroll.
- Burn-in: the `shift-N` class of `<html>` (the top bar moves one character, three positions) changes every ten minutes on the server, and a wall page reloads itself
  at every ten-minute mark so that the new class arrives.
- The footer holds only what a wall can use: the refresh interval, `read-only`, the hint of how to close the window (`Alt+F4 closes · F11 leaves full screen`;
  macOS: `Cmd+Q`) and the time of the last update; no pause, edit, size, theme or density links.
- Without JavaScript the `<noscript>` meta refresh still reloads the page every refresh interval; it shows the first screen (nothing scrolls; there is no server-side paging). `rotate=1` (the classic page's "take turns") does nothing here.

The display opens it (`[ui] web = app`, the default): `render.py --kiosk` (`[display] mode = fullscreen`) starts the full-screen window on
`http://127.0.0.1:<port>/?app=1&ui=1.dw&kiosk=1` and `render.py --open` (a normal window) on `/?app=1`, with the token as before; with `web = classic`
they open the classic page as before. The fallback page written to a file (`--file`, or a `[web] token_file` this user cannot read) is the classic one.

### Edit the layout

`Edit layout` (the overview's footer, or Appearance in the settings) opens `/?app=1&edit=1`: the overview as a grid of **previews** (the card
bodies cannot be clicked here), in the order of the layout and never by severity, with a bar above it (**Done**, **Reset layout** and a short help)
and these buttons in every card:

| Button | What it does |
|---|---|
| ↑ / ↓ | the card one place earlier / later |
| − / + | one column narrower / wider: `s1` to `s4`, a quarter to the whole width of the grid (shown as 1/4 … 4/4; on a narrow window the grid has fewer columns and the widths collapse, but the layout keeps them) |
| ✕ | hide the card. A hidden card stays in the grid, dimmed and marked "hidden", with a **show** button; showing it puts it last, 1 column wide |

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

**The order.** While the cookie (or `?ui=`) holds a layout of your own the cards stay in it, as if `order = fixed`, whatever `order` says: the editor
says so on its page, and so does the Order setting. A layout from `config.ini` is the administrator's and does not change that. **Reset layout** gives
the preset's back, with its order. The layout is **per browser**: to make it everyone's default copy the **Export** block of the settings into
`config.ini` (`layout =` and `hidden =` are in it).

The editor page does not reload by itself and has no pause link: a page that moves under your hand is no editor. It carries `KEYS_JS`,
`PREFS_JS` and `BUILDER_JS`; the policy is in the table below.

**The CSP of each page** is built by one function, `web.page_csp(scripts, shell, forms)`, from what the page carries:

| Page | `script-src` | `connect-src` | `require-trusted-types-for 'script'; trusted-types nuc-frag` | `form-action` |
|---|---|---|---|---|
| classic pages | none (`default-src 'none'`) | none | no | `'none'` |
| classic AI page | none | none | no | `'self'` (not when locked) |
| classic map graph | the graph script's hash | none | no | `'none'` |
| shell pages | the hashes of `REFRESH_JS`, `KEYS_JS`, `PREFS_JS` | `'self'` | yes | `'none'` |
| shell edit page (`?edit=1`) | the hashes of `KEYS_JS`, `PREFS_JS`, `BUILDER_JS` | `'self'` | no (no script there parses markup) | `'none'` |
| shell AI page | the same three as the shell pages | `'self'` | yes | `'self'` (not when locked) |
| shell map graph | the same three as the shell pages and the graph script's | `'self'` | yes | `'none'` |

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

A classic page ignores `frag`. The refresh script parses the fragment inertly and takes over only the tags and attributes of two allowlists
(`webjs.FRAG_TAGS`, `webjs.FRAG_ATTRS`: no script, style, link, image, frame, `on…`, `style`; `href` only `/?…` or `#`; `action` only a path of this server);
a test parses every fragment and checks the same lists.

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

## The graph view's script

The MAP's graph view carries one small inline script (`src/graphjs.py`, ~16 KB, no library) for dragging, zooming and panning. On the
classic pages it is the only script; on the shell's graph page it comes with the shell's own ([above](#the-shells-scripts)). It is boxed in:

- its SHA-256 is in that page's CSP (`script-src 'sha256-…'`, next to the shell's scripts' on the shell): no other script, inline or
  loaded, can run, and every other classic page keeps a CSP without `script-src`;
- it only moves what the server drew: it never builds HTML from data and uses no network API (`tests/test_graphjs.py`); on the classic
  page `default-src 'none'` (no `connect-src`) means it could not open a connection anyway; it keeps the view (zoom, pan, circles
  placed by hand) in `sessionStorage`;
- the page works without it: every circle is a link, zoom has links, and the page then refreshes by `<meta refresh>`
  (inside `<noscript>`; with the script, the script reloads the page itself, never while you are dragging);
- `tests/test_graphjs.py` rejects any change that adds markup building, `eval`, timers with strings, network or storage
  APIs, globals, or anything outside the page's own elements.

## Why there is no configuration editor

`config.ini` is owned by root; the web process is unprivileged. Writing it from the browser would require either a root process listening on the network
or a privileged helper — a large jump in risk for a file you change a few times a year. Edit it over SSH, then `sudo systemctl restart nuc-console nuc-console-collector nuc-console-web`.
(The shell's [layout editor](#edit-the-layout) changes only the `nuc_ui` cookie of your browser, never `config.ini`; the settings page's Export block is what you paste there.)

## Threat model in one paragraph

The page shows your topology (ports, container names, client IPs seen on databases, and on the map which service talks to which), exactly like the monitor. With loopback + `tailscale serve` only your tailnet can read it.
With a token, anyone holding the token can. It cannot change the machine, with one exception on the AI page: whoever can open it can set up the local model
(download the pinned files into the AI folder, start the model server on 127.0.0.1, turn the advisor on and off, delete those files) and ask it questions, as the
unprivileged web account, within the unit's sandbox; `[ai] web_actions = no` removes that. Rendering is cached for half the refresh interval per layout size.
The graph view's script runs in your browser and cannot send anything anywhere (the CSP forbids connections). The shell's scripts may connect
to this server only (`connect-src 'self'`), and the page's only way to put markup into the DOM is Trusted Types' one policy, whose input is checked against an allowlist first.

## macOS and Windows

There the web view **is** the dashboard: the installers run it as the hidden user `_nuc-console` (macOS LaunchDaemon) or as
LOCAL SERVICE (Windows scheduled task `\nuc-console\web`), with `--local`: while `[web] enabled = no` it listens on **127.0.0.1
only**, without a token (nothing on the network can reach it), whatever `bind` says. Set `[web] enabled = yes` (and a token for a
non-loopback `bind`) to reach it from other devices as described above, then run the installer again. `[display] mode = none`
with `[web] enabled = no` runs no web view at all.
Windows has no mode bits: keep `token_file` inside `%ProgramData%\nuc-console`, whose ACL lets only SYSTEM and Administrators write
(and limit who can read the file with an ACL if other people use the machine).

With a token, what opens the dashboard at login (`render.py --open`, `[display] mode = browser`) puts it in the address as `?token=` when the
logged-in user can read the token file (the web view moves it into a cookie, as above; for a moment it is in the browser's command line,
which other users of the machine can list). When that user cannot read it (the usual case on macOS, where the file is the service user's),
the dashboard is shown from a page written to a file instead, as the full-screen window does, and `display.log` says why.

A portable run ([PORTABLE.md](PORTABLE.md): `./run.sh --web` (the default on macOS) or `run.cmd`) starts the web view the same way on any system: 127.0.0.1 only, no
token, a free port (or `--port`), whatever `[web]` says; it stops with the run.
