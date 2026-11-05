"""velcro tray icon for Windows (ctypes only): watches for recorders in the background and offers
Import now / Open log / Quit from the notification-area icon. Untested outside Windows."""
import ctypes, os, subprocess, sys, threading
from ctypes import wintypes
import velcro_win as v

u, k, sh = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.shell32
WM_USER, WM_TRAY, WM_COMMAND, WM_DESTROY, WM_RBUTTONUP = 0x400, 0x401, 0x111, 0x2, 0x205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
ID_IMPORT, ID_LOG, ID_SETTINGS, ID_QUIT, ID_RECORD, ID_RELOAD, ID_ADD = 1, 2, 3, 4, 5, 6, 100  # ID_ADD+i: "Use drive i as recorder"
WM_HOTKEY, HOTKEY_ID = 0x312, 1
mm = ctypes.windll.winmm
LRESULT = ctypes.c_ssize_t


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.DefWindowProcW.restype = LRESULT
LOG = os.path.join(v.state_dir(), "velcro.log")


def log(msg):
    os.makedirs(v.state_dir(), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{v.now_iso()} {msg}\n")


class Recording:
    """Hotkey recording through the Windows multimedia API (no extra install): microphone -> WAV."""
    def __init__(self):
        self.path, self.since = None, 0

    def _mci(self, cmd):
        buf = ctypes.create_unicode_buffer(256)
        err = mm.mciSendStringW(cmd, buf, 255, None)
        return err, buf.value

    @property
    def active(self):
        return self.path is not None

    def start(self):
        d = os.path.join(v.state_dir(), "hotkey")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, v.datetime.datetime.now().strftime("%Y%m%d%H%M%S") + ".wav")
        # The default format (11 kHz, 8-bit) is refused by many devices (error 322), so try common 16-bit mono rates.
        started = False
        for rate in (16000, 44100, 48000, 22050, 11025):
            if self._mci("open new type waveaudio alias velcrorec")[0]:
                break
            if not any(self._mci(c)[0] for c in ("set velcrorec bitspersample 16", "set velcrorec channels 1", f"set velcrorec samplespersec {rate}")) \
                    and not self._mci("record velcrorec")[0]:
                started = True
                break
            self._mci("close velcrorec")
        if not started:
            self.path = None
            return False
        self.since = __import__("time").time()
        return True

    def stop(self):
        path, secs = self.path, __import__("time").time() - self.since
        self._mci("stop velcrorec")
        err, _ = self._mci(f'save velcrorec "{path}"')
        self._mci("close velcrorec")
        self.path = None
        return path if not err and secs >= 1 and os.path.exists(path) else None


def message(text):
    u.MessageBoxW(None, text, "velcro", 0x40)  # MB_ICONINFORMATION


def self_install():
    """Double-clicking the downloaded velcro.exe installs it: copy to %LOCALAPPDATA%\\velcro\\app, start at sign-in,
    start the installed copy. Re-running a newer download replaces the installed one. No admin needed."""
    if not getattr(sys, "frozen", False):
        return False
    target = os.path.join(v.state_dir(), "app", "velcro.exe")
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(target)):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    q = lambda x: "'" + x.replace("'", "''") + "'"
    ps = (f"$t={q(target)}; Get-Process velcro -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq $t }} | Stop-Process -Force; "
          f"for($i=0;$i -lt 30;$i++){{ try {{ Copy-Item -LiteralPath {q(sys.executable)} $t -Force -ErrorAction Stop; break }} catch {{ Start-Sleep 1 }} }}; "
          "$l=Join-Path ([Environment]::GetFolderPath('Startup')) 'velcro.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); "
          "$s.TargetPath=$t; $s.Save(); Start-Process $t")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], creationflags=0x08000000)
    log(f"install: {sys.executable} -> {target} rc={r.returncode}")
    if r.returncode == 0 and os.path.exists(target):
        hk = v.get(v.read_settings(), "record.hotkey")
        message("velcro is installed and running (notification area, bottom right; it starts at sign-in too).\n\n"
                f"Record: press {hk}, press it again to stop. The file is copied to {v.dest_dir(v.read_settings())}.\n"
                "Recorder: plug it in, then right-click the velcro icon > Use <drive> as recorder.")
    else:
        message(f"velcro could not be installed (see {LOG}).")
    return True


def main():
    if self_install():
        return
    k.CreateMutexW(None, False, "Local\\velcro-tray")
    if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: one tray icon per user
        message("velcro is already running (notification area, bottom right).")
        return
    nid = NOTIFYICONDATA()
    rec = Recording()
    timer = {"id": 1}

    def tip(text):
        nid.uFlags = NIF_TIP
        nid.szTip = text[:127]
        sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def toggle(hwnd):
        s = v.read_settings()
        if not rec.active:
            if not rec.start():
                log("hotkey: could not start recording (no microphone?)")
                balloon("Couldn't start recording (microphone?)")
                return
            log("hotkey: recording started")
            u.SetTimer(hwnd, timer["id"], 1000, None)
            nid.hIcon = u.LoadIconW(None, ctypes.c_wchar_p(32513))  # IDI_ERROR (red) while recording
            nid.uFlags = NIF_ICON
            sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            tip("velcro: recording 0:00")
        else:
            u.KillTimer(hwnd, timer["id"])
            path = rec.stop()
            log(f"hotkey: recording stopped -> {path}")
            nid.hIcon = u.LoadIconW(None, ctypes.c_wchar_p(32516))
            nid.uFlags = NIF_ICON
            sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            tip("velcro")
            if path:
                def upload():
                    ok, bad = v.import_files([path], s, say)
                    say(f"hotkey recording uploaded: {ok}" if ok and not bad else f"hotkey recording upload failed (file kept at {path})")
                threading.Thread(target=upload, daemon=True).start()
            else:
                balloon("Recording was too short or could not be saved")

    drives = []  # unregistered drives shown in the last menu, indexed by ID_ADD+i

    def register_hotkey(hwnd, quiet=False):
        u.UnregisterHotKey(hwnd, HOTKEY_ID)
        text = v.get(v.read_settings(), "record.hotkey")
        hk = v.parse_hotkey(text)
        if not hk or not u.RegisterHotKey(hwnd, HOTKEY_ID, hk[0] | 0x4000, hk[1]):  # 0x4000: no auto-repeat
            log(f"hotkey: {text} not available")
            balloon(f"Shortcut {text} is not available (bad value or used by another app)")
            return
        log(f"hotkey: {text} registered")
        if not quiet:
            balloon(f"Shortcut is now {text}")

    def balloon(text):
        nid.uFlags = NIF_INFO
        nid.szInfo, nid.szInfoTitle = text[:255], "velcro"
        sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def say(msg):
        log(msg)
        if msg.startswith("import: deleted") or "could not" in msg or "failed" in msg:
            balloon(msg)

    def proc(hwnd, msg, wp, lp):
        if msg == WM_HOTKEY:
            toggle(hwnd)
        elif msg == 0x113 and rec.active:  # WM_TIMER
            secs = int(__import__("time").time() - rec.since)
            tip(f"velcro: recording {secs // 60}:{secs % 60:02d}")
        elif msg == WM_TRAY and lp == WM_RBUTTONUP:
            m = u.CreatePopupMenu()
            hk = v.get(v.read_settings(), "record.hotkey")
            items = [(ID_RECORD, f"Stop recording ({hk})" if rec.active else f"Start recording ({hk})"), (ID_IMPORT, "Import now")]
            s = v.read_settings()
            drives[:] = [d for d in v.removable_drives() if not v.is_recorder(s, d[1], d[2])]
            items += [(ID_ADD + i, f"Use {r[:2]} {l or '(no name)'} as recorder") for i, (r, l, sn) in enumerate(drives)]
            items += [(ID_SETTINGS, "Open settings file"), (ID_RELOAD, "Reload settings (shortcut)"), (ID_LOG, "Open log"), (ID_QUIT, "Quit")]
            for i, t in items:
                u.AppendMenuW(m, 0, i, t)
            pt = wintypes.POINT(); u.GetCursorPos(ctypes.byref(pt)); u.SetForegroundWindow(hwnd)
            u.TrackPopupMenu(m, 0, pt.x, pt.y, 0, hwnd, None)
        elif msg == WM_COMMAND:
            c = wp & 0xFFFF
            if c == ID_RECORD:
                toggle(hwnd)
            elif c == ID_IMPORT:
                threading.Thread(target=lambda: [v.import_volume(r, v.read_settings(), l, sn, say) for r, l, sn in v.removable_drives()
                                                 if v.is_recorder(v.read_settings(), l, sn)], daemon=True).start()
            elif ID_ADD <= c < ID_ADD + len(drives):
                r, l, sn = drives[c - ID_ADD]
                v.add_recorder(v.read_settings(), l, sn)
                log(f"recorder added: {r} {l} ({sn})")
                balloon(f"{r[:2]} {l} is now a recorder. Copying its recordings...")
                threading.Thread(target=lambda: v.import_volume(r, v.read_settings(), l, sn, say), daemon=True).start()
            elif c == ID_RELOAD:
                register_hotkey(hwnd)
            elif c == ID_LOG:
                os.startfile(LOG) if os.path.exists(LOG) else None
            elif c == ID_SETTINGS:
                p = v.settings_path(); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "a").close(); os.startfile(p)
            elif c == ID_QUIT:
                if rec.active:
                    toggle(hwnd)  # save and upload what was recorded before exiting
                u.DestroyWindow(hwnd)
        elif msg == WM_DESTROY:
            sh.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid)); u.PostQuitMessage(0)
        else:
            return u.DefWindowProcW(hwnd, msg, wp, lp)
        return 0

    cb = WNDPROC(proc)
    wc = WNDCLASS(); wc.lpfnWndProc = cb; wc.lpszClassName = "VelcroTray"; wc.hInstance = k.GetModuleHandleW(None)
    u.RegisterClassW(ctypes.byref(wc))
    hwnd = u.CreateWindowExW(0, "VelcroTray", "velcro", 0, 0, 0, 0, 0, None, None, wc.hInstance, None)
    nid.cbSize, nid.hWnd, nid.uID = ctypes.sizeof(nid), hwnd, 1
    nid.uFlags, nid.uCallbackMessage = NIF_MESSAGE | NIF_ICON | NIF_TIP, WM_TRAY
    nid.hIcon, nid.szTip = u.LoadIconW(None, ctypes.c_wchar_p(32516)), "velcro"  # IDI_INFORMATION
    sh.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    register_hotkey(hwnd, quiet=True)

    def on_new(root, label, serial, n):
        log(f"unregistered drive {root} {label} ({serial}) has {n} recordings")
        balloon(f"{root[:2]} {label} has {n} recordings. Right-click the velcro icon > Use {root[:2]} as recorder to back it up.")
    threading.Thread(target=v.run_loop, args=(say,), kwargs={"on_new": on_new}, daemon=True).start()
    m = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(m)); u.DispatchMessageW(ctypes.byref(m))


if __name__ == "__main__":
    main()
