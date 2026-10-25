"""Diagnostics: python -m afkj_overlay.diag  -> short report to paste into chat."""
import glob, os, platform, subprocess, sys, time
from . import config as cfgmod
from .winread import strategies, is_admin, IS_WIN, ERR_NAMES
from .parser import parse_bytes

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    cfg = cfgmod.load(); d = cfg.get("log_dir_resolved") or ""
    print("=== AFKJ diag ===")
    print("OS: %s | Python %s | админ: %s" % (
        platform.platform(), platform.python_version(), "ДА" if is_admin() else "нет"))
    if IS_WIN:
        try:
            tl = subprocess.run(["tasklist", "/NH"], capture_output=True, text=True, timeout=10).stdout
            procs = sorted({l.split()[0] for l in tl.splitlines() if "journey" in l.lower()})
            print("Игра запущена: %s" % (
                "ДА (%s)" % ", ".join(procs) if procs else "нет (процесс *journey* не найден)"))
        except Exception as e:
            print("Игра запущена: ? (%s)" % e)
    print("Папка логов: %s %s" % (
        d or "НЕ НАЙДЕНА", "(из config.json)" if cfg.get("log_dir") else "(автопоиск)"))
    files = sorted(glob.glob(os.path.join(d, cfg.get("file_glob", "log-client-ev*.dat_*")))) if d else []
    if not files:
        print("Файлы log-client-ev*.dat_* не найдены"); return

    for p in files:
        try:
            st = os.stat(p)
            info = "%d байт, изменён %s" % (
                st.st_size, time.strftime("%H:%M:%S", time.localtime(st.st_mtime)))
        except OSError as e:
            info = "stat ошибка: %s" % e
        print("\nФайл: %s (%s)" % (os.path.basename(p), info))

        ok = False
        for name, fn in strategies():
            t0 = time.time()
            try:
                data = fn(p); dt = time.time() - t0
                evs = parse_bytes(data)
                last = max((e["ts"] for e in evs if e["ts"]), default="—")
                prep = sum(1 for e in evs if e["data"].get("action_type") == "battle_prepare_start")
                print("  %-10s OK  %d байт за %.2fс, событий %d, подготовок %d, последнее %s UTC" % (
                    name, len(data), dt, len(evs), prep, last))
                ok = True
            except OSError as e:
                code = getattr(e, "winerror", None) or e.errno
                print("  %-10s ОШИБКА [%s] %s" % (name, code, ERR_NAMES.get(code, e)))
            except Exception as e:
                print("  %-10s ОШИБКА %r" % (name, e))
        if not ok:
            print("  Ни один способ не сработал. Если игра запущена с повышенными правами, "
                  "запустите run_admin.bat diag.")
    print("\nСкопируйте весь текст выше и пришлите.")

if __name__ == "__main__":
    main()
