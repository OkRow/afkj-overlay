"""Collector timestamp helper. Runs on any OS."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

if __name__ == "__main__":
    unittest.main()
