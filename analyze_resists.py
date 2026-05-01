"""
Per-mob resistance / weakness inference from observed combat logs.

For each (mob, damage_school) pair, computes mean damage you dealt. Compares to
the global baseline (your average for that school across all mobs). Ratios:
  ratio < 0.6  → resistant
  ratio < 0.4  → strongly resistant
  ratio > 1.4  → weak
  ratio > 1.6  → strongly weak

Output: a per-mob table, plus a top-resistances and top-weaknesses summary.
Conservative: requires n>=15 hits per (mob, school) before reporting a ratio.

Usage:
    python analyze_resists.py [--logs-dir DIR] [--min-samples N] [--mob NAME]
"""
import argparse
import glob
import os
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import dps_tracker as t


def collect(paths):
    """Walk logs, return {(mob, school): [hits], ...} keyed by mob+school."""
    per_mob_school = defaultdict(list)
    per_school     = defaultdict(list)
    for p in paths:
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for raw in f:
                    parts = raw.rstrip('\n').split('|', 3)
                    if len(parts) < 4 or parts[0] != 'OUT':
                        continue
                    try: dmg = int(parts[2])
                    except ValueError: continue
                    msg = parts[3]
                    mob = t.extract_mob_outgoing(msg)
                    if not mob:
                        continue
                    school = t.categorize_outgoing(msg)
                    if school in ('Unknown', 'Justice'):
                        # Justice is a player ability, not informative for mob resists.
                        continue
                    per_mob_school[(mob, school)].append(dmg)
                    per_school[school].append(dmg)
        except OSError as e:
            print(f"  [warn] {p}: {e}", file=sys.stderr)
    return per_mob_school, per_school


def label_ratio(ratio):
    if ratio < 0.4:  return 'STRONG RESIST'
    if ratio < 0.6:  return 'resist'
    if ratio > 1.6:  return 'STRONG WEAK'
    if ratio > 1.4:  return 'weak'
    return 'normal'


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


def main():
    ap = argparse.ArgumentParser(description="Per-mob resistance inference from combat logs.")
    ap.add_argument('--logs-dir', default=None,
                    help='Directory to scan (default: script + ./dist)')
    ap.add_argument('--extra-glob', action='append', default=[])
    ap.add_argument('--min-samples', type=int, default=15,
                    help='Minimum hits per (mob, school) before inferring (default 15)')
    ap.add_argument('--global-min', type=int, default=30,
                    help='Minimum hits for global baseline per school (default 30)')
    ap.add_argument('--mob', default=None,
                    help='Filter to one mob name (substring match, case-insensitive)')
    args = ap.parse_args()

    paths = set()
    if args.logs_dir:
        paths.update(glob.glob(os.path.join(args.logs_dir, 'dps_events_*.log')))
    else:
        paths.update(glob.glob(str(SCRIPT_DIR / 'dps_events_*.log')))
        paths.update(glob.glob(str(SCRIPT_DIR / 'dist' / 'dps_events_*.log')))
    for g in args.extra_glob:
        paths.update(glob.glob(g))
    paths = sorted(paths)
    if not paths:
        print("No dps_events_*.log files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Replaying {len(paths)} log files...")
    per_mob_school, per_school = collect(paths)

    # Global baseline per school (must clear --global-min)
    baseline = {}
    print()
    print("=== Global baselines (your avg per school across all mobs) ===")
    for sch, hits in sorted(per_school.items()):
        if len(hits) < args.global_min:
            print(f"  {sch:<8}  n={len(hits):>4}  (skipped, below --global-min={args.global_min})")
            continue
        m = mean(hits)
        baseline[sch] = m
        print(f"  {sch:<8}  n={len(hits):>5}  mean={m:>6.1f}")

    # Per-mob resistance table
    print()
    print("=== Per-mob resistance / weakness (n >= --min-samples) ===")
    print(f"{'Mob':<32} {'School':<8} {'n':>5} {'mean':>7} {'ratio':>7}  Label")
    print('-' * 80)
    rows = []
    for (mob, sch), hits in per_mob_school.items():
        if args.mob and args.mob.lower() not in mob.lower():
            continue
        if len(hits) < args.min_samples:
            continue
        if sch not in baseline:
            continue
        m = mean(hits)
        ratio = m / baseline[sch]
        rows.append((mob, sch, len(hits), m, ratio))
    # Sort: mob alphabetical, school by ratio so anomalies surface
    rows.sort(key=lambda r: (r[0].lower(), -abs(r[4] - 1.0)))
    for mob, sch, n, m, ratio in rows:
        label = label_ratio(ratio)
        if label == 'normal':
            continue  # skip the boring rows
        print(f"{mob[:30]:<32} {sch:<8} {n:>5} {m:>7.1f} {ratio:>7.2f}  {label}")

    # Top resistances and weaknesses across all mobs
    print()
    print("=== Top STRONG RESIST (smallest ratios) ===")
    for mob, sch, n, m, ratio in sorted(rows, key=lambda r: r[4])[:10]:
        if ratio < 0.6:
            print(f"  {mob:<32} {sch:<8}  ratio={ratio:.2f}  ({n} hits, mean {m:.0f} vs baseline {baseline[sch]:.0f})")
    print()
    print("=== Top STRONG WEAK (largest ratios) ===")
    for mob, sch, n, m, ratio in sorted(rows, key=lambda r: -r[4])[:10]:
        if ratio > 1.4:
            print(f"  {mob:<32} {sch:<8}  ratio={ratio:.2f}  ({n} hits, mean {m:.0f} vs baseline {baseline[sch]:.0f})")


if __name__ == '__main__':
    main()
