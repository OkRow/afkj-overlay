"""Recommendation engine v0 + view model shared by console and UI."""
from .gamedata import CLS_RU, DMG_RU, FAC_RU, RESULT_RU

PLACEHOLDER_PREFIX = "ПРИМЕР"
DEMO_GID = 4033303  # Новик, «Сложный»

def recommend(gd, store, boss_tid, only_owned=True, top_n=10):
    owned = gd.owned(); score, reasons, placeholder_tids = {}, {}, set()
    def add(t, pts, why, ph=False):
        t = gd.to_tid(t)
        if not t: return
        score[t] = score.get(t, 0) + pts; reasons.setdefault(t, []).append(why)
        if ph: placeholder_tids.add(t)
    entry = gd.manual.get("bosses", {}).get(str(boss_tid))
    if entry is None:
        name = (gd.boss(boss_tid).get("en") or "").lower()
        entry = next((v for k, v in gd.manual.get("bosses", {}).items() if k.lower() == name), None)
    teams = (entry or {}).get("teams", [])
    for tm in teams:
        src = tm.get("source", ""); ph = src.startswith(PLACEHOLDER_PREFIX)
        tag = "пример" if ph else src
        for h in tm.get("heroes", []): add(h, 50, f"в составе ({tag})", ph)
        for h in tm.get("alternatives", []): add(h, 25, f"замена ({tag})", ph)
    hist = store.boss_history(boss_tid) if (store and boss_tid) else []
    best = None
    for b in hist:
        for h in b["heroes"]:
            add(h, 10 + (5 if b["result"] == 1 else 0), "был в вашем бою против босса")
        if b["mvp"]: add(b["mvp"], 15, "MVP вашего боя")
        if b["damage"] and (not best or b["damage"] > best["damage"]): best = b
    items = []
    for t, s in score.items():
        own = t in owned
        if only_owned and not own: continue
        rs = []
        for r in reasons[t]:
            if r not in rs: rs.append(r)
        cnt = {r: reasons[t].count(r) for r in rs}
        rs = [f"{r} ×{cnt[r]}" if cnt[r] > 1 else r for r in rs]
        items.append({"tid": t, "name": gd.hero_name(t), "score": s + (5 if own else 0), "owned": own,
                      "reasons": rs, "placeholder": t in placeholder_tids and all(PLACEHOLDER_PREFIX in x or "пример" in x for x in reasons[t]),
                      "cls": CLS_RU.get(gd.hero(t).get("cls"), ""), "dmg": DMG_RU.get(gd.hero(t).get("dmg"), "")})
    items.sort(key=lambda x: -x["score"])
    return {"items": items[:top_n], "placeholder": any(i["placeholder"] for i in items[:top_n]),
            "teams": teams, "history_n": len(hist), "best": best,
            "mvp_counts": _mvp_counts(gd, hist)}

def _mvp_counts(gd, hist):
    c = {}
    for b in hist:
        if b["mvp"]: c[gd.hero_name(b["mvp"])] = c.get(gd.hero_name(b["mvp"]), 0) + 1
    return c

def fmt_num(n):
    if n is None: return "—"
    for div, s in ((1e9, " млрд"), (1e6, " млн"), (1e3, " тыс")):
        if n >= div: return f"{n/div:.2f}".rstrip("0").rstrip(".").replace(".", ",") + s
    return str(n)

def build_view(gd, store, gid, only_owned=True, top_n=10):
    info = gd.resolve(gid)
    v = {"gid": gid, "title": info["title"], "kind": info.get("kind"), "bosses": []}
    if info.get("kind") == "dream":
        v["mode"] = "Царство снов / босс"
        v["difficulty"] = info.get("level_ru") or info.get("level_en") or str(info.get("level"))
    elif info.get("kind") == "bossrush":
        v["mode"] = f"{info.get('event_ru') or 'Владыка великанов'} (2 босса подряд)"
        v["difficulty"] = f"этап {info.get('stage')}"
    else:
        v["mode"] = "Этап (противники неизвестны в v0.1)"; v["difficulty"] = ""
    for t in info["bosses"]:
        b = gd.boss(t)
        v["bosses"].append({"tid": t, "name": b.get("ru") or b.get("en") or f"Босс #{t}",
                            "cls": CLS_RU.get(b.get("cls"), b.get("cls") or ""), "dmg": DMG_RU.get(b.get("dmg"), b.get("dmg") or ""),
                            "desc": b.get("desc_ru") or b.get("desc_en") or "",
                            "tags": [x.get("ru") or x.get("en") for x in gd.boss_tags(t, info.get("boss_group"))],
                            "skills": list(dict.fromkeys(s.get("ru") for s in b.get("skills", []) if s.get("ru")))[:5],
                            "recs": recommend(gd, store, t, only_owned, top_n)})
    return v

def result_line(gd, payload):
    d = payload["data"]
    if payload.get("replay"): return f"Просмотр записи боя · MVP: {gd.hero_name(d.get('honor_hero_id'))}"
    res = RESULT_RU.get(d.get("battle_result"), f"результат {d.get('battle_result')}")
    secs = d.get("battle_sec"); s = f"{secs:g} с" if isinstance(secs, (int, float)) else "—"
    line = f"{res} · {s} · MVP: {gd.hero_name(d.get('honor_hero_id')) if d.get('honor_hero_id') else '—'}"
    if payload.get("damage"): line += f" · урон {fmt_num(payload['damage'])}"
    return line

def view_to_text(v):
    L = [f"=== {v['title']}  [{v['gid']}] ===", f"Режим: {v['mode']}" + (f" · Сложность: {v['difficulty']}" if v.get('difficulty') else "")]
    for b in v["bosses"]:
        L.append(f"Босс: {b['name']} (#{b['tid']}) · {b['cls']} · {b['dmg']}")
        if b["desc"]: L.append(f"  {b['desc']}")
        if b["tags"]: L.append("  Теги игры: " + ", ".join(b["tags"]))
        if b["skills"]: L.append("  Навыки: " + ", ".join(b["skills"]))
        r = b["recs"]
        L.append("  Рекомендуемые герои" + (" [ПРИМЕР — тестовые данные]" if r["placeholder"] else "") + ":")
        for i, it in enumerate(r["items"], 1):
            L.append(f"   {i:2}. {it['name']:<16} {it['score']:>3}  {it['cls']}, {it['dmg']} — " + "; ".join(it["reasons"]) + ("" if it["owned"] else " (нет у вас)"))
        if not r["items"]: L.append("   (нет данных — добавьте составы в data/manual_recs.json)")
        if r["history_n"]: L.append(f"  Ваших боёв против босса: {r['history_n']}; MVP: " + ", ".join(f"{k} ×{n}" for k, n in r["mvp_counts"].items()))
        if r["best"]: L.append(f"  Лучший урон: {fmt_num(r['best']['damage'])}")
    return "\n".join(L)
