"""A read-only, case-insensitive virtual view of one Data sub-tree.

Mod Organizer 2 overlays every enabled mod on top of the game's Data folder:
the file from the highest-priority provider wins, and ``overwrite`` is above
every mod. BodySlide sees that merged tree when it is launched through MO2.
This module rebuilds the same view for the ``Tools/BodySlide`` sub-tree
without mounting anything.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

__all__ = ["DataProvider", "VirtualFile", "VirtualFS", "find_case_insensitive"]


@dataclass(frozen=True)
class DataProvider:
    name: str  # mod name, "<overwrite>", "<game data>" or a user label
    root: Path  # the folder that corresponds to Data/
    priority: int  # higher wins
    kind: str = "mod"  # "game", "mod", "overwrite", "extra"


@dataclass(frozen=True)
class VirtualFile:
    rel: str  # path relative to the sub-tree, original case, forward slashes
    abs_path: Path
    provider: DataProvider
    size: int
    mtime: float

    @property
    def key(self) -> str:
        return self.rel.lower()


def find_case_insensitive(root: Path, rel: str) -> Optional[Path]:
    """Return the real path of ``root/rel`` matching each component case-insensitively."""
    cur = root
    for part in [p for p in rel.replace("\\", "/").split("/") if p]:
        direct = cur / part
        if direct.exists():
            cur = direct
            continue
        try:
            with os.scandir(cur) as it:
                match = next((e.name for e in it if e.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        cur = cur / match
    return cur


@dataclass
class VirtualFS:
    subtree: str
    providers: list[DataProvider]
    files: dict[str, list[VirtualFile]] = field(default_factory=dict)  # key -> versions, highest priority first
    scan_errors: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        providers: Iterable[DataProvider],
        subtree: str = "Tools/BodySlide",
        include: Optional[Callable[[str], bool]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> "VirtualFS":
        provs = sorted(providers, key=lambda p: p.priority, reverse=True)
        vfs = cls(subtree=subtree, providers=provs)
        for prov in provs:
            if cancel and cancel():
                break
            base = find_case_insensitive(prov.root, subtree)
            if base is None or not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, onerror=lambda e, _p=prov: vfs.scan_errors.append(f"{_p.name}: {e}")):
                rel_dir = os.path.relpath(dirpath, base)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                for fn in filenames:
                    rel = f"{rel_dir}/{fn}" if rel_dir else fn
                    if include and not include(rel):
                        continue
                    ap = Path(dirpath) / fn
                    try:
                        st = ap.stat()
                    except OSError as exc:
                        vfs.scan_errors.append(f"{prov.name}: {exc}")
                        continue
                    vf = VirtualFile(rel=rel, abs_path=ap, provider=prov, size=st.st_size, mtime=st.st_mtime)
                    vfs.files.setdefault(vf.key, []).append(vf)
        return vfs

    def resolve(self, rel: str) -> Optional[VirtualFile]:
        versions = self.files.get(rel.replace("\\", "/").lower().strip("/"))
        return versions[0] if versions else None

    def versions(self, rel: str) -> list[VirtualFile]:
        return list(self.files.get(rel.replace("\\", "/").lower().strip("/"), []))

    def iter_winners(self, prefix: str = "", suffixes: tuple[str, ...] = ()) -> Iterator[VirtualFile]:
        pre = prefix.lower().strip("/")
        pre = pre + "/" if pre else ""
        for key in sorted(self.files):
            if pre and not key.startswith(pre):
                continue
            if suffixes and not key.endswith(tuple(s.lower() for s in suffixes)):
                continue
            yield self.files[key][0]

    def overridden(self) -> dict[str, list[VirtualFile]]:
        """Files provided by more than one provider (conflicts)."""
        return {k: v for k, v in self.files.items() if len(v) > 1}
