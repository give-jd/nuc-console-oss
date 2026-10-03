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
| [3. A new web front end](#3-a-new-web-front-end) | smooth, live pages and charts, in any browser | planned |
| [4. The desktop app and its packages](#4-the-desktop-app-and-its-packages) | `.msi`/`.exe`, `.dmg`, `.deb`/`.rpm`/AppImage, built on every release | planned |
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

The pages are rebuilt as a front end that updates in place from the stream of step 2: no reloads, live charts, transitions. Proposed:
Svelte with TypeScript, and uPlot for the charts. The views move one at a time (AI, Overview, CPU, Health, Map), each when it can do
everything the current one does; the current pages stay until then.

- **Built only in CI.** Node runs in the release workflow and the archives carry the built files: nobody builds anything to install
  or run nuc-console.
- The Python core stays standard library only. `CONTRIBUTING.md` gets the rules of the front end (dependencies pinned with a lock file,
  the same Content Security Policy and no external script).

## 4. The desktop app and its packages

A desktop shell built with [Tauri 2](https://tauri.app): a native window, an icon in the tray or menu bar, start at login. It shows the
front end of step 3 and runs the Python core beside it (the release archives already carry their own Python).

- **Packages built by the release workflow on every tag**: Windows `.msi` and `.exe` (x64, arm64), macOS `.dmg` (Apple silicon,
  Intel), Linux `.deb`, `.rpm` and AppImage. Double-click, done.
- **Updates** from inside the app, from the GitHub releases.
- **Not signed yet**: at the first start Windows SmartScreen and macOS Gatekeeper warn about an unknown publisher; the install guide
  says how to open it anyway.

## Later: signing and stores

- Code signing on Windows and Developer ID signing with notarization on macOS, so that the warnings above go away.
- The Microsoft Store (with the signed `.msi`/`.exe`), and the package catalogs: winget, Homebrew, Flathub.

## What stays

The collector and its least-privilege split, the problem inventory, the history, the `nuc-console-*` commands, the Linux console
(tty) and the current install scripts. The rules on privacy stay too: nothing is sent anywhere, and the AI analyses and never acts.
