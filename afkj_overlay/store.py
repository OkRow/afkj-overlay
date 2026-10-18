import json, os, sqlite3, threading
from .paths import USER

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, key TEXT UNIQUE, ts_utc TEXT, event_name TEXT,
  action_type TEXT, battle_group_id INTEGER, raw_json TEXT, src_file TEXT);
CREATE TABLE IF NOT EXISTS battles(server_battle_id TEXT PRIMARY KEY, group_id INTEGER, mode INTEGER, title TEXT,
  boss_tid INTEGER, ts_start TEXT, ts_end TEXT, result INTEGER, secs REAL, self_gs INTEGER, oppo_gs INTEGER,
  damage INTEGER, mvp_tid INTEGER, formation_id INTEGER, is_replay INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS battle_heroes(server_battle_id TEXT, slot INTEGER, hero_tid INTEGER, level INTEGER,
  quality INTEGER, ex_level INTEGER, PRIMARY KEY(server_battle_id, hero_tid));
CREATE TABLE IF NOT EXISTS roster(hero_tid INTEGER PRIMARY KEY, owned INTEGER, source TEXT, level INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS overrides(kind TEXT, id INTEGER, name TEXT, PRIMARY KEY(kind, id));
"""

class Store:
    def __init__(self, path=None):
        self.path = path or os.path.join(USER, "afkj.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA); self.db.commit()

    def add_event(self, ev, src):
        d = ev["data"]
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO events(key,ts_utc,event_name,action_type,battle_group_id,raw_json,src_file) VALUES(?,?,?,?,?,?,?)",
                                  (ev["key"], ev["ts"], ev["event_name"], d.get("action_type"), d.get("battle_group_id"), ev["raw"], src))
            return cur.rowcount == 1

    def commit(self):
        with self.lock: self.db.commit()

    def upsert_battle(self, sid, **kw):
        if not sid: return
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO battles(server_battle_id) VALUES(?)", (sid,))
            kw = {k: v for k, v in kw.items() if v is not None}
            if kw:
                self.db.execute(f"UPDATE battles SET {', '.join(k+'=?' for k in kw)} WHERE server_battle_id=?", (*kw.values(), sid))
            self.db.commit()

    def add_heroes(self, sid, heroes):
        with self.lock:
            for i, h in enumerate(heroes):
                self.db.execute("INSERT OR REPLACE INTO battle_heroes VALUES(?,?,?,?,?,?)",
                                (sid, i, h.get("tid"), h.get("level"), h.get("quality"), h.get("exclusiveLevel")))
            self.db.commit()

    def boss_history(self, boss_tid):
        with self.lock:
            rows = self.db.execute("SELECT server_battle_id,result,secs,damage,mvp_tid,self_gs,ts_end FROM battles "
                                   "WHERE boss_tid=? AND is_replay=0 AND ts_end IS NOT NULL", (boss_tid,)).fetchall()
            out = []
            for sid, res, secs, dmg, mvp, gs, ts in rows:
                heroes = [r[0] for r in self.db.execute("SELECT hero_tid FROM battle_heroes WHERE server_battle_id=? ORDER BY slot", (sid,))]
                out.append({"sid": sid, "result": res, "secs": secs, "damage": dmg, "mvp": mvp, "gs": gs, "ts": ts, "heroes": heroes})
            return out

    def counts(self):
        with self.lock:
            return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("events", "battles", "battle_heroes")}
