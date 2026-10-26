"""Roster picker: one question on first launch, then a filterable checklist."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QScrollArea, QWidget,
    QPushButton, QMessageBox, QFrame, QComboBox,
)
from . import config as cfgmod
from .gamedata import FAC_RU, FAC_ORDER, CLS_RU

NOTICE = "Герои, которых вы выставите в бой, сами добавятся в список имеющихся."
PAPER = QColor("#f4f1ea")
INK = QColor("#1c1917")
MUTED = QColor("#4a5568")


def _light_palette(ink=INK):
    pal = QPalette()
    white = QColor("#ffffff")
    highlight = QColor("#e7e2d8")
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        pal.setColor(group, QPalette.Window, PAPER)
        pal.setColor(group, QPalette.WindowText, ink)
        pal.setColor(group, QPalette.Base, white)
        pal.setColor(group, QPalette.Text, ink)
        pal.setColor(group, QPalette.Button, white)
        pal.setColor(group, QPalette.ButtonText, ink)
        pal.setColor(group, QPalette.Highlight, highlight)
        pal.setColor(group, QPalette.HighlightedText, INK)
        pal.setColor(group, QPalette.PlaceholderText, MUTED)
    return pal


def _use_light_palette(root):
    """Windows is in dark mode, and QWindows11Style then paints control text
    from the theme. That color is white. The style uses it whenever the widget
    itself has not set ButtonText/Text, even if a parent palette already says
    the text is dark. The parent's light fill still applies, so the label is
    white on white. setPalette on every widget marks those brushes as set.
    """
    pal = _light_palette()
    widgets = [root, *root.findChildren(QWidget)]
    for box in root.findChildren(QComboBox):
        widgets.append(box.view())
    for w in widgets:
        w.setStyleSheet("")
        w.setPalette(pal)
    root.setAutoFillBackground(True)
    for area in root.findChildren(QScrollArea):
        area.setAutoFillBackground(True)
        area.viewport().setAutoFillBackground(True)
        area.viewport().setPalette(pal)


def prompt_roster(gd, cfg, parent=None):
    """Ask once. The checklist is its own window, so closing it leaves the overlay up."""
    if cfg.get("roster_prompted"):
        return False
    if not _ask_now(parent):
        _mark_asked(cfg)
        return False
    dlg = RosterDialog(gd)
    saved = dlg.exec() == QDialog.Accepted
    _mark_asked(cfg)
    return saved


def _ask_now(parent):
    # Not a child of the translucent tool window: that pairing kills the process on Windows.
    box = QMessageBox(None)
    box.setAttribute(Qt.WA_QuitOnClose, False)
    box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    box.setWindowTitle("Ваши герои")
    box.setText("Отметить героев, которые у вас уже есть?")
    box.setInformativeText(NOTICE)
    now = box.addButton("Сейчас", QMessageBox.AcceptRole)
    box.addButton("Позже", QMessageBox.RejectRole)
    box.setDefaultButton(now)
    _use_light_palette(box)
    box.exec()
    return box.clickedButton() is now


def _mark_asked(cfg):
    cfg["roster_prompted"] = True
    cfgmod.save(cfg)


class RosterDialog(QDialog):
    def __init__(self, gd, parent=None):
        # The overlay is a translucent tool window. Parenting this dialog to it
        # crashes python312.dll as soon as the window is created.
        del parent
        self._build(gd)

    def _build(self, gd):
        super().__init__(None)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_NoSystemBackground, False)
        self.gd = gd
        self.setWindowTitle("Ваши герои")
        self.resize(480, 680)
        self.boxes = {}
        self.rows = []          # (tid, checkbox, faction, class, header)
        self.headers = {}
        owned = gd.owned()
        self._seen = set(owned)
        root = QVBoxLayout(self)
        title = QLabel("Отметьте героев, которые у вас есть")
        font = QFont(title.font())
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        note = QLabel(NOTICE)
        note.setWordWrap(True)
        root.addWidget(note)

        filters = QHBoxLayout()
        self.fac_box = QComboBox()
        self.fac_box.addItem("Все фракции", "")
        for fac in FAC_ORDER:
            self.fac_box.addItem(FAC_RU.get(fac, fac), fac)
        self.cls_box = QComboBox()
        self.cls_box.addItem("Все классы", "")
        for cls, ru in CLS_RU.items():
            self.cls_box.addItem(ru, cls)
        self.fac_box.currentIndexChanged.connect(self._apply_filter)
        self.cls_box.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.fac_box, 1)
        filters.addWidget(self.cls_box, 1)
        root.addLayout(filters)

        mark = QHBoxLayout()
        on = QPushButton("Отметить показанных")
        off = QPushButton("Снять отметку с показанных")
        on.clicked.connect(lambda: self._set_visible(True))
        off.clicked.connect(lambda: self._set_visible(False))
        mark.addWidget(on)
        mark.addWidget(off)
        root.addLayout(mark)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 8, 4)
        for fac, rows in gd.playable_by_faction():
            head = QLabel(FAC_RU.get(fac, fac or "Без фракции"))
            head_font = QFont(head.font())
            head_font.setBold(True)
            head.setFont(head_font)
            lay.addWidget(head)
            self.headers[fac] = head
            for tid, h in rows:
                name = h.get("ru") or h.get("en") or str(tid)
                cls = h.get("cls") or ""
                cls_ru = CLS_RU.get(cls, cls)
                box = QCheckBox(f"{name}  ·  {cls_ru}" if cls_ru else name)
                box.setChecked(tid in owned)
                self.boxes[tid] = box
                self.rows.append((tid, box, fac, cls, head))
                lay.addWidget(box)
        lay.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        bar = QHBoxLayout()
        self.count = QLabel()
        bar.addWidget(self.count, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        save = QPushButton("Сохранить")
        save.setDefault(True)
        save.clicked.connect(self._save)
        bar.addWidget(close)
        bar.addWidget(save)
        root.addLayout(bar)
        for box in self.boxes.values():
            box.toggled.connect(self._refresh_count)
        _use_light_palette(self)
        note.setPalette(_light_palette(MUTED))
        self._refresh_count()

    def _match(self, fac, cls):
        want_fac = self.fac_box.currentData() or ""
        want_cls = self.cls_box.currentData() or ""
        return (not want_fac or fac == want_fac) and (not want_cls or cls == want_cls)

    def _apply_filter(self):
        visible_heads = set()
        for _tid, box, fac, cls, head in self.rows:
            show = self._match(fac, cls)
            box.setVisible(show)
            if show:
                visible_heads.add(head)
        for head in self.headers.values():
            head.setVisible(head in visible_heads)
        self._refresh_count()

    def _set_visible(self, checked):
        for _tid, box, fac, cls, _head in self.rows:
            if self._match(fac, cls):
                box.setChecked(checked)
        self._refresh_count()

    def _refresh_count(self):
        n = sum(1 for b in self.boxes.values() if b.isChecked())
        shown = sum(1 for _t, box, fac, cls, _h in self.rows if self._match(fac, cls))
        self.count.setText(f"Отмечено: {n} · показано: {shown}")

    def _save(self):
        chosen = [tid for tid, box in self.boxes.items() if box.isChecked()]
        self.gd.set_owned(chosen, snapshot=self._seen)
        self.accept()
