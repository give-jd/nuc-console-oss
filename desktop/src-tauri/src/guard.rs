//! What the app checks before it trusts something it read from the data folder: a pid before it signals it, a token before it uses it.
//! Standard library only (`rustc --test src/guard.rs` runs its tests without the Tauri build).
use std::path::Path;
#[cfg(any(target_os = "macos", windows))]
use std::process::Command;

/// A pid the app may signal. Not 0 (`kill(0)` is the caller's own process group), not 1, and one a signed `pid_t` holds (`kill(-pid)` is a
/// group: a pid that wraps to a negative number would signal one).
pub fn pid_ok(pid: u32) -> bool {
    pid > 1 && i32::try_from(pid).is_ok()
}

/// Does the command line of a process name `script` as one of its words (not as part of another path)?
pub fn cmdline_has_script(cmdline: &str, script: &Path) -> bool {
    let (line, want) = if cfg!(windows) {
        (cmdline.to_lowercase(), script.to_string_lossy().to_lowercase())
    } else {
        (cmdline.to_string(), script.to_string_lossy().into_owned())
    };
    if want.is_empty() {
        return false;
    }
    let edge = |c: Option<char>| c.map_or(true, |c| c.is_whitespace() || c == '"' || c == '\'');
    line.match_indices(&want).any(|(i, m)| edge(line[..i].chars().last()) && edge(line[i + m.len()..].chars().next()))
}

/// The command line of a live process, or None (gone, not readable, or no way to ask on this system).
pub fn process_cmdline(pid: u32) -> Option<String> {
    if pid_ok(pid) {
        cmdline_of(pid)
    } else {
        None
    }
}

#[cfg(target_os = "linux")]
fn cmdline_of(pid: u32) -> Option<String> {
    let raw = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    let words: Vec<String> = raw.split(|b| *b == 0).filter(|w| !w.is_empty()).map(|w| String::from_utf8_lossy(w).into_owned()).collect();
    if words.is_empty() {
        None
    } else {
        Some(words.join(" "))
    } // a zombie has none
}

#[cfg(target_os = "macos")]
fn cmdline_of(pid: u32) -> Option<String> {
    let out = Command::new("/bin/ps").args(["-p", &pid.to_string(), "-o", "command="]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if out.status.success() && !text.is_empty() {
        Some(text)
    } else {
        None
    }
}

#[cfg(windows)]
fn cmdline_of(pid: u32) -> Option<String> {
    use std::os::windows::process::CommandExt;
    let root = std::env::var_os("SystemRoot").map(std::path::PathBuf::from).unwrap_or_else(|| std::path::PathBuf::from(r"C:\Windows"));
    let out = Command::new(root.join(r"System32\WindowsPowerShell\v1.0\powershell.exe")) // never one found on the PATH
        .args(["-NoProfile", "-NonInteractive", "-Command"])
        .arg(format!("(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"))
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if out.status.success() && !text.is_empty() {
        Some(text)
    } else {
        None
    }
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn cmdline_of(_pid: u32) -> Option<String> {
    None // no way to ask on this system: never "yes"
}

/// Is `pid` the core, that is the process of `script` (core/run.sh, core\run.ps1)? A pid the OS gave to something else since, or that
/// cannot be looked at, is not.
pub fn is_core(pid: u32, script: &Path) -> bool {
    process_cmdline(pid).map_or(false, |line| cmdline_has_script(&line, script))
}

/// What the web view accepts as its token (src/webhttp.py TOKEN_OK): 16 or more of A-Z a-z 0-9 . _ ~ -
pub fn token_ok(s: &str) -> bool {
    s.len() >= 16 && s.len() <= 512 && s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'~' | b'-'))
}

/// The access token of the web view the core made in `data` (web.token, 0600, this account's). None if it is not there or not a token.
pub fn read_token(data: &Path) -> Option<String> {
    let text = std::fs::read_to_string(data.join("web.token")).ok()?;
    let token = text.trim();
    if token_ok(token) {
        Some(token.to_string())
    } else {
        None
    }
}

/// open-app.html in `data`, readable by this account only: a page that sends the browser on to `url` (the core's /app with the token).
/// The browser is given this file to open, not the address: a command line can be listed by every user of the machine.
pub fn write_open_page(data: &Path, url: &str) -> Option<std::path::PathBuf> {
    use std::io::Write;
    let path = data.join("open-app.html");
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut file = opts.open(&path).ok()?;
    write!(file, "<!doctype html><meta charset=\"utf-8\"><meta name=\"referrer\" content=\"no-referrer\"><meta http-equiv=\"refresh\" content=\"0;url={url}\"><title>nuc-console</title>\n").ok()?;
    Some(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    #[cfg(unix)]
    use std::process::Command;

    #[test]
    fn a_pid_that_is_a_group_or_init_or_nothing_is_never_signalled() {
        for bad in [0u32, 1, 2_147_483_648, u32::MAX] {
            assert!(!pid_ok(bad), "{bad}");
        }
        assert!(pid_ok(2) && pid_ok(4242) && pid_ok(i32::MAX as u32));
    }

    #[test]
    fn the_script_must_be_a_word_of_the_command_line() {
        let s = PathBuf::from("/opt/nuc console/core/run.sh");
        assert!(cmdline_has_script("/bin/sh /opt/nuc console/core/run.sh --web --no-open", &s));
        assert!(cmdline_has_script("/bin/sh /opt/nuc console/core/run.sh", &s));
        assert!(!cmdline_has_script("/bin/sh /opt/nuc console/core/run.sh.evil --web", &s));
        assert!(!cmdline_has_script("/bin/sh /home/x/opt/nuc console/core/run.sh", &s));
        assert!(!cmdline_has_script("vim", &s));
        assert!(!cmdline_has_script("anything", &PathBuf::new()));
    }

    #[cfg(unix)]
    #[test]
    fn a_live_process_is_the_core_only_when_it_runs_the_script() {
        let script = "/tmp/nuc guard test/core/run.sh";
        let mut child = Command::new("/bin/sh").args(["-c", "sleep 20; :", script]).spawn().unwrap();
        let mut ok = false;
        for _ in 0..100 {
            // (until the child has exec'd its shell, its command line is still this test's)
            ok = is_core(child.id(), Path::new(script));
            if ok {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let other = is_core(child.id(), Path::new("/tmp/elsewhere/run.sh"));
        let me = is_core(std::process::id(), Path::new(script));
        let _ = child.kill();
        let _ = child.wait();
        assert!(ok, "the process of the script is the core");
        assert!(!other, "another script is not");
        assert!(!me, "this process is not");
        assert!(!is_core(child.id(), Path::new(script)), "a process that ended is not");
        assert!(!is_core(0, Path::new(script)) && !is_core(1, Path::new(script)));
    }

    #[test]
    fn a_token_is_what_the_web_view_accepts() {
        assert!(token_ok("abcdefghijklmnop") && token_ok("A-b_c.d~e0123456789xyz"));
        for bad in
            ["", "short", "abcdefghijklmno", "abcdefghijklmnop&x=1", "abcdefghijklmnop\n", "abcdefghijklmn p", "caf\u{e9}abcdefghijklmnop"]
        {
            assert!(!token_ok(bad), "{bad:?}");
        }
        let dir = std::env::temp_dir().join(format!("nuc-guard-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(read_token(&dir), None);
        std::fs::write(dir.join("web.token"), "abcdefghijklmnopqrstuvwx\n").unwrap();
        assert_eq!(read_token(&dir).as_deref(), Some("abcdefghijklmnopqrstuvwx"));
        std::fs::write(dir.join("web.token"), "x").unwrap();
        assert_eq!(read_token(&dir), None);
        let page = write_open_page(&dir, "http://127.0.0.1:5000/app?token=abcdefghijklmnop").unwrap();
        assert!(std::fs::read_to_string(&page).unwrap().contains("url=http://127.0.0.1:5000/app?token=abcdefghijklmnop\""));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&page).unwrap().permissions().mode() & 0o777, 0o600);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
