def start_hotkeys(mapping, log=print):
    """mapping: {'<f8>': callable}. Returns listener or None (then use tray menu)."""
    try:
        from pynput import keyboard
        l = keyboard.GlobalHotKeys(mapping); l.daemon = True; l.start()
        log("[горячие клавиши] " + ", ".join(mapping))
        return l
    except Exception as e:
        log(f"[горячие клавиши] недоступны ({e}); используйте меню в трее")
        return None
