"""Read-only parser for BodySlide/Outfit Studio slider data files (``.osd``).

Layout (little endian, packed): the 32-bit magic ``"OSD\\0"`` (bytes ``00 44 53 4F``), u32 version, u32 count,
then per entry: u8 name length, name bytes, u16 diff count and ``count``
records of (u16 vertex index, 3 x f32 offset) = 14 bytes each.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = ["OsdError", "OsdFile", "read_osd", "read_osd_bytes"]

_REC = np.dtype([("index", "<u2"), ("diff", "<f4", (3,))])


class OsdError(Exception):
    pass


@dataclass
class OsdFile:
    version: int
    entries: dict[str, np.ndarray] = field(default_factory=dict)  # name -> structured array (index, diff)

    @property
    def names(self) -> list[str]:
        return list(self.entries)

    def dense(self, name: str, n_vertices: int) -> np.ndarray:
        """(n_vertices, 3) offsets for one entry (zeros where not stored)."""
        out = np.zeros((n_vertices, 3), dtype=np.float32)
        rec = self.entries.get(name)
        if rec is not None and len(rec):
            idx = rec["index"].astype(np.int64)
            ok = idx < n_vertices
            out[idx[ok]] = rec["diff"][ok]
        return out


def read_osd_bytes(data: bytes) -> OsdFile:
    if len(data) < 12 or data[:4] != b"\x00DSO":
        raise OsdError("not an OSD file (bad magic)")
    version, count = struct.unpack_from("<II", data, 4)
    pos = 12
    out = OsdFile(version=version)
    for _ in range(count):
        if pos + 1 > len(data):
            raise OsdError("truncated OSD (name length)")
        n = data[pos]
        pos += 1
        name = data[pos : pos + n].decode("utf-8", errors="replace")
        pos += n
        if pos + 2 > len(data):
            raise OsdError("truncated OSD (diff count)")
        (k,) = struct.unpack_from("<H", data, pos)
        pos += 2
        size = k * _REC.itemsize
        if pos + size > len(data):
            raise OsdError(f"truncated OSD entry '{name}'")
        out.entries[name] = np.frombuffer(data, dtype=_REC, count=k, offset=pos).copy()
        pos += size
    return out


def read_osd(path: str | Path) -> OsdFile:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise OsdError(f"cannot read {path}: {exc}") from exc
    return read_osd_bytes(data)
