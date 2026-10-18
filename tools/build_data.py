#!/usr/bin/env python3
"""Precompute compact data/*.json for the overlay from a local clone of
github.com/Koko-boya/AFKJourneyData (Tables/) + Localization strings (en, ru).
Usage: python tools/build_data.py --tables <repo>/Tables --strings-dir <dir with strings_en.json, strings_ru.json> --out data
"""
import argparse, json, os, re
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--tables", default="/workspace/afkj/repo/Tables")
ap.add_argument("--strings-dir", default="/workspace/afkj")
ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data"))
a = ap.parse_args()
T = a.tables
J = lambda p: json.load(open(p, encoding="utf-8"))
rows = lambda n: J(os.path.join(T, n))["rows"]
EN = J(os.path.join(a.strings_dir, "strings_en.json")); RU = J(os.path.join(a.strings_dir, "strings_ru.json"))
def en(k): return EN.get(k, k) if isinstance(k, str) else k
def ru(k): return RU.get(k, k) if isinstance(k, str) else k
A = J(os.path.join(T, "Array.json"))["data"]; P1, P2 = A["1"], A["2"]
def arr(v):
    if isinstance(v, dict) and "$arrayRef" in v:
        n = v["$arrayRef"]; return [P1[str(i)] for i in range(P2[str(n)], P2[str(n + 1)])]
    if isinstance(v, dict): return [v[k] for k in sorted(v, key=int)]
    return v or []
FAC = {1: "Lightbearer", 2: "Wilder", 3: "Mauler", 4: "Graveborn", 5: "Celestial", 6: "Hypogean", 9: "Dimensional", 7: "Boss"}
JOB = {1: "Tank", 2: "Warrior", 3: "Mage", 4: "Support", 5: "Marksman", 6: "Rogue"}
def clean(s): return re.sub(r"<[^>]+>|\[/?\w*\]", "", s or "")
def fill(desc, r):
    def rep(m):
        v = r.get(m.group(1)); pct = m.group(2) == "%"
        if not isinstance(v, (int, float)): return m.group(0)
        return f"{v*100 if pct else v:g}" + ("%" if pct else "")
    return re.sub(r"\{(SArg\d+|PlusRatio)(%?)\}", rep, desc)
os.makedirs(a.out, exist_ok=True)
def dump(name, obj):
    p = os.path.join(a.out, name); json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(name, os.path.getsize(p))

# heroes / units
units = {r["ID"]: r for r in rows("Unit.json")}
heroes = {}
for tid, u in units.items():
    dn = u.get("DisplayName")
    if not dn or not en(dn) or tid in (900, 901, 903): continue
    heroes[str(tid)] = {"ru": ru(dn), "en": en(dn), "fac": FAC.get(u["UnitRace"], u["UnitRace"]),
                        "cls": JOB.get(u["UnitJob"], u["UnitJob"]), "dmg": u["DamageType"],
                        "icon": os.path.basename(u.get("IconSquare") or ""), "hero": bool(u["UnitType"] == 1 and tid < 900 and u["Enable"])}
dump("heroes.json", heroes)

# skills for bosses
sidx = J(os.path.join(T, "SkillIndex.json"))["data"]["Skill"]
def skills(uid):
    f = sidx.get(str(uid)); out = {}
    if not f: return []
    for r in J(os.path.join(T, "Skill", f"Skill{f}.json"))["rows"]:
        if r.get("CasterID") != uid or not r.get("IsShow") or r["Slot"] in out: continue
        out[r["Slot"]] = {"ru": ru(r.get("DisplayName")), "en": en(r.get("DisplayName")),
                          "desc_ru": fill(clean(ru(r.get("Description") or "")), r)[:400]}
    return [out[k] for k in sorted(out)]

def icon_tid(p):
    m = re.search(r"herohead_(\d+)\.png", p or ""); return int(m.group(1)) if m else None

bosses, groups = {}, {}
def boss(tid):
    if str(tid) not in bosses:
        u = units.get(tid, {})
        bosses[str(tid)] = {"ru": ru(u.get("DisplayName")), "en": en(u.get("DisplayName")),
                            "cls": JOB.get(u.get("UnitJob")), "dmg": u.get("DamageType"),
                            "desc_ru": ru(u.get("Description")), "desc_en": en(u.get("Description")),
                            "skills": skills(tid), "tags": {}}
    return bosses[str(tid)]
lvl = {r["LevelID"]: {"ru": ru(r["DisplayName"]), "en": en(r["DisplayName"])} for r in rows("DreamBossLevelDisplay.json")}
msgs = {r["BossGroupID"]: r for r in rows("DreamBossBossMessage.json")}
for r in rows("DreamBoss.json"):
    m = msgs.get(r["BossGroupID"]); tid = icon_tid(m.get("BossIcon")) if m else None
    if not tid: continue
    b = boss(tid)
    tags = []
    for k in ("RecommendGroup1", "RecommendGroup2"):
        t = arr(m.get(k))
        if t and en(t[0]): tags.append({"ru": ru(t[0]), "en": en(t[0])})
    b["tags"][str(r["BossGroupID"])] = tags
    groups[str(r["BossID"])] = {"kind": "dream", "bosses": [tid], "boss_group": r["BossGroupID"], "season": r["SeasonType"],
                                "level": r["BossLevel"], "level_ru": lvl.get(r["BossLevel"], {}).get("ru"), "level_en": lvl.get(r["BossLevel"], {}).get("en")}
disp = {r["SpanID"]: r for r in rows("BossRushDisplay.json")}
for r in rows("BossRush.json"):
    d = disp.get(r["SpanID"]); tids = [icon_tid(d.get(k)) for k in ("BossIcon1", "BossIcon2")] if d else []
    tids = [t for t in tids if t]
    for t in tids: boss(t)
    groups[str(r["BattleGroupID"])] = {"kind": "bossrush", "bosses": tids, "span": r["SpanID"], "stage": r["StageID"],
                                       "event_ru": ru(d["ActivityTitle"]) if d else None}
dump("bosses.json", bosses)
dump("boss_groups.json", groups)

# all BattleGroupName titles (dedup string table)
titles, tidx, out = [], {}, {}
def ti(s):
    if s not in tidx: tidx[s] = len(titles); titles.append(s)
    return tidx[s]
for r in rows("BattleGroupName.json"):
    t_ru, t_en = ru(r["StrParam1"]), en(r["StrParam1"])
    mode = en(r["Mode"]) if r["Mode"] else ""
    num = r["IntParam1"] if r["IntParam1"] and str(r["IntParam1"]).isdigit() else ""
    out[str(r["ID"])] = [ti(mode or ""), ti(t_ru or ""), ti(t_en or ""), num]
dump("group_titles.json", {"strings": titles, "groups": out})
