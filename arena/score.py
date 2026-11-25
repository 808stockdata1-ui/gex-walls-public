"""Compare GEX Walls versions on the same yardstick (read-only).

Usage: python arena/score.py <folder> [<folder> ...]
Each folder is a clone (or Joe's live GEXWalls folder); `gexwalls/audit/` or `audit/` inside it is read.
Prints: latest audit score, failed checks, and graded-pick stats from that version's own picks_log.jsonl.
"""
from __future__ import annotations

import glob
import json
import statistics
import sys
from pathlib import Path

MIN_COUNTED = 0.10          # same rule as audit.py: picks paying < 10% of capital are not scored


def counted(x: dict) -> bool:
    if x.get("type") == "swing":
        return bool(x.get("price")) and (x.get("target") or 0) / x["price"] - 1 >= MIN_COUNTED - 1e-9
    return (x.get("g") or 0) >= MIN_COUNTED - 1e-9


def audit_dir(folder: Path) -> Path | None:
    for d in (folder / "gexwalls" / "audit", folder / "audit"):
        if (d / "history.jsonl").exists() or (d / "picks_log.jsonl").exists():
            return d
    return None


def one(folder: Path) -> dict:
    d = audit_dir(folder)
    out = {"version": folder.name, "audit": None, "failed": None, "closed": 0, "clusters": 0,
           "win": None, "pred": None, "brier": None, "pnl": None}
    if not d:
        return out
    hist = [json.loads(x) for x in (d / "history.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
        if (d / "history.jsonl").exists() else []
    if hist:
        out["audit"], out["failed"], out["when"] = hist[-1].get("overall"), hist[-1].get("n_failed"), \
            f"{hist[-1].get('date')} {hist[-1].get('time', '')}"
    log = d / "picks_log.jsonl"
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()] if log.exists() else []
    closed = [x for x in rows if x.get("status") == "closed" and counted(x) and x.get("p") is not None]
    if closed:
        out.update(closed=len(closed), clusters=len({(x["ticker"], x.get("exp") or x.get("closed")) for x in closed}),
                   win=sum(1 for x in closed if x["win"]) / len(closed) * 100,
                   pred=statistics.fmean(x["p"] for x in closed) * 100,
                   brier=statistics.fmean((x["p"] - (1 if x["win"] else 0)) ** 2 for x in closed),
                   pnl=statistics.fmean(x.get("pnl_pct") or 0 for x in closed))
    return out


def main(args: list[str]) -> None:
    folders = [Path(p) for a in args for p in (glob.glob(a) or [a]) if Path(p).is_dir()]
    if not folders:
        sys.exit(__doc__)
    f = lambda v, n=1: "-" if v is None else f"{v:.{n}f}"
    print(f"{'version':<22}{'audit':>6}{'fail':>5}{'closed':>7}{'clust':>6}{'win%':>7}{'pred%':>7}{'Brier':>7}{'P/L%':>7}")
    for r in sorted((one(p) for p in folders), key=lambda r: (r["brier"] is None, r["brier"] or 0)):
        print(f"{r['version']:<22}{f(r['audit'], 0):>6}{f(r['failed'], 0):>5}{r['closed']:>7}{r['clusters']:>6}"
              f"{f(r['win']):>7}{f(r['pred']):>7}{f(r['brier'], 3):>7}{f(r['pnl']):>7}")
    print("Compare picks only once a version has >= 20 clusters; lower Brier = better calibrated.")


if __name__ == "__main__":
    main(sys.argv[1:])
