"""Diagnostics: python -m afkj_overlay.diag [--vss]  -> short report to paste into chat."""
import glob, os, platform, sys, time
from . import config as cfgmod
from .winread import strategies, is_admin, IS_WIN, volume_device_path
from .parser import parse_bytes

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    want_vss = "--vss" in sys.argv
    cfg = cfgmod.load(); d = cfg.get("log_dir_resolved") or ""
    print("=== AFKJ diag ===")
    print("OS: %s | Python %s | админ: %s" % (
        platform.platform(), platform.python_version(), "ДА" if is_admin() else "нет"))
    if IS_WIN:
        try:
            import subprocess
            tl = subprocess.run(["tasklist", "/NH"], capture_output=True, text=True, timeout=10).stdout
            procs = sorted({l.split()[0] for l in tl.splitlines() if "journey" in l.lower()})
            print("Игра запущена: %s" % (
                "ДА (%s)" % ", ".join(procs) if procs else "нет (процесс *journey* не найден)"))
        except Exception as e:
            print("Игра запущена: ? (%s)" % e)
    print("Папка логов: %s %s" % (
        d or "НЕ НАЙДЕНА", "(из config.json)" if cfg.get("log_dir") else "(автопоиск)"))
    if d and IS_WIN:
        try:
            from .winread import get_filesystem
            drive = os.path.splitdrive(os.path.abspath(d))[0]
            print("Файловая система: %s (том %s)" % (get_filesystem(drive), drive))
            print("volume device: %s" % volume_device_path(drive))
        except Exception as e:
            print("Файловая система: ? (%s)" % e)
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

        results = {}
        for name, fn in strategies(include_vss=want_vss, include_backup=True):
            if name in ("raw_ntfs", "vss", "backup") and not is_admin():
                print("  %-10s ПРОПУЩЕНО (нужен администратор: run_admin.bat diag)" % name)
                continue
            t0 = time.time()
            try:
                data = fn(p); dt = time.time() - t0
                evs = parse_bytes(data)
                last = max((e["ts"] for e in evs if e["ts"]), default="—")
                prep = sum(1 for e in evs if e["data"].get("action_type") == "battle_prepare_start")
                print("  %-10s OK  %d байт за %.2fс, событий %d, подготовок %d, последнее %s UTC" % (
                    name, len(data), dt, len(evs), prep, last))
                results[name] = (True, len(data), len(evs), prep, last, dt, data)
            except OSError as e:
                code = getattr(e, "winerror", None) or e.errno
                from .winread import ERR_NAMES
                # Full message includes sub-step labels (open_attr / open_volume / ...)
                print("  %-10s ОШИБКА [%s] %s" % (name, code, e))
                results[name] = (False, None, None, None, None, time.time() - t0, None)
            except Exception as e:
                print("  %-10s ОШИБКА %r" % (name, e))
                results[name] = (False, None, None, None, None, time.time() - t0, None)

        if results.get("raw_ntfs", (False,))[0] and results.get("vss", (False,))[0]:
            rb, vb = results["raw_ntfs"][6], results["vss"][6]
            re_, ve = results["raw_ntfs"][2], results["vss"][2]
            same = rb == vb
            print("  сравнение  raw_ntfs vs vss: байты %s (%s / %s), события %s / %s%s" % (
                "совпали" if same else "РАЗЛИЧАЮТСЯ",
                results["raw_ntfs"][1], results["vss"][1], re_, ve,
                "" if same else " — возможно, данные ещё в кэше игры и не сброшены на диск"))
        elif not any(r[0] for r in results.values()):
            print("  Ни один способ не сработал." + (
                "" if want_vss else
                " Вариант: run_admin.bat diag --vss"))

        print("  примечание: raw_ntfs читает с диска; VSS видит кэш ОС. "
              "FlushFileBuffers по чужому дескриптору недоступен.")
    print("\nСкопируйте весь текст выше и пришлите.")

if __name__ == "__main__":
    main()
