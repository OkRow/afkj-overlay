"""Render the overlay panel for the Novik demo to screenshot_demo.png (works with QT_QPA_PLATFORM=offscreen)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor
from afkj_overlay import config as cfgmod
from afkj_overlay.gamedata import GameData
from afkj_overlay.store import Store
from afkj_overlay.ui import Overlay
from afkj_overlay.recs import DEMO_GID

app = QApplication(sys.argv)
cfg = cfgmod.load(); gd = GameData(); st = Store(sys.argv[2] if len(sys.argv) > 2 else ":memory:")
ov = Overlay(gd, st, cfg); ov.show_group(DEMO_GID); app.processEvents(); ov.adjustSize(); app.processEvents()
img = ov.grab()
bg = QPixmap(img.width() + 40, img.height() + 40); bg.fill(QColor(70, 90, 70))  # fake game background
p = QPainter(bg); p.drawPixmap(20, 20, img); p.end()
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshot_demo.png")
bg.save(out); print("saved", out, bg.width(), bg.height())
# also mini/result state
ov.on_battle("end", {"gid": DEMO_GID, "info": gd.resolve(DEMO_GID), "data": {"battle_result": 4, "battle_sec": 90, "honor_hero_id": 139}})
app.processEvents(); ov.adjustSize(); ov.grab().save(out.replace(".png", "_end.png")); print("saved", out.replace(".png", "_end.png"))
