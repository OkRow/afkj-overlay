import glob, json, os
from .paths import CONFIG

DEFAULTS = {
    "log_dir": "",
    "file_glob": "log-client-ev*.dat_*",
    "poll_interval": 0.5,
    "hotkey_toggle": "<f8>",
    "opacity": 0.92,
    "pos": [60, 120],
    "show_only_owned": True,
    "top_n": 10,
    # Event timestamps in the game log are UTC. Age = now_utc - event_utc.
    # Local TZ (e.g. UTC+3) must not affect this. 600 s covers VSS lag (~5-60 s).
    "live_max_age_sec": 600,
    "hide_on_battle_start": False,
    # Asked once, on the first windowed launch: mark owned heroes now or later.
    "roster_prompted": False,
}

def autodetect_log_dir():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    cands = (glob.glob(os.path.join(home, ".limpc", "*", "alilog"))
             + glob.glob(os.path.join(home, ".limpc", "*", "*", "alilog")))
    cands = [c for c in cands if os.path.isdir(c)]
    if not cands: return ""
    return max(cands, key=lambda c: max([os.path.getmtime(p) for p in glob.glob(os.path.join(c, "*"))] or [0]))

def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        try: cfg.update(json.load(open(CONFIG, encoding="utf-8")))
        except Exception as e: print("config.json read error:", e)
    else:
        save(cfg)
    if not cfg.get("log_dir"):
        cfg["log_dir_resolved"] = autodetect_log_dir()
    else:
        cfg["log_dir_resolved"] = cfg["log_dir"]
    return cfg

def save(cfg):
    out = {k: v for k, v in cfg.items() if not k.endswith("_resolved")}
    json.dump(out, open(CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
