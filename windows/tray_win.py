"""velcro tray icon for Windows (ctypes only): watches for recorders in the background and offers
Import now / Open log / Quit from the notification-area icon. Untested outside Windows."""
import ctypes, os, sys, threading
from ctypes import wintypes
import velcro_win as v

u, k, sh = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.shell32
WM_USER, WM_TRAY, WM_COMMAND, WM_DESTROY, WM_RBUTTONUP = 0x400, 0x401, 0x111, 0x2, 0x205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
ID_IMPORT, ID_LOG, ID_SETTINGS, ID_QUIT = 1, 2, 3, 4
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


def main():
    nid = NOTIFYICONDATA()

    def balloon(text):
        nid.uFlags = NIF_INFO
        nid.szInfo, nid.szInfoTitle = text[:255], "velcro"
        sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def say(msg):
        log(msg)
        if msg.startswith("import: deleted") or "could not" in msg:
            balloon(msg)

    def proc(hwnd, msg, wp, lp):
        if msg == WM_TRAY and lp == WM_RBUTTONUP:
            m = u.CreatePopupMenu()
            for i, t in ((ID_IMPORT, "Import now"), (ID_SETTINGS, "Open settings file"), (ID_LOG, "Open log"), (ID_QUIT, "Quit")):
                u.AppendMenuW(m, 0, i, t)
            pt = wintypes.POINT(); u.GetCursorPos(ctypes.byref(pt)); u.SetForegroundWindow(hwnd)
            u.TrackPopupMenu(m, 0, pt.x, pt.y, 0, hwnd, None)
        elif msg == WM_COMMAND:
            c = wp & 0xFFFF
            if c == ID_IMPORT:
                threading.Thread(target=lambda: [v.import_volume(r, v.read_settings(), l, sn, say) for r, l, sn in v.removable_drives()
                                                 if v.is_recorder(v.read_settings(), l, sn)], daemon=True).start()
            elif c == ID_LOG:
                os.startfile(LOG) if os.path.exists(LOG) else None
            elif c == ID_SETTINGS:
                p = v.settings_path(); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "a").close(); os.startfile(p)
            elif c == ID_QUIT:
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
    threading.Thread(target=v.run_loop, args=(say,), daemon=True).start()
    m = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(m)); u.DispatchMessageW(ctypes.byref(m))


if __name__ == "__main__":
    main()
