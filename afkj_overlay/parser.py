"""Extract JSON events from alilog *.dat_* files.

The file is a sequence of protobuf-like records; each record has
  event_time\\x12\\x13YYYY-MM-DD HH:MM:SS  (UTC)
  event_name\\x12<len><name>
  custom_content\\x12<varint len>{...json...}
The big file is a fixed-size ring buffer that is overwritten in place, so
events are NOT in file order. We scan for every '{' that starts a JSON object,
decode it with json.JSONDecoder.raw_decode and pair it with the nearest
preceding event_time / event_name.
"""
import hashlib, json, re

_DEC = json.JSONDecoder()
_OBJ = re.compile(rb'\{"')
_TS = re.compile(rb'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')
_NAME = re.compile(rb'event_name\x12[\x00-\x7f]([A-Za-z0-9_]{1,64})')
LOOKBACK = 1200
MAX_OBJ = 2_000_000

def _varint_before(buf, pos):
    """custom_content\\x12<varint> right before pos -> length or None."""
    i = buf.rfind(b"custom_content\x12", max(0, pos - 32), pos)
    if i < 0: return None
    j = i + len(b"custom_content\x12"); n = shift = 0
    while j < pos:
        b = buf[j]; n |= (b & 0x7F) << shift; j += 1; shift += 7
        if not b & 0x80: break
    return n if j == pos else None

def parse_bytes(buf, start=0):
    """Return list of events: dict(ts, event_name, data, raw, pos, key)."""
    out, pos, n = [], start, len(buf)
    while True:
        m = _OBJ.search(buf, pos)
        if not m: break
        s = m.start(); obj = raw = None
        ln = _varint_before(buf, s); end_b = None
        if ln and s + ln <= n:
            raw = buf[s:s + ln].decode("utf-8", "replace")
            try: obj = json.loads(raw); end_b = s + ln
            except ValueError: obj = None
        if obj is None:
            txt = buf[s:s + MAX_OBJ].decode("utf-8", "replace")
            try:
                obj, end = _DEC.raw_decode(txt); raw = txt[:end]
                end_b = s + len(raw.encode("utf-8"))
            except ValueError:
                pos = s + 1; continue
        if not isinstance(obj, dict):
            pos = s + 1; continue
        ctx = buf[max(0, s - LOOKBACK):s]
        ts = _TS.findall(ctx); nm = _NAME.findall(ctx)
        ev = {"ts": ts[-1].decode() if ts else None,
              "event_name": nm[-1].decode() if nm else None,
              "data": obj, "raw": raw, "pos": s}
        ev["key"] = hashlib.sha1(f"{ev['ts']}|{ev['event_name']}|{raw}".encode("utf-8", "replace")).hexdigest()
        out.append(ev)
        pos = end_b if end_b and end_b > s else s + 1
    return out

def parse_file(path):
    with open(path, "rb") as f: buf = f.read()
    evs = parse_bytes(buf)
    evs.sort(key=lambda e: (e["ts"] or "", e["pos"]))
    return evs

BATTLE_ACTIONS = ("battle_prepare_start", "battle_start", "battle_end")
def is_battle_event(ev):
    return ev["data"].get("action_type") in BATTLE_ACTIONS
