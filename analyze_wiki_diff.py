"""
Compare observed mob stats (from logs) against the published Wyvern wiki.

For each mob you've fought enough times, fetches the wiki page from
http://wiki.wyvernsource.com, parses HP / XP fields out of the page text,
and prints only the mismatches. Read-only - never edits anything.

Usage:
    python analyze_wiki_diff.py [--logs-dir DIR] [--mob NAME] [--min-kills 30]
"""
import argparse
import glob
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import dps_tracker as t

WIKI_BASE = "http://wiki.wyvernsource.com/index.php"


# Patterns to find numeric fields in wiki page source. The wiki uses both
# infobox templates ({{Monster | hp = N}}) and prose ("HP: N"). Be lenient.
HP_PATTERNS = [
    re.compile(r'\|\s*hp\s*=\s*([\d,]+)',         re.I),
    re.compile(r'\|\s*hit[\s_-]*points?\s*=\s*([\d,]+)', re.I),
    re.compile(r'\bHP\s*[:=]\s*([\d,]+)',         re.I),
    re.compile(r'\bHit\s+Points?\s*[:=]\s*([\d,]+)', re.I),
]
XP_PATTERNS = [
    re.compile(r'\|\s*xp\s*=\s*([\d,]+)',         re.I),
    re.compile(r'\|\s*experience\s*=\s*([\d,]+)', re.I),
    re.compile(r'\bXP\s*[:=]\s*([\d,]+)',         re.I),
    re.compile(r'\bExperience\s*[:=]\s*([\d,]+)', re.I),
]


def fetch_wiki_source(mob_name):
    """Fetch the raw wiki source for a mob page. Returns text or None on miss."""
    # Try URL-encoded name first; wiki URLs use _ for spaces
    title = mob_name.replace(' ', '_')
    url = f"{WIKI_BASE}?title={urllib.parse.quote(title)}&action=raw"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # mob not in wiki
        raise
    except urllib.error.URLError:
        return None


def first_match(text, patterns):
    """Return the first numeric value matched by any pattern, as int. None if none match."""
    for p in patterns:
        m = p.search(text)
        if m:
            try:
                return int(m.group(1).replace(',', ''))
            except ValueError:
                continue
    return None


def collect_observed(paths):
    """Replay logs into a per-mob stats dict (subset of dps_tracker schema)."""
    mob_stats = {}
    active_encounters = {}
    last_kill = {}
    for p in paths:
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for raw in f:
                    parts = raw.rstrip('\n').split('|', 3)
                    if len(parts) < 3:
                        continue
                    etype = parts[0]
                    try: ts = int(parts[1])
                    except ValueError: continue
                    if etype == 'OUT' and len(parts) >= 4:
                        try: dmg = int(parts[2])
                        except ValueError: continue
                        mob = t.extract_mob_outgoing(parts[3])
                        if not mob: continue
                        st = mob_stats.setdefault(mob, {'kills':0, 'encounter_damages':[],
                                                       'xp_samples':[]})
                        enc = active_encounters.setdefault(mob, {'damage':0})
                        enc['damage'] += dmg
                    elif etype == 'KILL' and len(parts) >= 3:
                        mob = t.extract_mob_kill(parts[2])
                        if not mob: continue
                        st = mob_stats.setdefault(mob, {'kills':0, 'encounter_damages':[],
                                                       'xp_samples':[]})
                        st['kills'] += 1
                        enc = active_encounters.pop(mob, None)
                        if enc and enc['damage'] > 0:
                            st['encounter_damages'].append(enc['damage'])
                        last_kill['mob'] = mob; last_kill['ts'] = ts
                    elif etype == 'EXP' and len(parts) >= 3:
                        try: xp = int(parts[2])
                        except ValueError: continue
                        if last_kill.get('mob') and ts - last_kill.get('ts', 0) < 2000:
                            mob_stats[last_kill['mob']]['xp_samples'].append(xp)
        except OSError as e:
            print(f"  [warn] {p}: {e}", file=sys.stderr)
    return mob_stats


def main():
    ap = argparse.ArgumentParser(description="Diff observed mob stats against the live Wyvern wiki.")
    ap.add_argument('--logs-dir', default=None)
    ap.add_argument('--extra-glob', action='append', default=[])
    ap.add_argument('--min-kills', type=int, default=10,
                    help='Skip mobs with fewer than N kills observed (default 10)')
    ap.add_argument('--mob', default=None,
                    help='Filter to one mob (substring, case-insensitive)')
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
        print("No dps_events_*.log files found.", file=sys.stderr); sys.exit(1)

    print(f"Replaying {len(paths)} log files...")
    observed = collect_observed(paths)
    candidates = [(m, st) for m, st in observed.items()
                  if st['kills'] >= args.min_kills
                  and (not args.mob or args.mob.lower() in m.lower())]
    candidates.sort(key=lambda x: -x[1]['kills'])
    print(f"{len(candidates)} mobs meet --min-kills={args.min_kills}")
    print()

    mismatches = 0
    no_wiki    = 0
    matches    = 0

    print(f"{'Mob':<32} {'Kills':>5}  {'Wiki HP':>9} {'Obs HP':>9} {'Wiki XP':>9} {'Obs XP':>9}")
    print('-' * 90)
    for mob, st in candidates:
        page = fetch_wiki_source(mob)
        if not page:
            no_wiki += 1
            print(f"{mob[:30]:<32} {st['kills']:>5}  {'(no wiki page)'}")
            continue
        wiki_hp = first_match(page, HP_PATTERNS)
        wiki_xp = first_match(page, XP_PATTERNS)
        obs_hp  = max(st['encounter_damages']) if st['encounter_damages'] else None
        obs_xp  = (sum(st['xp_samples']) / len(st['xp_samples'])
                   if st['xp_samples'] else None)
        # Compute mismatches
        hp_diff = obs_hp != wiki_hp if obs_hp is not None and wiki_hp is not None else False
        xp_diff = (obs_xp is not None and wiki_xp is not None and
                   abs(obs_xp - wiki_xp) / max(wiki_xp, 1) > 0.05)  # 5% tolerance
        if hp_diff or xp_diff:
            mismatches += 1
            mark = "MISMATCH"
        else:
            matches += 1
            mark = "ok"
        hp_str = (f"{wiki_hp:>9}" if wiki_hp is not None else f"{'-':>9}")
        oh_str = (f"{obs_hp:>9}"  if obs_hp  is not None else f"{'-':>9}")
        xp_str = (f"{wiki_xp:>9}" if wiki_xp is not None else f"{'-':>9}")
        ox_str = (f"{obs_xp:>9.0f}" if obs_xp is not None else f"{'-':>9}")
        print(f"{mob[:30]:<32} {st['kills']:>5}  {hp_str} {oh_str} {xp_str} {ox_str}  {mark}")

    print()
    print(f"Summary: {mismatches} mismatch, {matches} match, {no_wiki} no wiki page found")


if __name__ == '__main__':
    main()
