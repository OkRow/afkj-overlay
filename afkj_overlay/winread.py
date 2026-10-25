"""Read a file that another process (the game) keeps open.

Live order (first that works is remembered and tried first next time):
  python    - built-in open() (CRT: FILE_SHARE_READ|FILE_SHARE_WRITE)
  share_all - CreateFileW(GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE)
  read_data - CreateFileW(FILE_READ_DATA, share all)
  sync_read - CreateFileW(SYNCHRONIZE|FILE_READ_DATA, share all)
  dup_handle - duplicate the locker handle and map the file. Sees the cache
               and does not move the game write cursor.
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
    def _dup_handle(path): raise OSError("not windows")
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
_dup_cache = {}          # normcase path -> (pid, handle_value, canonical path)
_last_latency = {}
_last_bytes = {}

def strategies(include_vss=False, include_backup=False):
    s = [("python", _python)]
    if IS_WIN:
        s += [
            ("share_all", lambda p: _createfile(p, GENERIC_READ)),
            ("read_data", lambda p: _createfile(p, FILE_READ_DATA)),
            ("sync_read", lambda p: _createfile(p, SYNCHRONIZE | FILE_READ_DATA)),
            ("dup_handle", _dup_handle),
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
        except ReadThrottled:
            return False
        except ReadError:
            blocked = True
    return relaunch_elevated() if blocked else False
