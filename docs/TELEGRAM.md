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

**2. Pair it with this machine.** From the machine, as an administrator:

```bash
sudo nuc-console-telegram --setup          # Windows: nuc-console-telegram.cmd --setup  (administrator prompt)
```

It asks for the **token** and for **your Telegram @username** (the one shown in Telegram > Settings; if you have none, set one there first),
then prints a link: `https://t.me/<your_bot>?start=<code>`.

**3. Tap the link on your phone and press Start.** The machine notices it by itself, stores the chat and says *paired*. That is all: no numbers
to type, no chat id to look up. The notifier is on from now on (`--off` switches it off).

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
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The switch (`--on` / `--off` edit it). Nothing is sent while it is `no` or while the chat is not paired |
| `username` | empty | Your Telegram `@username` (5-32 letters, digits, `_`): the only person who gets the messages. `--setup` writes it |
| `detail` | `titles` | `titles`: only the **title** of the problem and the host name leave the machine. `full`: also its text (container names, ports, process names) |
| `resolved` | `yes` | Also send a message when a problem is gone |

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
| reading messages | the **service** never asks Telegram for anything you wrote and has **no commands**: you cannot control the machine from the chat. Only `--setup`, a command you run yourself, asks Telegram once for the single `/start` message that carries your one-time code, then stops |
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
   - **`telegram-unpaired`**, *Telegram notifications on, but not paired*: `enabled = yes` but no chat yet (or after `--forget`). Run `--setup` and press Start on the link, in the chat with **your** bot (not with @BotFather).
   - **`telegram-failing`**, *Telegram notifier not running* (no sign of life for 5 minutes) or *Telegram notifications failing for N min: reason* (sends failing for 10 minutes: no Internet, wrong or revoked token, you blocked the bot).
3. `sudo nuc-console-telegram --test` sends one message and prints Telegram's answer. `Unauthorized` = the token is wrong or revoked: `--setup` again with the new one. A message that never arrives although the answer is `ok` means you pressed Start on another bot, or muted the chat.
4. Logs: Linux `journalctl -u nuc-console-notify` · macOS `/var/log/nuc-console/notify.log` · Windows `%ProgramData%\nuc-console\logs\notify.log`.
5. Not running: Linux `systemctl status nuc-console-notify` · macOS `sudo launchctl print system/com.nuc-console.notify` · Windows `Get-ScheduledTask -TaskPath \nuc-console\ -TaskName notify`. The service always exists (the installers register it) and idles until the notifier is on and paired; it is started and stopped by `--setup`, `--on` and `--off`.

On Windows an ordinary (not administrator) `nuc-console-problems` cannot read the notifier's status and leaves the two Telegram problems out; the web view and the administrator prompt show them.

Uninstalling removes the notifier and **deletes its folder with the token** (`/var/lib/nuc-console-notify`, `%ProgramData%\nuc-console\notify`); revoke the token in @BotFather if you want it dead everywhere.
See also [CONFIGURATION.md](CONFIGURATION.md#telegram--alerts-on-your-phone-off-by-default) and [SECURITY.md](../SECURITY.md).
