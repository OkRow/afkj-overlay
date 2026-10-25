import argparse, os, sys, time
from . import config as cfgmod
from .gamedata import GameData
from .store import Store
from .collector import Collector
from .recs import build_view, view_to_text, result_line

class _Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, s):
        for st in self.streams:
            try: st.write(s); st.flush()
            except Exception: pass
    def flush(self):
        for st in self.streams:
            try: st.flush()
            except Exception: pass
    def reconfigure(self, **kw):
        for st in self.streams:
            if hasattr(st, "reconfigure"):
                try: st.reconfigure(**kw)
                except Exception: pass


def main():
    from .paths import USER
    log_path = os.path.join(USER, "overlay.log")
    logf = open(log_path, "a", encoding="utf-8", buffering=1)
    logf.write("\n--- start %s ---\n" % __import__("datetime").datetime.now().isoformat(timespec="seconds"))
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe
        sys.stdout = logf; sys.stderr = logf
    else:
        sys.stdout = _Tee(sys.stdout, logf); sys.stderr = _Tee(sys.stderr, logf)
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    ap = argparse.ArgumentParser(prog="afkj_overlay", description="AFK Journey overlay v0.1")
    ap.add_argument("--console", action="store_true", help="без окна, вывод в консоль")
    ap.add_argument("--replay", metavar="FILE", help="проиграть сохранённый .dat_* файл")
    ap.add_argument("--demo", action="store_true", help="показать симуляцию Новика без игры")
    ap.add_argument("--db", help="путь к SQLite (по умолчанию userdata/afkj.db)")
    ap.add_argument("--delay", type=float, default=0.0, help="пауза между боевыми событиями в --replay --console")
    a = ap.parse_args()
    cfg = cfgmod.load()
    if not a.demo and not a.replay:
        from .winread import elevate_if_log_locked
        if elevate_if_log_locked(cfg):
            return
    gd = GameData(); store = Store(a.db)

    if a.console:
        def on_battle(kind, p):
            ts = p.get("ts") or ""
            if kind == "prepare":
                print(f"\n[{ts} UTC] ПОДГОТОВКА К БОЮ"); print(view_to_text(build_view(gd, store, p["gid"], cfg.get("show_only_owned", True))))
            elif kind == "start": print(f"[{ts} UTC] Бой начался: {p['info']['title']} [{p['gid']}]")
            elif kind == "end": print(f"[{ts} UTC] Конец боя: {p['info']['title']} [{p['gid']}] — {result_line(gd, p)}")
        col = Collector(cfg, gd, store, on_battle, allow_elevate=True)
        if a.demo:
            from .recs import DEMO_GID
            on_battle("prepare", {"gid": DEMO_GID, "info": gd.resolve(DEMO_GID), "ts": "demo", "data": {}})
            return
        if a.replay:
            n = col.replay(a.replay, delay=a.delay); print(f"\nПроиграно событий: {n}; база: {store.counts()}"); return
        col.start(); print("Слежу за логом… Ctrl+C — выход")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: col.stop()
        return

    from .ui import run_ui
    factory = lambda cb: Collector(cfg, gd, store, cb, allow_elevate=True)
    sys.exit(run_ui(gd, store, cfg, collector_factory=None if a.demo else factory, demo=a.demo, replay=a.replay))

if __name__ == "__main__":
    main()
