"""Roster checklist: grouping, save, and the one-time prompt. No real roster files are written."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
from afkj_overlay.gamedata import GameData, FAC_ORDER
from afkj_overlay import roster_ui


class TestRosterData(unittest.TestCase):
    def test_groups_cover_every_playable_hero(self):
        gd = GameData()
        groups = gd.playable_by_faction()
        self.assertEqual([f for f, _ in groups], [f for f in FAC_ORDER if any(f == g[0] for g in groups)])
        n = sum(len(rows) for _, rows in groups)
        self.assertEqual(n, sum(1 for h in gd.heroes.values() if h.get("hero")))
        self.assertGreater(n, 100)
        for _, rows in groups:
            names = [h.get("ru") or "" for _, h in rows]
            self.assertEqual(names, sorted(names, key=str.casefold))

    def test_set_owned_writes_only_the_redirected_files(self):
        gd = GameData()
        with tempfile.TemporaryDirectory() as d:
            roster = os.path.join(d, "roster.json")
            auto = os.path.join(d, "roster_auto.json")
            gd.roster_base = {"_readme": "keep", "owned": [1, 999999]}
            gd.roster_auto = {"owned": [1, 2]}
            gd._roster_path = roster
            gd._auto_path = auto
            chosen = gd.set_owned([1, 2, "nope"])
            self.assertEqual(chosen, [1, 2])
            saved = json.load(open(roster, encoding="utf-8"))
            self.assertEqual(saved["owned"], [1, 2])
            self.assertEqual(saved["_readme"], "keep")
            self.assertEqual(json.load(open(auto, encoding="utf-8"))["owned"], [1, 2])
            gd.roster_auto = {"owned": [1, 2]}
            gd.set_owned([1])
            self.assertEqual(json.load(open(auto, encoding="utf-8"))["owned"], [1])
            self.assertEqual(gd.owned(), {1})
            gd.roster_base = {"owned": [1]}
            gd.roster_auto = {"owned": [2, 3]}
            gd.set_owned([1], snapshot={1, 2})
            self.assertEqual(json.load(open(auto, encoding="utf-8"))["owned"], [3])
            self.assertEqual(gd.owned(), {1, 3})

    def test_player_checklist_is_not_the_tracked_sample(self):
        from afkj_overlay.paths import DATA, USER
        gd = GameData()
        self.assertEqual(os.path.normcase(gd._roster_path), os.path.normcase(os.path.join(USER, "roster.json")))
        self.assertNotEqual(os.path.normcase(gd._roster_path), os.path.normcase(os.path.join(DATA, "roster.json")))


class TestRosterDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_checklist_prefills_owned_and_saves_selection(self):
        gd = GameData()
        with tempfile.TemporaryDirectory() as d:
            gd._roster_path = os.path.join(d, "roster.json")
            gd._auto_path = os.path.join(d, "auto.json")
            gd.roster_base = {"owned": []}
            gd.roster_auto = {"owned": []}
            dlg = roster_ui.RosterDialog(gd, None)
            self.assertEqual(len(dlg.boxes), sum(1 for h in gd.heroes.values() if h.get("hero")))
            texts = [w.text() for w in dlg.findChildren(QLabel)]
            self.assertIn("Светоносные", texts)
            self.assertIn("Порождения могил", texts)
            self.assertIn(roster_ui.NOTICE, texts)
            labels = [b.text() for b in dlg.boxes.values()]
            self.assertTrue(any(t.startswith("Вален") for t in labels))
            self.assertTrue(any("Танк" in t or "Маг" in t for t in labels))
            for box in dlg.boxes.values():
                box.setChecked(False)
            dlg.boxes[1].setChecked(True)
            dlg.boxes[60].setChecked(True)
            dlg._save()
            saved = json.load(open(gd._roster_path, encoding="utf-8"))
            self.assertEqual(saved["owned"], [1, 60])
            dlg.close()

    def test_filter_marks_only_matching_heroes(self):
        gd = GameData()
        dlg = roster_ui.RosterDialog(gd, None)
        for box in dlg.boxes.values():
            box.setChecked(False)
        fac_i = dlg.fac_box.findData("Lightbearer")
        cls_i = dlg.cls_box.findData("Rogue")
        self.assertGreaterEqual(fac_i, 0)
        self.assertGreaterEqual(cls_i, 0)
        dlg.show()
        dlg.fac_box.setCurrentIndex(fac_i)
        dlg.cls_box.setCurrentIndex(cls_i)
        shown = [(tid, box) for tid, box, fac, cls, _h in dlg.rows if dlg._match(fac, cls)]
        self.assertGreater(len(shown), 0)
        self.assertIn(f"показано: {len(shown)}", dlg.count.text())
        self.assertIn("Снять отметку с показанных", [b.text() for b in dlg.findChildren(roster_ui.QPushButton)])
        self.assertTrue(all(box.isVisible() for _tid, box in shown))
        self.assertFalse(dlg.boxes[1].isVisible())  # Вален, светоносный воин
        dlg._set_visible(True)
        self.assertTrue(all(box.isChecked() for _tid, box in shown))
        self.assertFalse(dlg.boxes[1].isChecked())
        dlg.close()

    def test_dialog_client_area_is_opaque(self):
        gd = GameData()
        dlg = roster_ui.RosterDialog(gd)
        dlg.show()
        self.app.processEvents()
        img = dlg.grab().toImage()
        self.assertFalse(img.isNull())
        self.assertGreater(img.width(), 100)
        paper = ink = 0
        for y in range(0, img.height(), 12):
            for x in range(8, img.width(), 16):
                c = img.pixelColor(x, y)
                self.assertEqual(c.alpha(), 255)
                total = c.red() + c.green() + c.blue()
                if total > 600:
                    paper += 1
                elif total < 200:
                    ink += 1
        self.assertGreater(paper, 20)
        self.assertGreater(ink, 5)
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QPushButton
        for btn in dlg.findChildren(QPushButton):
            self.assertTrue(btn.palette().isBrushSet(QPalette.Active, QPalette.ButtonText))
            self.assertEqual(btn.styleSheet(), "")
        for box in (dlg.fac_box, dlg.cls_box):
            self.assertTrue(box.palette().isBrushSet(QPalette.Active, QPalette.Text))
            self.assertEqual(box.styleSheet(), "")
        combo = dlg.fac_box.grab().toImage()
        light = dark = 0
        for y in range(combo.height()):
            for x in range(combo.width()):
                c = combo.pixelColor(x, y)
                self.assertGreater(c.alpha(), 200)
                total = c.red() + c.green() + c.blue()
                if total > 680:
                    light += 1
                elif c.red() < 60 and c.green() < 60 and c.blue() < 60:
                    dark += 1
        self.assertGreater(light, 10)
        self.assertGreater(dark, 8)
        dlg.close()

    def test_roster_button_opens_a_separate_window(self):
        from afkj_overlay.ui import Overlay
        gd = GameData()
        ov = Overlay(gd, None, {"opacity": 1, "pos": [0, 0], "show_only_owned": True})
        ov.show()
        ov.pick_roster()
        for _ in range(20):
            self.app.processEvents()
            if ov._roster_dlg is not None:
                break
        dlg = ov._roster_dlg
        self.assertIsNotNone(dlg)
        self.assertIsNone(dlg.parent())
        self.assertTrue(ov.isVisible())
        self.assertGreater(len(dlg.boxes), 100)
        ov.set_mini(True)
        self.assertFalse(ov.btn_roster.isVisible())
        self.assertFalse(ov.btn_owned.isVisible())
        ov.set_mini(False)
        self.assertTrue(ov.btn_roster.isVisible())
        dlg.reject()
        self.app.processEvents()
        self.assertIsNone(ov._roster_dlg)
        self.assertTrue(ov.isVisible())
        ov.close()

    def test_later_marks_prompt_without_opening_checklist(self):
        cfg = {"roster_prompted": False, "log_dir": "x"}
        written = {}

        def fake_save(c):
            written.update(c)

        orig = roster_ui.cfgmod.save
        orig_ask = roster_ui._ask_now
        roster_ui.cfgmod.save = fake_save
        roster_ui._ask_now = lambda parent=None: False
        try:
            self.assertFalse(roster_ui.prompt_roster(GameData(), cfg))
        finally:
            roster_ui.cfgmod.save = orig
            roster_ui._ask_now = orig_ask
        self.assertTrue(cfg["roster_prompted"])
        self.assertTrue(written["roster_prompted"])
        self.assertEqual(written["log_dir"], "x")

    def test_question_mentions_battles(self):
        box = QMessageBox()
        box.setInformativeText(roster_ui.NOTICE)
        self.assertIn("бой", box.informativeText())


if __name__ == "__main__":
    unittest.main()
