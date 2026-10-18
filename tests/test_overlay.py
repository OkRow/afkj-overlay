import glob, io, os, sys, tempfile, unittest, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from afkj_overlay.parser import parse_bytes, parse_file
from afkj_overlay.gamedata import GameData
from afkj_overlay.store import Store
from afkj_overlay.collector import Collector
from afkj_overlay.recs import build_view, view_to_text

ATT = os.environ.get("AFKJ_TEST_DIR", "/home/box/agent-data/agents/58fade04-a04d-4d87-ad8e-11b011c22fa8/attachments")
def att(prefix):
    g = glob.glob(os.path.join(ATT, prefix + "*.dat_000")); return g[0] if g else None

class TestParser(unittest.TestCase):
    def test_synthetic_record(self):
        js = b'{"action_type":"battle_prepare_start","battle_group_id":4033303}'
        rec = (b"\x00\x01garbage\n\nevent_time\x12\x132026-10-18 17:13:22\x12\x14\n\nevent_name\x12\x06battle"
               b"\x12\x50\n\x0ecustom_content\x12" + bytes([len(js)]) + js + b"\x00\x00{broken")
        evs = parse_bytes(rec)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["ts"], "2026-10-18 17:13:22")
        self.assertEqual(evs[0]["event_name"], "battle")
        self.assertEqual(evs[0]["data"]["battle_group_id"], 4033303)

    def test_truncated_tail_ignored(self):
        js = b'{"action_type":"battle_start","battle_group_id":1,"x":"' + "ж".encode() + b'"}'
        whole = b"event_time\x12\x132026-10-18 00:00:00 custom_content\x12" + bytes([len(js)]) + js
        self.assertEqual(len(parse_bytes(whole)), 1)
        self.assertEqual(len(parse_bytes(whole[:-5])), 0)

    @unittest.skipUnless(att("fb0f9c0e"), "attachment missing")
    def test_small_file_prepare(self):
        evs = parse_file(att("fb0f9c0e"))
        prep = [e for e in evs if e["data"].get("action_type") == "battle_prepare_start"]
        self.assertEqual(len(evs), 35)
        self.assertEqual(prep[0]["data"]["battle_group_id"], 4033303)
        self.assertEqual(prep[0]["ts"], "2026-10-18 17:13:22")

    @unittest.skipUnless(att("efa7da96"), "attachment missing")
    def test_big_ring_file(self):
        evs = parse_file(att("efa7da96"))
        self.assertGreater(len(evs), 2000)
        self.assertTrue(all(e["ts"] for e in evs))
        acts = [e["data"].get("action_type") for e in evs]
        self.assertGreater(acts.count("battle_end"), 100)
        bi = [e for e in evs if "battle_input" in e["data"]]
        self.assertTrue(any(e["data"]["battle_group_id"] == 504047 for e in bi))

class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.gd = GameData(); self.db = tempfile.mktemp(suffix=".db"); self.st = Store(self.db)

    def test_resolve(self):
        self.assertEqual(self.gd.resolve(4033303)["bosses"], [3016])
        self.assertEqual(self.gd.resolve(406025)["bosses"], [3055, 3040])
        self.assertIn("591", self.gd.resolve(281791)["title"])
        self.assertEqual(self.gd.resolve(999999999)["title"], "Этап #999999999")
        self.assertEqual(self.gd.hero_name(139), "Сенея")

    def test_novik_view_placeholder(self):
        v = build_view(self.gd, self.st, 4033303, True)
        r = v["bosses"][0]["recs"]
        self.assertTrue(r["placeholder"])
        self.assertEqual([i["tid"] for i in r["items"]][:5], [139, 131, 132, 86, 60])
        self.assertIn("ПРИМЕР", view_to_text(v))

    @unittest.skipUnless(att("efa7da96"), "attachment missing")
    def test_replay_and_history(self):
        got = []
        cfg = {"live_max_age_sec": 600}
        col = Collector(cfg, self.gd, self.st, lambda k, p: got.append((k, p["gid"])), log=lambda *a: None)
        col.replay(att("efa7da96")); col.replay(att("fb0f9c0e"))
        kinds = [k for k, _ in got]
        self.assertIn(("prepare", 4033303), got)
        self.assertGreater(kinds.count("end"), 50)
        n_events = self.st.counts()["events"]
        col.replay(att("efa7da96"))  # dedup: nothing new
        self.assertEqual(self.st.counts()["events"], n_events)
        h = self.st.boss_history(3016)
        self.assertEqual(len(h), 2)                       # 2 own Novik attempts, replays excluded
        self.assertEqual({b["mvp"] for b in h}, {139})
        heroes = [r[0] for r in self.st.db.execute("SELECT hero_tid FROM battle_heroes")]
        self.assertIn(5016, heroes)
        v = build_view(self.gd, self.st, 4033303, True)
        top = v["bosses"][0]["recs"]["items"][0]
        self.assertEqual(top["tid"], 139)                 # Senea: example team + MVP x2

if __name__ == "__main__":
    unittest.main(verbosity=2)
