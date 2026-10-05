# Telegram notifications

New and resolved ATTENTION problems on your phone, as a Telegram message, so you do not have to look at the monitor.
**Optional, off by default, free.** It uses a Telegram bot that you create yourself in a minute: there is no central server
and no account to open with this project. It only **sends**: no listener, no webhook, no commands, and the service never reads a message.

```
nuc-console · nuc
NEW  ‼ Database/broker open on the LAN
NEW  Container exited with an error
OK   ufw is off
```

Every message starts with the host name (here `nuc`), so with the same bot on several of your machines you see which one speaks.
`NEW` is a problem that appeared, `OK` one that went away, `‼` marks errors and port changes. `nuc-console-telegram --preview` prints
the message the current problems would send, without sending anything.

## Set it up in three steps

**1. Create your bot (once, on your phone).** In Telegram open [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and
a username ending in `bot`. BotFather answers with a **token** (`123456789:AA…`): copy it. Optional: send `/setjoingroups` and choose
*Disable*, so nobody can add your bot to a group. The token is a password: whoever has it can write as your bot.

**2. Pair it with this machine.** On the web view's **Telegram page** (the ⚙ settings › *Telegram page*, or `/?view=telegram`): paste the
token and your @username and press **Pair** ([below](#from-the-web-view)). Or from the machine, as an administrator:

```bash
sudo nuc-console-telegram --setup          # Windows: nuc-console-telegram.cmd --setup  (administrator prompt)
```

It asks for the **token** and for **your Telegram @username** (the one shown in Telegram > Settings; if you have none, set one there first),
then prints a link: `https://t.me/<your_bot>?start=<code>` (the page shows the same link, with an *Open in Telegram* button).

**3. Tap the link on your phone and press Start.** The machine notices it by itself, stores the chat and says *paired*. That is all: no numbers
to type, no chat id to look up. The notifier is on from now on (`--off`, or *Switch off* on the page, switches it off).

## In the desktop app and a portable run

The [desktop app](DESKTOP.md) and a portable run (`run.sh` / `run.cmd`, [PORTABLE.md](PORTABLE.md)) start the notifier themselves, beside the
web view, as your account: there is no service to install and no terminal to open. Open **⚙ settings › Phone alerts › Set up Telegram alerts**:
the Telegram page shows the four steps (create the bot with @BotFather, paste its token and your @username, press Start in Telegram, **Send a
test**), and the state of the alerts from then on. The token and the paired chat are kept in the data folder, in `notify/` (0711, files 0600:
only your account can read them; Windows: `notify\private` in your own folder), the notifier's log is `logs/notify.log`. The notifier stops
when you quit the app (or `run.sh`), and starts again with it. In a console in a terminal (`./run.sh --console`) it only sends: there is no
page to pair from.

## From the web view

The **Telegram page** of the web view (`/?view=telegram`, linked from the settings page) does what the command line does, without a terminal:
it shows whether the alerts are on and reach someone (and the notifier's last message and last error), pairs (the token, your @username, then
the link to press Start on, with *Cancel*), switches on and off, and sends a test. It reloads by itself only while a pairing waits or an
answer is due.

**The token goes one way.** The page checks the token with Telegram (`getMe`), makes the one-time link and waits for your Start (the same
code as `--setup`), then hands the pairing to the notifier service and forgets it: the web view never stores it and can never read it again.
The handover is a file in the notifier's `inbox/` folder, where the web view's account may **create files and do nothing else**:

| | |
|---|---|
| Linux | `/var/lib/nuc-console-notify/inbox`, mode 2730, owner and group `nuc-console-notify`; the web view's unit is in that group (`SupplementaryGroups=`, `ReadWritePaths=`), so it can add a file but not list or read the folder. The notifier (another account) reads each request, deletes it, keeps the token in its own 0600 file |
| macOS | `/var/lib/nuc-console-notify/inbox` (0700): the web view and the notifier are the same account, `_nuc-console`, as for the token |
| Windows | `%ProgramData%\nuc-console\notify\inbox`: LOCAL SERVICE (the web view) may only add files (`W` on the folder), NETWORK SERVICE (the notifier) reads and deletes them |

The notifier says what it did in `status.json` (the page shows it) and keeps what the page chose in `web.json` next to it (0644, no secret:
`{"v": 1, "enabled": true, "username": "your_name"}`), laid over `config.ini`: the alerts are on when `config.ini` says `enabled = yes`
**or** the page switched them on, and the @username the page paired replaces `username`. `config.ini`'s `enabled = yes` cannot be switched
off from the page (it says *on by config.ini*). The command line has the last word: `--setup`, `--on`, `--off` and `--forget` write the page's
choice into `config.ini` and delete `web.json`.

**Nothing changes silently.** Pairing again from the page first tells the chat paired until then (*this machine now sends its alerts to
@…*), and *Switch off* tells the paired chat before it goes quiet: someone who can open the web view cannot quietly take the alerts away.

**Who can do it:** whoever can open the web view, as for the AI page's buttons: on Linux the web view is off unless you enable it, then
loopback (with `tailscale serve`: your tailnet) or a token; on macOS and Windows it listens on 127.0.0.1 (every local user and program).
The forms carry the page's CSRF token and the same checks as the AI page ([WEB.md](WEB.md#the-telegram-pages-buttons)). If that is more
than you want, `[telegram] web_actions = no`: the page only shows, a post is refused with `403`, and the notifier reads no request.

**The service waits for the page.** While the page may set it up (`web_actions = yes` and a web view runs here: `[web] enabled`, or on
macOS and Windows the dashboard), the notifier keeps running when it is off or not paired, writes its status every 30 s and looks at its
inbox every 2 s; otherwise it exits as before. On Linux the web view's unit starts it (`Wants=`). If the page says the notifier does not
take its requests, start it: `sudo systemctl restart nuc-console-notify` (macOS `sudo launchctl kickstart -k system/com.nuc-console.notify`,
Windows: run the installer again).

## Switch it on and off, test it

| Command | |
|---|---|
| `sudo nuc-console-telegram --setup` | pair (steps 2 and 3 above); it switches the notifications on |
| `sudo nuc-console-telegram --off` / `--on` | switch the notifications off or on again (sets `[telegram] enabled`; the pairing is kept) |
| `sudo nuc-console-telegram --test` | send a test message now and say what Telegram answered |
| `nuc-console-telegram --status [--json]` | on or off, paired or not, when the last message went out, the last error |
| `sudo nuc-console-telegram --forget` | delete the token and the paired chat from this machine. Revoke the token in @BotFather too (`/revoke`) if it leaked |

Windows: the same commands as `nuc-console-telegram.cmd` from an **administrator** prompt (also for `--status`: ordinary users cannot open the notifier's folder).

## Settings

```ini
# /etc/nuc-console/config.ini   (Windows: %ProgramData%\nuc-console\config.ini)
[telegram]
enabled = no
username = your_telegram_name
detail = titles
resolved = yes
web_actions = yes
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The switch (`--on` / `--off` edit it; the web page can switch on what this leaves off). Nothing is sent while it is off or while the chat is not paired |
| `username` | empty | Your Telegram `@username` (5-32 letters, digits, `_`): the only person who gets the messages. `--setup` writes it |
| `detail` | `titles` | `titles`: only the **title** of the problem and the host name leave the machine. `full`: also its text (container names, ports, process names) |
| `resolved` | `yes` | Also send a message when a problem is gone |
| `web_actions` | `yes` | The web view's Telegram page may pair, switch and test; `no`: it only shows, and the notifier reads no request ([above](#from-the-web-view)) |

The **token is never in `config.ini`** (it is world-readable, and so is `config.ini.dist`). It lives in the notifier's own folder, next to the paired chat:

| | |
|---|---|
| Linux | `/var/lib/nuc-console-notify` (mode 0711), owned by **`nuc-console-notify`**, a user of its own: the web view's user `nuc-console` (the web view may be reachable on your LAN) cannot read the token. `token`, `chat.json` and `sent.json` are 0600; `status.json` (0644) holds no secret |
| macOS | `/var/lib/nuc-console-notify` (0711, owned by `_nuc-console`; files 0600). The macOS web view only ever listens on 127.0.0.1 |
| Windows | the notifier runs as **NETWORK SERVICE**, not as the web view's LOCAL SERVICE. `%ProgramData%\nuc-console\notify\private` (token, chat): only SYSTEM, Administrators and NETWORK SERVICE; `%ProgramData%\nuc-console\notify` itself holds `status.json` only, readable by the dashboard |

Changes to `config.ini`: `--on` / `--off` apply them at once; after editing the file by hand restart the service
(Linux `sudo systemctl restart nuc-console-notify`, macOS `sudo launchctl kickstart -k system/com.nuc-console.notify`,
Windows `nuc-console-telegram.cmd --on` from an administrator prompt: it stops and starts the task, `Start-ScheduledTask` alone does nothing while it runs).

## When a message is sent

- A problem must stay on the list for **two checks in a row** (2 cycles of 30 s) before it is announced: a container that restarts and recovers, or a
  one-second blip, sends nothing. When it is gone you get a "resolved" message (unless `resolved = no`).
- At most **20 messages per hour**: a flapping service cannot flood your phone.
- The first time it runs (nothing sent yet) it sends **one summary** of what is open now, not one message per existing problem.

## What leaves the machine, and what never does

| Leaves the machine (HTTPS to `api.telegram.org`, nothing else) | |
|---|---|
| default (`detail = titles`) | the host name and the **title** of each problem ("Container unhealthy", "New exposed port"), the bot token (as Telegram's API requires, in the request URL, over TLS) |
| `detail = full` | also the problem's text: container names, ports, process names. It is then stored by Telegram. Bot chats are not end-to-end encrypted: Telegram can read them |

| Never | |
|---|---|
| a listener | no port is opened: the notifier is an HTTPS **client** (the systemd unit allows only outgoing connections, no `AF_NETLINK`, no capabilities). No webhook |
| reading messages | the **service** never asks Telegram for anything you wrote and has **no commands**: you cannot control the machine from the chat. Only a pairing you start (`--setup`, or *Pair* on the web page) asks Telegram for the single `/start` message that carries your one-time code, then stops. The page's requests reach the service as files in its inbox, never through Telegram |
| the token in `config.ini`, logs, `status.json` or the screen | it is read from its 0600 file by the service user only; the dashboard and the status file show an error, never the token |
| a central server | there is none: your bot, your token, your chat. Nothing is sent to this project or anyone else |
| root | the service runs as an unprivileged user (Linux `nuc-console-notify`, macOS `_nuc-console`, Windows NETWORK SERVICE) with the same hardening as the web view; on Linux and Windows it is not the web view's account, so a web view reachable on the LAN cannot read the token |

The notifier reads the same state files as the dashboard, so it needs no more rights than the monitor. The pairing link carries a **one-time code**:
only a Start that carries it pairs the chat.

## Why a bot of your own?

A Telegram bot can only write to someone who has **started it first**, and it can never write to an `@username` it has not talked to: the Bot API needs a numeric
chat id, which only appears when you press Start. So each person creates a bot (free, a minute, the token) and presses Start once; the machine learns the chat id by itself
and you never see a number. Because the bot is yours, there is no shared service to run, trust or pay for.

The **same bot can serve all your own machines**: run `--setup` on each machine with the same token and the same `@username`. Each machine pairs separately and the messages start
with its host name.

## Troubleshooting

1. `nuc-console-telegram --status`: is it on, paired, when did the last message go out, what was the last error?
2. The dashboard says it too, in ATTENTION (and `nuc-console-problems` explains the fix):
   - **`telegram-unpaired`**, *Telegram notifications on, but not paired*: `enabled = yes` but no chat yet (or after `--forget`). Pair it (the web view's Telegram page, or `--setup`) and press Start on the link, in the chat with **your** bot (not with @BotFather).
   - **`telegram-failing`**, *Telegram notifier not running* (no sign of life for 5 minutes) or *Telegram notifications failing for N min: reason* (sends failing for 10 minutes: no Internet, wrong or revoked token, you blocked the bot).
3. `sudo nuc-console-telegram --test` sends one message and prints Telegram's answer. `Unauthorized` = the token is wrong or revoked: `--setup` again with the new one. A message that never arrives although the answer is `ok` means you pressed Start on another bot, or muted the chat.
4. Logs: Linux `journalctl -u nuc-console-notify` · macOS `/var/log/nuc-console/notify.log` · Windows `%ProgramData%\nuc-console\logs\notify.log`.
5. Not running: Linux `systemctl status nuc-console-notify` · macOS `sudo launchctl print system/com.nuc-console.notify` · Windows `Get-ScheduledTask -TaskPath \nuc-console\ -TaskName notify`. The service always exists (the installers register it) and idles until the notifier is on and paired; it is started and stopped by `--setup`, `--on` and `--off`.

On Windows an ordinary (not administrator) `nuc-console-problems` cannot read the notifier's status and leaves the two Telegram problems out; the web view and the administrator prompt show them.

Uninstalling removes the notifier and **deletes its folder with the token** (`/var/lib/nuc-console-notify`, `%ProgramData%\nuc-console\notify`); revoke the token in @BotFather if you want it dead everywhere.
See also [CONFIGURATION.md](CONFIGURATION.md#telegram--alerts-on-your-phone-off-by-default) and [SECURITY.md](../SECURITY.md).
