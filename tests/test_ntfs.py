"""Unit tests for the pure-Python NTFS $DATA / runlist parser (no Windows required)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afkj_overlay import ntfs

def _u16(n): return n.to_bytes(2, "little")
def _u32(n): return n.to_bytes(4, "little")
def _u64(n): return n.to_bytes(8, "little")
def _s8(n): return n.to_bytes(1, "little", signed=True)

class TestRunlist(unittest.TestCase):
    def test_single_run(self):
        # header 0x11: 1-byte length, 1-byte offset; length=4; offset=+10
        data = bytes([0x11, 4, 10, 0])
        self.assertEqual(ntfs.parse_runlist(data), [(4, 10)])

    def test_sparse_then_data(self):
        # sparse 2 clusters (off_sz=0), then 3 clusters at LCN 100
        data = bytes([0x01, 2, 0x11, 3, 100, 0])
        self.assertEqual(ntfs.parse_runlist(data), [(2, None), (3, 100)])

    def test_relative_lcn(self):
        # first run LCN=50 len=1; second delta=-5 -> LCN=45 len=2
        data = bytes([0x11, 1, 50, 0x11, 2, _s8(-5)[0], 0])
        self.assertEqual(ntfs.parse_runlist(data), [(1, 50), (2, 45)])

class TestFixup(unittest.TestCase):
    def test_apply(self):
        # 1024-byte record, 2 sectors => usa_cnt=3 (marker + 2 words)
        rec = bytearray(1024)
        rec[0:4] = b"FILE"
        rec[4:6] = _u16(0x30)          # usa_off
        rec[6:8] = _u16(3)             # usa_cnt
        rec[0x30:0x32] = b"\xAA\xAA"   # marker
        rec[0x32:0x34] = b"\x11\x22"   # sector0 original last 2
        rec[0x34:0x36] = b"\x33\x44"   # sector1 original last 2
        rec[510:512] = b"\xAA\xAA"     # placeholder
        rec[1022:1024] = b"\xAA\xAA"
        out = ntfs.apply_fixup(bytes(rec))
        self.assertEqual(out[510:512], b"\x11\x22")
        self.assertEqual(out[1022:1024], b"\x33\x44")

def _make_resident(payload: bytes) -> bytes:
    """Build a minimal FILE record with a resident unnamed $DATA holding payload."""
    # Header: FILE + usa + attr_off=0x38 + ...
    rec = bytearray(1024)
    rec[0:4] = b"FILE"
    rec[4:6] = _u16(0x30); rec[6:8] = _u16(3)
    rec[0x14:0x16] = _u16(0x38)        # first attribute offset
    # dummy USA
    rec[0x30:0x36] = b"\x00\x00" + b"\x00\x00" + b"\x00\x00"
    # $DATA resident attribute at 0x38
    off = 0x38
    name = b""                         # unnamed
    # attr layout: type(4) len(4) nonres(1) namelen(1) nameoff(2) flags(2) id(2)
    # then resident: value_len(4) value_off(2) ...
    value_off = 0x18                   # from start of attr
    alen = value_off + len(payload)
    alen = (alen + 7) & ~7             # 8-byte align
    attr = bytearray(alen)
    attr[0:4] = _u32(ntfs.ATTR_DATA)
    attr[4:8] = _u32(alen)
    attr[8] = 0                        # resident
    attr[9] = 0                        # name length
    attr[0x0A:0x0C] = _u16(0x18)       # name offset (unused)
    attr[0x10:0x14] = _u32(len(payload))
    attr[0x14:0x16] = _u16(value_off)
    attr[value_off:value_off + len(payload)] = payload
    rec[off:off + alen] = attr
    # END attribute
    end = off + alen
    rec[end:end + 4] = _u32(ntfs.ATTR_END)
    return bytes(rec)

def _make_nonresident(runs_raw: bytes, real_size: int, flags: int = 0) -> bytes:
    rec = bytearray(1024)
    rec[0:4] = b"FILE"
    rec[4:6] = _u16(0x30); rec[6:8] = _u16(3)
    rec[0x14:0x16] = _u16(0x38)
    rec[0x30:0x36] = b"\x00" * 6
    off = 0x38
    # non-resident header is 0x40 bytes before runlist typically
    run_off = 0x40
    alen = run_off + len(runs_raw) + 1
    alen = (alen + 7) & ~7
    attr = bytearray(alen)
    attr[0:4] = _u32(ntfs.ATTR_DATA)
    attr[4:8] = _u32(alen)
    attr[8] = 1                        # non-resident
    attr[9] = 0
    attr[0x0A:0x0C] = _u16(0x40)
    attr[0x0C:0x0E] = _u16(flags)
    attr[0x20:0x22] = _u16(run_off)
    attr[0x30:0x38] = _u64(real_size)
    attr[run_off:run_off + len(runs_raw)] = runs_raw
    attr[run_off + len(runs_raw)] = 0  # terminator
    rec[off:off + alen] = attr
    rec[off + alen:off + alen + 4] = _u32(ntfs.ATTR_END)
    return bytes(rec)

class TestExtract(unittest.TestCase):
    def test_resident(self):
        payload = b"hello-resident-data"
        rec = _make_resident(payload)
        out = ntfs.extract_data(rec, 4096, lambda l, c: b"", fixup=False)
        self.assertEqual(out, payload)

    def test_nonresident(self):
        # 2 clusters at LCN 5; cluster_size=16 for easy math
        runs = bytes([0x11, 2, 5, 0])
        real = 20
        rec = _make_nonresident(runs, real)
        disk = {5: b"AAAA" * 4, 6: b"BBBB" * 4}  # 16 bytes each
        def read_clusters(lcn, count):
            out = b""
            for i in range(count):
                out += disk[lcn + i]
            return out
        out = ntfs.extract_data(rec, 16, read_clusters, fixup=False)
        self.assertEqual(out, (b"AAAA" * 4 + b"BBBB" * 4)[:real])

    def test_sparse_supported(self):
        # sparse 1 cluster then data 1 cluster at LCN 7
        runs = bytes([0x01, 1, 0x11, 1, 7, 0])
        real = 24
        rec = _make_nonresident(runs, real)
        def read_clusters(lcn, count):
            return b"X" * (16 * count)
        out = ntfs.extract_data(rec, 16, read_clusters, fixup=False)
        self.assertEqual(out, (b"\x00" * 16 + b"X" * 16)[:real])

    def test_compressed_rejected(self):
        runs = bytes([0x11, 1, 1, 0])
        rec = _make_nonresident(runs, 8, flags=ntfs.FLAG_COMPRESSED)
        with self.assertRaises(ntfs.NtfsUnsupported):
            ntfs.extract_data(rec, 16, lambda l, c: b"\x00" * 16, fixup=False)

    def test_extents_helper(self):
        def read_clusters(lcn, count):
            return bytes([lcn & 0xFF]) * (8 * count)
        out = ntfs.extents_to_bytes([(2, 3), (1, -1)], 8, 20, read_clusters)
        self.assertEqual(out, (bytes([3]) * 16 + b"\x00" * 8)[:20])

if __name__ == "__main__":
    unittest.main()
