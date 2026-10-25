import datetime as dt, glob, hashlib, os, threading, time
from .parser import parse_file, parse_bytes, BATTLE_ACTIONS
from .winread import (
    read_file, ReadError, ReadThrottled, is_admin, last_info, vss_cleanup,
    vss_mark_pending, vss_seconds_until_ready, vss_is_pending,
)

# These readers can see new bytes while size and mtime stay put (ring buffer, exclusive lock).
# vss keeps its own trailing-edge retry, so it is not re-read on every poll.
_REREAD_METHODS = {"dup_handle", "raw_ntfs"}

def _utc(ts):
    """Parse game log timestamp as UTC. Game writes UTC wall-clock (not local)."""
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

class Collector:
    """Watches the alilog folder, stores events, calls on_battle(kind, payload)."""
    def __init__(self, cfg, gd, store, on_battle=None, log=print, on_status=None, allow_elevate=False):
        self.cfg, self.gd, self.store, self.on_battle, self.log = cfg, gd, store, on_battle, log
        self.on_status = on_status; self.status = None; self.read_method = None
        self.allow_elevate = allow_elevate; self._elev_asked = False
        self.seen = set(r[0] for r in store.db.execute("SELECT key FROM events"))
        self.files = {}; self._digest = {}; self.methods = {}
        self._stop = threading.Event(); self._wake = threading.Event()
        self._vss_retry_at = 0.0   # monotonic deadline to retry after throttle
        self.observer = None

    # ---------- event handling ----------
    def handle(self, ev, src="", live=True, check_age=True):
        if ev["key"] in self.seen: return False
        self.seen.add(ev["key"]); self.store.add_event(ev, src)
        d = ev["data"]; act = d.get("action_type")
        if act not in BATTLE_ACTIONS or not d.get("battle_group_id"): return True
        gid = d["battle_group_id"]; info = self.gd.resolve(gid)
        boss = info["bosses"][0] if info["bosses"] else None
        sid = d.get("server_battle_id")
        payload = {"gid": gid, "info": info, "ts": ev["ts"], "data": d}
        if act == "battle_start":
            self.store.upsert_battle(sid, group_id=gid, title=info["title"], boss_tid=boss, ts_start=ev["ts"],
                                     self_gs=d.get("self_team_gs"), oppo_gs=d.get("oppo_team_gs"))
        elif act == "battle_end":
            replay = int(d.get("self_team_gs") == 0 and not d.get("battle_sec")) if "self_team_gs" in d else None
            dmg = None; heroes = []
            bi = d.get("battle_input")
            if isinstance(bi, dict):
                try: dmg = bi.get("input", {}).get("damage")
                except Exception: pass
                try: heroes = bi["data"]["self"]["players"][0]["heroes"]
                except Exception: heroes = []
            self.store.upsert_battle(sid, group_id=gid, title=info["title"], boss_tid=boss, ts_end=ev["ts"],
                                     result=d.get("battle_result"), secs=d.get("battle_sec"), damage=dmg,
                                     mvp_tid=d.get("honor_hero_id"), formation_id=d.get("formation"),
                                     self_gs=d.get("self_team_gs") or None, oppo_gs=d.get("oppo_team_gs"), is_replay=replay)
            if heroes and not replay:
                self.store.add_heroes(sid, heroes)
                new = self.gd.add_owned([h.get("tid") for h in heroes])
                if new: self.log("[ростер] добавлены: %s" % ", ".join(self.gd.hero_name(t) for t in new))
            payload.update(replay=bool(replay), damage=dmg)
            if bi is not None and ev.get("event_name") == "battlemonitor":
                return True
        if live and self.on_battle:
            if check_age:
                t = _utc(ev["ts"])
                now = dt.datetime.now(dt.timezone.utc)
                age = (now - t).total_seconds() if t else 1e9
                max_age = float(self.cfg.get("live_max_age_sec", 600))
                # Late discovery grace: VSS can lag ~5–60 s; still show if within max_age.
                # age is always (now_utc - event_utc); local TZ (e.g. UTC+3) must not affect this.
                if age > max_age:
                    self.log("[сборщик] пропуск %s age=%.1fs > %ss (ts=%s UTC, now=%s UTC)" % (
                        act, age, int(max_age), ev["ts"], now.strftime("%Y-%m-%d %H:%M:%S")))
                    return True
                if age < 0:
                    # Clock skew / mis-tagged timezone: still show, but log.
                    self.log("[сборщик] ts в будущем на %.1fs (ts=%s) — показываю" % (-age, ev["ts"]))
            kind = {"battle_prepare_start": "prepare", "battle_start": "start", "battle_end": "end"}[act]
            self.on_battle(kind, payload)
        return True

    def set_status(self, level, text):
        if self.status != (level, text):
            self.status = (level, text); self.log("[статус] %s" % text)
            if self.on_status: self.on_status(level, text)

    def scan_file(self, path, live=True):
        """Returns n_new (int), None on hard error, or 'throttled' if VSS cooldown."""
        t0 = time.time()
        try:
            data, method = read_file(path, live=live)
        except ReadThrottled as e:
            vss_mark_pending()
            retry = getattr(e, "retry_after", None) or vss_seconds_until_ready()
            self._vss_retry_at = time.monotonic() + max(0.1, float(retry))
            self.log("[сборщик] %s — повтор через %.1fс" % (e, retry))
            return "throttled"
        except ReadError as e:
            det = "; ".join("%s: %s" % (n, m) for n, m in e.details)
            if self.status is None or self.status[0] != "error":
                self.log("[сборщик] %s: %s" % (os.path.basename(path), det))
            if not is_admin():
                if self.allow_elevate and not self._elev_asked:
                    self._elev_asked = True
                    from .winread import relaunch_elevated
                    if relaunch_elevated():
                        os._exit(0)
                hint = "игра держит лог эксклюзивно — подтвердите UAC или запустите run_admin.bat"
            else:
                hint = "закройте игру — история прочитается после сессии; пришлите вывод diag.bat"
            self.set_status("error", "Нет доступа к логу — %s" % hint)
            return None
        except Exception as e:
            self.log("[сборщик] ошибка чтения %s: %s" % (path, e)); return None

        self.methods[path] = method
        if method != self.read_method:
            self.read_method = method
            self.log("[сборщик] читаю лог способом: %s" % method)
        lat = last_info().get("latency", {}).get(method)
        if lat is None:
            lat = time.time() - t0
        lat_s = ", %.0f мс" % (lat * 1000)
        self.set_status("ok", "Лог читается (%s%s) · жду подготовку к бою" % (method, lat_s))

        digest = hashlib.sha1(data).digest()
        if self._digest.get(path) == digest:
            if method == "vss" and vss_is_pending():
                self._vss_retry_at = time.monotonic() + vss_seconds_until_ready()
            return 0
        try:
            evs = parse_bytes(data); evs.sort(key=lambda e: (e["ts"] or "", e["pos"]))
        except Exception as e:
            self.log("[сборщик] ошибка разбора %s: %s" % (path, e)); return None
        self._digest[path] = digest

        n = 0; prepares = 0
        for ev in evs:
            if self.handle(ev, os.path.basename(path), live=live):
                n += 1
                if ev["data"].get("action_type") == "battle_prepare_start":
                    prepares += 1
        self.store.commit()
        self.log("[сборщик] read %s %s: %d байт, %.0f мс, новых %d, подготовок %d" % (
            method, os.path.basename(path), len(data), lat * 1000, n, prepares))

        # Trailing edge: if VSS saw a mid-snapshot change, force another pass after cooldown
        if method == "vss" and vss_is_pending():
            self._vss_retry_at = time.monotonic() + vss_seconds_until_ready()
            self.log("[сборщик] vss: файл изменился во время снимка — повтор через %.1fс" %
                     vss_seconds_until_ready())
        return n

    # ---------- watching ----------
    def log_dir(self):
        return self.cfg.get("log_dir_resolved") or ""

    def _poll_once(self, live=True):
        d = self.log_dir()
        if not d or not os.path.isdir(d):
            self.set_status("error", "Папка логов не найдена — укажите log_dir в config.json"); return
        files = glob.glob(os.path.join(d, self.cfg.get("file_glob", "log-client-ev*.dat_*")))
        if not files:
            self.set_status("warn", "Файл лога пока не создан — запустите игру"); return

        # Trailing-edge: if a VSS retry is due, clear remembered sig so we re-read
        due = self._vss_retry_at and time.monotonic() >= self._vss_retry_at
        if due:
            self._vss_retry_at = 0.0
            for p in files:
                self.files.pop(p, None)
            self.log("[сборщик] vss trailing-edge: повторное чтение")

        for p in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
            if self.files.get(p) == sig and self.methods.get(p) not in _REREAD_METHODS:
                continue
            n = self.scan_file(p, live=live)
            if n is None:
                continue                      # hard error — retry next poll (don't lock sig)
            if n == "throttled":
                # Do NOT update files[p]: change must be re-tried after cooldown
                continue
            self.files[p] = sig
            # If trailing-edge pending after successful vss, drop sig so next due pass re-reads
            if vss_is_pending():
                self.files.pop(p, None)

    def start(self):
        try: vss_cleanup()
        except Exception: pass
        d = self.log_dir()
        self.log("[сборщик] папка логов: %s" % (d or "НЕ НАЙДЕНА (укажите log_dir в config.json)"))
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            wake = self._wake
            class H(FileSystemEventHandler):
                def on_any_event(self, e): wake.set()
            if d and os.path.isdir(d):
                self.observer = Observer(); self.observer.schedule(H(), d, recursive=False); self.observer.start()
        except Exception as e:
            self.log("[сборщик] watchdog недоступен (%s), только опрос" % e)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        iv = float(self.cfg.get("poll_interval", 0.5))
        try:
            self._poll_once(live=False)
        except Exception as e:
            self.log("[сборщик] ошибка начального чтения: %s" % e)
        while not self._stop.is_set():
            # Wake earlier if a VSS trailing-edge retry is scheduled
            wait = iv
            if self._vss_retry_at:
                wait = min(wait, max(0.05, self._vss_retry_at - time.monotonic()))
            self._wake.wait(wait); self._wake.clear()
            try:
                if not self.log_dir() or not os.path.isdir(self.log_dir()):
                    from .config import autodetect_log_dir
                    if not self.cfg.get("log_dir"):
                        self.cfg["log_dir_resolved"] = autodetect_log_dir()
                self._poll_once(live=True)
            except Exception as e:
                self.log("[сборщик] ошибка: %s" % e)

    def stop(self):
        try: vss_cleanup()
        except Exception: pass
        self._stop.set(); self._wake.set()
        if self.observer:
            try: self.observer.stop()
            except Exception: pass

    def replay(self, path, delay=0.0):
        evs = parse_file(path)
        for ev in evs:
            is_b = ev["data"].get("action_type") in BATTLE_ACTIONS
            self.handle(ev, os.path.basename(path), live=True, check_age=False)
            if is_b and delay: time.sleep(delay)
        self.store.commit()
        return len(evs)
