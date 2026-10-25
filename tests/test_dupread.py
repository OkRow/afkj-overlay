"""Exclusive-lock read: duplicate the owner's handle and map the file.

The mapping must not move the owner's file pointer — that pointer is the game's write cursor.
"""
import os, subprocess, sys, tempfile, textwrap, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AFKJ_NO_ELEVATE"] = "1"

from afkj_overlay.winread import clear_read_state

WIN = sys.platform == "win32"

CHILD = textwrap.dedent(r"""
    import ctypes, os, sys, time
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                     ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    path, stop = sys.argv[1], sys.argv[2]
    h = k32.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    if not h or h == wintypes.HANDLE(-1).value:
        print("open fail", ctypes.get_last_error()); sys.exit(2)
    k32.SetFilePointerEx(h, 17, ctypes.byref(ctypes.c_longlong(0)), 0)
    open(stop + ".ready", "w").write(str(os.getpid()))
    while not os.path.exists(stop):
        time.sleep(0.02)
    cur = ctypes.c_longlong(0)
    k32.SetFilePointerEx(h, 0, ctypes.byref(cur), 1)
    open(stop + ".pos", "w").write(str(cur.value))
    k32.CloseHandle(h)
""")

@unittest.skipUnless(WIN, "Windows handle duplication")
class TestDupHandle(unittest.TestCase):
    def test_reads_exclusive_file_without_moving_pointer(self):
        from afkj_overlay.winread import read_file
        clear_read_state()
        d = tempfile.mkdtemp()
        path = os.path.join(d, "log-client-ev10076.dat_000")
        payload = b"EVENT-POINTER-SAFE-" + b"0123456789abcdef" * 40
        open(path, "wb").write(payload)
        stop = os.path.join(d, "stop")
        child_py = os.path.join(d, "hold.py")
        open(child_py, "w", encoding="utf-8").write(CHILD)
        proc = subprocess.Popen([sys.executable, child_py, path, stop])
        try:
            deadline = time.time() + 5
            while not os.path.exists(stop + ".ready") and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(os.path.exists(stop + ".ready"), "child did not lock the file")
            data, method = read_file(path, live=True)
            self.assertEqual(method, "dup_handle")
            self.assertEqual(data, payload)
            # cached handle, second read still must not seek
            data2, method2 = read_file(path, live=True)
            self.assertEqual(method2, "dup_handle")
            self.assertEqual(data2, payload)
        finally:
            open(stop, "w").close()
            proc.wait(timeout=5)
            clear_read_state()
        self.assertTrue(os.path.exists(stop + ".pos"), "child did not report its pointer")
        self.assertEqual(open(stop + ".pos").read().strip(), "17")

class TestStuckMtime(unittest.TestCase):
    def test_reread_when_stat_unchanged(self):
        from afkj_overlay.collector import Collector
        from afkj_overlay.gamedata import GameData
        from afkj_overlay.store import Store
        d = tempfile.mkdtemp()
        f = os.path.join(d, "log-client-ev1.dat_000")
        def rec(gid):
            js = ('{"action_type":"battle_prepare_start","battle_group_id":%d}' % gid).encode()
            ts = b"2026-10-26 00:00:00"
            return b"event_time\x12\x13" + ts + b"event_name\x12\x06battle\x12custom_content\x12" + bytes([len(js)]) + js
        a, b = rec(4033303), rec(4033304)
        self.assertEqual(len(a), len(b))
        open(f, "wb").write(a)
        got = []
        cfg = {"log_dir_resolved": d, "log_dir": d, "file_glob": "log-client-ev*.dat_*", "live_max_age_sec": 10**9}
        col = Collector(cfg, GameData(), Store(":memory:"), lambda k, p: got.append(p["gid"]), log=lambda *x: None)
        col._poll_once(live=True)
        self.assertEqual(got, [4033303])
        st = os.stat(f)
        open(f, "wb").write(b)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        col.methods[f] = "python"   # metadata looks unchanged: must not notice the rewrite
        col._poll_once(live=True)
        self.assertEqual(got, [4033303])
        col.methods[f] = "dup_handle"
        col._poll_once(live=True)
        self.assertEqual(got, [4033303, 4033304])

if __name__ == "__main__":
    unittest.main(verbosity=2)
