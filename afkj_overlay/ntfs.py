"""Minimal, pure-Python NTFS structures: enough to pull one file's $DATA out of an
MFT file record and to turn FSCTL extents/runlists into byte ranges.

Nothing here opens a volume; all raw reads go through a `read_clusters(lcn, count)`
callback so the parser is testable on any OS with synthetic bytes. The Windows glue
(opening the volume, FSCTL calls) lives in winread.py.
"""

ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF
# non-resident $DATA flags (attribute header, offset 0x0C)
FLAG_COMPRESSED = 0x0001
FLAG_ENCRYPTED = 0x4000
FLAG_SPARSE = 0x8000

class NtfsUnsupported(Exception): pass

def _u(b, o, n): return int.from_bytes(b[o:o + n], "little")
def _s(b, o, n): return int.from_bytes(b[o:o + n], "little", signed=True)

def apply_fixup(record):
    """Replace the per-sector update-sequence words back into the record. NTFS stores
    the last 2 bytes of every 512-byte sector in the update sequence array and writes a
    marker in their place so torn writes are detectable. Returns a new bytes object."""
    rec = bytearray(record)
    usa_off = _u(rec, 0x04, 2); usa_cnt = _u(rec, 0x06, 2)
    if usa_cnt < 1: return bytes(rec)
    for i in range(1, usa_cnt):
        word = rec[usa_off + i * 2: usa_off + i * 2 + 2]
        pos = i * 512 - 2
        if pos + 2 <= len(rec): rec[pos:pos + 2] = word
    return bytes(rec)

def parse_runlist(data):
    """Decode a non-resident $DATA runlist. Returns [(length_clusters, lcn_or_None), ...];
    lcn is None for a sparse run (no disk allocation)."""
    runs = []; i = 0; lcn = 0
    while i < len(data):
        hdr = data[i]; i += 1
        if hdr == 0: break
        len_sz = hdr & 0x0F; off_sz = (hdr >> 4) & 0x0F
        if len_sz == 0: break
        length = _u(data, i, len_sz); i += len_sz
        if off_sz == 0:
            runs.append((length, None))            # sparse / unallocated
        else:
            lcn += _s(data, i, off_sz); i += off_sz
            runs.append((length, lcn))
    return runs

def iter_attributes(record):
    off = _u(record, 0x14, 2)
    n = len(record)
    while off + 8 <= n:
        atype = _u(record, off, 4)
        if atype == ATTR_END: break
        alen = _u(record, off + 4, 4)
        if alen < 8 or off + alen > n: break
        yield off, atype, alen
        off += alen

def find_data_attr(record, name=""):
    """Return the offset of the unnamed (or named) $DATA attribute, or None."""
    for off, atype, alen in iter_attributes(record):
        if atype != ATTR_DATA: continue
        nlen = record[off + 9]; noff = _u(record, off + 0x0A, 2)
        aname = record[off + noff: off + noff + nlen * 2].decode("utf-16-le", "replace") if nlen else ""
        if aname == name: return off
    return None

def extract_data(record, cluster_size, read_clusters, name="", fixup=True):
    """Pull the file's $DATA content out of one MFT record.
    read_clusters(lcn, count) -> bytes of count*cluster_size. Returns the file bytes
    truncated to the real size. Raises NtfsUnsupported for compressed/encrypted data."""
    if fixup: record = apply_fixup(record)
    off = find_data_attr(record, name)
    if off is None: raise NtfsUnsupported("нет атрибута $DATA")
    nonres = record[off + 8]
    if not nonres:
        clen = _u(record, off + 0x10, 4); coff = _u(record, off + 0x14, 2)
        return bytes(record[off + coff: off + coff + clen])
    flags = _u(record, off + 0x0C, 2)
    if flags & (FLAG_COMPRESSED | FLAG_ENCRYPTED):
        raise NtfsUnsupported("сжатый/зашифрованный $DATA не поддерживается")
    run_off = _u(record, off + 0x20, 2)
    real_size = _u(record, off + 0x30, 8)
    alen = _u(record, off + 4, 4)
    runs = parse_runlist(record[off + run_off: off + alen])
    out = bytearray()
    for length, lcn in runs:
        if lcn is None: out += b"\x00" * (length * cluster_size)
        else: out += read_clusters(lcn, length)
        if len(out) >= real_size: break
    return bytes(out[:real_size])

def extents_to_bytes(extents, cluster_size, file_size, read_clusters):
    """extents: [(clusters, lcn_or_-1), ...] from FSCTL_GET_RETRIEVAL_POINTERS.
    Returns file bytes truncated to file_size."""
    out = bytearray()
    for clusters, lcn in extents:
        if lcn is None or lcn < 0: out += b"\x00" * (clusters * cluster_size)
        else: out += read_clusters(lcn, clusters)
        if len(out) >= file_size: break
    return bytes(out[:file_size])
