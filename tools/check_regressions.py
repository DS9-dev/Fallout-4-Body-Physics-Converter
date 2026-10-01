"""Check the known regression cases against a real installation (read-only).

    python tools\\check_regressions.py --mo2 "T:\\MO_Main _ baseCBBE" [--report reports\\latest.json]

With --report it reads an existing analysis instead of re-running it.
Expected outcomes come from the Phase 1 specification:
  * MM69 Raider Punk Top V2 (CBBE)       -> CONVERT
  * Vtaw BlackMamba / Wardrobe 8 3BBB    -> KEEP (never re-converted)
  * ASA 3BBB conversion                  -> KEEP
  * CP5 jacket (historical problem case) -> REVIEW (never an automatic CONVERT)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbbe3bbb.app.controller import AppController, AppPaths  # noqa: E402
from cbbe3bbb.profiles.store import Profile  # noqa: E402
from cbbe3bbb.reports.writer import load_report  # noqa: E402

# (label, mod regex, project regex, allowed actions)
CASES = [
    ("Raider Punk Top V1 (CBBE) - Phase 2 target", r"raider punk", r"^MM69 Raider Punk Top - CBBE$", {"CONVERT"}),
    ("Raider Punk Top V1 3BBB AUTO (after conversion)", r"3bbb auto", r"^MM69 Raider Punk Top - CBBE - 3BBB AUTO$", {"KEEP"}),
    ("Raider Punk Top V2 (CBBE)", r"raider punk", r"^MM69 Raider Punk Top V2 - CBBE$", {"CONVERT"}),
    ("Vtaw BlackMamba 3BBB", r"blackmamba 3bbb", r".", {"KEEP"}),
    ("Vtaw Wardrobe 8 3BBB (converted items)", r"wardrobe8twb-3b", r"(Shorts|Pants|Top)_TWB$", {"KEEP"}),
    ("ASA 3BBB", r"asa .*3bbb", r".", {"KEEP"}),
    ("CP5 jacket", r"clothes pack 5", r"jacket", {"REVIEW"}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mo2")
    ap.add_argument("--game")
    ap.add_argument("--report")
    ap.add_argument("--include-disabled", action="store_true", default=True)
    a = ap.parse_args()
    if a.report:
        rep = load_report(Path(a.report))
    else:
        ctl = AppController(AppPaths.default())
        prof = Profile(name="regression", mo2_dir=a.mo2 or "", game_dir=a.game or "")
        prof.analysis.include_disabled_mods = a.include_disabled
        ctl.detect(prof)
        rep = ctl.analyze(write=False)
    failures = 0
    for label, mod_re, proj_re, allowed in CASES:
        hits = [p for p in rep.projects if re.search(mod_re, p.mod, re.I) and re.search(proj_re, p.project, re.I)]
        if not hits:
            print(f"[ ? ] {label}: not found in this installation")
            continue
        bad = [p for p in hits if p.action not in allowed]
        state = "OK " if not bad else "BAD"
        print(f"[{state}] {label}: {len(hits)} project(s), expected {'/'.join(sorted(allowed))}")
        for p in bad[:10]:
            print(f"        {p.action:<11} {p.project}  <- {p.reasons[0] if p.reasons else ''}")
        failures += bool(bad)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
