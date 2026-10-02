# HEALTH: what keeps going wrong, and how to keep the machine healthy

The dashboard shows *now*. The **HEALTH** screen (console key `4`, web **health** link) shows the last day, week or month:
which apps use the CPU and the memory, which crash, hang or get killed for lack of memory, which services and containers
keep restarting or failing, when the machine runs hot, which disks are filling up, which log messages are flooding or new.
Each finding comes with how to fix or check it. It works the same on Linux, macOS and Windows.

No AI is involved in the findings: they are rules over numbers, so they can be checked. An optional local model can turn
them into plain-language advice (the ADVICE block below) and answer questions about the history; it is off by default, runs
on this machine and only suggests: [AI.md](AI.md).

## What is kept

The collector (root / SYSTEM) keeps a small history in one SQLite file:

| OS | File |
|---|---|
| Linux, macOS | `/var/lib/nuc-console/history.db` |
| Windows | `%ProgramData%\nuc-console\lib\history.db` |

| Every | What | From |
|---|---|---|
| minute (written every 5 min) | per app: CPU seconds, memory (max, average), number of processes; the host: CPU %, memory %, swap %, load, CPU temperature, throttling | the CPU screen's samplers (`/proc`; `ps`; Windows API) and, on macOS/Windows, the collector's `sensors.json` |
| 5 minutes, only what is new since the last look | crashes, hangs, out-of-memory kills, restarts, services that failed, unexpected shutdowns, hardware errors, failed logins (a count), and log messages of error level as *templates* | Linux: `journalctl -o json` (and Docker's restart count / OOMKilled / exit code); Windows: the System and Application event logs (event ids, never the message text); macOS: the crash, hang and Jetsam reports in `DiagnosticReports` (their first line only) |
| day | used and total space of every real filesystem; the boot time | `statvfs`; `boot.json` |

**Never stored**: command lines and arguments (an app is its process name), log lines as they are (a template replaces
numbers, ids, IP addresses, paths, quoted strings and every value after `=` or `:` with a placeholder, so
`login failed for url=https://user:pw@host token=abc` becomes `login failed for url=<v> token=<v>`), the IP addresses and
user names of failed logins (only how many), the body of crash reports.

Retention: hourly figures, log templates 30 days; events 90 days; disks and boots about a year. A busy machine keeps a few
MB (30 days of 200 apps an hour is about 9 MB). The file is world-readable like the other state files (names and counts
only). To start over: stop the collector, delete `history.db`, start it. Uninstalling leaves it in place, with the rest
of `/var/lib/nuc-console` (`%ProgramData%\nuc-console`).

## The findings

Each rule is a few lines of `src/health.py`, with its thresholds at the top of the file:

| Finding | When |
|---|---|
| CPU hog (warn) | an app at ≥ 80 % of one core for ≥ 6 hours of the period, or at ≥ 50 % of all cores for ≥ 2 hours |
| CPU share (info) | the busiest app used ≥ 25 % of all CPU time (when the machine used at least one core-hour) |
| Memory leak? (warn) | an app's memory grows ≥ 3 days in a row by ≥ 50 MB/day (and ≥ 10 %/day of its size), steadily (r² ≥ 0.7), with no restart or crash in between |
| Memory pressure (warn) | memory ≥ 95 % or swap ≥ 50 % in ≥ 3 hours, with the largest apps of those hours |
| Crash / hang loop | ≥ 3 crashes and hangs of one app (warn), ≥ 10 (error) |
| Out of memory (error) | any app killed for lack of memory (OOM killer, Windows resource exhaustion, macOS Jetsam) |
| Restart loop (warn) | a service or container restarted ≥ 5 times within 24 hours, or exiting with an error ≥ 3 times |
| Service failed (warn); unexpected shutdown, hardware error (error) | any |
| Hot hours | hours at or above the sensor's high mark (90 °C when it gives none): warn at ≥ 2 a day on average, with the apps running then; throttling |
| Disk full in N days | a linear fit of ≥ 7 days of use: < 30 days warn, < 7 days error; ≥ 90 % used is a warning anyway |
| Noisy / new log messages (info) | one template ≥ 2000 times a day; templates never seen before today (warn at ≥ 10) |
| Failed logins | a day with ≥ 20 and ≥ 5× the usual (info), ≥ 100 (warn) |
| Slower boot (info) | the last boot took ≥ 1.5× the median of the previous ones |

With less than 24 hours of history the screen says "collecting" and the rules that need days (memory trend, disk
forecast, new log messages, failed-login spikes) wait; events (crashes, OOM kills...) are reported from the first one.

## The screen

![HEALTH page in a browser, demo data](img/health.png)

The findings first (errors, then warnings, then information), then the busiest apps per day for CPU and memory (with a
trend: `↗ +180M/day`), the events by kind, the noisy and new log templates, the disks with the days left, hot hours and
boot times. On a narrow console the sections become one line each.

| Console | Web | |
|---|---|---|
| `4` (`h` from the dashboard) | the **health** link in the bottom bar | open it; `Esc` or `q` goes back (and so does 10 minutes without a key) |
| `d`, `w`, `m` | `period=1`, `7`, `30` | the last 24 hours, 7 days (default), 30 days (the digits are the screens: `1` to `5`) |
| `↑` `↓` (`k` `j`), `PgUp` `PgDn`, `Home` `End` | a click on a finding (`sel=<id>`) | move through the findings |
| `Enter` or `Space` | the finding's link | its details: what, the numbers behind it, how to fix or check it |
| | `pause=1` | no reload while you read |

For a quick look over SSH: `render.py --once --view health` (`--period 1|7|30`, `--select TEXT`, `--details`, `--demo`).
The report is computed at most once a minute per period, however many keys or browsers ask.

### ADVICE (optional)

With the advisor on (`[ai] enabled = yes`, or turned on from the AI page or screen) and a model server running ([AI.md](AI.md)), an **ADVICE** block appears under the findings, before the
tables: a few lines in plain language about this period's findings, headed `ADVICE (AI, <model>) — check before acting`, with the
findings it relies on cited in `[brackets]` and listed on a `cites:` line. The screens never ask the model (a key press or a page would have to wait for it):
they show an answer that already exists, and with none yet a line says so and names the command that asks. That command is
`sudo nuc-console-ask advise [--days N]` (1, 7 or 30 days; default 7), or `[ai] daily = yes` for one digest of the last 7 days a day
([AI.md](AI.md#the-daily-digest)); a question: `nuc-console-ask "why is the disk filling up?"`. On the web **AI** page the same is a click: **advice now**
(last 24 h, 7 days, 30 days) asks the model for a fresh advice in the background (never while a page is built), and the chat on that page answers questions; the advice
shows in that page's chat, and it reaches these screens when the account that ran the page may write the shared advice (root, or a portable run), which an
installation's unprivileged web account may not: use `sudo nuc-console-ask advise` or `[ai] daily = yes` for the screens. The screens show the latest answer
for the period in view from the shared `advice.json` when it is at most 36 hours old, with its age ("generated 5 h ago"); a cited
finding that no longer exists is not a link. The model gets the findings as data (names and numbers,
never log lines), may only read the history through fixed queries, and cannot run or change anything: the commands in its advice are
for you to check and run. With the advisor off the screen is exactly as above.

## Turning it off

`[features] health = no` in `config.ini`: no history thread, no file written. `[dashboard] health_in_rotation = yes` adds
the HEALTH screen to the pages a monitor with no keyboard rotates through. The advice is off unless the advisor is on (`[ai] enabled = yes`, or the AI page).
