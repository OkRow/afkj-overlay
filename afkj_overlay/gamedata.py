import json, os, tempfile, threading
from .paths import DATA, USER

def _j(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p): return default
    return json.load(open(p, encoding="utf-8"))

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return default
    return data if isinstance(data, dict) else default

def _dump_json(path, obj, indent=None):
    # Collector and the checklist both rewrite these files. A direct "w" can tear the JSON.
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roster-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try: os.remove(tmp)
            except OSError: pass

FAC_RU = {"Lightbearer": "Светоносные", "Wilder": "Дикари", "Mauler": "Громилы", "Graveborn": "Порождения могил",
          "Celestial": "Небожители", "Hypogean": "Подземные", "Dimensional": "Измерения", "Boss": "Босс"}
CLS_RU = {"Tank": "Танк", "Warrior": "Воин", "Mage": "Маг", "Support": "Поддержка", "Marksman": "Стрелок", "Rogue": "Разбойник"}
DMG_RU = {"AD": "физ. урон", "AP": "маг. урон"}
RESULT_RU = {1: "Победа", 2: "Поражение", 4: "Бой завершён (на урон)"}
FAC_ORDER = ["Lightbearer", "Wilder", "Mauler", "Graveborn", "Celestial", "Hypogean", "Dimensional"]

class GameData:
    def __init__(self):
        self.heroes = _j("heroes.json", {})
        self.bosses = _j("bosses.json", {})
        self.boss_groups = _j("boss_groups.json", {})
        gt = _j("group_titles.json", {"strings": [], "groups": {}})
        self._s, self._g = gt["strings"], gt["groups"]
        self.manual = _j("manual_recs.json", {"bosses": {}})
        # The sample in data/ stays in git. The player's checks live next to roster_auto.
        self._roster_path = os.path.join(USER, "roster.json")
        self._auto_path = os.path.join(USER, "roster_auto.json")
        self._roster_lock = threading.Lock()
        if os.path.exists(self._roster_path):
            self.roster_base = _read_json(self._roster_path, {"owned": []})
        else:
            self.roster_base = _j("roster.json", {"owned": []})
        self.roster_auto = _read_json(self._auto_path, {"owned": []}) if os.path.exists(self._auto_path) else {"owned": []}
        self._name2tid = {}
        for tid, h in self.heroes.items():
            if h.get("hero"):
                for n in (h.get("ru"), h.get("en")):
                    if n: self._name2tid.setdefault(n.lower(), int(tid))

    # heroes
    def hero_name(self, tid):
        h = self.heroes.get(str(tid)); return (h.get("ru") or h.get("en")) if h else f"Герой #{tid}"
    def hero(self, tid): return self.heroes.get(str(tid), {})
    def to_tid(self, x):
        if isinstance(x, int): return x
        if isinstance(x, str) and x.isdigit(): return int(x)
        return self._name2tid.get(str(x).lower())

    # roster
    def owned(self):
        return set(int(x) for x in self.roster_base.get("owned", [])) | set(int(x) for x in self.roster_auto.get("owned", []))
    def add_owned(self, tids):
        with self._roster_lock:
            new = [t for t in tids if t and t not in self.owned() and self.hero(t).get("hero")]
            if not new:
                return new
            doc = dict(self.roster_auto)
            doc["owned"] = sorted(set(doc.get("owned", [])) | set(new))
            _dump_json(self._auto_path, doc)
            self.roster_auto = doc
            return new

    def playable_by_faction(self):
        buckets = {}
        for tid, h in self.heroes.items():
            if not h.get("hero"):
                continue
            buckets.setdefault(h.get("fac") or "", []).append((int(tid), h))
        order = [f for f in FAC_ORDER if f in buckets]
        order += [f for f in buckets if f not in order]
        out = []
        for fac in order:
            rows = sorted(buckets[fac], key=lambda it: (it[1].get("ru") or it[1].get("en") or "").casefold())
            out.append((fac, rows))
        return out

    def set_owned(self, tids, snapshot=None):
        """Write the checked heroes to the player file.

        Auto ids are removed only when `snapshot` contained them and the player
        left them unchecked. Ids the collector appended after the dialog opened
        stay. `snapshot=None` drops every unchecked auto id.
        """
        with self._roster_lock:
            chosen = []
            for t in tids:
                try:
                    tid = int(t)
                except (TypeError, ValueError):
                    continue
                if self.hero(tid).get("hero"):
                    chosen.append(tid)
            chosen = sorted(set(chosen))
            base = dict(self.roster_base)
            base["owned"] = chosen
            base["source"] = "отмечено в оверлее"
            keep = set(chosen)
            auto_ids = []
            for t in self.roster_auto.get("owned", []):
                try:
                    auto_ids.append(int(t))
                except (TypeError, ValueError):
                    continue
            if snapshot is None:
                kept_auto = [t for t in auto_ids if t in keep]
            else:
                seen = set()
                for t in snapshot:
                    try:
                        seen.add(int(t))
                    except (TypeError, ValueError):
                        continue
                kept_auto = [t for t in auto_ids if t in keep or t not in seen]
            auto = dict(self.roster_auto)
            auto["owned"] = sorted(set(kept_auto))
            _dump_json(self._auto_path, auto)
            self.roster_auto = auto
            _dump_json(self._roster_path, base, indent=2)
            self.roster_base = base
            return chosen

    # groups
    def title(self, gid):
        g = self._g.get(str(gid))
        if not g: return None
        mode, t_ru, t_en, num = self._s[g[0]], self._s[g[1]], self._s[g[2]], g[3]
        t = t_ru or t_en or mode
        return f"{t} {num}".strip() if num else t
    def resolve(self, gid):
        """-> dict(gid, title, kind, bosses:[tid], level_ru, boss_group, ...)"""
        gid = int(gid)
        info = {"gid": gid, "title": self.title(gid) or f"Этап #{gid}", "kind": None, "bosses": []}
        bg = self.boss_groups.get(str(gid))
        if bg: info.update(bg)
        return info
    def boss(self, tid): return self.bosses.get(str(tid), {})
    def boss_tags(self, tid, boss_group=None):
        tags = self.boss(tid).get("tags", {})
        if boss_group and str(boss_group) in tags: return tags[str(boss_group)]
        for v in tags.values():
            if v: return v
        return []
