"""
Cross-session mob-stats aggregator for the Wyvern DPS tracker.

Walks every dps_events_*.log file on disk, replays the events through the same
extraction/categorization pipeline the live tracker uses, and emits one
wiki-markup report per mob into mob_reports/.

Usage:
    python aggregate_mobs.py [--logs-dir DIR] [--out-dir DIR] [--min-samples N]

The default behavior:
  - Scans the script directory and ./dist for dps_events_*.log files
  - Writes one .txt file per mob into mob_reports/
  - Also writes a summary mob_reports/_index.txt with all mobs sorted by sample size
  - Uses the same WIKI_MIN_SAMPLES gating as the live tracker, so low-sample
    stats are commented out instead of published
"""
import argparse
import glob
import os
import re
import sys
from pathlib import Path

# Reuse the live tracker's parsing + formatting logic. Keeps a single source
# of truth for verbs/regexes/wiki layout - no copy-paste drift.
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import dps_tracker as t


def replay_log(path, mob_stats, active_encounters, kill_state):
    """Replay one log file's events into the shared mob_stats dict."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                parts = raw.rstrip('\n').split('|', 3)
                if len(parts) < 3:
                    continue
                etype = parts[0]
                try:
                    ts = int(parts[1])
                except ValueError:
                    continue

                if etype == 'OUT' and len(parts) >= 4:
                    try: dmg = int(parts[2])
                    except ValueError: continue
                    msg = parts[3]
                    mob = t.extract_mob_outgoing(msg)
                    if not mob:
                        continue
                    ms = mob_stats.setdefault(mob, t._new_mob_record())
                    if not ms['first_seen_ms']: ms['first_seen_ms'] = ts
                    ms['last_seen_ms'] = ts
                    ms['damage'] += dmg
                    ms['hits']   += 1
                    verb = t._extract_outgoing_verb(msg)
                    if verb:
                        t._update_verb_stat(ms['out_verbs'], verb, dmg)
                    enc = active_encounters.get(mob)
                    if enc is None:
                        active_encounters[mob] = {'damage': dmg, 'last_ts': ts}
                    else:
                        enc['damage'] += dmg
                        enc['last_ts'] = ts

                elif etype == 'IN' and len(parts) >= 3:
                    try: dmg = int(parts[2])
                    except ValueError: continue
                    if dmg <= 0:
                        continue
                    msg = parts[3] if len(parts) >= 4 else ""
                    cat = t.categorize_incoming(msg) if msg else 'Unknown'
                    src = t.extract_mob_incoming(msg) if msg else None
                    if not src:
                        continue
                    ms = mob_stats.setdefault(src, t._new_mob_record())
                    if not ms['first_seen_ms']: ms['first_seen_ms'] = ts
                    ms['last_seen_ms'] = ts
                    ms['damage_in'] += dmg
                    ms['hits_in']   += 1
                    ms['in_types'][cat] = ms['in_types'].get(cat, 0) + dmg
                    verb = t._extract_incoming_verb(msg)
                    if verb:
                        t._update_verb_stat(ms['in_verbs'], verb, dmg)

                elif etype == 'KILL' and len(parts) >= 3:
                    data = parts[2] if len(parts) == 3 else parts[2]
                    mob = t.extract_mob_kill(data)
                    if not mob:
                        continue
                    ms = mob_stats.setdefault(mob, t._new_mob_record())
                    ms['kills'] += 1
                    ms['last_seen_ms'] = ts
                    enc = active_encounters.pop(mob, None)
                    if enc and enc['damage'] > 0:
                        ms['encounter_damages'].append(enc['damage'])
                    kill_state['last_mob'] = mob
                    kill_state['last_ts']  = ts

                elif etype == 'EXP' and len(parts) >= 3:
                    try: xp = int(parts[2])
                    except ValueError: continue
                    last_mob = kill_state.get('last_mob')
                    last_ts  = kill_state.get('last_ts', 0)
                    if last_mob and ts - last_ts < 2000:
                        ms = mob_stats.setdefault(last_mob, t._new_mob_record())
                        ms['xp'] += xp
                        ms['xp_samples'].append(xp)

                # AGENT_READY / ATTACHED / DEATH / DBG ignored for aggregation
    except OSError as e:
        print(f"  [warn] {path}: {e}")


def safe_filename(s):
    """Make a mob name safe to use as a filename across OSes."""
    return re.sub(r'[^A-Za-z0-9._-]+', '_', s).strip('_') or 'unnamed'


def main():
    ap = argparse.ArgumentParser(description="Aggregate Wyvern combat logs into per-mob wiki reports.")
    ap.add_argument('--logs-dir', default=None,
                    help='Directory to scan for dps_events_*.log (default: script + ./dist)')
    ap.add_argument('--out-dir', default=str(SCRIPT_DIR / 'mob_reports'),
                    help='Directory to write per-mob reports (default: ./mob_reports)')
    ap.add_argument('--extra-glob', action='append', default=[],
                    help='Additional glob to scan, repeatable')
    args = ap.parse_args()

    # Collect logs
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

    print(f"Replaying {len(paths)} log files into shared mob_stats...")
    mob_stats = {}
    active_encounters = {}
    kill_state = {}
    for p in paths:
        replay_log(p, mob_stats, active_encounters, kill_state)
    print(f"Aggregated {len(mob_stats)} unique mobs.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-mob report files
    for mob, st in mob_stats.items():
        markup = t._format_mob_for_wiki(mob, st)
        fname = safe_filename(mob) + '.txt'
        (out_dir / fname).write_text(markup + "\n", encoding='utf-8')

    # Index file - sorted by total samples
    rows = sorted(
        mob_stats.items(),
        key=lambda x: -(x[1]['hits'] + x[1]['hits_in']))
    idx_lines = [
        f"# Wyvern mob aggregation - {len(mob_stats)} mobs from {len(paths)} log files",
        "",
        f"{'Mob':<32} {'Out hits':>9} {'In hits':>8} {'Kills':>6} {'Encounters':>11} {'XP samp':>8}",
        "-" * 80,
    ]
    for mob, st in rows:
        idx_lines.append(
            f"{mob[:30]:<32} {st['hits']:>9} {st['hits_in']:>8} "
            f"{st['kills']:>6} {len(st['encounter_damages']):>11} "
            f"{len(st['xp_samples']):>8}")
    (out_dir / '_index.txt').write_text("\n".join(idx_lines) + "\n", encoding='utf-8')

    print(f"Wrote {len(mob_stats)} per-mob reports + _index.txt to {out_dir}/")


if __name__ == '__main__':
    main()
