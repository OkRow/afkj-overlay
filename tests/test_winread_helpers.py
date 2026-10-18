"""Helpers from winread that run on any OS."""
import os, sys, unittest, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afkj_overlay.winread import volume_device_path, VSS_MIN_INTERVAL, ReadThrottled

class TestVolumeDevicePath(unittest.TestCase):
    def test_colon(self):
        self.assertEqual(volume_device_path("C:"), r"\\.\C:")
    def test_letter_only(self):
        self.assertEqual(volume_device_path("C"), r"\\.\C:")
    def test_with_slash(self):
        self.assertEqual(volume_device_path(r"c:\\"), r"\\.\C:")
    def test_d(self):
        self.assertEqual(volume_device_path("D:"), r"\\.\D:")
    def test_regression_v012_missing_colon(self):
        buggy = r"\\.\%s" % "C:".rstrip(":")
        self.assertEqual(buggy, r"\\.\C")
        self.assertNotEqual(volume_device_path("C:"), buggy)
        self.assertTrue(volume_device_path("C:").endswith(":"))
    def test_empty(self):
        with self.assertRaises(ValueError):
            volume_device_path("")
    def test_vss_interval_is_5(self):
        self.assertEqual(VSS_MIN_INTERVAL, 5.0)

class TestAgeUTC(unittest.TestCase):
    def test_utc_age_independent_of_local(self):
        import datetime as dt
        from afkj_overlay.collector import _utc
        ts = "2026-10-18 17:00:00"
        t = _utc(ts)
        self.assertEqual(t.tzinfo, dt.timezone.utc)
        # Simulate "now" 30 s later UTC — must be within 600 s regardless of UTC+3 local
        now = dt.datetime(2026, 10, 18, 17, 0, 30, tzinfo=dt.timezone.utc)
        age = (now - t).total_seconds()
        self.assertAlmostEqual(age, 30.0, places=3)
        self.assertLess(age, 600)

class TestThrottleDoesNotLockSig(unittest.TestCase):
    """Regression: ReadThrottled must NOT mark the file sig as consumed."""
    def test_throttled_return(self):
        from afkj_overlay.collector import Collector
        from afkj_overlay.gamedata import GameData
        from afkj_overlay.store import Store
        import afkj_overlay.collector as colmod
        import tempfile
        d = tempfile.mkdtemp()
        f = os.path.join(d, "log-client-ev10076.dat_000")
        open(f, "wb").write(b'{"action_type":"x"}')
        col = Collector({"log_dir_resolved": d, "log_dir": d, "file_glob": "log-client-ev*.dat_*",
                         "live_max_age_sec": 600}, GameData(), Store(":memory:"),
                        log=lambda *a: None)
        real = colmod.read_file
        def fake(path, live=True):
            raise ReadThrottled("vss: троттлинг", retry_after=2.5)
        colmod.read_file = fake
        try:
            r = col.scan_file(f, live=True)
            self.assertEqual(r, "throttled")
            col._poll_once(live=True)
            self.assertNotIn(f, col.files)
            self.assertGreater(col._vss_retry_at, 0)
        finally:
            colmod.read_file = real

if __name__ == "__main__":
    unittest.main()
