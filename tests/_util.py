"""Shared test helpers (sys.path setup, one fake installation per process)."""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from synth import FakeInstall, build_fake_install  # noqa: E402

_FAKE: FakeInstall | None = None


def fake_install() -> FakeInstall:
    global _FAKE
    if _FAKE is None:
        d = Path(tempfile.mkdtemp(prefix="cbbe3bbb-fake-"))
        atexit.register(shutil.rmtree, d, True)
        _FAKE = build_fake_install(d)
    return _FAKE


def tree_digest(root: Path) -> dict[str, tuple[int, int, str]]:
    """{relative path: (size, mtime_ns, sha1)} for every file below root."""
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = Path(dp) / f
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest())
    return out


def temp_home() -> Path:
    d = Path(tempfile.mkdtemp(prefix="cbbe3bbb-home-"))
    atexit.register(shutil.rmtree, d, True)
    return d

import logging  # noqa: E402

logging.getLogger("cbbe3bbb").setLevel(logging.ERROR)
