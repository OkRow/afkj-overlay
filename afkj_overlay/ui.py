import html, os, sys
from PySide6.QtCore import Qt, QObject, Signal, QPoint, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap, QIcon, QAction, QFont, QBrush, QPen
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
                               QSystemTrayIcon, QMenu)
from . import config as cfgmod
from .recs import build_view, result_line, fmt_num

CSS = """
QWidget#panel { color: #e8e6e3; }
QLabel { color: #e8e6e3; }
QPushButton { background: rgba(255,255,255,0.08); color: #e8e6e3; border: 1px solid rgba(255,255,255,0.18);
              border-radius: 6px; padding: 3px 9px; }
QPushButton:checked { background: rgba(120,170,255,0.35); }
QPushButton#close { border: none; background: transparent; font-size: 15px; padding: 0 4px; }
"""

class Bridge(QObject):
    battle = Signal(str, dict)
    toggle = Signal()
    status = Signal(str, str)

class Overlay(QWidget):
    def __init__(self, gd, store, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.gd, self.store, self.cfg = gd, store, cfg
        self.setObjectName("panel"); self.setAttribute(Qt.WA_TranslucentBackground); self.setStyleSheet(CSS)
        self.setWindowOpacity(float(cfg.get("opacity", 0.92)))
        self.only_owned = bool(cfg.get("show_only_owned", True))
        self.gid = None; self.mini = False; self._drag = None; self._result = ""
        self.status = ("warn", "Запуск…")
        f = QFont("Segoe UI" if sys.platform == "win32" else "DejaVu Sans", 10); self.setFont(f)
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 10, 14, 12); lay.setSpacing(6)
        top = QHBoxLayout()
        self.head = QLabel(); self.head.setTextFormat(Qt.RichText); self.head.setWordWrap(True)
        top.addWidget(self.head, 1)
        self.btn_owned = QPushButton("Только мои"); self.btn_owned.setCheckable(True); self.btn_owned.setChecked(self.only_owned)
        self.btn_owned.clicked.connect(self._toggle_owned); top.addWidget(self.btn_owned, 0, Qt.AlignTop)
        b = QPushButton("×"); b.setObjectName("close"); b.clicked.connect(self.hide); top.addWidget(b, 0, Qt.AlignTop)
        lay.addLayout(top)
        self.body = QLabel(); self.body.setTextFormat(Qt.RichText); self.body.setWordWrap(True)
        lay.addWidget(self.body)
        self.foot = QLabel(); self.foot.setTextFormat(Qt.RichText); self.foot.setWordWrap(True)
        lay.addWidget(self.foot)
        self.setFixedWidth(430)
        x, y = cfg.get("pos", [60, 120]); self.move(int(x), int(y))

    # ---- painting / dragging ----
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(18, 20, 28, 225))); p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
    def mouseMoveEvent(self, e):
        if self._drag is not None: self.move(e.globalPosition().toPoint() - self._drag)
    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None; self.cfg["pos"] = [self.x(), self.y()]; cfgmod.save(self.cfg)
    def mouseDoubleClickEvent(self, e):
        if self.mini: self.set_mini(False)

    def _toggle_owned(self):
        self.only_owned = self.btn_owned.isChecked()
        self.btn_owned.setText("Только мои" if self.only_owned else "Все герои")
        self.cfg["show_only_owned"] = self.only_owned; cfgmod.save(self.cfg)
        if self.gid: self.show_group(self.gid, keep_result=True)

    # ---- content ----
    def show_group(self, gid, keep_result=False):
        self.gid = gid; v = build_view(self.gd, self.store, gid, self.only_owned, int(self.cfg.get("top_n", 10)))
        self.view = v
        if not keep_result: self._result = ""
        e = html.escape
        names = " + ".join(b["name"] for b in v["bosses"]) or v["title"]
        self.head.setText(f"<div style='font-size:16px;font-weight:600'>{e(names)}</div>"
                          f"<div style='color:#9fb3c8'>{e(v['title'])} · {e(v['mode'])}"
                          + (f" · <b>{e(v['difficulty'])}</b>" if v.get("difficulty") else "") + "</div>")
        parts = []
        for b in v["bosses"]:
            if len(v["bosses"]) > 1: parts.append(f"<div style='margin-top:4px;font-weight:600'>{e(b['name'])}</div>")
            parts.append(f"<div style='color:#c9c9c9'>{e(b['cls'])} · {e(b['dmg'])}</div>")
            if b["desc"]: parts.append(f"<div style='color:#b8b8b8'>{e(b['desc'])}</div>")
            if b["tags"]:
                parts.append("<div>" + " ".join(f"<span style='background:#2c4a6e;color:#dfefff'>&nbsp;{e(t)}&nbsp;</span>" for t in b["tags"]) + "</div>")
            r = b["recs"]
            badge = " <span style='background:#a8551b;color:#fff'>&nbsp;ПРИМЕР — тестовые данные&nbsp;</span>" if r["placeholder"] else ""
            parts.append(f"<div style='margin-top:6px;font-weight:600'>Рекомендуемые герои{badge}</div>")
            if r["items"]:
                rows = []
                for i, it in enumerate(r["items"], 1):
                    col = "#ffffff" if it["owned"] else "#8a8a8a"
                    rows.append(f"<tr><td style='color:#7f8ea3'>{i}.</td><td style='color:{col};font-weight:600'>{e(it['name'])}</td>"
                                f"<td style='color:#9fb3c8'>&nbsp;{e(it['cls'])}</td></tr>"
                                f"<tr><td></td><td colspan='2' style='color:#a0a0a0;font-size:11px'>{e('; '.join(it['reasons']))}"
                                + ("" if it["owned"] else " · нет у вас") + "</td></tr>")
                parts.append("<table cellspacing='1'>" + "".join(rows) + "</table>")
            else:
                parts.append("<div style='color:#a0a0a0'>Нет данных. Добавьте составы в data/manual_recs.json "
                             + ("или переключите «Все герои»" if self.only_owned else "") + "</div>")
            if r["history_n"]:
                mv = ", ".join(f"{k} ×{n}" for k, n in r["mvp_counts"].items())
                parts.append(f"<div style='color:#9fb3c8'>Ваших боёв: {r['history_n']}" + (f" · MVP: {e(mv)}" if mv else "")
                             + (f" · рекорд урона {fmt_num(r['best']['damage'])}" if r["best"] else "") + "</div>")
        if not v["bosses"]:
            parts.append("<div style='color:#b8b8b8'>Босс не определён для этого этапа. Состав врагов обычных этапов — в следующих версиях.</div>")
        self.body.setText("".join(parts))
        self._set_foot()
        self.set_mini(False); self.show(); self.raise_()

    def set_status(self, level, text):
        self.status = (level, text)
        if self.gid is None and self.isVisible(): self._show_waiting()
        else: self._set_foot()
        if level == "error" and not self.isVisible(): self._show_waiting()

    def _status_html(self):
        level, text = self.status
        col = {"ok": "#7fd18b", "warn": "#ffd479", "error": "#ff7b72"}.get(level, "#cccccc")
        return f"<div style='color:{col};font-size:11px'>● {html.escape(text)}</div>"

    def _show_waiting(self):
        self.head.setText("<b>AFKJ Overlay</b>")
        level, _ = self.status
        self.body.setText("Ожидание подготовки к бою…" if level == "ok" else
                          ("<span style='color:#ff7b72;font-weight:600'>Нет данных из игры.</span> См. статус ниже."
                           if level == "error" else "Ожидание…"))
        self.body.setVisible(True); self._set_foot(); self.adjustSize(); self.show(); self.raise_()

    def _set_foot(self):
        t = f"<div style='color:#ffd479;font-weight:600'>{html.escape(self._result)}</div>" if self._result else ""
        if self.status[0] != "ok" or self.gid is None: t += self._status_html()
        self.foot.setText(t + "<div style='color:#6c7a8a;font-size:10px'>F8 — показать/скрыть · перетаскивайте мышью"
                         + (" · двойной клик — развернуть" if self.mini else "") + "</div>")

    def set_mini(self, mini):
        self.mini = mini; self.body.setVisible(not mini); self.btn_owned.setVisible(not mini)
        self._set_foot(); self.adjustSize(); self.resize(self.width(), self.sizeHint().height())

    def on_battle(self, kind, p):
        if kind == "prepare":
            self.show_group(p["gid"])
        elif kind == "start":
            if self.gid != p["gid"]: self.show_group(p["gid"])
            if self.cfg.get("hide_on_battle_start"): self.hide()
            else:
                self._result = "Бой идёт…"; self._set_foot(); self.set_mini(True)
        elif kind == "end":
            if self.gid != p["gid"]: self.show_group(p["gid"])
            self._result = result_line(self.gd, p); self.show_group(p["gid"], keep_result=True); self.set_mini(True); self.show()

    def toggle(self):
        if self.isVisible(): self.hide()
        else:
            if self.gid is None: self._show_waiting()
            self.show(); self.raise_()

def make_icon():
    pm = QPixmap(64, 64); pm.fill(Qt.transparent); p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(60, 120, 220)); p.setPen(Qt.NoPen); p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor(255, 255, 255)); f = QFont(); f.setBold(True); f.setPointSize(22); p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "A"); p.end(); return QIcon(pm)

from .recs import DEMO_GID

def demo_sequence(bridge, gd, delays=(0, 12000, 16000)):
    """Simulate Novik: prepare -> start -> end."""
    base = {"battle_group_id": DEMO_GID}
    info = gd.resolve(DEMO_GID)
    QTimer.singleShot(delays[0], lambda: bridge.battle.emit("prepare", {"gid": DEMO_GID, "info": info, "data": {**base, "action_type": "battle_prepare_start"}}))
    QTimer.singleShot(delays[1], lambda: bridge.battle.emit("start", {"gid": DEMO_GID, "info": info, "data": {**base, "action_type": "battle_start"}}))
    QTimer.singleShot(delays[2], lambda: bridge.battle.emit("end", {"gid": DEMO_GID, "info": info, "data": {**base, "action_type": "battle_end",
                      "battle_result": 4, "battle_sec": 90, "honor_hero_id": 139}}))

def run_ui(gd, store, cfg, collector_factory=None, demo=False, replay=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bridge = Bridge(); ov = Overlay(gd, store, cfg)
    bridge.battle.connect(ov.on_battle); bridge.toggle.connect(ov.toggle); bridge.status.connect(ov.set_status)
    from .paths import ROOT
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(make_icon(), app); menu = QMenu()
        for text, fn in (("Показать/Скрыть", ov.toggle), ("Тест (Новик)", lambda: demo_sequence(bridge, gd)),
                         ("Открыть папку данных", lambda: os.startfile(ROOT) if hasattr(os, "startfile") else None),
                         ("Выход", app.quit)):
            a = QAction(text, menu); a.triggered.connect(fn); menu.addAction(a)
        tray.setContextMenu(menu); tray.setToolTip("AFKJ Overlay"); tray.show(); app._tray = (tray, menu)
    from .hotkeys import start_hotkeys
    app._hk = start_hotkeys({cfg.get("hotkey_toggle", "<f8>"): lambda: bridge.toggle.emit()})
    col = None
    if collector_factory:
        col = collector_factory(lambda k, p: bridge.battle.emit(k, p))
        col.on_status = lambda lv, t: bridge.status.emit(lv, t)
        if replay:
            import threading
            threading.Thread(target=lambda: col.replay(replay, delay=1.5), daemon=True).start()
        else:
            col.start()
    if demo:
        ov.status = ("ok", "Демо-режим (без игры)"); demo_sequence(bridge, gd)
    else:
        ov.toggle()  # small "waiting" panel so the user sees it's alive
    app.aboutToQuit.connect(lambda: col.stop() if col else None)
    return app.exec()
