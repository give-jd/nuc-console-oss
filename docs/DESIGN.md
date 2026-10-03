# Visualization principles applied

Summary of research into best practices for full-screen monitoring dashboards, applied to the TUI.
**[Certain]** = claim present in the source; **[Hypothesis]** = translation to a TUI. The full text of Few's PDFs
was not verified (search snippets only).

| Principle | Source | Application |
|-----------|--------|-------------|
| Only the essentials, readable "at a glance" [Certain] | Few, *Dashboard Design*: https://www.perceptualedge.com/files/Dashboard_Design_Course.pdf | Aggregate status always in the header ("✔ ALL OK" / "✖ N PROBLEMS") |
| Use color sparingly, red appears only when something is wrong [Certain] | Few, *Rich Data, Poor Data*: https://www.perceptualedge.com/articles/Whitepapers/Rich_Data_Poor_Data.pdf | Normal state is neutral (LOC column grey); yellow/red only for anomalies |
| One color = one meaning, consistent across panels [Certain] | Grafana best practices: https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/ | Green ok · yellow attention · red problem · cyan filtered |
| Color is not the only means (WCAG 1.4.1) [Certain] | https://w3c.github.io/wcag21/understanding/use-of-color.html | Every state has a symbol: ✔ ✖ ! ● ◐ ? · (the 16 ANSI colors change with the tty palette) |
| From general to particular [Certain] | Grafana best practices (same URL) | "TO WATCH" section at the top, detail below; local-only in compact form (exception-based) |
| Proximity and common region group items [Certain] | NN/g: https://www.nngroup.com/videos/proximity-gestalt/ · https://www.nngroup.com/articles/common-region/ | Blank line between sections, titles with a rule, no blank line inside a group |
| Missing data ≠ ok: No Data and Error are distinct from Normal [Certain] | https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/nodata-and-error-states/ · https://grafana.com/docs/grafana/latest/alerting/guides/missing-data/ | A rule that cannot be interpreted or a section not collected = "?" treated as open, never "blocked" |
| Do not propagate the last value without its age [Certain: known bugs https://github.com/grafana/grafana/issues/42868] | | Data older than 60/120 s: warning in "TO WATCH" and in the header |
| Burn-in: move fixed elements, rotating layout [Certain, but on LCD/OLED and digital signage: https://digitalsignage.com/digital_signage/docs/technical/screen-burn-prevention/] | | The header shifts by 1-2 columns every 10 minutes; pages rotate every 15 s |
| Source × destination matrix for policies [Certain] | UniFi zone matrix: https://help.ui.com/hc/en-us/articles/115003173168-Zone-Based-Firewalls-in-UniFi | Service × {local, LAN, Tailscale, Internet} matrix |
| Large matrices confuse: aggregate [Certain] | PolicyVis, LISA 2007: https://www.usenix.org/legacy/event/lisa07/tech/full_papers/tran/tran.pdf | Grouping by exposure level, counts instead of lists |

## Gaps (no source found)

Optimal density for distance reading, refresh rate and anti-flicker on a tty, contrast of the 16 ANSI colors at a distance.
Choices made here **without a source**: 2 s refresh (configurable 1–10 s: `[dashboard] refresh_seconds`), redraw without `clear`, ~100 content columns per table.

## Not yet applied

- Alternating rows or dotted guides on wide tables.

## Section order

The overview keeps a fixed order, top-left to bottom-right, following the "most important first" rule of dashboard design: ATTENTION (what needs action), then the security posture (EXPOSURE, FIREWALL), resources (SYSTEM), workloads (CONTAINER, DATABASE), history (BOOT) and finally the detail panels. Columns are filled in that order and never back-filled, so a line more or less in one block does not move sections around. Change it with `[dashboard] sections` in `config.ini`.

Fixed or by severity. Without a `[ui]` section that sets the layout, the order above is the only one. With `[ui] layout`, `hidden` or `preset` the cards follow that list, still in a fixed order. `[ui] order = severity` puts the cards with the worst state first, and that is where the "sections do not jump" rule above could break, so the order is a pure function of the cards' states (✖ and down, then !, then ?, then fine; stable within a state; `attention` stays on top): a card changes place only when a state changes, never when a number does, and the same states are always the same order. The console does not turn it on by itself: `order = severity` has to be written, so a monitor that nobody watches never reshuffles. The state is also written on the section's title (`── ✖ EXPOSURE ──`): a symbol, never the colour alone.

The header carries the same rule for the screens: `[1 Overview]  2 Map  3 CPU  4 Health  5 AI` with the current one in reverse video and in brackets; the KPI row under it is the figures that decide whether to look further (`[ui] kpis`), each with a symbol and a `?` when its source is missing.

## Exposure classification

| Bind address | Exposure |
|--------------|----------|
| 127.0.0.0/8 or ::1 | local only |
| 100.64.0.0/10 or fd7a:115c:a1e0::/48 | Tailscale only |
| 0.0.0.0, :: or a LAN IP | LAN + Tailscale (tailscale0 is reachable) |
| port served by `tailscale funnel` | public Internet |

tailscaled installs a ts-input rule accepting tailscale0 traffic before ufw, so tailnet peers reach a listening service regardless of ufw. For the LAN ufw applies, but Docker-published ports bypass ufw (Docker inserts rules in nat/FORWARD before the ufw chains), so only a DOCKER-USER rule or a 127.0.0.1 bind really protects them. The collector flags every 0.0.0.0 container port not covered by DOCKER-USER.

## Expected vs actual

The baseline says what *changed*; `[expose]` in `config.ini` says what you *meant*: the widest reach a service may have (`local`, `tailnet`, `lan`, `internet`, the same four groups as above). `exposure.py` compares it with the reach it computed: `expose_apply` gives each exposure row the declaration that names it, `expose_over` is true when the row's group is wider (INTERNET > LAN > TAILNET > LOCALE). A key names what is behind the row (container, compose service or project, process, unit, database name or kind, `[webapps]` name) or a port; behind a Funnel it is whatever listens on the backend, found by `exposure.row_owners`, the same lookup the map draws. The most restrictive matching key wins and an unknown firewall verdict counts as open, as everywhere else.

It shows in three places: ATTENTION (`over-exposed`, an error listing each service; `expose-unmatched`, a warning for a name that matches nothing), the matrix and the compact overview (a red "beyond config.ini: local" instead of the note, or a grey "expected: LAN" before it), and the MAP (a "declared reach" fact on the port node, an error finding when it is exceeded). It only adds alarms: `db-open-lan` and `docker-bypass` stay as they were, declared or not.

## Map

The MAP answers the question the exposure matrix leaves open: *what is behind* a reachable port. It is a tree, not a
node-and-arrow drawing: on a text console seen from a distance, edges that cross in box-drawing characters stop being
readable after a handful of nodes, while an indented path (`LAN → :8080 → shop-web-1 → shop-api-1 → shop-db-1`) reads
the same at 80 and at 226 columns. A node reached by two paths appears twice: that it can be reached from two sides is
the information.

| Root | Walks | Question |
|------|-------|----------|
| INTERNET, LAN, TAILNET, LOCAL | entry port → its process or container → what that one uses, and so on | who can reach what, and through what? |
| IMPACT | a container that is down or unhealthy, a failed unit, a declared web app not listening → **who depends on it** | what breaks if this is down? |
| STACKS | compose project → containers → what they use | how is each project wired? |
| OUTBOUND | a service → the remote addresses it connects to | who talks to other hosts? |

Every edge carries its evidence, and the evidence is drawn, never hidden behind one kind of arrow:

| Edge | Evidence | Glyph |
|------|----------|-------|
| seen | a live TCP connection observed: inside the container's network namespace (Linux), or on the host's sockets | `━━►` |
| declared | compose `depends_on`, a container's environment naming the other one as a host (only the match is kept, never the value), a systemd/Windows service dependency | `╌╌►` |
| possible | same docker network as a database, nothing else known | `┄┄►` |
| connected to it | an address seen connected to the entry (`◄━━`) | `◄━━` |

Connections are sampled every 30 s and remembered for 24 h, so a nightly job still shows up the next morning; a connection
shorter than the sample is not seen, and the map says so. On macOS and Windows the containers live in Docker Desktop's
VM: their own connections are out of reach, the map says that too and shows the declared and same-network links only.
State propagates along `seen` and `declared` edges: a database that is down turns what depends on it yellow, all the way
up to the entry the LAN uses to reach it.

### Tree or graph

On the console the MAP is only a tree, for the reason above. In a browser the same graph can also be drawn as circles and
lines (the **tree | graph** switch): it shows at a glance the hubs and the islands that a tree spreads over many rows, and
the *local graph* of one node (what is one or two links away) answers "what touches this?" without scrolling. The tree
stays the default because it is the one that reads the same everywhere and states every path in words.

- The positions are computed by the server (`src/graphlayout.py`, a force-directed layout): deterministic, each node starting
  from a hash of its id, so the same graph is always drawn the same way and a refresh does not move what did not change.
- The page is complete without a script: every circle is a link to its details, zoom is a link. One small inline script
  (`src/graphjs.py`) adds dragging, panning, wheel zoom and a light physics; it is pinned by its SHA-256 in that page's CSP,
  cannot open connections, and is the only script of the classic pages (the shell's pages carry their own as well; docs/WEB.md).
- Edges keep the tree's evidence styles: seen solid, declared dashed, same network dotted; zone → port and port → owner
  links are thin and grey (structure, not traffic).

## CPU

The overview keeps one line of bars per core; the **CPU** screen (key `3`, web `cpu`) is the htop-like view for when
something is busy: what the processor is, what each core does, how hot it is, and which processes cost what.

| | Linux | macOS | Windows |
|---|---|---|---|
| Model, cores, threads, caches | `/proc/cpuinfo`, sysfs topology and caches | `sysctl` (no subprocess) | registry, `GetLogicalProcessorInformationEx` |
| P/E cores | `/sys/devices/cpu_core` and `cpu_atom` (Intel), capacity classes (ARM) | `hw.perflevel0/1` (counts) | `EfficiencyClass` |
| Per-core load | `/proc/stat` (user, system, iowait, irq, steal) | `host_processor_info` | `NtQuerySystemInformation` |
| Frequency | cpufreq per core, governor, driver | Intel: one value; Apple Silicon: per cluster from `powermetrics` (collector) | per core (`CallNtPowerInformation` / PDH) |
| Temperature | hwmon: coretemp per core, k10temp, zenpower, ARM SoC, thermal zones | collector: `powermetrics`, `smctemp`, `osx-cpu-temp` | collector: LibreHardwareMonitor, OpenHardwareMonitor, ACPI |
| Processes | `/proc/<pid>` | `ps` (fixed argv) | Toolhelp, `GetProcessTimes`, working set |

The rules are the dashboard's: what cannot be read is `?`, never a guess or a zero; one source failing leaves the others
on screen with a note; processes are sampled only while the screen is shown (it costs a little CPU), and only their names
are read. Temperatures on macOS and Windows need root/SYSTEM, so the collector writes them to `sensors.json` every 10 s
(30 s on Windows, where each reading starts PowerShell) and the screen merges them.

## Health

The HEALTH screen answers questions about time ("what keeps crashing?", "when will this disk be full?"), which a snapshot
cannot. Decisions:

- **A history, in SQLite**: the data is numbers and events, and the questions are aggregations (top apps of the week, a
  trend), which is what SQL is for; `sqlite3` is in the standard library on every OS, the embeddable Windows Python
  included. One writer (the collector, WAL mode), readers open it read-only. No vector database: there is no free text to
  search, log lines become templates and are counted.
- **Rules before any model**: every finding is a small function with named thresholds (`src/health.py`) and the numbers it
  used, so it can be checked and tested; a rule that needs days says "collecting" until it has them. An optional local
  model can only rephrase these findings (docs/HEALTH.md), never replace them.
- **Cheap and bounded**: the collector samples once a minute, writes every five, reads only what is new since its last
  look (journal cursor, event-log record id, crash-report mtime), keeps the top 200 apps an hour, prunes daily.
- **Nothing that can hold a secret**: process names, not command lines; templates, not log lines; counts, not the IP
  addresses or user names of failed logins.

Full description, what is collected per OS and every rule: [docs/HEALTH.md](HEALTH.md).

## Code layout

Standard library only, and flat: every module is one file in `src/` (the installers copy `src/*.py`, `tools/build_release.py` ships
`src/`), no packages. Each process has **one entry script**; the modules it imports sit next to it. The dependencies run one way: `ui` is
read by `ansi`, `exposure` and `cards`, `exposure` by `graph` and `cards`, `graph` and `cards` by `render`, `render` by `web` and `notify`.

| Entry script | Process |
|---|---|
| `collector.py` (with `collect_darwin.py`, `collect_windows.py`) | the privileged collector: writes the container, network and boot state as JSON (and `history.db`) |
| `render.py` | the unprivileged console: the overview, the MAP, CPU, HEALTH and AI screens on the tty (macOS/Windows: `--kiosk`, `--open`); `--once` for one frame |
| `web.py` | the read-only web view of the same screens, their data API (`/api/v1`) and the live app's page (`/app`) |
| `notify.py` | the optional Telegram notifier (and the requests of the web view's Telegram page, read from its inbox) |
| `aisetup.py`, `advisor.py` | `nuc-console-ai` and `nuc-console-ask` |
| `update.py` | the logic of `nuc-console-update` |

| Module | Holds |
|---|---|
| `render.py` | the inputs (`snapshot`, `Sampler`, `CpuFeed`, the HEALTH and AI data), the problem list, the drawing of every screen, the TTY layout engine (`pack`, the levels, `slides`, `frame`) and the main loop. `page_overview` owns the caps of a frame (the `Caps` it builds the cards with: `full`, and the `expand` and `trunc` sets); `render.py` imports what it draws with from `ui`, `ansi`, `exposure`, `cards` and `screens` and re-exports none of it: a test, a tool or another module imports a name from the module it lives in |
| `ui.py` | what a colour **means**: the semantic tokens (`ok`, `warn`, `err`, `unknown`, `info`, `muted`, `accent`, `strong`, the header banners, `sel`), the ANSI themes (`default` is the SGR codes the console has always written; `light`, `hc`, `mono`) and the CSS palettes (`dark`, `light`, `hc`) as plain data, `sgr(token)`; the text helpers that have no colour and no clock: `safe`, `plural`, the human sizes, durations, rates and ages; and **the components** every screen is to be built from (`Span`, `Line`, `Card`, `Kpi`, `Table`, `KV`, `Bar`, `Spark`, `Pill`, `Msg`, `Wrap`, `Group`, `Tree`, `Details`, `Notice`, `More`, `Problem`, `Accepted`, `RichMsg`, `Hint`, and `Raw`, the lines of a section drawn the old way; and, for the system, container, database and boot cards, `Head` (a part's heading), `Indent`, `Grid` (the CPU cores: Lines of a `Bar` in columns), `Timeline` (stacked segments of a whole: the boot stages), `NoteTable` (a `Table` whose rows carry notes under them) and `Flow` (facts that flow after a lead mark); a `Bar` may carry a `tone` when it is a share and not a level; for the AI screen `Badge` (a verdict as a pill, with its symbol), `Spec` (what is known of one model, labelled, commands whole), `Question` (a question that waits for y or n), and the web's `Action` (a form button that posts to the engine's endpoints with its hidden fields, a `data-key` from the keymap), `Controls` (a row of parts) and `Qa` (one exchange of the chat), which the console draws as nothing: it has keys). A `Table` column carries the console's width (`w`, `gap`, `clip`, centred or not) next to its priority, and a `Table` its console layout (`indent`, `head_line`, `titled` groups, `fill`); a `Bar` and a `Spark` the console's width too, which the web ignores. Plain classes with `__slots__`; text from the machine is cleaned in the constructor; no value without a state (`ok`, `warn`, `err`, `down`, `unknown`, `info`): a `Bar` or a `Kpi` that could not be read is `unknown` and reads `?`, never fine |
| `ansi.py` | the console primitives every screen is drawn from: `c`, `section`, `msg`, `msg_wrap`, `kv`, `bar`, `sparkline`, `pad`, `clip`, `columns`, `fit_join`, `cell` (the exposure matrix glyph); and **the console's drawing of the components**: `render(node, w)` returns `(lines, truncated)` for any component (a node that is not one is `?`), `card_lines(card, w)` adds the section's title line and the note (a card whose body starts with `Raw` carries its own), and the drawing of `Head`, `Indent`, `Grid`, `Timeline` (the boot stages' raw colours are `STAGE_COLORS`), `NoteTable` and `Flow` in a block of their own. A colour that is a state is asked of `ui.sgr(token)` |
| `cards.py` | the **card registry** and the **KPI model**. `register(id, title, feature, builder)` puts a card (the ids are `nuc_config.SECTIONS`) in `CARDS`; `build(id, ctx, k, caps)` returns its `ui.Card` at detail level `k` and remembers it for the frame by (id, level, caps), so the layout engine never builds a card twice. `Ctx` is the data of a frame (the Sampler's reading, the collectors' state, the problems, the configuration, a memo); `Caps` is what the layout lets a card show (`full`, the `expand` and `trunc` sets that `render.page_overview` owns). A card is either **native** (a builder decorated with `@native(id, title, feature)` that turns the data into components, honouring the level `k` and the caps through `caps.lim` and `caps.opened`; `NATIVE` holds them; today every card of the overview: `attention`, `exposure`, `webapps`, `firewall`, `system`, `containers`, `databases`, `boot`, `network_traffic`, `sessions`, `tailscale`, `docker_disk` and `disks`) or `Raw` (the lines `render.py` has always drawn; still understood by both renderers). A native card says what it hid with a `More` and `card.truncated`, which the Details pages read (the layout collects it in `trunc`); a `Wrap` that the width cuts says so when it is drawn. The web may show more than the console through fields the console does not draw: ATTENTION's `Problem` carries its id, why it matters, the fix and the command that accepts it (the words of this OS and of this install, `PROBLEMS_CMD` / `ACCEPT_CMD`, from `render.ProblemList.info`) and a `Details` lists what was accepted, with the reason and the `--forget` command (`ProblemList.known`), while the console keeps its one line "N accepted as known"; a `Span.full` is the whole name where the console had to cut it. A card's state comes from the problems that belong to it (`PROBLEM_CARDS`) and is `unknown` when its source is missing. `KPIS` has one builder per `prefs.KPI_IDS`; `kpis(ctx, ids)` returns the row, each `unknown` (`?`) when its source is missing. Nothing in it draws or reads the host |
| `exposure.py` | the exposure model: the rows of ports and their verdict per way in, the firewall rules read, the `[expose]` check, the web apps, the baseline comparison, and who is behind a row. No drawing, no files |
| `tgweb.py` | the engine of the web view's **Telegram page**: the pairing (getMe, the one-time link, `notify.pair` in a thread, Cancel), the requests it hands to the notifier through `NOTIFY_DIR/inbox` (pair, on, off, test: one file each, never read back), the notifier's answers (status.json) and the demo's simulated flow. `web.py` turns a form into a call and `snapshot()` into the page; nothing here draws |
| `prefs.py` | the preferences of the interface: the `[ui]` section, the cookie and `?ui=` grammar, the presets, which cards and KPIs show (`visible_cards`, `KPI_IDS`). Pure: nothing is read or drawn |
| `graph.py`, `graphlayout.py`, `graphjs.py` | the MAP model (nodes, edges, the tree's rows), the positions of the graph view, the graph view's script |
| `htmlview.py` | ANSI to HTML (`to_html`) and the CSS of the web pages; and, in its own section at the end, **the web's drawing of the components**: `html(node)` is semantic HTML (a `Card` is an `<article>`, numbers are `<td class="n">`, tones and states are classes such as `t-err` and `st-warn`, a `Bar` and a `Spark` are SVG with attributes and no inline style, a `More` is a `<details>`, a `Problem` has its id in `<code class="pid">` and its fix in `<code class="cmd">`; `<h3 class="sub">`, `<ul class="grid">`, a `Timeline` is an SVG of `<rect class="seg seg-NAME">` plus a legend, the notes of a `NoteTable` row a `<tr class="sub">`), everything escaped, a link only when it is a path, a query or http(s). The new shell draws its cards with it; and, in its "shell" section, the markup builders of the new shell: top bar, KPI tiles, cards, footer, help. Pure functions of the data they get; every control a link or a form |
| `screens.py` | the **view-models of the full screens**, one block per screen (`# ---- CPU`, `# ---- HEALTH`, `# ---- MAP`, `# ---- AI`): the state a screen keeps while open (`HealthView`, `health_key`, `health_sync`; `MapView`, `map_key`, `map_sync`; `AiView`, `ai_key`, `ai_sync`: what each key does is `ui.KEYMAP`'s) and the builders that turn a report into components. A builder is told the console's width and height, or `None` for both when the web reads it: the console gets what fits (`health_lines`: the sections at the fullest level that fits, the findings in view, the details pane beside or under them), the web everything (`health_model`: `Title` with a `Seg` of periods, `Finding`s with their `Pane`, an `Advice`, a `Cols` of `Table`s with `Bar` and `Series`). Console-only layout lives in fields the web ignores (`Table.fit`, `Table.clip`, `Col.w/gap/clip`, `Line.clip`, `Cols.h`, `Cols.once`, `Split`, `Title`'s dropping of parts). The clock and the advisor's answer come in as arguments; nothing in it reads a file or the host. The AI block holds the model rows (`ai_rows`: every text cleaned, every number checked), `AiView` and its keys, and the pieces of the screen as components: `ai_title`, `ai_work_nodes` (the switch, a download as a `Bar`, the answer to the last key, the folder), `ai_hw_nodes` and `ai_status_nodes` (each at four levels of detail, side by side from 110 columns), `ai_table` (the models, the verdict a `Badge`, the cursor row solid), `ai_spec` (the details of a model, a `Spec`), `ai_legend` and `ai_footer` (the keys, or a `Question` while one waits). `ai_lines` lays them out for a console of w by h, byte for byte as the screen was drawn before; the actions themselves stay in the one engine, `aiweb.py`, which `render.ai_do` (the keys) and `web.py` (the forms) call. The `# ---- MAP` block holds `MapView`, `map_key`, `map_sync`, `map_select`, `map_layout` and the builders: `map_nodes(G, rs, w, h, cursor_key, details, top, only)` lays the Map out for a console (the title with the legend of the arrows, the notes, an `Outline` of `Branch`es, the `Props` of the selected node beside the tree from `MAP_PANE_W` columns or below it; `map_view` draws it) and `map_web(G, rs, st, sel, nid, links)` for the web (nothing cut: the `Title` carries the choices as `Seg`s with their keys, the rows are links, the `Props` come with a link that closes them; `MapLinks` makes the URLs). New components: `Branch`, `Outline`, `Props`, `Legend` (and `Title`'s `legend` and `segs`). The CPU block holds `CpuView` and the key actions (`cpu_key`, `cpu_rows`, `cpu_sync`, `cpu_select`), the constants, and `cpu_view(d, ctx, w, h, sort, cur, details, top, links)`, which lays the body out for a console of w by h (header, one `Meter` per logical CPU in a `Grid`, temperatures, the process `Table` with its priority columns and the cursor row, the details `KV` beside the table from `CPU_PANE_W` columns on or below it) and returns a `CpuScreen`. The console draws the nodes with `ansi.render` (byte for byte what it drew before they were components); the web draws the same nodes with `htmlview.html` at `WEB_W` and a huge height (nothing cut), with `links` making the column heads sort links (`data-key` from the keymap) and the rows selection links. What only one surface draws is in the nodes: `Cap`, `Split` and the console fields of `Col`, `Table`, `KV`, `Wrap`, `Grid` for the console; `Tiles` (key figures) for the web, inside `Only`. It imports `ansi` and `ui`, never `render`; what it needs of the world comes in as a `CpuCtx` (whose OS, the topology, the clock) |
| `webapi.py` | the **data API**'s documents and events (`/api/v1`, docs/WEB.md): `data(node)` turns any component into JSON (`{"type": its class, field: value}`, every slot of the class and its bases less `CONSOLE_ONLY`, the console's widths and indents; `Only("console")` left out, `Only("web")` and `Cap` spliced in, as the web draws them), `problems(pb)` a frame's problems, `document(view, body, at)` a view's document with its `rev` (a hash of the data, not of the time: the ETag and the event id), `event`/`comment`/`retry` the lines of a stream. Pure: `web.py` checks the request, builds the very components its pages draw (`cpu_nodes`, `health_nodes`, `map_nodes`, `ai_nodes`, the overview's cards and KPIs) and serves what these return |
| `appjs.py` | the **live app**'s script (`/app`) as a constant: a router, the stream of the data API, `draw(node)` (htmlview.html's markup built with `createElement`, element for element: `tools/browser_check.py` compares the two in a browser), the overview's cards and key figures (the shell's markup), a morph that turns the page into each new drawing node by node (keyed by `data-k`, `data-card`, `data-problem`: focus, open `<details>`, scroll and typed text stay), and the forms of the AI screen posted in the background. Its tags and attributes are the fixed lists `HTML_TAGS`, `SVG_TAGS`, `ATTRS`; `tests/jsrules.py` (policy `app`) fixes the rest |
| `webcss.py` | the style sheet of the shell (the default web interface) as constants (installers copy only `src/*.py`): tokens per theme, densities, zoom classes, components, and the classic pages' classes with their colours turned into the variables (`themed`). Served at `/s/app.<sha8>.css` |
| `nuc_config.py` | `config.ini`, the paths of each OS and of the portable run. `load()` reads the file; `current()` is the process's one configuration dict, loaded once (`render.CFG` is that object) |
| `cpuinfo.py`, `procs.py`, `hostinfo.py`, `winapi.py` | the CPU and process producers, the host metrics on macOS and Windows, the Windows API calls |
| `health.py`, `history.py` | the HEALTH findings and the history database they read |
| `aihw.py`, `aisetup.py`, `advisor.py` | the hardware the AI screen rates, the installer and server of the local model, the advisor |
| `demo.py` | the synthetic data of `--demo` |

Where a change goes: a colour that means a state is a token in `ui.py`, not a literal code in new drawing code; a rule about what is
reachable is `exposure.py` (or `graph.py` for the MAP), not a line of a screen; a card of the overview is a builder in `cards.py` (`@cards.native`: data to components, no ANSI; `render.py` registers them in the order of the screen) and a KPI with `@cards.kpi(id)` in `cards.py` (add its id to `prefs.KPI_IDS`, its label to `KPI_LABELS` and its cookie code to `prefs.KPI_CODES`); the screen itself, its layout and its words are
`render.py`. On-screen strings are English. A test that replaces a name must replace it where it is used (`exposure.X`, not `render.X`).

## macOS and Windows

There the firewall decides per **program**, so the collector (root / SYSTEM, the only one that sees the program behind every
socket) judges each listening port and writes the verdict next to it; the renderer uses it for both the LAN and the
Tailscale column (the OS firewall filters the Tailscale interface too). Docker Desktop's port proxy is an ordinary program
there, so the Linux "Docker bypasses ufw" problem does not exist.

| Windows Firewall | Verdict |
|---|---|
| active profile off | open (no firewall) |
| "block all incoming connections" | blocked |
| default inbound allow | open |
| an enabled block rule matching protocol, port, program, service | blocked (block wins) |
| an enabled allow rule, remote `*` or `LocalSubnet` | open |
| an allow rule limited to some addresses | filtered |
| port keywords (RPC…), local address, interface (type), authenticated peers, a service with no process | unknown `?` |
| no matching rule (default inbound block) | blocked; **unknown** if Group Policy rules exist (not in the local store) |
| several active profiles (one per network) | the most exposed one |

Rules Windows creates for Store apps (with an owner or a package) apply only inside that app's AppContainer; the Wi-Fi Direct
and Teredo rule groups apply only to those interfaces.

| macOS Application Firewall | Verdict |
|---|---|
| off | open (no firewall) |
| "block all incoming connections" | blocked, except essential services (Bonjour, DHCP, IPsec) |
| program listed as allowed / blocked | open / blocked |
| Apple's own program, "automatically allow built-in software" on | open |
| any other program | unknown `?` (macOS asks the user, or allows it if signed: not verified) |
| `pf` enabled with rules of its own | what would be open becomes unknown `?` (not interpreted) |
