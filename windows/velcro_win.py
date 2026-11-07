#!/usr/bin/env python3
"""velcro for Windows: copy recordings off a USB recorder, verify each copy with SHA-256, list it in
.velcro-import.jsonl (same lines as the Mac velcro), and optionally delete it from the recorder.

Python standard library only. Everything below `Windows layer` is portable so it can be tested on Linux.

  python velcro_win.py import [DRIVE_ROOT ...]   import now (all registered recorders when none given)
  python velcro_win.py run                        watch for recorders being plugged in
  python velcro_win.py set KEY VALUE | settings | recorder add ROOT
"""
import hashlib, json, os, shutil, sys, threading, time, wave, datetime

VERSION = "0.2.2"  # windows-v<VERSION> on GitHub releases
RELEASES = "https://api.github.com/repos/dgitco/velcro/releases?per_page=30"
SEP = "\t"
MANIFEST = ".velcro-import.jsonl"
IMPORT_LOCK = threading.Lock()  # one import at a time: hotkey upload, "Import now" and the watcher share the manifest
DEFAULTS = {"import.ext": "wav mp3 m4a aac flac wma ogg", "import.delete": "yes", "import.auto": "yes", "record.hotkey": "ctrl+alt+r"}


def config_dir():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "velcro")


def state_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state"), "velcro")


def settings_path():
    return os.path.join(config_dir(), "settings")


def read_settings(path=None):
    """key = value per line; `recorder` may repeat, so values are lists."""
    out = {}
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
    except FileNotFoundError:
        pass
    return out


def write_settings(s, path=None):
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for k, vals in s.items():
            for v in vals:
                f.write(f"{k} = {v}\n")


def get(s, key):
    v = s.get(key)
    return v[-1] if v else DEFAULTS.get(key, "")


def is_on(s, key):
    return get(s, key).lower() in ("yes", "on", "true", "1")


def now_iso():
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def duration_s(path):
    """WAV only (standard library); other types stay null like an unreadable header on the Mac."""
    if not path.lower().endswith(".wav"):
        return None
    try:
        with wave.open(path, "rb") as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:
        return None


def manifest_lines(dest):
    try:
        with open(os.path.join(dest, MANIFEST), encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def manifest_add(dest, name, original, size, sha, at, device, uuid, secs):
    """Append one line unless this file+size+hash is already listed. Key order matches the Mac."""
    for e in manifest_lines(dest):
        if e.get("file") == name and e.get("size") == size and e.get("sha256") == sha:
            return
    line = {"file": name, "size": size, "sha256": sha, "imported_at": at, "device": device,
            "device_uuid": uuid, "original": original, "duration_s": secs}
    with open(os.path.join(dest, MANIFEST), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def put_verified(src, dest_dir, want=None):
    """Copy src into dest_dir under a hidden name, read the copy back, compare SHA-256, then rename.
    Returns (name, sha, is_new) or None. A numbered name (a-2.wav) is used when a different file has the name."""
    size = os.path.getsize(src)
    name = os.path.basename(src)
    base, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(os.path.join(dest_dir, name)):
        p = os.path.join(dest_dir, name)
        if os.path.samefile(p, src):  # importing a file onto itself: never "already there", never deletable
            return None
        if os.path.getsize(p) == size:
            want = want or sha256_file(src)
            if sha256_file(p) == want:
                return name, want, False
        n += 1
        name = f"{base}-{n}{ext}"
    part = os.path.join(dest_dir, f".{name}.velcro-part")
    try:
        h = hashlib.sha256()
        with open(src, "rb") as i, open(part, "wb") as o:
            while True:
                b = i.read(1 << 20)
                if not b:
                    break
                h.update(b)
                o.write(b)
            o.flush()
            os.fsync(o.fileno())
        got = h.hexdigest()
        if want and got != want:
            os.remove(part)
            return None
        if os.path.getsize(part) != size or sha256_file(part) != got:
            os.remove(part)
            return None
        shutil.copystat(src, part)
        os.rename(part, os.path.join(dest_dir, name))
        return name, got, True
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
        return None


def find_files(root, s):
    sub = get(s, "import.folder").strip("/\\")
    top = os.path.join(root, sub) if sub else root
    if not os.path.isdir(top):
        return []
    exts = {"." + e.lower().lstrip(".") for e in get(s, "import.ext").split()}
    out = []
    for d, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "System Volume Information"]
        for f in files:
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.join(d, f))
    return sorted(out)


def queue_dir():
    return os.path.join(state_dir(), "queue")


def dest_dir(s):
    return get(s, "import.dest") or os.path.join(os.path.expanduser("~"), "Recordings")  # local folder unless set


def dest_ready(dest):
    return bool(dest) and os.path.isdir(dest)


def inside(path, root):
    """True when path is root or under it (same drive and folder prefix, case-insensitive on Windows)."""
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # different drives
        return False


def holds_backups(root, s):
    """The drive at root holds the backup folder or the waiting queue: it must never be read as a recorder,
    or the backups would be "imported" onto themselves and then deleted."""
    return inside(dest_dir(s), root) or inside(queue_dir(), root)


def _import_volume(root, s, device=None, uuid="", log=print):
    """Import every recording on the recorder at root. Returns (imported, failed)."""
    dest = dest_dir(s)
    device = device or os.path.basename(root.rstrip("/\\")) or root
    if holds_backups(root, s):
        log(f"{device}: holds the backup folder {dest}, so it is not imported as a recorder (nothing copied or deleted)")
        return 0, 0
    files = find_files(root, s)
    if dest_ready(dest):
        d, queued = dest, False
    elif os.path.isdir(os.path.dirname(dest.rstrip("/\\"))) :
        os.makedirs(dest, exist_ok=True)
        d, queued = dest, False
    else:
        d, queued = queue_dir(), True
        os.makedirs(os.path.join(d, ".meta"), exist_ok=True)
    keep = not is_on(s, "import.delete")
    ok, failed, copied = [], [], 0
    done = {(e.get("device_uuid"), e.get("original"), e.get("size")) for e in manifest_lines(d)} if not queued else set()
    for f in files:
        if keep and not queued and (uuid, os.path.basename(f), os.path.getsize(f)) in done:
            continue
        secs = duration_s(f)
        r = put_verified(f, d)
        if not r:
            failed.append(f)
            continue
        name, sha, new = r
        copied += new
        at = now_iso()
        if queued:
            meta = os.path.join(d, ".meta", name)
            if not os.path.exists(meta):
                with open(meta, "w", encoding="utf-8") as m:
                    m.write(SEP.join([os.path.basename(f), str(os.path.getsize(f)), sha, at, device, uuid, "" if secs is None else str(secs)]))
        else:
            manifest_add(d, name, os.path.basename(f), os.path.getsize(f), sha, at, device, uuid, secs)
        ok.append(f)
        log(f"import: {f} -> {d}\\{name} ({sha})" if os.name == "nt" else f"import: {f} -> {d}/{name} ({sha})")
    if failed:
        log(f"{device}: {len(failed)} of {len(files)} recordings could not be copied or verified; nothing deleted")
        return len(ok), len(failed)
    if not keep:
        for f in ok:
            os.remove(f)
        log(f"import: deleted {len(ok)} verified file(s) from {device}")
    return len(ok), 0


def _import_files(files, s, log=print):
    """Upload chosen files (a hotkey recording, say) the way a recorder import does, but never delete the source."""
    s = dict(s)
    s["import.delete"] = ["no"]
    ok = failed = 0
    dest = dest_dir(s)
    for f in files:
        d, queued = (dest, False)
        if not dest_ready(dest):
            if os.path.isdir(os.path.dirname(dest.rstrip("/\\"))):
                os.makedirs(dest, exist_ok=True)
            else:
                d, queued = queue_dir(), True
                os.makedirs(os.path.join(d, ".meta"), exist_ok=True)
        r = put_verified(f, d)
        if not r:
            failed += 1
            continue
        name, sha, _ = r
        secs = duration_s(f)
        at = now_iso()
        if queued:
            with open(os.path.join(d, ".meta", name), "w", encoding="utf-8") as m:
                m.write(SEP.join([os.path.basename(f), str(os.path.getsize(f)), sha, at, "Manual upload", "", "" if secs is None else str(secs)]))
        else:
            manifest_add(d, name, os.path.basename(f), os.path.getsize(f), sha, at, "Manual upload", "", secs)
        ok += 1
        log(f"import: {f} -> {d}")
    return ok, failed


MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "opt": 0x1, "option": 0x1, "shift": 0x4, "win": 0x8, "cmd": 0x8, "command": 0x8}


def parse_hotkey(text):
    """"ctrl+alt+r" -> (modifier mask, virtual key code) for RegisterHotKey, or None. Same words as the Mac."""
    mods, key = 0, None
    for w in (x.strip() for x in text.lower().split("+")):
        if w in MODS:
            mods |= MODS[w]
        elif key is None and len(w) == 1 and w.isascii() and w.isalnum():
            key = ord(w.upper())
        elif key is None and w.startswith("f") and w[1:].isdigit() and 1 <= int(w[1:]) <= 12:
            key = 0x6F + int(w[1:])
        else:
            return None
    return (mods, key) if mods and key else None


def _flush_queue(s, log=print):
    """Move recordings waiting on this PC to import.dest once it is reachable; each verified before removal."""
    dest, q = dest_dir(s), queue_dir()
    if not dest_ready(dest) or not os.path.isdir(q):
        return 0
    moved = 0
    for name in sorted(x for x in os.listdir(q) if not x.startswith(".")):
        src = os.path.join(q, name)
        meta = os.path.join(q, ".meta", name)
        orig, size, sha, at, device, uuid, secs = name, "", "", now_iso(), "", "", ""
        if os.path.exists(meta):
            parts = open(meta, encoding="utf-8").read().split(SEP) + [""] * 7
            orig, size, sha, at, device, uuid, secs = parts[:7]
        r = put_verified(src, dest, sha or None)
        if not r:
            continue
        n, h, _ = r
        manifest_add(dest, n, orig, os.path.getsize(src), h, at, device, uuid, float(secs) if secs else None)
        os.remove(src)
        if os.path.exists(meta):
            os.remove(meta)
        moved += 1
        log(f"import: moved {name} -> {dest}")
    return moved


# ---- Windows layer: drive discovery (ctypes). Not exercised by the Linux tests. ----
def removable_drives(fixed=True):
    """[(root, label, volume serial hex)] for ready drives a recorder can show up as: removable, and (fixed=True)
    non-system fixed disks too, since some recorders present themselves as a fixed disk. Only drives registered with
    `recorder add` are ever imported, so listing extra drives here is harmless."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    system = os.environ.get("SystemDrive", "C:").upper().rstrip("\\") + "\\"
    out = []
    mask = k.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        t = k.GetDriveTypeW(root)
        if not (t == 2 or (fixed and t == 3 and root.upper() != system)):  # DRIVE_REMOVABLE / DRIVE_FIXED
            continue
        label = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        if not k.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), None, None, None, 0):
            continue
        out.append((root, label.value, f"{serial.value:08X}"))
    return out


def is_recorder(s, label, serial):
    for r in s.get("recorder", []):
        kind, _, rest = r.partition(":")
        key = rest.split(" ", 1)[0]
        if (kind == "uuid" and key.upper() == serial) or (kind == "name" and key == label):
            return True
    return False


def _ver(t):
    try:
        return tuple(int(x) for x in t.split("."))
    except ValueError:
        return ()


def http_get(url):
    """GET through WinINet: Windows' own certificate checks (Python's ssl misses roots Windows fetches on demand)."""
    import ctypes
    from ctypes import wintypes
    w = ctypes.windll.wininet
    w.InternetOpenW.restype = w.InternetOpenUrlW.restype = ctypes.c_void_p
    w.InternetOpenUrlW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    w.InternetReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    w.InternetCloseHandle.argtypes = [ctypes.c_void_p]
    s = w.InternetOpenW("velcro-windows", 0, None, None, 0)  # INTERNET_OPEN_TYPE_PRECONFIG: the user's proxy settings
    if not s:
        raise OSError("InternetOpen failed")
    try:
        h = w.InternetOpenUrlW(s, url, "Accept: application/vnd.github+json\r\n", -1, 0x80000000 | 0x04000000, None)  # RELOAD | NO_CACHE_WRITE
        if not h:
            raise OSError(f"InternetOpenUrl failed ({ctypes.GetLastError()})")
        try:
            out, buf, n = bytearray(), ctypes.create_string_buffer(65536), wintypes.DWORD()
            while w.InternetReadFile(h, buf, len(buf), ctypes.byref(n)) and n.value:
                out += buf.raw[:n.value]
            return out.decode("utf-8")
        finally:
            w.InternetCloseHandle(h)
    finally:
        w.InternetCloseHandle(s)


def newer_release(current=VERSION, fetch=None):
    """(version, page url, zip url) of the newest windows-v* release above `current`, else None. fetch(url) -> JSON text."""
    fetch = fetch or http_get
    best = None
    for rel in json.loads(fetch(RELEASES)):
        tag = rel.get("tag_name", "")
        if not tag.startswith("windows-v") or rel.get("draft"):
            continue
        v = tag[len("windows-v"):]
        if _ver(v) > _ver(current) and (best is None or _ver(v) > _ver(best[0])):
            z = next((a.get("browser_download_url") for a in rel.get("assets", []) if a.get("name", "").endswith(".zip")), None)
            best = (v, rel.get("html_url", ""), z or rel.get("html_url", ""))
    return best


def add_recorder(s, label, serial):
    s.setdefault("recorder", []).append(f"uuid:{serial} {label}")
    write_settings(s)


def run_loop(log=print, every=3, on_new=None):
    """Watch drives; import registered recorders when they appear. on_new(root, label, serial, n) is called once per
    plugging-in of an unregistered removable drive that holds recordings, so the tray can offer to register it."""
    seen, offered = set(), set()
    while True:
        s = read_settings()
        try:
            flush_queue(s, log)
            now = set()
            removable = {r for r, _, _ in removable_drives(fixed=False)} if on_new else set()
            for root, label, serial in removable_drives():
                if not is_recorder(s, label, serial):
                    if root in removable and (root + serial) not in offered and not holds_backups(root, s):
                        offered.add(root + serial)
                        n = len(find_files(root, s))
                        if n:
                            on_new(root, label, serial, n)
                    continue
                now.add(root + serial)
                if (root + serial) not in seen and is_on(s, "import.auto"):
                    import_volume(root, s, label, serial, log)
            seen = now
            if on_new:
                offered &= {r + sn for r, _, sn in removable_drives()}  # offer again after it is unplugged and back
        except Exception as e:  # keep watching whatever happens
            log(f"run: {e!r}")
        time.sleep(every)


def main(argv):
    s = read_settings()
    cmd = argv[0] if argv else "help"
    if cmd == "import":
        roots = argv[1:]
        if not roots:
            roots = [r for r, l, sn in removable_drives() if is_recorder(s, l, sn)]
        for r in roots:
            print(import_volume(r, s))
    elif cmd == "run":
        run_loop()
    elif cmd == "set" and len(argv) >= 3:
        if argv[1] == "record.hotkey" and not parse_hotkey(" ".join(argv[2:])):
            print("record.hotkey is modifiers plus one key, like ctrl+alt+r")
            return 2
        s[argv[1]] = [" ".join(argv[2:])]
        write_settings(s)
    elif cmd == "recorder" and argv[1:2] == ["add"] and len(argv) == 3:
        for r, label, serial in removable_drives():
            if r.rstrip("\\").upper() == argv[2].rstrip("\\").upper():
                if holds_backups(r, s):
                    print(f"{label} ({r}) holds the backup folder {dest_dir(s)}; not added")
                    return 1
                add_recorder(s, label, serial)
                print(f"added {label} ({serial})")
                return 0
        print("no drive at", argv[2])
        return 1
    elif cmd == "settings":
        for k, v in s.items():
            for x in v:
                print(f"{k} = {x}")
    else:
        print(__doc__)
    return 0


def import_volume(*a, **k):
    with IMPORT_LOCK:
        return _import_volume(*a, **k)


def import_files(*a, **k):
    with IMPORT_LOCK:
        return _import_files(*a, **k)


def flush_queue(*a, **k):
    with IMPORT_LOCK:
        return _flush_queue(*a, **k)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
