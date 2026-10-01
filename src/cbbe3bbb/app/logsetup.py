"""Logging to ``logs/app.log`` (rotating), with personal path parts redacted."""
from __future__ import annotations

import getpass
import logging
import logging.handlers
import os
import re
from pathlib import Path

__all__ = ["setup_logging", "sanitize", "RedactingFilter"]


def _home_patterns() -> list[tuple[re.Pattern, str]]:
    pats: list[tuple[re.Pattern, str]] = []
    home = os.path.expanduser("~")
    if home and home not in ("~", "/", "/root"):
        pats.append((re.compile(re.escape(home).replace(r"\\", r"[\\/]"), re.I), "%USERPROFILE%"))
    # generic Windows profile paths: C:\Users\<name>
    pats.append((re.compile(r"([A-Za-z]:[\\/]Users[\\/])[^\\/\s\"']+", re.I), r"\1<user>"))
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - platform specific
        user = ""
    if user and len(user) >= 3 and user.lower() not in ("root", "user", "admin", "claude"):
        pats.append((re.compile(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", re.I), "<user>"))
    return pats


_PATTERNS = _home_patterns()


def sanitize(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = sanitize(msg)
        record.args = ()
        return True


def setup_logging(log_dir: Path, level: int = logging.INFO, console: bool = False) -> Path:
    """Configure the root logger. Safe to call repeatedly."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "app.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cbbe3bbb", False):
            root.removeHandler(h)
            h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(RedactingFilter())
    fh._cbbe3bbb = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        ch.addFilter(RedactingFilter())
        ch.setLevel(logging.WARNING)
        ch._cbbe3bbb = True  # type: ignore[attr-defined]
        root.addHandler(ch)
    root.setLevel(level)
    return path
