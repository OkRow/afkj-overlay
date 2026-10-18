"""Read a file that another process (the game) keeps open.

Live order (first that works is remembered and tried first next time):
  python    - built-in open() (CRT: FILE_SHARE_READ|FILE_SHARE_WRITE)
  share_all - CreateFileW(GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE)
  read_data - CreateFileW(FILE_READ_DATA, share all)
  sync_read - CreateFileW(SYNCHRONIZE|FILE_READ_DATA, share all)
  raw_ntfs  - admin: attribute-only open / dir FileId -> volume cluster reads
  vss       - admin: Volume Shadow Copy, trailing-edge throttle (~5 s)

`backup` (SeBackupPrivilege) is diag-only: bypasses ACLs, not sharing locks.

v0.1.3 BUGFIX: _open_volume used drive.rstrip(":") which turned "C:" into "C"
and opened "\\\\.\\C" instead of "\\\\.\\C:" -> ERROR_FILE_NOT_FOUND (2).
"""
import os, sys, time, tempfile, subprocess
from . import ntfs

IS_WIN = sys.platform == "win32"
GENERIC_READ = 0x80000000
FILE_READ_DATA = 0x0001
SYNCHRONIZE = 0x00100000
FILE_READ_ATTRIBUTES = 0x0080
SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_ATTRIBUTE_COMPRESSED = 0x800
FILE_ATTRIBUTE_ENCRYPTED = 0x4000
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
FSCTL_GET_NTFS_FILE_RECORD = 0x00090068
ERROR_HANDLE_EOF = 38
ERROR_MORE_DATA = 234
VSS_MIN_INTERVAL = 5.0  # seconds; snapshot itself ~3.5 s
ERR_NAMES = {
    2: "файл не найден", 3: "путь не найден", 5: "доступ запрещён (ACCESS_DENIED)",
    32: "файл занят другим процессом (SHARING_VIOLATION)", 33: "файл заблокирован (LOCK_VIOLATION)",
    38: "конец файла (HANDLE_EOF)", 1314: "нет привилегии (нужен администратор)",
}

class ReadError(Exception):
    def __init__(self, msg, details):
        super().__init__(msg); self.details = details

class ReadThrottled(Exception):
    """VSS cooldown; caller should keep last OK status and retry after .retry_after."""
    def __init__(self, msg, retry_after=0.0):
        super().__init__(msg); self.retry_after = retry_after

def volume_device_path(drive):
    """Normalize 'C:', 'C', 'C:\\' -> '\\\\.\\C:' (colon required)."""
    if not drive:
        raise ValueError("empty drive")
    letter = drive.strip().rstrip(":\\/").split(":")[0][-1:]
    if not letter or not letter.isalpha():
        raise ValueError("bad drive: %r" % drive)
    return r"\\.\%s:" % letter.upper()

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
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                     ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(wintypes.DWORD)] * 4
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    adv.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]

    INVALID = ctypes.c_void_p(-1).value
    FILE_LIST_DIRECTORY = 0x0001
    FileIdBothDirectoryInfo = 37
    ERROR_NO_MORE_FILES = 18

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]
    k32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]

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

    _backup_enabled = None
    def enable_backup_privilege():
        global _backup_enabled
        if _backup_enabled is not None:
            return _backup_enabled
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(tok)):
            _backup_enabled = (False, "OpenProcessToken ошибка %s" % ctypes.get_last_error())
            return _backup_enabled
        luid = LUID()
        if not adv.LookupPrivilegeValueW(None, "SeBackupPrivilege", ctypes.byref(luid)):
            _backup_enabled = (False, "LookupPrivilegeValue ошибка %s" % ctypes.get_last_error())
            return _backup_enabled
        tp = TOKEN_PRIV(1, (LUID_AND_ATTR * 1)(LUID_AND_ATTR(luid, 0x2)))
        adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        err = ctypes.get_last_error(); k32.CloseHandle(tok)
        _backup_enabled = (err == 0,
            "SeBackupPrivilege включена" if err == 0
            else "SeBackupPrivilege не включена (%s)" % ERR_NAMES.get(err, err))
        return _backup_enabled

    def _read_handle(h):
        size = ctypes.c_longlong(0)
        k32.GetFileSizeEx(h, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 1))
        got = wintypes.DWORD(0); out = bytearray()
        while True:
            if not k32.ReadFile(h, buf, len(buf), ctypes.byref(got), None):
                raise _oserr("ReadFile")
            if got.value == 0:
                break
            out += buf.raw[:got.value]
            if len(out) >= size.value:
                break
        return bytes(out)

    def _open(path, access, share=SHARE_ALL, flags=FILE_ATTRIBUTE_NORMAL, step="CreateFileW"):
        ctypes.set_last_error(0)
        h = k32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
        if _is_invalid(h):
            raise _oserr(step)
        return h

    def _createfile(path, access, flags=FILE_ATTRIBUTE_NORMAL):
        h = _open(path, access, flags=flags, step="CreateFileW")
        try:
            return _read_handle(h)
        finally:
            k32.CloseHandle(h)

    def _backup(path):
        ok, msg = enable_backup_privilege()
        if not ok:
            raise OSError(1314, msg)
        return _createfile(path, GENERIC_READ, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS)

    def get_filesystem(drive):
        root = drive.rstrip("\\/") + "\\"
        fs = ctypes.create_unicode_buffer(32)
        if not k32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 32):
            return "?"
        return fs.value or "?"

    def _cluster_size(drive):
        spc = wintypes.DWORD(); bps = wintypes.DWORD()
        free = wintypes.DWORD(); total = wintypes.DWORD()
        root = drive.rstrip("\\/") + "\\"
        if not k32.GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps),
                                     ctypes.byref(free), ctypes.byref(total)):
            raise _oserr("cluster_size")
        return spc.value * bps.value

    def _open_volume(drive):
        # MUST keep the colon: \\\\.\\C:  — rstrip(":") produced \\\\.\\C -> error 2
        vol_path = volume_device_path(drive)
        return _open(vol_path, GENERIC_READ, share=SHARE_ALL,
                     flags=FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING,
                     step="open_volume(%s)" % vol_path)

    def _read_aligned(vol, offset, nbytes, align):
        start = offset - (offset % align)
        end = offset + nbytes
        end = end + ((align - end % align) % align)
        size = end - start
        MEM_COMMIT = 0x1000; MEM_RESERVE = 0x2000; PAGE_READWRITE = 0x04; MEM_RELEASE = 0x8000
        ptr = k32.VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not ptr:
            raise _oserr("read/VirtualAlloc")
        try:
            newpos = ctypes.c_longlong(0)
            if not k32.SetFilePointerEx(vol, start, ctypes.byref(newpos), 0):
                raise _oserr("read/SetFilePointerEx")
            got = wintypes.DWORD(0)
            if not k32.ReadFile(vol, ptr, size, ctypes.byref(got), None):
                raise _oserr("read/ReadFile")
            raw = (ctypes.c_char * size).from_address(ptr).raw
            return raw[offset - start: offset - start + nbytes]
        finally:
            k32.VirtualFree(ptr, 0, MEM_RELEASE)

    def _retrieval_extents(h):
        class STARTING_VCN(ctypes.Structure):
            _fields_ = [("StartingVcn", ctypes.c_longlong)]
        inbuf = STARTING_VCN(0)
        size = 4096
        while True:
            out = ctypes.create_string_buffer(size)
            ret = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            ok = k32.DeviceIoControl(h, FSCTL_GET_RETRIEVAL_POINTERS,
                                     ctypes.byref(inbuf), ctypes.sizeof(inbuf),
                                     out, size, ctypes.byref(ret), None)
            err = ctypes.get_last_error()
            if not ok and err == ERROR_MORE_DATA:
                size *= 2; continue
            if not ok and err == ERROR_HANDLE_EOF:
                return []
            if not ok:
                raise _oserr("retrieval_pointers", err)
            break
        buf = out.raw
        ext_count = int.from_bytes(buf[0:4], "little")
        starting = int.from_bytes(buf[8:16], "little", signed=True)
        extents = []; prev = starting; o = 16
        for _ in range(ext_count):
            next_vcn = int.from_bytes(buf[o:o+8], "little", signed=True)
            lcn = int.from_bytes(buf[o+8:o+16], "little", signed=True)
            extents.append((next_vcn - prev, lcn))
            prev = next_vcn; o += 16
        return extents

    def _file_record(vol, file_ref, record_size=1024):
        class INPUT(ctypes.Structure):
            _fields_ = [("FileReferenceNumber", ctypes.c_longlong)]
        inbuf = INPUT(file_ref & 0xFFFFFFFFFFFF)
        size = 8 + 4 + record_size + 64
        out = ctypes.create_string_buffer(size)
        ret = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        if not k32.DeviceIoControl(vol, FSCTL_GET_NTFS_FILE_RECORD,
                                   ctypes.byref(inbuf), ctypes.sizeof(inbuf),
                                   out, size, ctypes.byref(ret), None):
            raise _oserr("file_record")
        length = int.from_bytes(out.raw[8:12], "little")
        return out.raw[12:12 + length]

    def _open_attr_clean(path):
        enable_backup_privilege()
        attempts = [
            (0, FILE_ATTRIBUTE_NORMAL, "open_attr(access=0)"),
            (FILE_READ_ATTRIBUTES, FILE_ATTRIBUTE_NORMAL, "open_attr(FILE_READ_ATTRIBUTES)"),
            (FILE_READ_ATTRIBUTES, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS,
             "open_attr(FILE_READ_ATTRIBUTES|BACKUP_SEMANTICS)"),
        ]
        errors = []; last = None
        for access, flags, step in attempts:
            try:
                return _open(path, access, share=SHARE_ALL, flags=flags, step=step), step
            except OSError as e:
                last = e; errors.append(str(e))
        code = getattr(last, "winerror", None) or getattr(last, "errno", None) or 32
        raise OSError(code, "open_attr: " + " | ".join(errors))

    def _file_id_via_directory(path):
        parent, name = os.path.split(os.path.abspath(path))
        name_l = name.lower()
        enable_backup_privilege()
        h = _open(parent, FILE_LIST_DIRECTORY, share=SHARE_ALL,
                  flags=FILE_FLAG_BACKUP_SEMANTICS, step="dir_enum/open_parent")
        try:
            buf_size = 256 * 1024
            while True:
                buf = ctypes.create_string_buffer(buf_size)
                ctypes.set_last_error(0)
                ok = k32.GetFileInformationByHandleEx(h, FileIdBothDirectoryInfo, buf, buf_size)
                err = ctypes.get_last_error()
                if not ok and err not in (ERROR_MORE_DATA, ERROR_NO_MORE_FILES):
                    raise _oserr("dir_enum/GetFileInformationByHandleEx", err)
                if not ok and err == ERROR_NO_MORE_FILES:
                    break
                raw = buf.raw
                offset = 0
                found_any = False
                while offset + 104 <= len(raw):
                    next_off = int.from_bytes(raw[offset:offset+4], "little")
                    end_of_file = int.from_bytes(raw[offset+40:offset+48], "little")
                    name_len = int.from_bytes(raw[offset+60:offset+64], "little")
                    file_id = int.from_bytes(raw[offset+96:offset+104], "little")
                    try:
                        entry = raw[offset+104:offset+104+name_len].decode("utf-16-le")
                    except Exception:
                        entry = ""
                    found_any = True
                    if entry.lower() == name_l:
                        return file_id, end_of_file
                    if next_off == 0:
                        break
                    offset += next_off
                if not ok and err == ERROR_MORE_DATA:
                    buf_size = min(buf_size * 2, 8 * 1024 * 1024)
                    continue
                if not found_any:
                    break
            raise OSError(2, "dir_enum: файл %r не найден в каталоге" % name)
        finally:
            k32.CloseHandle(h)

    def _raw_ntfs(path):
        if not is_admin():
            raise OSError(5, "raw_ntfs: нужен администратор")
        abs_path = os.path.abspath(path)
        drive = os.path.splitdrive(abs_path)[0]
        if not drive:
            raise OSError(3, "raw_ntfs: путь без диска")
        fs = get_filesystem(drive)
        if fs.upper() != "NTFS":
            raise OSError(1, "raw_ntfs: ФС %r, нужен NTFS" % fs)

        file_size = None
        file_ref = None
        extents = None

        try:
            h, _step = _open_attr_clean(abs_path)
            try:
                info = BY_HANDLE_FILE_INFORMATION()
                if not k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                    raise _oserr("get_info")
                if info.dwFileAttributes & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED):
                    raise ntfs.NtfsUnsupported("сжатый/зашифрованный файл не поддерживается")
                file_size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
                file_ref = (info.nFileIndexHigh << 32) | info.nFileIndexLow
                try:
                    extents = _retrieval_extents(h)
                except OSError:
                    extents = None
            finally:
                k32.CloseHandle(h)
        except OSError as e:
            try:
                file_ref, file_size = _file_id_via_directory(abs_path)
                extents = None
            except OSError as e2:
                raise OSError(
                    getattr(e, "winerror", None) or e.errno or 32,
                    "open_attr failed (%s); dir_enum failed (%s)" % (e, e2))

        cs = _cluster_size(drive)
        vol = _open_volume(drive)
        try:
            def read_clusters(lcn, count):
                return _read_aligned(vol, lcn * cs, count * cs, cs)
            if extents:
                return ntfs.extents_to_bytes(extents, cs, file_size, read_clusters)
            record = _file_record(vol, file_ref)
            return ntfs.extract_data(record, cs, read_clusters)
        finally:
            k32.CloseHandle(vol)

    # ---- VSS (trailing-edge throttle) ----
    _vss_last = 0.0
    _vss_pending = False   # change seen during / after snapshot -> retry when cooldown ends
    _vss_owned = []

    def _ps(cmd, timeout=180):
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout)

    def vss_cleanup():
        try:
            for sid in list(_vss_owned):
                _ps("$s=Get-WmiObject Win32_ShadowCopy | ? {$_.ID -eq '%s'}; if($s){$s.Delete()}" % sid)
                try: _vss_owned.remove(sid)
                except ValueError: pass
        except Exception:
            pass

    def vss_mark_pending():
        """Collector calls this when mtime changed but VSS was throttled / mid-snapshot."""
        global _vss_pending
        _vss_pending = True

    def vss_seconds_until_ready():
        left = VSS_MIN_INTERVAL - (time.time() - _vss_last)
        return max(0.0, left)

    def _vss(path, force=False):
        global _vss_last, _vss_pending
        if not is_admin():
            raise OSError(5, "vss: нужен администратор")
        now = time.time()
        left = VSS_MIN_INTERVAL - (now - _vss_last)
        if not force and left > 0:
            _vss_pending = True
            raise ReadThrottled(
                "vss: троттлинг %.0fс (осталось %.1fс)" % (VSS_MIN_INTERVAL, left),
                retry_after=left)
        drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        abs_path = os.path.abspath(path)
        rel = abs_path[2:] if abs_path[1:2] == ":" else abs_path
        rel_ps = rel.replace("'", "''")
        dst = os.path.join(tempfile.gettempdir(), "afkj_vss_%s.dat" % os.getpid())
        dst_ps = dst.replace("'", "''")
        # Capture mtime/size before snapshot to detect mid-snapshot writes
        try:
            st0 = os.stat(abs_path); sig0 = (st0.st_mtime_ns, st0.st_size)
        except OSError:
            sig0 = None
        ps = (
            "$r=(Get-WmiObject -List Win32_ShadowCopy).Create('%s','ClientAccessible');"
            "if($r.ReturnValue -ne 0){ throw \"Create shadow failed: $($r.ReturnValue)\" };"
            "$sid=$r.ShadowID; Write-Output \"SID=$sid\";"
            "$s=Get-WmiObject Win32_ShadowCopy | ? {$_.ID -eq $sid};"
            "$src=$s.DeviceObject + '%s';"
            "$dst='%s';"
            "cmd /c copy /y \"$src\" \"$dst\" | Out-Null;"
            "$s.Delete();"
            "Write-Output $dst"
        ) % (drive, rel_ps, dst_ps)
        out = _ps(ps)
        _vss_last = time.time()
        lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
        sid = next((l[4:] for l in lines if l.startswith("SID=")), None)
        dst_out = next((l for l in lines if l.lower().endswith(".dat") and os.path.exists(l)), None)
        if not dst_out or not os.path.exists(dst_out):
            if sid:
                _ps("$s=Get-WmiObject Win32_ShadowCopy | ? {$_.ID -eq '%s'}; if($s){$s.Delete()}" % sid)
            raise RuntimeError((out.stderr or out.stdout or "vss failed")[:400])
        try:
            data = open(dst_out, "rb").read()
        finally:
            try: os.remove(dst_out)
            except OSError: pass
        # Trailing edge: if file changed during the snapshot, schedule another one
        try:
            st1 = os.stat(abs_path); sig1 = (st1.st_mtime_ns, st1.st_size)
        except OSError:
            sig1 = None
        if sig0 is not None and sig1 is not None and sig1 != sig0:
            _vss_pending = True
        else:
            _vss_pending = False
        return data

else:
    def _createfile(path, access, flags=0): raise OSError("not windows")
    def _backup(path): raise OSError("not windows")
    def _raw_ntfs(path): raise OSError("not windows")
    def _vss(path, force=False): raise OSError("not windows")
    def vss_cleanup(): pass
    def vss_mark_pending(): pass
    def vss_seconds_until_ready(): return 0.0
    def enable_backup_privilege(): return (False, "not windows")
    def get_filesystem(drive): return "?"
    _vss_pending = False
    _vss_last = 0.0

# ---------- public API ----------
_preferred = None
_last_latency = {}
_last_bytes = {}

def strategies(include_vss=False, include_backup=False):
    s = [("python", _python)]
    if IS_WIN:
        s += [
            ("share_all", lambda p: _createfile(p, GENERIC_READ)),
            ("read_data", lambda p: _createfile(p, FILE_READ_DATA)),
            ("sync_read", lambda p: _createfile(p, SYNCHRONIZE | FILE_READ_DATA)),
            ("raw_ntfs", _raw_ntfs),
        ]
        if include_backup:
            s.append(("backup", _backup))
        if include_vss:
            s.append(("vss", lambda p: _vss(p, force=True)))
    return s

def live_strategies():
    s = strategies()
    if IS_WIN:
        s.append(("vss", lambda p: _vss(p, force=False)))
    return s

def read_file(path, live=True):
    """-> (bytes, strategy_name). Raises ReadError or ReadThrottled."""
    global _preferred
    order = live_strategies() if live else strategies(include_vss=True, include_backup=True)
    if _preferred:
        order = sorted(order, key=lambda s: s[0] != _preferred)
    details = []; throttled = None
    for name, fn in order:
        if name in ("raw_ntfs", "vss", "backup") and not is_admin():
            details.append((name, "пропущено: нужен запуск от администратора")); continue
        t0 = time.time()
        try:
            data = fn(path)
            dt = time.time() - t0
            _preferred = name; _last_latency[name] = dt; _last_bytes[name] = len(data)
            return data, name
        except ReadThrottled as e:
            throttled = e; details.append((name, str(e))); continue
        except OSError as e:
            code = getattr(e, "winerror", None) or e.errno
            details.append((name, ("%s [%s] %s" % (ERR_NAMES.get(code, ""), code, e)).strip()))
        except ntfs.NtfsUnsupported as e:
            details.append((name, str(e)))
        except Exception as e:
            details.append((name, repr(e)))
    if throttled:
        raise throttled
    raise ReadError("нет доступа к файлу лога", details)

def last_info():
    return dict(latency=_last_latency, bytes=_last_bytes, preferred=_preferred)

def vss_is_pending():
    if IS_WIN:
        return bool(globals().get("_vss_pending"))
    return False
