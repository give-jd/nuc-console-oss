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
Choices made here **without a source**: 2 s refresh, redraw without `clear`, ~100 content columns per table.

## Not yet applied

- "Expected vs actual" column/marker in the matrix (needs a baseline: issue #5).
- Alternating rows or dotted guides on wide tables.

## Section order

The overview keeps a fixed order, top-left to bottom-right, following the "most important first" rule of dashboard design: ATTENTION (what needs action), then the security posture (EXPOSURE, FIREWALL), resources (SYSTEM), workloads (CONTAINER, DATABASE), history (BOOT) and finally the detail panels. Columns are filled in that order and never back-filled, so a line more or less in one block does not move sections around. Change it with `[dashboard] sections` in `config.ini`.

## Exposure classification

| Bind address | Exposure |
|--------------|----------|
| 127.0.0.0/8 or ::1 | local only |
| 100.64.0.0/10 or fd7a:115c:a1e0::/48 | Tailscale only |
| 0.0.0.0, :: or a LAN IP | LAN + Tailscale (tailscale0 is reachable) |
| port served by `tailscale funnel` | public Internet |

tailscaled installs a ts-input rule accepting tailscale0 traffic before ufw, so tailnet peers reach a listening service regardless of ufw. For the LAN ufw applies, but Docker-published ports bypass ufw (Docker inserts rules in nat/FORWARD before the ufw chains), so only a DOCKER-USER rule or a 127.0.0.1 bind really protects them. The collector flags every 0.0.0.0 container port not covered by DOCKER-USER.
