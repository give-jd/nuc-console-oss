//! The desktop app of nuc-console (docs/DESKTOP.md): a native window around the portable core it carries in core/.
//!
//! It shows a start page, starts core/run.sh --web --no-open (core\run.ps1 -NoOpen on Windows) with NUC_CONSOLE_DATA in the
//! user's own data folder, reads the address the core prints and loads its live app (/app) in the window. An icon in the tray
//! (the menu bar on macOS) opens the window, opens the dashboard in the browser, turns "start at login" on and off, and quits.
//! Closing the window hides it: the core keeps collecting. Quitting stops the core and all it started. A link to anything but
//! the core opens in the browser, never in the window. One instance per user: starting it again shows the window, and
//! `nuc-console --quit` stops the one that runs. `--hidden` (start at login) starts without the window.
//!
//! `--smoke-test` is for CI: no window, it starts the core, asks it for /app and /api/v1/summary, stops it, and exits 0 when both
//! answered as they should (1 otherwise).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_opener::OpenerExt;

const WINDOW: &str = "main";
const START_PAGE: &str = "index.html";
const START_TIMEOUT: Duration = Duration::from_secs(150); // the first run on Windows unpacks nothing (the package did), but a slow disk
const STOP_TIMEOUT: Duration = Duration::from_secs(10);
const KEEP_LINES: usize = 40; // of the core's output, for the start page when it fails
const LOG_MAX: u64 = 1 << 20; // logs/desktop.log over 1 MB is kept once as .1, like the core's logs
const ADDRESS: &str = "dashboard on http://127.0.0.1:"; // what run.sh and run.ps1 print once the web view listens
const WEB_LOG_ADDRESS: &str = "nuc-console web view on http://127.0.0.1:"; // what web.py writes to logs/web.log

#[derive(Clone, Copy, Default)]
struct Opts {
    hidden: bool,
    quit: bool,
    smoke: bool,
}

fn parse_args<S: AsRef<str>>(argv: &[S]) -> Opts {
    let mut o = Opts::default();
    for a in argv.iter().skip(1) {
        match a.as_ref() {
            "--hidden" => o.hidden = true,
            "--quit" => o.quit = true,
            "--smoke-test" => o.smoke = true,
            _ => {}
        }
    }
    o
}

/// What the app knows about the core.
#[derive(Default)]
struct Core {
    child: Option<Child>, // the core this app started
    adopted: Option<u32>, // or the one a run of this app that ended badly left running: stopped on quit all the same
    base: Option<String>, // http://127.0.0.1:PORT, once the core said it
    status: String,       // what the start page says
    failed: bool,
    lines: Vec<String>, // the core's last lines of output
    on_start_page: bool,
    quitting: bool,
    log: Option<File>,
}

type Shared = Arc<Mutex<Core>>;

fn shared(app: &AppHandle) -> Shared {
    app.state::<Shared>().inner().clone()
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let opts = parse_args(&argv[..]);
    #[cfg(target_os = "linux")]
    {
        if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
            // WebKitGTK draws a blank window with some GPU drivers (NVIDIA, some Wayland setups) unless this is set
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        }
    }
    let core: Shared =
        Arc::new(Mutex::new(Core { status: "Starting the dashboard\u{2026}".into(), on_start_page: true, ..Core::default() }));
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if parse_args(&argv[..]).quit {
                app.exit(0);
            } else {
                show(app);
            }
        }))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .plugin(tauri_plugin_opener::init())
        .manage(core)
        .setup(move |app| {
            if opts.quit {
                std::process::exit(0); // no instance was running: nothing to stop
            }
            setup(app.handle(), opts)?;
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == WINDOW {
                    api.prevent_close(); // the core keeps collecting: the tray icon (or starting the app again) shows it
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("nuc-console: the app could not be built");
    app.run(|app, event| match event {
        RunEvent::Exit => stop(app),
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show(app),
        _ => {}
    });
}

fn setup(app: &AppHandle, opts: Opts) -> Result<(), Box<dyn std::error::Error>> {
    let core = plain(app.path().resource_dir()?.join("core"));
    let data = plain(app.path().app_local_data_dir()?);
    std::fs::create_dir_all(&data)?;
    if !opts.smoke {
        window(app, !opts.hidden)?;
        if let Err(e) = tray(app) {
            note(app, &format!("nuc-console: no tray icon ({e}): start the app again to show the window, nuc-console --quit to stop it"));
        }
    }
    let app = app.clone();
    std::thread::spawn(move || run_core(app, core, data, opts.smoke));
    Ok(())
}

/// A path without the \\?\ prefix Windows may give the folders of the app (PowerShell 5.1 does not take it for -File).
fn plain(p: PathBuf) -> PathBuf {
    let s = p.to_string_lossy();
    match s.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC\\") => PathBuf::from(rest),
        _ => p,
    }
}

// ---- the window ----------------------------------------------------------------------------------------------------------

fn window(app: &AppHandle, visible: bool) -> tauri::Result<WebviewWindow> {
    let nav = app.clone();
    let loads = app.clone();
    WebviewWindowBuilder::new(app, WINDOW, WebviewUrl::App(START_PAGE.into()))
        .title("nuc-console")
        .inner_size(1280.0, 840.0)
        .min_inner_size(640.0, 420.0)
        .visible(visible)
        .on_navigation(move |url| allowed(&nav, url))
        .on_page_load(move |webview, payload| {
            if matches!(payload.event(), PageLoadEvent::Finished) && is_app_page(payload.url()) {
                let (text, failed, lines) = {
                    let c = shared(&loads);
                    let c = c.lock().unwrap();
                    (c.status.clone(), c.failed, c.lines.join("\n"))
                };
                paint(&webview, &text, failed, if failed { &lines } else { "" });
            }
        })
        .build()
}

/// The start page and its style sheet: the app's own assets (tauri://localhost, http://tauri.localhost on Windows).
fn is_app_page(url: &Url) -> bool {
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        "about" => url.as_str() == "about:blank",
        _ => false,
    }
}

fn same_origin(url: &Url, base: &str) -> bool {
    Url::parse(base).map(|b| b.origin() == url.origin()).unwrap_or(false)
}

/// May the window go to `url`? The app's own pages and the core's; a web link opens in the browser instead, anything else nowhere.
fn allowed(app: &AppHandle, url: &Url) -> bool {
    if is_app_page(url) {
        return true;
    }
    let base = shared(app).lock().unwrap().base.clone();
    if let Some(base) = base {
        if same_origin(url, &base) {
            return true;
        }
    }
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
    }
    false
}

fn start_page_url() -> Url {
    let s = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    Url::parse(s).expect("a fixed URL")
}

/// A JavaScript string literal of `s`: nothing in it can end the script or the string.
fn js_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c if (c as u32) < 0x20 || matches!(c, '<' | '>' | '&' | '\u{2028}' | '\u{2029}') => {
                out.push_str(&format!("\\u{:04x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// What the start page shows: the status line and, when the core failed, its last lines (text only: no markup is built).
fn paint(w: &WebviewWindow, status: &str, failed: bool, lines: &str) {
    let js = format!(
        "(function(){{var s=document.getElementById(\"status\"),l=document.getElementById(\"log\");\
         if(s){{s.textContent={};}}if(l){{l.textContent={};}}document.documentElement.classList.toggle(\"failed\",{});}})();",
        js_string(status),
        js_string(lines),
        failed
    );
    let _ = w.eval(&js);
}

fn show(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(WINDOW) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// The status line of the start page (and of the log).
fn say(app: &AppHandle, text: &str, failed: bool) {
    let core = shared(app);
    let (on_start, lines) = {
        let mut c = core.lock().unwrap();
        c.status = text.to_string();
        c.failed = failed;
        (c.on_start_page, c.lines.join("\n"))
    };
    note(app, text);
    if let Some(w) = app.get_webview_window(WINDOW) {
        if failed && !on_start {
            core.lock().unwrap().on_start_page = true;
            let _ = w.navigate(start_page_url()); // its load paints the status
        } else if on_start {
            paint(&w, text, failed, if failed { &lines } else { "" });
        }
        if failed {
            show(app);
        }
    }
}

// ---- the tray icon -------------------------------------------------------------------------------------------------------

fn tray(app: &AppHandle) -> tauri::Result<()> {
    let at_login = app.autolaunch().is_enabled().unwrap_or(false);
    let open = MenuItem::with_id(app, "open", "Open nuc-console", true, None::<&str>)?;
    let browser = MenuItem::with_id(app, "browser", "Open in the browser", true, None::<&str>)?;
    let login = CheckMenuItem::with_id(app, "login", "Start at login", true, at_login, None::<&str>)?;
    let line = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit nuc-console", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &browser, &login, &line, &quit])?;
    let item = login.clone();
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("nuc-console")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "open" => show(app),
            "browser" => open_in_browser(app),
            "login" => start_at_login(app, &item),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn open_in_browser(app: &AppHandle) {
    let base = shared(app).lock().unwrap().base.clone();
    if let Some(base) = base {
        let _ = app.opener().open_url(format!("{base}/app"), None::<&str>);
    }
}

fn start_at_login(app: &AppHandle, item: &CheckMenuItem<Wry>) {
    let auto = app.autolaunch();
    let was = auto.is_enabled().unwrap_or(false);
    let done = if was { auto.disable() } else { auto.enable() };
    if let Err(e) = done {
        note(app, &format!("nuc-console: start at login could not be changed: {e}"));
    }
    let _ = item.set_checked(auto.is_enabled().unwrap_or(was)); // what is true now, whatever the click did to the tick
}

// ---- the core ------------------------------------------------------------------------------------------------------------

/// The address in a line of run.sh / run.ps1 ("nuc-console: dashboard on http://127.0.0.1:PORT/?fit=1 ..."): http://127.0.0.1:PORT.
fn address(line: &str, marker: &str) -> Option<String> {
    let rest = &line[line.find(marker)? + marker.len()..];
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    match digits.parse::<u16>() {
        Ok(port) if port > 0 && digits.len() <= 5 => Some(format!("http://127.0.0.1:{port}")),
        _ => None,
    }
}

#[cfg(unix)]
fn launcher(core: &Path) -> Command {
    use std::os::unix::process::CommandExt;
    let mut cmd = Command::new("/bin/sh");
    cmd.arg(core.join("run.sh")).args(["--web", "--no-open"]);
    cmd.process_group(0); // a group of its own: the app stops it, not a signal meant for whoever started the app
    cmd
}

#[cfg(windows)]
fn launcher(core: &Path) -> Command {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let root = std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let mut cmd = Command::new(root.join(r"System32\WindowsPowerShell\v1.0\powershell.exe")); // never one found on the PATH
    cmd.args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]).arg(core.join("run.ps1")).arg("-NoOpen");
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

fn spawn(core: &Path, data: &Path) -> std::io::Result<Child> {
    let mut cmd = launcher(core);
    cmd.env("NUC_CONSOLE_DATA", data)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONSTARTUP")
        .current_dir(data)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if std::env::var_os("APPIMAGE").is_some() {
        // the AppImage's own libraries are for the app, not for the commands the collector runs
        cmd.env_remove("LD_LIBRARY_PATH").env_remove("LD_PRELOAD");
    }
    cmd.spawn()
}

/// One that an earlier run left running (the app ended without stopping it): its pid and its address, when it still answers.
fn running_core(data: &Path) -> Option<(u32, String)> {
    let pid: u32 = std::fs::read_to_string(data.join("portable.pid")).ok()?.trim().parse().ok()?;
    let log = std::fs::read_to_string(data.join("logs").join("web.log")).ok()?;
    let base = log.lines().filter_map(|l| address(l, WEB_LOG_ADDRESS)).last()?;
    let addr: SocketAddr = base.trim_start_matches("http://").parse().ok()?;
    TcpStream::connect_timeout(&addr, Duration::from_secs(2)).ok()?;
    Some((pid, base))
}

fn run_core(app: AppHandle, core: PathBuf, data: PathBuf, smoke: bool) {
    let state = shared(&app);
    open_log(&state, &data);
    let script = core.join(if cfg!(windows) { "run.ps1" } else { "run.sh" });
    if !script.is_file() {
        let msg =
            format!("The app has no core: {} is missing (a build from a checkout needs tools/desktop_core.py first).", script.display());
        return give_up(&app, &msg, smoke);
    }
    let mut child = match spawn(&core, &data) {
        Ok(c) => c,
        Err(e) => return give_up(&app, &format!("The core could not be started: {e}"), smoke),
    };
    let (tx, rx) = channel::<String>();
    let streams: Vec<Box<dyn Read + Send>> = vec![
        Box::new(child.stdout.take().expect("piped")) as Box<dyn Read + Send>,
        Box::new(child.stderr.take().expect("piped")) as Box<dyn Read + Send>,
    ];
    for stream in streams {
        let tx = tx.clone();
        std::thread::spawn(move || drain(stream, tx));
    }
    drop(tx);
    state.lock().unwrap().child = Some(child);

    // the address, or the end of the output (the core stopped), or the time limit
    let deadline = Instant::now() + START_TIMEOUT;
    let mut base = None;
    let mut ended = false;
    while base.is_none() {
        match rx.recv_timeout(Duration::from_millis(250)) {
            Ok(line) => {
                base = address(&line, ADDRESS);
                remember(&state, line);
            }
            Err(RecvTimeoutError::Timeout) if Instant::now() < deadline => {}
            Err(RecvTimeoutError::Timeout) => break,
            Err(RecvTimeoutError::Disconnected) => {
                ended = true;
                break;
            }
        }
    }
    if base.is_none() && ended {
        let busy = state.lock().unwrap().lines.iter().any(|l| l.contains("already running"));
        if busy {
            if let Some((pid, found)) = running_core(&data) {
                let mut c = state.lock().unwrap();
                c.child = None;
                c.adopted = Some(pid);
                drop(c);
                remember(&state, format!("nuc-console: the core an earlier run left (pid {pid}) answers on {found}: it is used"));
                base = Some(found);
            }
        }
    }
    let base = match base {
        Some(b) => b,
        None => {
            let why = if ended { "The core stopped before the dashboard was up." } else { "The dashboard did not come up in time." };
            let msg = format!("{why} Its logs are in {}.", data.join("logs").display());
            stop(&app);
            return give_up(&app, &msg, smoke);
        }
    };
    state.lock().unwrap().base = Some(base.clone());
    if smoke {
        return smoke_test(&app, &base);
    }
    if let Some(w) = app.get_webview_window(WINDOW) {
        if let Ok(url) = Url::parse(&format!("{base}/app")) {
            state.lock().unwrap().on_start_page = false;
            let _ = w.navigate(url);
        }
    }

    // from now on: keep the core's output (its pipes must not fill up), and say so if it stops while the app runs
    loop {
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(line) => remember(&state, line),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if exited(&state) {
            break;
        }
    }
    while let Ok(line) = rx.recv_timeout(Duration::from_millis(200)) {
        remember(&state, line);
    }
    let quitting = state.lock().unwrap().quitting;
    if !quitting && exited(&state) {
        let msg = format!("The core stopped. Its logs are in {}; start the app again to restart it.", data.join("logs").display());
        say(&app, &msg, true);
    }
}

/// Read a stream of the core to its end, line by line, whatever its encoding (PowerShell writes the console's code page): the
/// lines go to `tx` while someone listens, and the stream is read to the end either way, so that the core never waits on a full pipe.
fn drain(stream: Box<dyn Read + Send>, tx: std::sync::mpsc::Sender<String>) {
    let mut reader = BufReader::new(stream);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        match reader.read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                let line = String::from_utf8_lossy(&buf).trim_end_matches(|c| c == '\r' || c == '\n').to_string();
                let _ = tx.send(line);
            }
        }
    }
}

/// Has the core this app started ended?
fn exited(state: &Shared) -> bool {
    let mut c = state.lock().unwrap();
    match c.child.as_mut() {
        Some(child) => !matches!(child.try_wait(), Ok(None)),
        None => c.adopted.is_none(),
    }
}

fn give_up(app: &AppHandle, msg: &str, smoke: bool) {
    if smoke {
        let lines = shared(app).lock().unwrap().lines.join("\n");
        eprintln!("{lines}\nsmoke test: FAILED: {msg}");
        app.exit(1);
    } else {
        say(app, msg, true);
    }
}

fn remember(state: &Shared, line: String) {
    let mut c = state.lock().unwrap();
    if let Some(f) = c.log.as_mut() {
        let _ = writeln!(f, "{line}");
    }
    c.lines.push(line);
    if c.lines.len() > KEEP_LINES {
        let extra = c.lines.len() - KEEP_LINES;
        c.lines.drain(..extra);
    }
}

fn note(app: &AppHandle, text: &str) {
    remember(&shared(app), text.to_string());
}

/// logs/desktop.log in the data folder: what the core printed, and what the app said.
fn open_log(state: &Shared, data: &Path) {
    let logs = data.join("logs");
    if std::fs::create_dir_all(&logs).is_err() {
        return;
    }
    let path = logs.join("desktop.log");
    if std::fs::metadata(&path).map(|m| m.len() > LOG_MAX).unwrap_or(false) {
        let _ = std::fs::rename(&path, logs.join("desktop.log.1"));
    }
    state.lock().unwrap().log = OpenOptions::new().create(true).append(true).open(path).ok();
}

/// Stop the core: run.sh on SIGTERM (or run.ps1's process tree) stops all it started; a core that does not end in time is killed.
fn stop(app: &AppHandle) {
    let state = shared(app);
    let (child, adopted) = {
        let mut c = state.lock().unwrap();
        c.quitting = true;
        (c.child.take(), c.adopted.take())
    };
    if let Some(mut child) = child {
        terminate(child.id());
        let deadline = Instant::now() + STOP_TIMEOUT;
        while matches!(child.try_wait(), Ok(None)) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(100));
        }
        if matches!(child.try_wait(), Ok(None)) {
            kill(child.id());
            let _ = child.wait();
        }
    }
    if let Some(pid) = adopted {
        terminate(pid);
    }
}

#[cfg(unix)]
fn terminate(pid: u32) {
    if let Ok(pid) = i32::try_from(pid) {
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
    }
}

#[cfg(unix)]
fn kill(pid: u32) {
    if let Ok(pid) = i32::try_from(pid) {
        unsafe {
            libc::kill(-pid, libc::SIGKILL); // its group: run.sh and all it started (spawn gave it a group of its own)
        }
    }
}

#[cfg(windows)]
fn terminate(pid: u32) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let root = std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let _ = Command::new(root.join(r"System32\taskkill.exe"))
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(windows)]
fn kill(pid: u32) {
    terminate(pid);
}

// ---- --smoke-test --------------------------------------------------------------------------------------------------------

fn get(base: &str, path: &str) -> std::io::Result<(u16, String)> {
    let host = base.trim_start_matches("http://");
    let addr: SocketAddr = host.parse().map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "not an address"))?;
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_secs(10))?;
    s.set_read_timeout(Some(Duration::from_secs(30)))?;
    write!(s, "GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n")?;
    let mut body = Vec::new();
    s.read_to_end(&mut body)?;
    let text = String::from_utf8_lossy(&body).into_owned();
    let status = text.split_whitespace().nth(1).and_then(|c| c.parse().ok()).unwrap_or(0);
    Ok((status, text))
}

fn smoke_test(app: &AppHandle, base: &str) {
    let checks: [(&str, &str); 2] = [("/app", "<main id=\"app\""), ("/api/v1/summary", "\"api\":1")];
    let mut failed = Vec::new();
    for (path, want) in checks {
        match get(base, path) {
            Ok((200, text)) if text.contains(want) => println!("smoke test: {base}{path}: 200, {want}"),
            Ok((status, _)) => failed.push(format!("{path}: status {status}, or no {want}")),
            Err(e) => failed.push(format!("{path}: {e}")),
        }
    }
    stop(app);
    if failed.is_empty() {
        println!("smoke test: ok");
        app.exit(0);
    } else {
        give_up(app, &failed.join("; "), true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_address_is_read_from_both_launchers_and_the_web_log() {
        let sh = "nuc-console: dashboard on http://127.0.0.1:43117/?fit=1 (Ctrl+C to stop)";
        let ps = "nuc-console: dashboard on http://127.0.0.1:8787/?fit=1 (close this window or press Ctrl+C to stop)";
        assert_eq!(address(sh, ADDRESS).as_deref(), Some("http://127.0.0.1:43117"));
        assert_eq!(address(ps, ADDRESS).as_deref(), Some("http://127.0.0.1:8787"));
        assert_eq!(
            address("nuc-console web view on http://127.0.0.1:5000 (local)", WEB_LOG_ADDRESS).as_deref(),
            Some("http://127.0.0.1:5000")
        );
        for bad in [
            "dashboard on http://127.0.0.1:/",
            "dashboard on http://127.0.0.1:0/",
            "dashboard on http://127.0.0.1:99999/",
            "dashboard on http://127.0.0.1:1234567/",
            "dashboard on http://192.0.2.1:80/",
            "nothing here",
        ] {
            assert_eq!(address(bad, ADDRESS), None, "{bad}");
        }
    }

    #[test]
    fn a_js_string_cannot_end_the_script_or_the_string() {
        assert_eq!(js_string("plain"), "\"plain\"");
        assert_eq!(js_string("a\"b\\c\nd"), "\"a\\\"b\\\\c\\nd\"");
        assert_eq!(js_string("</script><!--&"), "\"\\u003c/script\\u003e\\u003c!--\\u0026\"");
        assert_eq!(js_string("\u{2028}\u{2029}\t\u{1b}"), "\"\\u2028\\u2029\\u0009\\u001b\"");
        assert_eq!(js_string("caf\u{e9} \u{2026}"), "\"caf\u{e9} \u{2026}\"");
    }

    #[test]
    fn the_window_stays_on_the_app_and_the_core() {
        let u = |s: &str| Url::parse(s).unwrap();
        assert!(is_app_page(&u("tauri://localhost/index.html")));
        assert!(is_app_page(&u("http://tauri.localhost/index.html")));
        assert!(!is_app_page(&u("tauri://example.com/")));
        assert!(!is_app_page(&u("http://tauri.localhost.example.com/")));
        assert!(!is_app_page(&u("https://example.com/")));
        assert!(!is_app_page(&u("about:srcdoc")));
        assert!(same_origin(&u("http://127.0.0.1:8787/app?view=cpu"), "http://127.0.0.1:8787"));
        assert!(!same_origin(&u("http://127.0.0.1:8788/app"), "http://127.0.0.1:8787"));
        assert!(!same_origin(&u("http://localhost:8787/app"), "http://127.0.0.1:8787"));
        assert!(!same_origin(&u("https://127.0.0.1:8787/app"), "http://127.0.0.1:8787"));
    }

    #[test]
    fn options() {
        let o = parse_args(&["nuc-console", "--hidden"]);
        assert!(o.hidden && !o.quit && !o.smoke);
        let o = parse_args(&["nuc-console", "--quit", "--smoke-test"]);
        assert!(!o.hidden && o.quit && o.smoke);
        let o = parse_args(&["--hidden"]); // the program's own name is not an option
        assert!(!o.hidden);
    }

    #[test]
    fn plain_paths() {
        assert_eq!(plain(PathBuf::from(r"\\?\C:\Program Files\nuc-console\core")), PathBuf::from(r"C:\Program Files\nuc-console\core"));
        assert_eq!(plain(PathBuf::from(r"\\?\UNC\server\share")), PathBuf::from(r"\\?\UNC\server\share"));
        assert_eq!(plain(PathBuf::from("/usr/lib/nuc-console/core")), PathBuf::from("/usr/lib/nuc-console/core"));
    }
}
