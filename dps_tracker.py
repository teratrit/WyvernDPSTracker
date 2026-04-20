"""
Wyvern DPS Tracker
Hooks into the Wyvern game client via Java Attach API to capture all combat
damage (outgoing and incoming) with millisecond timestamps and per-type breakdown.
"""

import atexit
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from pynput import keyboard as kb

SCRIPT_DIR  = Path(__file__).parent.resolve()
LOG_FILE    = SCRIPT_DIR / "dps_events_v2.log"
SESSION_GAP = 15  # seconds of inactivity before session auto-ends

# ── Damage categorization ─────────────────────────────────────────────────────

# Unambiguous outgoing verb → type (checked first to avoid monster-name false positives)
VERB_TYPES = {
    'poisoned': 'Poison', 'poison':   'Poison',
    'froze':    'Cold',   'freeze':   'Cold',
    'burned':   'Fire',   'burn':     'Fire',
    'scorched': 'Fire',   'scorch':   'Fire',
    'engulfed': 'Fire',   'engulf':   'Fire',
    'shocked':  'Shock',  'shock':    'Shock',
    'zapped':   'Shock',  'zap':      'Shock',
    'blasted':  'Shock',  'blast':    'Shock',
    'corroded': 'Acid',   'corrode':  'Acid',
    'drowned':  'Water',  'drown':    'Water',
    'decimated':'Holy',   'decimate': 'Holy',
    'condemned':'Holy',   'condemn':  'Holy',
}

# Physical outgoing verbs — fallback after elemental checks
OUTGOING_VERB_RE = re.compile(r'^You\s+(\w+)', re.I)
PHYSICAL_VERBS = {
    'cut':       'Cut',   'slice':     'Cut',   'sliced':    'Cut',
    'slash':     'Cut',   'slashed':   'Cut',   'carve':     'Cut',
    'carved':    'Cut',   'cleave':    'Cut',   'cleaved':   'Cut',
    'hew':       'Cut',   'hewed':     'Cut',
    'smash':     'Smash', 'smashed':   'Smash', 'crush':     'Smash',
    'crushed':   'Smash', 'slam':      'Smash', 'slammed':   'Smash',
    'bash':      'Smash', 'bashed':    'Smash', 'pummel':    'Smash',
    'pummeled':  'Smash', 'smote':     'Smash', 'smite':     'Smash',
    'stagger':   'Smash', 'staggered': 'Smash', 'hit':       'Smash',
    'strike':    'Smash', 'struck':    'Smash', 'graze':     'Smash',
    'grazed':    'Smash', 'overwhelm': 'Smash', 'overwhelmed':'Smash',
    'stab':      'Stab',  'stabbed':   'Stab',  'pierce':    'Stab',
    'pierced':   'Stab',  'skewer':    'Stab',  'skewered':  'Stab',
    'impale':    'Stab',  'impaled':   'Stab',
}

# Incoming verbs
INCOMING_VERB_RE = re.compile(
    r'(hits|damages|slashes|stabs|bites|claws|burns|zaps|smashes|crushes|'
    r'strikes|blasts|freezes|shocks|drowns|staggers|cuts|pierces|impales|graze[sd]?)\s+you', re.I)
INCOMING_VERBS = {
    'hits':    'Smash', 'damages': 'Smash', 'strikes': 'Smash',
    'smashes': 'Smash', 'crushes': 'Smash', 'staggers':'Smash', 'graze':   'Smash', 'grazes':  'Smash', 'grazed':  'Smash',
    'slashes': 'Cut',   'cuts':    'Cut',   'claws':   'Cut',
    'stabs':   'Stab',  'pierces': 'Stab',  'impales': 'Stab',  'bites':   'Stab',
    'burns':   'Fire',  'zaps':    'Shock', 'shocks':  'Shock', 'freezes': 'Cold',
    'blasts':  'Shock', 'drowns':  'Water',
}

# Element keyword patterns — used for flavor-text scan (outgoing) and full-msg scan (incoming)
# Note: no \bice\b or \bfrozen\b to avoid matching monster names in incoming messages
ELEMENT_PATTERNS = [
    (re.compile(r'poison|venom',                                                    re.I), 'Poison'),
    (re.compile(r'lightning|energy.surge|electr',                                   re.I), 'Shock'),
    (re.compile(r'flame|burn|fire|inferno|incinerat|scorch|sear|magma|lava|\bhot\b',re.I), 'Fire'),
    (re.compile(r'arctic|glacial|frost|freeze|froze|chill|blizzard',               re.I), 'Cold'),
    (re.compile(r'acid|corrosi|dissolv|caustic',                                    re.I), 'Acid'),
    (re.compile(r'water|drown|flood',                                               re.I), 'Water'),
    (re.compile(r'death|necrotic|drain|dark.energy|shadow|unholy|wither',          re.I), 'Death'),
    (re.compile(r'holy|radiance|divinity|divine|vengeance|sacred',                 re.I), 'Holy'),
    (re.compile(r'spirit|arcane|rend',                                              re.I), 'Magic'),
]

# Extracts flavor text between a preposition ("with"/"in") and "for X damage"
FLAVOR_RE     = re.compile(r'\b(?:with|in)\s+(.+?)\s+for\s+\d+\s+damage', re.I)
HOLE_RE       = re.compile(r'make a hole|daylight through', re.I)
NEARLY_CUT_RE = re.compile(r'nearly cut.*in half', re.I)

TYPE_COLORS = {
    'Shock':  '#87ceeb', 'Fire':   '#ff6347', 'Cold':   '#add8e6',
    'Acid':   '#7fff00', 'Death':  '#9370db', 'Poison': '#00ff7f',
    'Holy':   '#da70d6', 'Magic':  '#9966ff', 'Water':  '#4169e1',
    'Cut':    '#ffa500', 'Smash':  '#cd853f', 'Stab':   '#daa520',
}


def _check_elements(text):
    for pat, dtype in ELEMENT_PATTERNS:
        if pat.search(text):
            return dtype
    return None


def categorize_outgoing(msg):
    if not msg:
        return 'Unknown'
    # 1. Unambiguous verb (avoids monster-name false positives like "Ice Riagor")
    m = OUTGOING_VERB_RE.match(msg)
    if m:
        verb = m.group(1).lower()
        if verb in VERB_TYPES:
            return VERB_TYPES[verb]
    # 2. Element keywords in flavor text only (safe — no monster names after "with/in")
    flavor = FLAVOR_RE.search(msg)
    if flavor:
        elem = _check_elements(flavor.group(1))
        if elem:
            return elem
    # 3. Magic markers
    if re.search(r"'s spirit\b", msg, re.I) or re.search(r'\brend\b', msg, re.I):
        return 'Magic'
    # 4. Physical verb fallback
    if HOLE_RE.search(msg):
        return 'Stab'
    if NEARLY_CUT_RE.search(msg):
        return 'Cut'
    if m:
        return PHYSICAL_VERBS.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


def categorize_incoming(msg):
    if not msg:
        return 'Unknown'
    # Full message element scan (incoming messages don't start with player's attack verb)
    elem = _check_elements(msg)
    if elem:
        return elem
    m = INCOMING_VERB_RE.search(msg)
    if m:
        return INCOMING_VERBS.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


# ── Session ────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    start_ms: int  = 0
    end_ms:   int  = 0
    total:    int  = 0
    count:    int  = 0
    max_hit:  int  = 0
    hits:     list = field(default_factory=list)
    active:   bool = True
    cats:     dict = field(default_factory=lambda: defaultdict(
        lambda: {'damage': 0, 'count': 0, 'max': 0}))

    @property
    def elapsed_s(self):
        end = self.end_ms or int(time.time() * 1000)
        return (end - self.start_ms) / 1000.0 if self.start_ms else 0.0

    @property
    def dps(self):
        e = self.elapsed_s
        return self.total / e if e > 0 else 0.0

    @property
    def avg(self):
        return self.total / self.count if self.count else 0.0

    def crit_stats(self):
        """Auto-detect normal/crit split via 1D k-means (k=2).
        Returns dict with normal_avg, crit_avg, crit_pct, crit_boost
        or None if there aren't enough distinct hits to split."""
        if self.count < 6:
            return None
        dmg = sorted(d for _, d, _ in self.hits)
        # Initial split at mean
        split = sum(dmg) / len(dmg)
        for _ in range(10):
            low  = [d for d in dmg if d <  split]
            high = [d for d in dmg if d >= split]
            if not low or not high:
                return None
            low_avg  = sum(low)  / len(low)
            high_avg = sum(high) / len(high)
            new_split = (low_avg + high_avg) / 2
            if abs(new_split - split) < 0.5:
                break
            split = new_split
        # Require meaningful separation — at least 20% above normal avg
        if high_avg < low_avg * 1.20:
            return None
        crit_pct   = len(high) / len(dmg) * 100
        crit_boost = (high_avg - low_avg) / low_avg * 100
        return {
            'normal_avg': low_avg,
            'crit_avg':   high_avg,
            'crit_pct':   crit_pct,
            'crit_boost': crit_boost,
        }

    def add(self, ts, damage, cat='Unknown'):
        if not self.start_ms:
            self.start_ms = ts
        self.hits.append((ts, damage, cat))
        self.total   += damage
        self.count   += 1
        self.max_hit  = max(self.max_hit, damage)
        c = self.cats[cat]
        c['damage'] += damage
        c['count']  += 1
        c['max']     = max(c['max'], damage)

    def finalize(self):
        self.end_ms = self.hits[-1][0] if self.hits else 0
        self.active = False


# ── GUI ────────────────────────────────────────────────────────────────────────

class DPSTrackerGUI:
    def __init__(self):
        self.out_session   = None
        self.in_session    = None
        self.last_out_ms   = 0
        self.last_in_ms    = 0
        self._out_dummy    = False
        self._paused       = True
        self.exp_total     = 0
        self.exp_start_ms  = 0
        self.kill_count    = 0
        self._shutdown   = False
        self._build_ui()
        self._start_log_reader()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Wyvern DPS Tracker")
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#0d1117')
        self.root.geometry('440x820')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        def _on_key(key):
            if key == kb.Key.f12:
                self.root.after(0, self._toggle_tracking)
        self._hotkey = kb.Listener(on_press=_on_key)
        self._hotkey.daemon = True
        self._hotkey.start()

        big  = tkfont.Font(family='Consolas', size=28, weight='bold')
        lbl  = tkfont.Font(family='Consolas', size=10)
        sm   = tkfont.Font(family='Consolas', size=9)

        self.status = tk.Label(self.root, text="Connecting...", fg='#f0883e',
                               bg='#0d1117', font=sm, anchor='w')
        self.status.pack(fill=tk.X, padx=12, pady=(8, 0))

        self.out_dps, self.out_stats, self.out_bd = self._build_section(
            "OUTGOING", '#3fb950', big, lbl, sm,
            extra_stats=["Normal", "Crit Avg", "Crit %", "Crit Boost"])
        self.in_dps,  self.in_stats,  self.in_bd  = self._build_section(
            "INCOMING", '#f85149', big, lbl, sm)

        # Timing row
        tf = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        tf.pack(fill=tk.X, padx=12, pady=4)
        self.time_labels = {}
        for key in ("Session Start", "Duration"):
            row = tk.Frame(tf, bg='#161b22')
            row.pack(fill=tk.X, padx=8)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=sm, width=14, anchor='w').pack(side=tk.LEFT)
            v = tk.Label(row, text="—", fg='#c9d1d9', bg='#161b22', font=sm, anchor='e')
            v.pack(side=tk.RIGHT)
            self.time_labels[key] = v

        # EXP tracker
        ef = tk.Frame(self.root, bg='#0d1117')
        ef.pack(fill=tk.X, padx=12, pady=(4, 0))
        tk.Label(ef, text="EXP", fg='#d2a8ff', bg='#0d1117',
                 font=lbl, anchor='w').pack(side=tk.LEFT)
        self.exp_rate_lbl = tk.Label(ef, text="— XP/hr", fg='#484f58',
                                     bg='#0d1117', font=big, anchor='e')
        self.exp_rate_lbl.pack(side=tk.RIGHT)

        erf = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        erf.pack(fill=tk.X, padx=12, pady=2)
        self.exp_labels = {}
        for key in ("Total XP", "Kills"):
            row = tk.Frame(erf, bg='#161b22')
            row.pack(fill=tk.X, padx=8)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=sm, width=8, anchor='w').pack(side=tk.LEFT)
            v = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22', font=sm, anchor='e')
            v.pack(side=tk.RIGHT)
            self.exp_labels[key] = v

        # Controls
        bf = tk.Frame(self.root, bg='#0d1117')
        bf.pack(fill=tk.X, padx=12, pady=2)
        self.toggle_btn = tk.Button(
            bf, text="Start (F12)", font=sm, bg='#238636', fg='#ffffff',
            bd=0, cursor='hand2', width=8, command=self._toggle_tracking)
        self.toggle_btn.pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(bf, text="Reset", font=sm, bg='#21262d', fg='#c9d1d9',
                  bd=0, cursor='hand2', width=8, command=self._reset).pack(side=tk.LEFT)


        # Combat log
        lf = tk.Frame(self.root, bg='#0d1117')
        lf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
        tk.Label(lf, text="Combat Log", fg='#7d8590', bg='#0d1117',
                 font=lbl, anchor='w').pack(anchor='w')
        self.log = tk.Text(lf, bg='#161b22', fg='#8b949e', font=('Consolas', 9),
                           height=6, bd=0, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)
        for tag, fg in [('out', '#58a6ff'), ('in', '#f85149'),
                        ('kill', '#3fb950'), ('info', '#7d8590')]:
            self.log.tag_configure(tag, foreground=fg)

        self._tick()

    def _build_section(self, title, color, big, lbl, sm, extra_stats=None):
        hdr = tk.Frame(self.root, bg='#0d1117')
        hdr.pack(fill=tk.X, padx=12, pady=(6, 0))
        tk.Label(hdr, text=title, fg=color, bg='#0d1117',
                 font=lbl, anchor='w').pack(side=tk.LEFT)
        dps_lbl = tk.Label(hdr, text="— DPS", fg='#484f58', bg='#0d1117',
                           font=big, anchor='e')
        dps_lbl.pack(side=tk.RIGHT)

        sf = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        sf.pack(fill=tk.X, padx=12, pady=2)
        stats = {}
        for key in ("Damage", "Hits", "Avg", "Max") + tuple(extra_stats or []):
            row = tk.Frame(sf, bg='#161b22')
            row.pack(fill=tk.X, padx=8)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=sm, width=8, anchor='w').pack(side=tk.LEFT)
            v = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22', font=sm, anchor='e')
            v.pack(side=tk.RIGHT)
            stats[key] = v

        bd = tk.Text(self.root, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
                     height=5, bd=1, relief='groove', state=tk.DISABLED,
                     wrap=tk.NONE, highlightthickness=0)
        bd.pack(fill=tk.X, padx=12, pady=2)
        for cat, c in TYPE_COLORS.items():
            bd.tag_configure(cat, foreground=c)
        bd.tag_configure('Unknown', foreground='#8b949e')
        bd.tag_configure('hdr', foreground='#7d8590')
        return dps_lbl, stats, bd

    # ── Tick / refresh ────────────────────────────────────────────────────────

    def _tick(self):
        if self._shutdown:
            return
        now = int(time.time() * 1000)
        gap = SESSION_GAP * 1000

        if self.out_session and self.out_session.active and self.last_out_ms:
            if now - self.last_out_ms > gap:
                self._end_session('out')
        if self.in_session and self.in_session.active and self.last_in_ms:
            if now - self.last_in_ms > gap:
                self._end_session('in')

        self._refresh(self.out_session, self.out_dps, self.out_stats, self.out_bd, '#3fb950', '#58a6ff')
        self._refresh(self.in_session,  self.in_dps,  self.in_stats,  self.in_bd,  '#f85149', '#d29922')
        self._refresh_exp()

        starts = [s.start_ms for s in (self.out_session, self.in_session) if s and s.start_ms]
        if starts:
            earliest = min(starts)
            dt = datetime.fromtimestamp(earliest / 1000.0)
            self.time_labels["Session Start"].config(
                text=dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}")
            elapsed = (now - earliest) / 1000.0
            m, s = divmod(elapsed, 60)
            self.time_labels["Duration"].config(text=f"{int(m)}:{s:05.2f}")

        self.root.after(100, self._tick)

    def _refresh(self, session, dps_lbl, stats, bd, hi_color, mid_color):
        if not session or session.count == 0:
            dps_lbl.config(text="— DPS", fg='#484f58')
            return
        dps = session.dps
        dps_lbl.config(
            text=f"{dps:.1f} DPS",
            fg=hi_color if dps >= 100 else mid_color if dps > 0 else '#484f58')
        stats["Damage"].config(text=f"{session.total:,}")
        stats["Hits"].config(text=str(session.count))
        stats["Avg"].config(text=f"{session.avg:.0f}")
        stats["Max"].config(text=str(session.max_hit))

        if "Crit %" in stats:
            cs = session.crit_stats()
            if cs:
                stats["Normal"].config(    text=f"{cs['normal_avg']:.0f}")
                stats["Crit Avg"].config(  text=f"{cs['crit_avg']:.0f}")
                stats["Crit %"].config(    text=f"{cs['crit_pct']:.1f}%")
                stats["Crit Boost"].config(text=f"+{cs['crit_boost']:.0f}%")
            else:
                for key in ("Normal", "Crit Avg", "Crit %", "Crit Boost"):
                    stats[key].config(text="—")

        bd.config(state=tk.NORMAL)
        bd.delete('1.0', tk.END)
        bd.insert(tk.END, f"{'Type':<10}{'Hits':>5}{'Dmg':>8}{'Avg':>6}{'DPS':>7}{'%':>5}\n", 'hdr')
        elapsed = session.elapsed_s
        for name, st in sorted(session.cats.items(), key=lambda x: x[1]['damage'], reverse=True):
            cnt, dmg = st['count'], st['damage']
            avg  = dmg / cnt if cnt else 0
            cdps = dmg / elapsed if elapsed > 0 else 0
            pct  = dmg / session.total * 100 if session.total else 0
            tag  = name if name in TYPE_COLORS else 'Unknown'
            bd.insert(tk.END, f"{name:<10}{cnt:>5}{dmg:>8,}{avg:>6.0f}{cdps:>7.1f}{pct:>4.0f}%\n", tag)
        bd.config(state=tk.DISABLED)

    # ── Session management ────────────────────────────────────────────────────

    def _new_session(self, direction):
        if direction == 'out':
            if self.out_session and self.out_session.active:
                self._end_session('out')
            self.out_session = Session()
        else:
            if self.in_session and self.in_session.active:
                self._end_session('in')
            self.in_session = Session()

    def _end_session(self, direction):
        s = self.out_session if direction == 'out' else self.in_session
        if s and s.count > 0:
            s.finalize()
            label = "OUT" if direction == 'out' else "IN"
            self._log(f"--- {label}: {s.dps:.1f} DPS, {s.total:,} dmg, {s.elapsed_s:.1f}s ---", 'info')
        if direction == 'out':
            self.out_session = None
        else:
            self.in_session = None

    # ── Event handling ────────────────────────────────────────────────────────

    def _handle(self, etype, ts, data):
        if self._shutdown:
            return

        if etype == "AGENT_READY":
            self.status.config(text="Agent loaded...", fg='#f0883e')
        elif etype == "ATTACHED":
            self._paused = False
            # Only init on first attach — re-attach must preserve accumulated
            # exp_total so the rate stays anchored to the session's actual start
            if self.exp_start_ms == 0:
                self.exp_start_ms = int(time.time() * 1000)
            self.toggle_btn.config(text="Stop (F12)", bg='#da3633')
            self.status.config(text="Tracking...", fg='#3fb950')
        elif etype == "ERROR":
            self.status.config(text=f"Error: {data}", fg='#f85149')

        elif etype in ("OUT", "HIT"):
            if self._paused:
                return
            parts = data.split('|', 1)
            try:
                dmg = int(parts[0])
            except ValueError:
                return
            msg      = parts[1] if len(parts) > 1 else ""
            cat      = categorize_outgoing(msg)
            is_dummy = 'Training Dummy' in msg

            session_type_changed = self.out_session and self.out_session.active and (is_dummy != self._out_dummy)
            if not self.out_session or not self.out_session.active or session_type_changed:
                self._new_session('out')
                self._out_dummy = is_dummy

            self.out_session.add(ts, dmg, cat)
            self.last_out_ms = ts
            elapsed = (ts - self.out_session.start_ms) / 1000.0
            self._log(f"  OUT {elapsed:6.2f}s {dmg:>5d} [{cat}]", 'out')
            self.status.config(text="Tracking...", fg='#3fb950')

        elif etype == "IN":
            if self._paused:
                return
            parts = data.split('|', 1)
            try:
                dmg = int(parts[0])
            except ValueError:
                return
            if dmg <= 0:
                return
            msg = parts[1] if len(parts) > 1 else ""
            cat = categorize_incoming(msg) if msg else 'Unknown'

            if not self.in_session or not self.in_session.active:
                self._new_session('in')
            self.in_session.add(ts, dmg, cat)
            self.last_in_ms = ts
            elapsed = (ts - self.in_session.start_ms) / 1000.0
            cat_tag = f" [{cat}]" if msg else ""
            self._log(f"   IN {elapsed:6.2f}s {dmg:>5d}{cat_tag}", 'in')

        elif etype == "KILL":
            if not self._paused:
                self.kill_count += 1
                self._refresh_exp()
            self._log(f"  KILL  {data}", 'kill')
            if 'Training Dummy' in data:
                if self.out_session and self.out_session.active:
                    self._end_session('out')
                self._out_dummy = False

        elif etype == "EXP":
            if self._paused:
                return
            try:
                xp = int(data)
            except ValueError:
                return
            if self.exp_start_ms == 0:
                self.exp_start_ms = int(time.time() * 1000)
            self.exp_total += xp
            self._log(f"  EXP  +{xp:,}", 'info')
            self._refresh_exp()

        elif etype == "DEATH":
            self._log("  DIED", 'in')
            if self.out_session and self.out_session.active:
                self._end_session('out')
            if self.in_session and self.in_session.active:
                self._end_session('in')

    # ── Log reader ────────────────────────────────────────────────────────────

    def _start_log_reader(self):
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while not LOG_FILE.exists() and not self._shutdown:
            time.sleep(0.1)
        if self._shutdown:
            return
        with open(LOG_FILE, 'r') as f:
            while not self._shutdown:
                line = f.readline()
                if not line:
                    time.sleep(0.01)
                    continue
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|', 2)
                if len(parts) < 2:
                    continue
                try:
                    ts = int(parts[1])
                except ValueError:
                    continue
                etype = parts[0]
                data  = parts[2] if len(parts) > 2 else ""
                self.root.after(0, self._handle, etype, ts, data)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, text, tag='out'):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n", tag)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _toggle_tracking(self):
        self._paused = not self._paused
        if self._paused:
            if self.out_session and self.out_session.active:
                self._end_session('out')
            if self.in_session and self.in_session.active:
                self._end_session('in')
            self.toggle_btn.config(text="Start (F12)", bg='#238636')
            self.status.config(text="Stopped", fg='#f0883e')
        else:
            if self.exp_start_ms == 0:
                self.exp_start_ms = int(time.time() * 1000)
            self.toggle_btn.config(text="Stop (F12)", bg='#da3633')
            self.status.config(text="Tracking...", fg='#3fb950')

    def _refresh_exp(self):
        # Always use wall clock for the rate so burst kills don't inflate it
        now_ms = int(time.time() * 1000)
        self.exp_labels["Total XP"].config(text=f"{self.exp_total:,}")
        self.exp_labels["Kills"].config(text=str(self.kill_count))
        if self.exp_total > 0 and self.exp_start_ms > 0:
            elapsed_s = (now_ms - self.exp_start_ms) / 1000
            # Don't report a rate until tracking has run for at least 10 seconds
            if elapsed_s < 10:
                self.exp_rate_lbl.config(text="… XP/hr", fg='#7d8590')
            else:
                rate = self.exp_total * 3600 / elapsed_s
                self.exp_rate_lbl.config(
                    text=f"{rate:,.0f} XP/hr", fg='#d2a8ff')
        else:
            self.exp_rate_lbl.config(text="— XP/hr", fg='#484f58')

    def _reset(self):
        if self.out_session and self.out_session.active:
            self._end_session('out')
        if self.in_session and self.in_session.active:
            self._end_session('in')
        self.out_session   = None
        self.in_session    = None
        self.last_out_ms   = 0
        self.last_in_ms    = 0
        self._out_dummy    = False
        self._paused       = True
        self.exp_total     = 0
        self.exp_start_ms  = 0
        self.kill_count    = 0
        self.exp_labels["Total XP"].config(text="0")
        self.exp_labels["Kills"].config(text="0")
        self.exp_rate_lbl.config(text="— XP/hr", fg='#484f58')
        self.toggle_btn.config(text="Start (F12)", bg='#238636')
        self.log.config(state=tk.NORMAL)
        self.log.delete('1.0', tk.END)
        self.log.config(state=tk.DISABLED)
        self.status.config(text="Reset. Press Start.", fg='#f0883e')

    def _on_close(self):
        self._shutdown = True
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ── Java helpers ──────────────────────────────────────────────────────────────

def _res_dir():
    return Path(sys._MEIPASS) if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR

def _run_dir():
    return Path(sys.executable).parent if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR


def find_java():
    candidates = []
    jh = os.environ.get('JAVA_HOME')
    if jh:
        candidates.append(Path(jh) / "bin" / "java.exe")
    # Also search per-user install locations (e.g. Adoptium installed via winget)
    local_prog = Path(os.environ.get('LOCALAPPDATA', '')) / "Programs"
    for base in (Path("C:/Program Files/Java"),
                 Path("C:/Program Files/Eclipse Adoptium"),
                 Path("C:/Program Files/Android/Android Studio1/jbr"),
                 Path("C:/Program Files/Microsoft"),
                 Path("C:/Program Files/Zulu"),
                 local_prog / "Eclipse Adoptium",
                 local_prog / "Java",
                 local_prog / "Microsoft",
                 local_prog / "Zulu"):
        if base.is_dir():
            if (base / "bin" / "java.exe").exists():
                candidates.append(base / "bin" / "java.exe")
            else:
                for child in base.iterdir():
                    if (child / "bin" / "java.exe").exists():
                        candidates.append(child / "bin" / "java.exe")
    p = shutil.which("java")
    if p:
        candidates.append(Path(p))
    for java in candidates:
        if not java.exists():
            continue
        try:
            r = subprocess.run([str(java), "--list-modules"],
                               capture_output=True, text=True, timeout=10)
            if "jdk.attach" in r.stdout:
                return str(java)
        except Exception:
            continue
    return None


def attach_agent():
    java = find_java()
    if not java:
        print("ERROR: No JDK with jdk.attach found.")
        print("Install a JDK (Java 11+) — https://adoptium.net/")
        return False

    res, run = _res_dir(), _run_dir()
    # Copy agent.jar to a unique name so the JVM doesn't use a cached class loader
    # from a previous load in the same session.
    import shutil as _shutil
    agent_src  = res / "agent.jar"
    agent_copy = run / f"agent_{int(time.time())}.jar"
    _shutil.copy2(str(agent_src), str(agent_copy))
    agent = str(agent_copy)
    from datetime import datetime as _dt
    ts_str = _dt.now().strftime("%Y%m%d_%H%M%S")
    log    = str(run / f"dps_events_{ts_str}.log")

    global LOG_FILE
    LOG_FILE = Path(log)

    print(f"Using Java: {java}")
    print("Attaching to Wyvern JVM...")
    try:
        r = subprocess.run(
            [java, "-cp", str(res / "attacher"), "dps.DPSAttacher", agent, log],
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("Attach timed out.")
        return False

    print(r.stdout)
    try:
        agent_copy.unlink(missing_ok=True)
    except Exception:
        pass
    if r.returncode != 0:
        time.sleep(1)
        if LOG_FILE.exists():
            print("Agent may already be loaded. Continuing.")
            return True
        # Return stderr so main() can print it after the user-facing message
        return r.stderr.strip() or "Unknown attach error"
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Wyvern DPS Tracker ===\n")

    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, True, "WyvernDPSTracker_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("Another DPS Tracker is already running!")
        input("Press Enter to close...")
        sys.exit(1)

    result = attach_agent()
    if result is not True:
        print("\nCould not attach to game.")
        print("  1. Make sure Wyvern is running BEFORE launching the tracker.")
        print("  2. JDK 11+ installed? (not JRE) — https://adoptium.net/")
        java = find_java()
        if not java:
            print("\n  >> No JDK found!")
        else:
            print(f"\n  >> Java found: {java}")
        if isinstance(result, str):
            print(f"\n  >> Attach output:\n{result}")
        # Check for agent error file (written by dps24+ on agentmain crash)
        err_file = LOG_FILE.parent / (LOG_FILE.name + ".err")
        if err_file.exists():
            try:
                print(f"\n  >> Agent error:\n{err_file.read_text()}")
            except Exception:
                pass
        # Also check the guaranteed-writable tmp debug file
        import tempfile as _tmp
        tmp_dbg = Path(_tmp.gettempdir()) / "dps_agent_debug.txt"
        if tmp_dbg.exists():
            try:
                print(f"\n  >> Agent debug ({tmp_dbg}):\n{tmp_dbg.read_text()}")
            except Exception:
                pass
        input("\nPress Enter to close...")
        sys.exit(1)

    lock = Path(str(LOG_FILE) + ".lock")
    lh   = open(lock, 'w')
    lh.write(str(os.getpid()))
    lh.flush()

    def cleanup():
        try:
            lh.close()
            lock.unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(cleanup)

    print("\nStarting GUI...")
    app = DPSTrackerGUI()
    app.run()
    cleanup()
    sys.exit(0)


if __name__ == '__main__':
    main()
