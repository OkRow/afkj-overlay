"""Read a file that another process (the game) keeps open.

Order (the first success is remembered and tried first next time):
  python     - built-in open(), when the game is not holding the file
  dup_handle - duplicate the locker's handle and map the file. Sees the
               cache and does not move the game's write cursor.
"""
import os, subprocess, sys, time

IS_WIN = sys.platform == "win32"
FILE_READ_ATTRIBUTES = 0x0080
SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
ERR_NAMES = {
    2: "файл не найден", 3: "путь не найден", 5: "доступ запрещён (ACCESS_DENIED)",
    32: "файл занят другим процессом (SHARING_VIOLATION)", 33: "файл заблокирован (LOCK_VIOLATION)",
}

class ReadError(Exception):
    def __init__(self, msg, details):
        super().__init__(msg); self.details = details

def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def _python(path):
    with open(path, "rb") as f:
        return f.read()

if IS_WIN:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    adv.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]

    INVALID = ctypes.c_void_p(-1).value

    class LUID(ctypes.Structure):
        _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]
    class LUID_AND_ATTR(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]
    class TOKEN_PRIV(ctypes.Structure):
        _fields_ = [("Count", wintypes.DWORD), ("Privileges", LUID_AND_ATTR * 1)]

    def _is_invalid(h):
        if h is None:
            return True
        try:
            v = int(h) if not isinstance(h, int) else h
        except (TypeError, ValueError):
            return True
        return v in (-1, 0xFFFFFFFF) or (INVALID is not None and v == INVALID)

    def _oserr(step, err=None):
        e = ctypes.get_last_error() if err is None else err
        return OSError(e, "%s: %s" % (step, ERR_NAMES.get(e, "WinError %s" % e)))

    def _open(path, access, share=SHARE_ALL, flags=FILE_ATTRIBUTE_NORMAL, step="CreateFileW"):
        ctypes.set_last_error(0)
        h = k32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
        if _is_invalid(h):
            raise _oserr(step)
        return h

    # ---- duplicate the locker's handle and map (does not move its file pointer) ----
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    rstrtmgr = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
    ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.POINTER(ctypes.c_ulong)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.DuplicateHandle.restype = wintypes.BOOL
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.GetFileType.restype = wintypes.DWORD
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.CreateFileMappingW.restype = wintypes.HANDLE
    k32.CreateFileMappingW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k32.UnmapViewOfFile.restype = wintypes.BOOL
    PROCESS_DUP_HANDLE = 0x0040
    PROCESS_QUERY_INFORMATION = 0x0400
    DUPLICATE_SAME_ACCESS = 0x2
    FILE_TYPE_DISK = 1
    PAGE_READONLY = 0x02
    PAGE_READWRITE = 0x04
    PAGE_WRITECOPY = 0x08
    FILE_MAP_READ = 0x04
    FILE_MAP_COPY = 0x01
    SystemExtendedHandleInformation = 64
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    _HANDLE_ENTRY = 40
    _HANDLE_HEADER = 16

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]
    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", RM_UNIQUE_PROCESS),
                    ("strAppName", wintypes.WCHAR * 256),
                    ("strServiceShortName", wintypes.WCHAR * 64),
                    ("ApplicationType", wintypes.DWORD),
                    ("AppStatus", wintypes.ULONG),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL)]
    rstrtmgr.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    rstrtmgr.RmStartSession.restype = wintypes.DWORD
    rstrtmgr.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
                                            wintypes.UINT, wintypes.LPVOID, wintypes.UINT, wintypes.LPVOID]
    rstrtmgr.RmRegisterResources.restype = wintypes.DWORD
    rstrtmgr.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
                                   ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD)]
    rstrtmgr.RmGetList.restype = wintypes.DWORD
    rstrtmgr.RmEndSession.argtypes = [wintypes.DWORD]
    rstrtmgr.RmEndSession.restype = wintypes.DWORD

    def _enable_privilege(name):
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(wintypes.HANDLE(-1), 0x0020 | 0x0008, ctypes.byref(tok)):
            return False
        luid = LUID()
        if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            k32.CloseHandle(tok)
            return False
        tp = TOKEN_PRIV(1, (LUID_AND_ATTR * 1)(LUID_AND_ATTR(luid, 0x2)))
        adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        err = ctypes.get_last_error()
        k32.CloseHandle(tok)
        return err == 0

    def _final_path(h):
        buf = ctypes.create_unicode_buffer(1024)
        n = k32.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
        if n == 0:
            return None
        if n >= len(buf):
            buf = ctypes.create_unicode_buffer(n + 1)
            n = k32.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
            if n == 0:
                return None
        return buf.value

    def _canonical_dos(path):
        h = _open(path, FILE_READ_ATTRIBUTES)
        try:
            got = _final_path(h)
        finally:
            k32.CloseHandle(h)
        if not got:
            raise _oserr("GetFinalPathNameByHandleW")
        return got

    def _map_read(h):
        """Copy the file through a mapping. ReadFile/SetFilePointer would move the owner's pointer."""
        size = ctypes.c_longlong(0)
        if not k32.GetFileSizeEx(h, ctypes.byref(size)):
            raise _oserr("GetFileSizeEx")
        if size.value <= 0:
            return b""
        last = "map failed"
        for prot, access, name in (
            (PAGE_READONLY, FILE_MAP_READ, "READONLY"),
            (PAGE_WRITECOPY, FILE_MAP_COPY, "WRITECOPY"),
            (PAGE_READWRITE, FILE_MAP_READ, "READWRITE"),
        ):
            mh = k32.CreateFileMappingW(h, None, prot, 0, 0, None)
            if not mh:
                last = f"{name} {ctypes.get_last_error()}"
                continue
            view = k32.MapViewOfFile(mh, access, 0, 0, 0)
            if not view:
                last = f"{name} view {ctypes.get_last_error()}"
                k32.CloseHandle(mh)
                continue
            try:
                return ctypes.string_at(view, size.value)
            finally:
                k32.UnmapViewOfFile(view)
                k32.CloseHandle(mh)
        raise OSError(5, f"dup_handle: не удалось отобразить файл ({last})")

    def _system_handles():
        size = 8 << 20
        while size <= 256 << 20:
            buf = ctypes.create_string_buffer(size)
            ret = ctypes.c_ulong(0)
            st = ntdll.NtQuerySystemInformation(SystemExtendedHandleInformation, buf, size, ctypes.byref(ret))
            if st == STATUS_INFO_LENGTH_MISMATCH:
                size *= 2
                continue
            if st != 0:
                raise OSError(1, f"dup_handle: NtQuerySystemInformation {st:#x}")
            n = int.from_bytes(buf.raw[0:8], "little")
            return buf, n
        raise OSError(1, "dup_handle: слишком много дескрипторов в системе")

    def _lockers(path):
        """PIDs that have the file open, via Restart Manager. Empty if nobody does."""
        session = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(32)
        if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            arr = (wintypes.LPCWSTR * 1)(os.path.abspath(path))
            if rstrtmgr.RmRegisterResources(session, 1, arr, 0, None, 0, None) != 0:
                return []
            needed = wintypes.UINT(0)
            count = wintypes.UINT(0)
            reason = wintypes.DWORD(0)
            rstrtmgr.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reason))
            if needed.value == 0:
                return []
            infos = (RM_PROCESS_INFO * needed.value)()
            count = wintypes.UINT(needed.value)
            if rstrtmgr.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reason)) != 0:
                return []
            return [int(infos[i].Process.dwProcessId) for i in range(count.value)]
        finally:
            rstrtmgr.RmEndSession(session)

    def _read_owner_handle(pid, hv, want):
        """-> bytes, or None if this handle is no longer the file. Raises OSError if the process is gone."""
        ph = k32.OpenProcess(PROCESS_DUP_HANDLE | PROCESS_QUERY_INFORMATION, False, pid)
        if not ph:
            raise OSError(5, f"dup_handle: нет доступа к процессу {pid}")
        try:
            dup = wintypes.HANDLE()
            if not k32.DuplicateHandle(ph, wintypes.HANDLE(hv), wintypes.HANDLE(-1),
                                       ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS):
                raise OSError(ctypes.get_last_error() or 6, "dup_handle: DuplicateHandle")
            dh = dup.value
            try:
                if k32.GetFileType(dh) != FILE_TYPE_DISK:
                    return None
                got = _final_path(dh)
                if not got or os.path.normcase(got) != os.path.normcase(want):
                    return None
                return _map_read(dh)
            finally:
                k32.CloseHandle(dh)
        finally:
            k32.CloseHandle(ph)

    def _scan_owner(path, want, pids):
        opened = {}
        try:
            for pid in pids:
                ph = k32.OpenProcess(PROCESS_DUP_HANDLE | PROCESS_QUERY_INFORMATION, False, pid)
                if ph:
                    opened[pid] = ph
            if not opened:
                raise OSError(5, "dup_handle: нет доступа к процессу, который держит лог (нужен администратор)")
            buf, n = _system_handles()
            raw = buf.raw
            for i in range(n):
                o = _HANDLE_HEADER + i * _HANDLE_ENTRY
                pid = int.from_bytes(raw[o + 8:o + 16], "little")
                ph = opened.get(pid)
                if not ph:
                    continue
                hv = int.from_bytes(raw[o + 16:o + 24], "little") & 0xFFFFFFFF
                dup = wintypes.HANDLE()
                if not k32.DuplicateHandle(ph, wintypes.HANDLE(hv), wintypes.HANDLE(-1),
                                           ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS):
                    continue
                dh = dup.value
                try:
                    if k32.GetFileType(dh) != FILE_TYPE_DISK:
                        continue
                    got = _final_path(dh)
                    if not got or os.path.normcase(got) != os.path.normcase(want):
                        continue
                    try:
                        data = _map_read(dh)
                    except OSError:
                        continue
                    _dup_cache[os.path.normcase(os.path.abspath(path))] = (pid, hv, want)
                    return data
                finally:
                    k32.CloseHandle(dh)
        finally:
            for ph in opened.values():
                k32.CloseHandle(ph)
        raise OSError(2, "dup_handle: дескриптор лога в процессе игры не найден")

    _debug_on = False
    def _dup_handle(path):
        global _debug_on
        if not _debug_on:
            _debug_on = True
            _enable_privilege("SeDebugPrivilege")
        key = os.path.normcase(os.path.abspath(path))
        cached = _dup_cache.get(key)
        if cached:
            try:
                data = _read_owner_handle(*cached)
                if data is not None:
                    return data
            except OSError:
                pass
            _dup_cache.pop(key, None)
        pids = [p for p in _lockers(path) if p != os.getpid()]
        if not pids:
            raise OSError(2, "dup_handle: файл никто не держит")
        want = _canonical_dos(path)
        return _scan_owner(path, want, pids)

else:
    def _dup_handle(path): raise OSError("not windows")

# ---------- public API ----------
_preferred = None
_dup_cache = {}          # normcase path -> (pid, handle_value, canonical path)
_last_latency = {}
_last_bytes = {}

def strategies():
    return [("python", _python), ("dup_handle", _dup_handle)]

def read_file(path, live=True):
    """-> (bytes, strategy_name). Raises ReadError. `live` is kept for callers."""
    global _preferred
    order = strategies()
    if _preferred:
        order = sorted(order, key=lambda s: s[0] != _preferred)
    details = []
    for name, fn in order:
        t0 = time.time()
        try:
            data = fn(path)
            dt = time.time() - t0
            _preferred = name; _last_latency[name] = dt; _last_bytes[name] = len(data)
            return data, name
        except OSError as e:
            code = getattr(e, "winerror", None) or e.errno
            details.append((name, ("%s [%s] %s" % (ERR_NAMES.get(code, ""), code, e)).strip()))
        except Exception as e:
            details.append((name, repr(e)))
    raise ReadError("нет доступа к файлу лога", details)

def last_info():
    return dict(latency=_last_latency, bytes=_last_bytes, preferred=_preferred)

def clear_read_state():
    """Drop the remembered strategy and the duplicated-handle cache. For tests."""
    global _preferred
    _preferred = None
    _dup_cache.clear()
    _last_latency.clear()
    _last_bytes.clear()

def relaunch_elevated():
    """Start this program again with a UAC prompt. True if the elevated process was launched.
    The child inherits AFKJ_ELEVATED=1 so it does not prompt again."""
    if not IS_WIN or is_admin() or os.environ.get("AFKJ_ELEVATED") or os.environ.get("AFKJ_NO_ELEVATE"):
        return False
    os.environ["AFKJ_ELEVATED"] = "1"
    from .paths import ROOT
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    params = subprocess.list2cmdline(["-m", "afkj_overlay", *sys.argv[1:]])
    rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, ROOT, 1)
    return bool(rc) and int(rc) > 32

def elevate_if_log_locked(cfg):
    """If every current log file is locked and we are not elevated, relaunch elevated.
    Returns True when this process should exit because a replacement was started."""
    if not IS_WIN or is_admin() or os.environ.get("AFKJ_ELEVATED") or os.environ.get("AFKJ_NO_ELEVATE"):
        return False
    import glob as _glob
    d = cfg.get("log_dir_resolved") or ""
    if not d or not os.path.isdir(d):
        return False
    files = _glob.glob(os.path.join(d, cfg.get("file_glob") or "log-client-ev*.dat_*"))
    if not files:
        return False
    blocked = False
    for p in files:
        try:
            read_file(p, live=True)
            return False
        except ReadError:
            blocked = True
    return relaunch_elevated() if blocked else False
