import json, os
from .paths import DATA, USER

def _j(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p): return default
    return json.load(open(p, encoding="utf-8"))

FAC_RU = {"Lightbearer": "Светоносные", "Wilder": "Дикари", "Mauler": "Громилы", "Graveborn": "Порождения могил",
          "Celestial": "Небожители", "Hypogean": "Подземные", "Dimensional": "Измерения", "Boss": "Босс"}
CLS_RU = {"Tank": "Танк", "Warrior": "Воин", "Mage": "Маг", "Support": "Поддержка", "Marksman": "Стрелок", "Rogue": "Разбойник"}
DMG_RU = {"AD": "физ. урон", "AP": "маг. урон"}
RESULT_RU = {1: "Победа", 2: "Поражение", 4: "Бой завершён (на урон)"}

class GameData:
    def __init__(self):
        self.heroes = _j("heroes.json", {})
        self.bosses = _j("bosses.json", {})
        self.boss_groups = _j("boss_groups.json", {})
        gt = _j("group_titles.json", {"strings": [], "groups": {}})
        self._s, self._g = gt["strings"], gt["groups"]
        self.manual = _j("manual_recs.json", {"bosses": {}})
        self.roster_base = _j("roster.json", {"owned": []})
        self._auto_path = os.path.join(USER, "roster_auto.json")
        self.roster_auto = json.load(open(self._auto_path, encoding="utf-8")) if os.path.exists(self._auto_path) else {"owned": []}
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
        new = [t for t in tids if t and t not in self.owned() and self.hero(t).get("hero")]
        if new:
            self.roster_auto["owned"] = sorted(set(self.roster_auto.get("owned", [])) | set(new))
            json.dump(self.roster_auto, open(self._auto_path, "w", encoding="utf-8"), ensure_ascii=False)
        return new

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
