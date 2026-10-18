import datetime as dt, os, sys, tempfile, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from afkj_overlay.gamedata import GameData
from afkj_overlay.store import Store
from afkj_overlay.collector import Collector

def rec(action, gid, ts=None, extra=""):
    ts = ts or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    js = ('{"action_type":"%s","battle_group_id":%d%s}' % (action, gid, extra)).encode()
    ln = len(js); var = bytes([ln]) if ln < 128 else bytes([(ln & 0x7F) | 0x80, ln >> 7])
    return b"\n\nevent_time\x12\x13" + ts.encode() + b"\x12\x14\n\nevent_name\x12\x06battle\x12\x00\n\x0ecustom_content\x12" + var + js

class TestLive(unittest.TestCase):
    def test_live_append_and_recreate(self):
        d = tempfile.mkdtemp(); f = os.path.join(d, "log-client-ev10076.dat_000")
        open(f, "wb").write(rec("battle_end", 1, ts="2026-01-01 00:00:00"))   # old history -> no popup
        got = []
        cfg = {"log_dir_resolved": d, "log_dir": d, "poll_interval": 0.5, "live_max_age_sec": 600, "file_glob": "log-client-ev*.dat_*"}
        col = Collector(cfg, GameData(), Store(":memory:"), lambda k, p: got.append((k, p["gid"], time.time())), log=lambda *a: None)
        col.start(); time.sleep(0.3)
        t0 = time.time()
        with open(f, "ab") as fh: fh.write(rec("battle_prepare_start", 4033303))
        while not got and time.time() - t0 < 3: time.sleep(0.05)
        self.assertEqual(got[0][:2], ("prepare", 4033303)); lat = got[0][2] - t0
        self.assertLess(lat, 2.0)
        # recreate (truncate + new content)
        open(f, "wb").write(rec("battle_start", 406025, extra=',"server_battle_id":"x-1","self_team_gs":5'))
        t0 = time.time()
        while len(got) < 2 and time.time() - t0 < 3: time.sleep(0.05)
        col.stop()
        self.assertEqual(got[1][:2], ("start", 406025))
        print(f"latency prepare: {lat:.2f}s, after recreate: {got[1][2]-t0:.2f}s")

if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestNoAccess(unittest.TestCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_status_when_unreadable_then_post_session(self):
        d = tempfile.mkdtemp(); f = os.path.join(d, "log-client-ev10076.dat_000")
        open(f, "wb").write(rec("battle_end", 4033303, extra=',"server_battle_id":"s-1","self_team_gs":7,"battle_sec":90,"battle_result":4,"honor_hero_id":139'))
        os.chmod(f, 0)
        st = []
        cfg = {"log_dir_resolved": d, "log_dir": d, "file_glob": "log-client-ev*.dat_*", "live_max_age_sec": 600}
        col = Collector(cfg, GameData(), Store(":memory:"), None, log=lambda *a: None, on_status=lambda l, t: st.append((l, t)))
        col._poll_once(live=False)
        self.assertEqual(st[-1][0], "error"); self.assertIn("Нет доступа к логу", st[-1][1])
        os.chmod(f, 0o644)            # "game closed" -> post-session read works
        col._poll_once(live=False)
        self.assertEqual(st[-1][0], "ok")
        self.assertEqual(len(col.store.boss_history(3016)), 1)
