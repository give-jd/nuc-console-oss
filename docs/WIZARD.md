# The setup guide

For someone who has just installed the console or opened the desktop app: five short steps that say what the screen shows, what is on and off, what each optional
piece costs, and how to set it up. It explains; it changes nothing by itself, and it never blocks the dashboard. It exists in the web view and the desktop app only: the text console has no guide.

| Where | How |
|---|---|
| Web view and desktop app | `/setup?step=1` to `step=5`, each its own URL and a plain link (no script). Reachable from the **setup guide** link in the footer, the settings page, and the banner **New here? Take the 2-minute tour** that the shell and the live app show until the guide is finished or skipped |

## The steps

1. **What you see**: the states (symbol, word, then colour), the key figures in force on this machine, ATTENTION.
2. **The screens**: Overview, MAP, CPU, HEALTH, AI with their console keys and web links; settings, Telegram, wall.
3. **On and off**: the web view (the only listener), the AI model (what it downloads, local only), Telegram (only titles leave unless `detail = full`), the collector as root and the `[features]` that reduce what it runs.
4. **Set it up**: which sections to show, the key figures (including `none`), preset, theme and density. Written only through the settings page's own actions.
5. **Where things live**: `config.ini`, the overlay, `nuc-console-config migrate`, `nuc-console-problems`, `nuc-console-accept`, the docs, and who can change what.

Every figure on steps 3 and 4 is read from the configuration in force when the page is drawn. A switch is shown as on or off, never as a tick: the guide does not say a thing is fine.

## What step 4 writes, and where

| Run | Step 4 |
|---|---|
| Portable run, desktop app | forms that write `config.ini` like the settings page does |
| Installation with the overlay (`[web] settings_actions = yes` and `[web] token_file`) | the same forms; the values go in `settings.ini` ([WEB.md](WEB.md#an-installations-settings-overlay)), sections can only be switched off |
| Installation without it (the default) | no form: why, the `sudoedit` command, the lines to put in `config.ini` and the command that applies them |

The forms post to `/settings/feature` and `/settings/config` (token, CSRF token, `Origin`, the allowlist `OVERLAY_ALLOW`: [WEB.md](WEB.md), [SECURITY.md](../SECURITY.md)); a hidden `to=setup` only sends you back to step 4. The CSP is the settings page's: no inline script.

## The marker

`setup.json` in the folder of `settings.ini` (the AI folder; a portable run's `ai` folder; `$NUC_CONSOLE_SETUP` names another file) holds `{"how": "finished" | "skipped", "at": ..., "version": ...}` and no setting. It is written by **Finish**, **Skip the guide**, **Dismiss** (`POST /setup/finish`, `/setup/skip`) Delete it to see the banner again. A marker that cannot be written only means the banner comes back; a link, a file over 2 KB or one that is not JSON counts for nothing.

See also [CONFIGURATION.md](CONFIGURATION.md) (every key), [WEB.md](WEB.md) (routes and the POST rules), [SECURITY.md](../SECURITY.md).
