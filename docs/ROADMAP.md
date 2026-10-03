# Roadmap

Where nuc-console is going, in the order it will be done. Each step is one or more pull requests, merged when its tests pass on Linux,
macOS and Windows; a step can change as it is built, and this page changes with it.

**The direction.** nuc-console started as a board for the monitor of a headless Linux box. From here the main targets are the
**desktop and the browser on Windows, macOS and Linux**: an app you install from a package, and an AI you turn on with one click,
with nothing else to install by hand. The Linux console (no X11) keeps working as it does today.

| Step | What you get | State |
|---|---|---|
| [1. The AI on Ollama](#1-the-ai-on-ollama) | the local AI works with one click on Windows, macOS and Linux, on the GPU when there is one | done |
| [2. A data API](#2-a-data-api) | everything the screens show, as JSON and as a live stream | done |
| [3. A new web front end](#3-a-new-web-front-end) | smooth, live pages and charts, in any browser | in progress: `/app` |
| [4. The desktop app and its packages](#4-the-desktop-app-and-its-packages) | `.msi`/`.exe`, `.dmg`, `.deb`/`.rpm`/AppImage, built on every release | in progress: the app and its packages |
| [Later: signing and stores](#later-signing-and-stores) | signed packages, Microsoft Store and the other catalogs | later |

## 1. The AI on Ollama

The model server changes from llamafile to [Ollama](https://github.com/ollama/ollama) (MIT). llamafile 0.10 does not use the GPU on
Windows and needs the Xcode Command Line Tools on Apple silicon; Ollama finds the GPU by itself on all three systems (NVIDIA, AMD and
Intel through CUDA or Vulkan, Apple silicon through Metal) and falls back to the CPU.

- **One click.** *Use this model* (or *AI on*) downloads the Ollama build of this system and processor (pinned version, SHA-256
  checked), starts it on `127.0.0.1` as a child of nuc-console with its models in nuc-console's AI folder, pulls the model from the
  Ollama library with a progress bar, loads it and turns the advisor on. No administrator rights, no installer, no service to set up.
  *AI off* stops it; *delete* removes the model or everything.
- **The same screens and commands.** The AI page, the AI screen and `nuc-console-ai` / `nuc-console-ask` keep their actions; the
  catalog keeps its advice on what fits this machine. Any other OpenAI-compatible server (LM Studio, `llama-server`...) still works
  through `[ai] endpoint`.
- **Tried for real in CI**: the `ai-pins` workflow installs the smallest model, serves it and asks it a question on Linux, Windows and
  macOS runners.
- **Still to come in this step**: use an Ollama that is already installed and running instead of a second copy; the ROCm add-on for AMD cards
  that Vulkan does not serve well.

## 2. A data API

The Python core (collector, history, AI engine) exposes what the screens show as JSON, and the changes as a live stream (Server-Sent
Events), on `127.0.0.1` with the same access rules as the web view (loopback or token; CSRF token and origin checks for actions). Nothing
visible changes: the console and the current pages keep working on top of the same data. This is what steps 3 and 4 are built on.

Done: `/api/v1/<view>` for the overview, CPU, Health, Map, AI and Telegram screens (their components as JSON, an `ETag` per document) and
`/api/v1/stream?view=<view>` (the document again each time it changes): [docs/WEB.md](WEB.md#the-data-api). The actions stay the AI and
Telegram pages' forms, whose CSRF token the documents carry.

## 3. A new web front end

The pages are rebuilt as a front end that updates in place from the stream of step 2: no reloads, live charts, transitions. The views move one
at a time, each when it can do everything the current one does; the current pages stay until then.

**Done: the live app, `/app`** ([docs/WEB.md](WEB.md#the-live-app)). The overview, CPU, Health, Map (as a tree) and AI screens are drawn in the
browser from the API's documents and morphed in place at each change; links, keys and the back button move inside the page, the AI screen's
buttons are posted in the background. It draws the same components into the same markup as the shell (a check in a real browser compares them
screen by screen), with the same style sheet, themes, densities and keys.

It is not Svelte, as first proposed: it is one first-party script (`src/appjs.py`), with **no framework, no dependency and no build**. That keeps
the project's rules (standard library only, nothing to build, nothing downloaded at release time) and its security model (a script pinned by its
hash, strict CSP with Trusted Types, static rules on what the script may do), and the release archives need no Node.

**Still to come in this step**: charts of the recent history (CPU, memory, network) that grow with the stream; the settings, the Telegram page,
the MAP as a graph and the layout editor inside the app; then `/app` becomes what `/` serves.

## 4. The desktop app and its packages

A desktop shell built with [Tauri 2](https://tauri.app): a native window, an icon in the tray or menu bar, start at login. It shows the
front end of step 3 and runs the Python core beside it (the release archives already carry their own Python).

- **Packages built by the release workflow on every tag**: Windows `.msi` and `.exe` (x64, arm64), macOS `.dmg` (Apple silicon,
  Intel), Linux `.deb`, `.rpm` and AppImage. Double-click, done.
- **Updates** from inside the app, from the GitHub releases.
- **Not signed yet**: at the first start Windows SmartScreen and macOS Gatekeeper warn about an unknown publisher; the install guide
  says how to open it anyway.

**Done: the app and its packages** ([docs/DESKTOP.md](DESKTOP.md)). `desktop/` starts the portable core it carries (the release
archive of its system and processor, Python included) with its data in the user's folder, shows the live app, and has the tray icon,
start at login and one instance per user. `.github/workflows/desktop.yml` builds the `.deb`, `.rpm` and AppImage (Linux x86-64), the
`.deb` and `.rpm` (Linux arm64), the `.dmg` (Apple silicon, Intel), the `.msi` and setup `.exe` (Windows x64) and the setup `.exe`
(Windows arm64), installs each on a runner of its own and starts it; `release.yml` attaches them to every
release.

**Still to come in this step**: the updates from inside the app (Tauri's updater checks a signature of its own: its key goes in the
repository's secrets, which the owner holds); the advice on screen naming the app's own commands instead of the portable ones.

## Later: signing and stores

- Code signing on Windows and Developer ID signing with notarization on macOS, so that the warnings above go away.
- The Microsoft Store (with the signed `.msi`/`.exe`), and the package catalogs: winget, Homebrew, Flathub.

## What stays

The collector and its least-privilege split, the problem inventory, the history, the `nuc-console-*` commands, the Linux console
(tty) and the current install scripts. The rules on privacy stay too: nothing is sent anywhere, and the AI analyses and never acts.
