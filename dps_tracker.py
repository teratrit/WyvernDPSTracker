"""
Wyvern DPS Tracker
Hooks into the Wyvern game client via Java Attach API to capture all combat
damage (outgoing and incoming) with millisecond timestamps and per-type breakdown.
"""

import atexit
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from pynput import keyboard as kb

SCRIPT_DIR  = Path(__file__).parent.resolve()
LOG_FILE    = SCRIPT_DIR / "dps_events_v2.log"
GAME_PID    = 0   # PID of the Wyvern client we watch; 0 = unknown
SESSION_GAP = 15  # seconds of inactivity before session auto-ends

# ── Damage categorization ─────────────────────────────────────────────────────
#
# Only patterns for strings actually observed in Wyvern logs are kept here.
# When a new verb / element flavor shows up in the wild, add it with a
# comment pointing at the source log or screenshot - never speculate.

# Outgoing physical verbs (first word after "You").
OUTGOING_VERB_RE = re.compile(r'^You\s+(\w+)', re.I)
PHYSICAL_VERBS = {
    # Stab - observed vs. Azrael, Water Elemental, grizzly bear
    'stabbed':  'Stab',
    'pierced':  'Stab',
    'skewered': 'Stab',
    'impaled':  'Stab',
    'bit':      'Stab',   # "You bit apex grizzly bear for 11 damage."
    # Cut - observed vs. Azrael, Training Dummy, City Guard
    'cut':      'Cut',
    'cleaved':  'Cut',
    'carved':   'Cut',
    'hewed':    'Cut',
    'sliced':   'Cut',  # "You sliced Training Dummy to the bone for 72 damage."
    # Smash - observed vs. Training Dummy, apex yak, grizzly bear
    'smashed':   'Smash',
    'smote':     'Smash',
    'hit':       'Smash',  # "You hit apex yak hard for 32 damage."
    'staggered': 'Smash',  # "You staggered apex grizzly bear with a tremendous blow ..."
}

# Outgoing element verbs - used AFTER FLAVOR_RE in categorize_outgoing.
# Only verbs that always map to a single element are here. Ambiguous verbs
# ('engulfed' can be Fire OR Holy, 'blasted' can be Shock/Cold/Holy) are
# deliberately omitted - flavor handles them via FLAVOR_RE which runs first.
ELEMENT_VERBS = {
    'shocked':       'Shock',  # "You shocked Training Dummy very badly for 58 damage."
    'zapped':        'Shock',  # "You zapped X with an energy surge for 80 damage."
    'burned':        'Fire',   # "You burned Training Dummy badly for 31 damage."
    'scorched':      'Fire',   # "You scorched X horribly with intense flame for 87 damage."
    'froze':         'Cold',   # "You froze Training Dummy for 17 damage."
    'covered':       'Cold',   # "You covered X in biting frost for 41 damage."
    'poisoned':      'Poison', # "You poisoned Training Dummy badly for 21 damage."
    'corroded':      'Acid',   # "You corroded Training Dummy very badly for 41 damage."
    'disintegrated': 'Acid',   # "You disintegrated X with a wave of acid for 88 damage."
    'decimated':     'Holy',   # "You decimated X with your astonishing divinity for 192 damage."
    'condemned':     'Holy',   # "You condemned X with your brilliant radiance for 80 damage."
    'overwhelmed':   'Holy',   # "You overwhelmed X with your holy power for 38 damage."
}

# Incoming verbs observed in real logs. Both past-tense and present-tense
# forms appear in Wyvern (e.g. "Wolverine bit you", "Argentavis shreds you").
INCOMING_VERBS = {
    # Smash
    'hits':      'Smash',  # "X hits you but does no damage."
    'hit':       'Smash',
    'crushed':   'Smash',  # "Caribou crushed your bones in its powerful jaws."
    'crushes':   'Smash',
    'smote':     'Smash',  # "Badass lead golem smote you with a godlike wrath."
    'smashed':   'Smash',
    'smashes':   'Smash',
    'staggered': 'Smash',
    'staggers':  'Smash',
    'rammed':    'Smash',  # "Musk Ox rammed you with a bone-crushing sound."
    'rams':      'Smash',
    'smacked':   'Smash',  # "Badass apex yak smacked you."
    'smacks':    'Smash',
    'grazed':    'Smash',  # "Gladiator grazed you."
    'grazes':    'Smash',
    # Stab
    'bit':       'Stab',   # "Wolverine bit you."
    'bites':     'Stab',
    'stabbed':   'Stab',   # "Gladiator stabbed you." / "stabbed you deeply."
    'stabs':     'Stab',
    'pierced':   'Stab',   # "Gladiator pierced you very deeply."
    'pierces':   'Stab',
    # Cut
    'cut':       'Cut',    # "Frozen Medusa cut you badly."
    'cuts':      'Cut',
    'hewed':     'Cut',    # "Gladiator hewed you like an ancient God."
    'shreds':    'Cut',    # "Argentavis shreds you with savage ferocity."
    'shred':     'Cut',
    'clawed':    'Cut',    # "Apex grizzly bear clawed you."
    'claws':     'Cut',
    'carved':    'Cut',    # "Gladiator carved you like a butcher."
    'carves':    'Cut',
    'sliced':    'Cut',    # "Gladiator sliced you to the bone."
    'slices':    'Cut',
    'gashes':    'Cut',    # "Argentavis gashes you deeply."
    'gashed':    'Cut',
    # Fire
    'burned':    'Fire',   # "Dragon breath burned you."
    'burns':     'Fire',
    'scorched':  'Fire',
    'singed':    'Fire',   # "Firefrost singed you."
    'singes':    'Fire',
    # Cold
    'froze':     'Cold',   # "Icestorm froze you."
    'freezes':   'Cold',
    'covered':   'Cold',   # "Icestorm covered you in frost."
    'covers':    'Cold',
    # Shock
    'shocked':   'Shock',  # "Thunderstorm shocked you."
    'shocks':    'Shock',
    'zapped':    'Shock',
    'blasted':   'Shock',  # "Thunderstorm blasted you with a huge bolt of lightning."
    'blasts':    'Shock',  # (default; FLAVOR overrides for arctic/holy)
    # Acid
    'slimed':    'Acid',   # "Medusa Archer slimed you."
    'slimes':    'Acid',
}
INCOMING_VERB_RE = re.compile(
    r'(' + '|'.join(sorted(INCOMING_VERBS, key=len, reverse=True)) + r')\s+you',
    re.I)

# Element flavor - keywords scanned in flavor text (outgoing) or full message
# (incoming). Conservative: only spell-flavor words that are unlikely to appear
# in monster names. Avoid 'ice', 'cold', 'frozen', 'death' - those collide
# with mob names like "Ice Riagor", "Frozen Medusa", "Angel of Death".
ELEMENT_PATTERNS = [
    (re.compile(r'\bholy\b|\bradiance\b|\bdivinity\b|\bsacred\b', re.I), 'Holy'),
    (re.compile(r'\bflame\b|\bfire\b|\bburning\b|\binferno\b',    re.I), 'Fire'),
    (re.compile(r'\bfrost\b|\bglacial\b|\barctic\b|\bchill\b',    re.I), 'Cold'),
    (re.compile(r'\blightning\b|\benergy\s+surge\b|\belectr',     re.I), 'Shock'),
    (re.compile(r'\bacid\b|\bcorrosi',                            re.I), 'Acid'),
]

# Flavor text between "with"/"in" and "for X damage" - feeds ELEMENT_PATTERNS.
FLAVOR_RE     = re.compile(r'\b(?:with|in)\s+(.+?)\s+for\s+\d+\s+damage', re.I)
# Unambiguous physical patterns - checked BEFORE FLAVOR_RE so that
# "make a hole in <monster>" doesn't let the monster name leak into flavor.
HOLE_RE       = re.compile(r'(?:make|made)\s+a\s+hole|daylight through', re.I)
NEARLY_CUT_RE = re.compile(r'nearly cut.*in half', re.I)
# "Argentavis tears away at you." - verb separated from "you" by "(away) at"
TEARS_AT_RE   = re.compile(r'\btears\s+(?:away\s+)?at\s+you\b', re.I)
# "Apex grizzly bear rived your flesh." / "rives your flesh" - verb + "your flesh"
RIVES_FLESH_RE = re.compile(r'\brive[ds]?\s+your\s+flesh\b', re.I)
# "Argentavis rips away flesh with its talons." - verb + (away) + bare "flesh"
RIPS_FLESH_RE  = re.compile(r'\brips?\s+(?:away\s+)?flesh\b', re.I)

# Incoming "You feel ..." status lines - these ARE real damage:
#  • "You feel sick." → poison DOT ticking
#  • "You suddenly feel very hot!" → fire-recoil from hitting a fire-aligned mob
#  • "You suddenly feel very cold!" → cold-recoil from hitting a cold-aligned mob
FEEL_TYPES = {
    'sick': 'Poison',
    'hot':  'Fire',
    'cold': 'Cold',
}
FEEL_RE = re.compile(
    r'^You\s+(?:suddenly\s+)?feel\s+(?:very\s+)?(' +
    '|'.join(FEEL_TYPES) + r')\b', re.I)

# ── Mob name extraction ──
# Strip-based parser: peel off the prefix ("You <verb>", "Justice cleaves", etc.),
# the "for N damage" tail, and any trailing flavor text. What's left is the mob.
_DAMAGE_TAIL     = re.compile(r'\s+for\s+\d+\s+damage[!.]?\s*$', re.I)
# Status-tag suffix the game appends to a mob's name while a debuff is active.
# Only known-status words are stripped so mob name variants like
# "Acid Hydros (cursed)" stay intact.
_STATUS_TAG_RE   = re.compile(r'\s*\((?:Frostbitten)\)', re.I)
_JUSTICE_CLEAVES = re.compile(r'^Justice\s+cleaves\s+', re.I)
_JUSTICE_RAINS   = re.compile(r'^Justice\s+rains\s+down.+?\s+upon\s+', re.I)
_DREAM_EATER     = re.compile(r'^Dream\s+Eater\s+rips\s+through\s+', re.I)
_YOU_MAKE_HOLE   = re.compile(r'^You\s+make\s+a\s+hole\s+in\s+', re.I)
_YOU_NEARLY_CUT  = re.compile(r'^You\s+nearly\s+cut\s+', re.I)
_YOU_VERB        = re.compile(r'^You\s+\w+\s+', re.I)
_KILL_PREFIX     = re.compile(r'^You\s+(?:killed|destroyed?)\s+', re.I)
# Flavor tails to strip iteratively from the right - longest patterns first matter
# inside each `with/in/like/...` group; outer alternatives are mutually exclusive.
_FLAVOR_TAIL = re.compile(
    r'\s+(?:'
    r'with\s+(?:a|an|your|brute|intense|strong|burning|biting|holy|suffocating|savage|deathly|godlike|tremendous|mighty|bone-crushing|astonishing|brilliant).+$'
    r'|in\s+(?:burning|biting|frost\b).*$'
    r'|through\s+a\s+vital\s+spot$'
    r'|like\s+(?:a|an)\s+.+$'
    r'|to\s+the\s+bone$'
    r'|so\s+hard.+$'
    r'|asunder$'
    r'|very\s+(?:badly|deeply|hard)$'
    r'|(?:badly|horribly|severely|deeply|hard)$'
    r')',
    re.I)


def extract_mob_outgoing(msg):
    """Pull the target mob name from an outgoing damage message. None if not parseable."""
    s = _DAMAGE_TAIL.sub('', msg)
    if s == msg:
        return None
    if _JUSTICE_CLEAVES.match(s):
        s = _JUSTICE_CLEAVES.sub('', s)
        s = re.sub(r'\s+with\s+brute\s+force\s*$', '', s, flags=re.I)
    elif _JUSTICE_RAINS.match(s):
        s = _JUSTICE_RAINS.sub('', s)
    elif _DREAM_EATER.match(s):
        s = _DREAM_EATER.sub('', s)
        s = re.sub(r',?\s*causing\s+them\s+to\s+scream\s+in\s+terror\s*$', '', s, flags=re.I)
    elif _YOU_MAKE_HOLE.match(s):
        s = _YOU_MAKE_HOLE.sub('', s)
        s = re.sub(r'\s+so\s+large.*$', '', s, flags=re.I)
    elif _YOU_NEARLY_CUT.match(s):
        s = _YOU_NEARLY_CUT.sub('', s)
        s = re.sub(r'\s+in\s+half\s*$', '', s, flags=re.I)
    elif _YOU_VERB.match(s):
        s = _YOU_VERB.sub('', s)
    else:
        return None
    # Iteratively strip flavor from the right
    while True:
        new_s = _FLAVOR_TAIL.sub('', s).strip()
        if new_s == s:
            break
        s = new_s
    # Strip any status tag like "(Frostbitten)" so the mob still rolls up
    # under its base name in stats.
    s = _STATUS_TAG_RE.sub('', s).strip()
    return s or None


def extract_mob_kill(line):
    """Pull the mob name from 'You killed X.' style kill lines."""
    m = _KILL_PREFIX.match(line)
    if not m:
        return None
    rest = line[m.end():].rstrip('.!').strip()
    return rest or None


# Incoming-mob source extraction. Format is usually "<MOB> <verb> you ..."
# Special cases handled separately: "X made a hole in you", "X nearly cut you in half".
_INC_HOLE_RE   = re.compile(r'^(.+?)\s+(?:made|make)\s+a\s+hole\s+in\s+you\b', re.I)
_INC_NEARLY_RE = re.compile(r'^(.+?)\s+nearly\s+cut\s+you\b', re.I)
_INC_TEARS_RE  = re.compile(r'^(.+?)\s+tears\s+(?:away\s+)?at\s+you\b', re.I)
_INC_RIVES_RE  = re.compile(r'^(.+?)\s+rive[ds]?\s+your\s+flesh\b', re.I)
_INC_RIPS_RE   = re.compile(r'^(.+?)\s+rips?\s+(?:away\s+)?flesh\b', re.I)


def extract_mob_incoming(msg):
    """Pull the source mob from an incoming damage message, if attributable.
    Returns None for self-effects ('You feel sick'), Justice (player ability),
    or unparseable lines."""
    if not msg:
        return None
    if FEEL_RE.match(msg):
        return None  # Self-effect, no mob source
    if msg.startswith('Justice ') or msg.startswith('Dream Eater '):
        return None  # Player ability, not incoming damage from a mob
    m = _INC_HOLE_RE.match(msg)
    if m:
        return m.group(1).strip() or None
    m = _INC_NEARLY_RE.match(msg)
    if m:
        return m.group(1).strip() or None
    m = _INC_TEARS_RE.match(msg)
    if m:
        return m.group(1).strip() or None
    m = _INC_RIVES_RE.match(msg)
    if m:
        return m.group(1).strip() or None
    m = _INC_RIPS_RE.match(msg)
    if m:
        return m.group(1).strip() or None
    m = INCOMING_VERB_RE.search(msg)
    if m:
        return msg[:m.start()].strip() or None
    return None


TYPE_COLORS = {
    # Elements actually produced by the categorizer:
    'Shock':   '#87ceeb', 'Fire':    '#ff6347', 'Cold':   '#add8e6',
    'Poison':  '#00ff7f', 'Holy':    '#da70d6', 'Acid':   '#7fff00',
    # Physical:
    'Cut':     '#ffa500', 'Smash':   '#cd853f', 'Stab':   '#daa520',
    # DMG-feed types not covered above (colors from the client's own
    # floating-text palette):
    'Bite':     '#e07856', 'Slay':  '#ff6b9d', 'Drowning': '#45d0c0',
    'Magic':    '#e879f9', 'Sun':   '#ffe066', 'Sting':    '#9fe635',
    'True':     '#ff5555', 'Hit':   '#f4f4f5',
    # Weapon ability:
    'Justice':     '#ffd700',
    'Dream Eater': '#c77dff',
}


def _check_elements(text):
    for pat, dtype in ELEMENT_PATTERNS:
        if pat.search(text):
            return dtype
    return None


def categorize_outgoing(msg):
    if not msg:
        return 'Unknown'
    # Justice - a weapon ability whose damage line starts with "Justice ..."
    # (e.g. "Justice cleaves <monster> with brute force for X damage!").
    # Game renders it in red ("damage") style; the agent already routes these
    # as OUT events so they count toward the player's damage, not against.
    if msg.startswith('Justice '):
        return 'Justice'
    if msg.startswith('Dream Eater '):
        return 'Dream Eater'
    # 1. Unambiguous physical patterns - MUST come before FLAVOR_RE because
    #    "make a hole in <monster> ..." would otherwise capture the monster
    #    name as flavor text and match element keywords in it.
    if HOLE_RE.search(msg):
        return 'Stab'
    if NEARLY_CUT_RE.search(msg):
        return 'Cut'
    # 2. Element keywords in flavor text (between "with/in" and "for X damage" -
    #    safe because monster names don't appear in that slot). Handles
    #    ambiguous verbs like 'engulfed' / 'blasted' which can be Fire/Cold/
    #    Holy/Shock depending on the spell flavor.
    flavor = FLAVOR_RE.search(msg)
    if flavor:
        elem = _check_elements(flavor.group(1))
        if elem:
            return elem
    # 3. Verb-based mapping - element verbs first (single-element verbs that
    #    don't always carry flavor text), then physical verbs.
    m = OUTGOING_VERB_RE.match(msg)
    if m:
        verb = m.group(1).lower()
        if verb in ELEMENT_VERBS:
            return ELEMENT_VERBS[verb]
        if verb in PHYSICAL_VERBS:
            return PHYSICAL_VERBS[verb]
    return 'Unknown'


def categorize_incoming(msg):
    if not msg:
        return 'Unknown'
    if msg.startswith('Justice '):
        return 'Justice'
    if msg.startswith('Dream Eater '):
        return 'Dream Eater'
    # "You feel ..." status lines - element-recoil and poison DOT damage.
    fm = FEEL_RE.match(msg)
    if fm:
        return FEEL_TYPES[fm.group(1).lower()]
    # 1. Unambiguous physical patterns - same protection as outgoing.
    if HOLE_RE.search(msg):
        return 'Stab'
    if NEARLY_CUT_RE.search(msg):
        return 'Cut'
    if TEARS_AT_RE.search(msg):
        return 'Cut'
    if RIVES_FLESH_RE.search(msg):
        return 'Cut'  # "Apex grizzly bear rived your flesh."
    if RIPS_FLESH_RE.search(msg):
        return 'Cut'  # "Argentavis rips away flesh with its talons."
    # 2. Element keyword scan (full message - kept conservative to avoid
    #    monster-name false positives like "Ice Riagor", "Angel of Death").
    elem = _check_elements(msg)
    if elem:
        return elem
    # 3. Verb match
    m = INCOMING_VERB_RE.search(msg)
    if m:
        return INCOMING_VERBS.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


# ── Session ────────────────────────────────────────────────────────────────────

HITS_BUFFER_MAX = 5000
BACKSTAB_TIMEOUT_MS = 2000


@dataclass
class Session:
    start_ms: int  = 0
    end_ms:   int  = 0
    total:    int  = 0
    count:    int  = 0
    max_hit:  int  = 0
    hits:     deque = field(
        default_factory=lambda: deque(maxlen=HITS_BUFFER_MAX))
    active:   bool = True
    cats:     dict = field(default_factory=lambda: defaultdict(
        lambda: {'damage': 0, 'count': 0, 'max': 0}))
    # Backstab and non-backstab tracked separately so we can show
    # mean/stdev/min/max for each. Welford's online algorithm avoids
    # keeping all hits in memory.
    backstab_count: int   = 0
    backstab_total: int   = 0
    backstab_min:   int   = 0
    backstab_max:   int   = 0
    backstab_mean:  float = 0.0
    backstab_m2:    float = 0.0
    normal_count:   int   = 0
    normal_total:   int   = 0
    normal_min:     int   = 0
    normal_max:     int   = 0
    normal_mean:    float = 0.0
    normal_m2:      float = 0.0
    # Server-authoritative crits (only populated by the DMG feed).
    crit_count:     int   = 0
    crit_total:     int   = 0
    # HP gained while this (incoming) session was active - regen, potions,
    # heals. Lets the INCOMING panel show net HP flow.
    healed:         int   = 0

    @property
    def elapsed_s(self):
        end = self.end_ms or int(time.time() * 1000)
        return (end - self.start_ms) / 1000.0 if self.start_ms else 0.0

    @property
    def dps(self):
        e = self.elapsed_s
        return self.total / e if e > 0 else 0.0

    @property
    def dps_no_backstab(self):
        e = self.elapsed_s
        return self.normal_total / e if e > 0 else 0.0

    @property
    def avg(self):
        return self.total / self.count if self.count else 0.0

    @property
    def backstab_stdev(self):
        if self.backstab_count < 2:
            return 0.0
        return (self.backstab_m2 / (self.backstab_count - 1)) ** 0.5

    @property
    def normal_stdev(self):
        if self.normal_count < 2:
            return 0.0
        return (self.normal_m2 / (self.normal_count - 1)) ** 0.5

    def add(self, ts, damage, cat='Unknown', backstab=False, crit=False):
        if not self.start_ms:
            self.start_ms = ts
        self.hits.append((ts, damage, cat))
        self.total   += damage
        self.count   += 1
        self.max_hit  = max(self.max_hit, damage)
        if crit:
            self.crit_count += 1
            self.crit_total += damage
        if backstab:
            self.backstab_count += 1
            self.backstab_total += damage
            if self.backstab_count == 1:
                self.backstab_min = damage
                self.backstab_max = damage
            else:
                if damage < self.backstab_min: self.backstab_min = damage
                if damage > self.backstab_max: self.backstab_max = damage
            delta = damage - self.backstab_mean
            self.backstab_mean += delta / self.backstab_count
            self.backstab_m2   += delta * (damage - self.backstab_mean)
        else:
            self.normal_count += 1
            self.normal_total += damage
            if self.normal_count == 1:
                self.normal_min = damage
                self.normal_max = damage
            else:
                if damage < self.normal_min: self.normal_min = damage
                if damage > self.normal_max: self.normal_max = damage
            delta = damage - self.normal_mean
            self.normal_mean += delta / self.normal_count
            self.normal_m2   += delta * (damage - self.normal_mean)
        c = self.cats[cat]
        c['damage'] += damage
        c['count']  += 1
        c['max']     = max(c['max'], damage)

    def finalize(self):
        self.end_ms = self.hits[-1][0] if self.hits else 0
        self.active = False


# ── GUI ────────────────────────────────────────────────────────────────────────

# Caps on Text widgets to prevent unbounded memory growth on long sessions.
COMBAT_LOG_MAX_LINES = 1500
HISTORY_MAX_LINES    = 2000


def _trim_text_widget(widget, max_lines):
    """If the widget has more than max_lines, drop the oldest 20%."""
    # Tk index '<n>.0' means line n (1-indexed). 'end-1c' is just before
    # the trailing newline. Counting via index lookup is cheap.
    line_count = int(widget.index('end-1c').split('.')[0])
    if line_count > max_lines:
        drop_to = line_count - int(max_lines * 0.8)
        widget.delete('1.0', f'{drop_to}.0')


def _format_in_types(types_dict, total):
    """Format the in_types dict as 'Cold 60% / Smash 40%', top 2 above 5%."""
    if not types_dict or total <= 0:
        return '—'
    items = sorted(types_dict.items(), key=lambda x: -x[1])
    parts = []
    for cat, dmg in items[:3]:
        pct = 100 * dmg / total
        if pct < 5 and parts:
            break
        parts.append(f'{cat} {pct:.0f}%')
    return ' / '.join(parts) if parts else '—'


def _new_mob_record():
    """Default factory for `mob_stats[mob_name]`. See DPSTrackerGUI.__init__ for the
    schema. Per-encounter samples are kept so HP estimates don't lose
    distribution info."""
    return {
        'damage':            0,
        'hits':              0,
        'kills':             0,
        'xp':                0,
        'damage_in':         0,
        'hits_in':           0,
        'in_types':          {},
        'encounter_damages': [],
        'xp_samples':        [],
        'fight_ms':          0,
        'first_seen_ms':     0,
        'last_seen_ms':      0,
        'backstab_damage':   0,
        'backstab_hits':     0,
        'out_types':         {},
    }


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
        # Per-mob aggregation. Schema:
        #   damage/hits/kills/xp           - running totals
        #   damage_in/hits_in              - running totals
        #   in_types: {cat: damage}        - schools the mob hits us with
        #   out_types: {cat: damage}       - our damage by school vs this mob
        #   encounter_damages: [int]       - total damage to kill, one per killed instance
        #   xp_samples: [int]              - xp received per kill
        #   first_seen_ms / last_seen_ms   - for time-spent
        self.mob_stats = defaultdict(_new_mob_record)
        # Active "encounter" damage tally per mob (closed on KILL, discarded on idle).
        self.active_encounters = {}   # mob_name → {'damage': int, 'last_ts': int}
        self.last_killed_mob = None
        self.last_kill_ms    = 0
        # Death forensics ring buffer - last few seconds of events leading up to death
        self.event_ring   = deque(maxlen=80)
        # Live Mob Stats window - None when closed
        self._mob_stats_win  = None
        self._mob_stats_text = None
        self.backstab_pending_until_ms = 0
        # Floating mini-window - small always-on-top DPS/XP/kills readout
        self._mini_win    = None
        self._mini_dps    = None
        self._mini_xphr   = None
        self._mini_kills  = None
        self._mini_top    = None
        # Rolling-window samples for XP rate (deque of (ts_ms, xp))
        # Last mob the player traded damage with (for active-row highlight)
        self.last_active_mob    = None
        self.last_active_mob_ms = 0
        # Web capture: our own entityId (from PLAYER event) and the last time
        # a DMG-feed event arrived. While the DMG feed is live it is the
        # authoritative source for session damage; OUT/IN text events then do
        # bookkeeping only (mob names and verbs) to avoid double-counting.
        self.player_entity_id = 0
        self.dmg_live_ms      = 0
        # Per-entity damage ledger from the DMG feed. Entity ids are anonymous
        # until a KILL line names the mob; at that point the freshest entity's
        # totals are attributed to mob_stats under the mob's name. This keeps
        # mob stats working with hitmsgs off and keys concurrent same-name
        # mobs separately.
        self.entity_stats = {}
        # Kills awaiting entity attribution. Under AoE several entities are
        # hit in the same volley, so "freshest entity" at KILL time is often a
        # survivor. Instead we wait ~1s: the entity that stops receiving
        # damage at the kill is the one that died.
        self.pending_kills = deque()   # (mob_name, kill_ts)
        # XP pairing: the web client sends "You receive N xp" a moment BEFORE
        # the kill line, so XP is held here and consumed by the next KILL.
        self.pending_xp = None         # (amount, ts) or None
        self.last_kill_xp_done = True
        self._shutdown   = False
        self._build_ui()
        self._start_log_reader()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Wyvern DPS Tracker")
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#0d1117')
        self.root.geometry('440x1100')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        def _on_key(key):
            if key == kb.Key.f12:
                self.root.after(0, self._toggle_tracking)
            elif key == kb.Key.f11:
                self.root.after(0, self._toggle_mini)
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
            extra_stats=["Backstab"])
        self.in_dps,  self.in_stats,  self.in_bd  = self._build_section(
            "INCOMING", '#f85149', big, lbl, sm,
            extra_stats=["Healed", "Net HP"])

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
                  bd=0, cursor='hand2', width=8, command=self._reset).pack(
                  side=tk.LEFT, padx=(0, 4))
        tk.Button(bf, text="Mob Stats", font=sm, bg='#21262d', fg='#c9d1d9',
                  bd=0, cursor='hand2', width=10, command=self._show_mob_stats).pack(
                  side=tk.LEFT, padx=(0, 4))
        tk.Button(bf, text="Mini (F11)", font=sm, bg='#21262d', fg='#c9d1d9',
                  bd=0, cursor='hand2', width=10, command=self._toggle_mini).pack(
                  side=tk.LEFT)


        # Combat log
        lf = tk.Frame(self.root, bg='#0d1117')
        lf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 4))
        tk.Label(lf, text="Combat Log", fg='#7d8590', bg='#0d1117',
                 font=lbl, anchor='w').pack(anchor='w')
        self.log = tk.Text(lf, bg='#161b22', fg='#8b949e', font=('Consolas', 9),
                           height=6, bd=0, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)
        for tag, fg in [('out', '#58a6ff'), ('in', '#f85149'),
                        ('kill', '#3fb950'), ('info', '#7d8590')]:
            self.log.tag_configure(tag, foreground=fg)

        # Session history - full summary block appended per completed session
        hf = tk.Frame(self.root, bg='#0d1117')
        hf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        tk.Label(hf, text="Session History", fg='#d2a8ff', bg='#0d1117',
                 font=lbl, anchor='w').pack(anchor='w')
        self.history = tk.Text(hf, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
                               height=12, bd=0, state=tk.DISABLED, wrap=tk.NONE)
        self.history.pack(fill=tk.BOTH, expand=True)
        for tag, fg in [('hdr', '#d2a8ff'), ('out', '#58a6ff'),
                        ('in', '#f85149'), ('info', '#7d8590')]:
            self.history.tag_configure(tag, foreground=fg)

        self._tick()
        self._poll_game_alive()

    def _write_history(self, text, tag='info'):
        self.history.config(state=tk.NORMAL)
        self.history.insert(tk.END, text + "\n", tag)
        # Trim oldest lines so the widget doesn't grow without bound.
        _trim_text_widget(self.history, HISTORY_MAX_LINES)
        self.history.see(tk.END)
        self.history.config(state=tk.DISABLED)

    def _toggle_mini(self):
        """Show/hide a tiny always-on-top window with DPS, XP/hr, kills."""
        if self._mini_win and self._mini_win.winfo_exists():
            self._mini_win.destroy()
            self._mini_win = None
            return
        win = tk.Toplevel(self.root)
        win.title("Wyvern Mini")
        win.configure(bg='#0d1117')
        win.geometry('220x108')
        win.attributes('-topmost', True)
        win.protocol('WM_DELETE_WINDOW', lambda: (win.destroy(),
                                                  setattr(self, '_mini_win', None)))
        # Allow dragging the borderless area by clicking the body
        for ev in ('<Button-1>', '<B1-Motion>'):
            win.bind(ev, self._mini_drag)
        big = tkfont.Font(family='Consolas', size=18, weight='bold')
        sm  = tkfont.Font(family='Consolas', size=9)

        row1 = tk.Frame(win, bg='#0d1117'); row1.pack(fill=tk.X, padx=8, pady=(6, 0))
        tk.Label(row1, text="DPS",  fg='#7d8590', bg='#0d1117', font=sm).pack(side=tk.LEFT)
        self._mini_dps = tk.Label(row1, text="—", fg='#3fb950', bg='#0d1117', font=big)
        self._mini_dps.pack(side=tk.RIGHT)

        row2 = tk.Frame(win, bg='#0d1117'); row2.pack(fill=tk.X, padx=8)
        tk.Label(row2, text="XP/hr", fg='#7d8590', bg='#0d1117', font=sm).pack(side=tk.LEFT)
        self._mini_xphr = tk.Label(row2, text="—", fg='#d2a8ff', bg='#0d1117', font=sm)
        self._mini_xphr.pack(side=tk.RIGHT)

        row3 = tk.Frame(win, bg='#0d1117'); row3.pack(fill=tk.X, padx=8)
        tk.Label(row3, text="Kills", fg='#7d8590', bg='#0d1117', font=sm).pack(side=tk.LEFT)
        self._mini_kills = tk.Label(row3, text="0", fg='#c9d1d9', bg='#0d1117', font=sm)
        self._mini_kills.pack(side=tk.RIGHT)

        row4 = tk.Frame(win, bg='#0d1117'); row4.pack(fill=tk.X, padx=8, pady=(0, 4))
        tk.Label(row4, text="Top hit", fg='#7d8590', bg='#0d1117', font=sm).pack(side=tk.LEFT)
        self._mini_top = tk.Label(row4, text="0", fg='#f0883e', bg='#0d1117', font=sm)
        self._mini_top.pack(side=tk.RIGHT)

        for ev in ('<Button-1>', '<B1-Motion>'):
            for child in (row1, row2, row3, row4):
                child.bind(ev, self._mini_drag)
        self._mini_win = win
        self._refresh_mini()

    def _mini_drag(self, event):
        """Drag-to-move the mini window since it has no native title bar to grab."""
        if not (self._mini_win and self._mini_win.winfo_exists()):
            return
        if event.type == tk.EventType.ButtonPress:
            self._mini_drag_x = event.x_root - self._mini_win.winfo_x()
            self._mini_drag_y = event.y_root - self._mini_win.winfo_y()
        else:  # Motion
            x = event.x_root - getattr(self, '_mini_drag_x', 0)
            y = event.y_root - getattr(self, '_mini_drag_y', 0)
            self._mini_win.geometry(f'+{x}+{y}')

    def _refresh_mini(self):
        if not (self._mini_win and self._mini_win.winfo_exists()):
            return
        # DPS - current outgoing session
        if self.out_session and self.out_session.count:
            dps = self.out_session.dps
            self._mini_dps.config(text=f"{dps:.0f}",
                fg='#3fb950' if dps >= 100 else '#58a6ff')
        else:
            self._mini_dps.config(text="—", fg='#484f58')
        # XP/hr - same calc as main panel
        rate = self._xp_rate()
        if rate:
            self._mini_xphr.config(text=f"{rate:,.0f}")
        else:
            self._mini_xphr.config(text="—")
        self._mini_kills.config(text=str(self.kill_count))
        top = self.out_session.max_hit if self.out_session else 0
        self._mini_top.config(text=f"{top:,}" if top else "0")

    def _show_mob_stats(self):
        """Open the live Mob Stats window. If already open, just bring it forward."""
        if self._mob_stats_win and self._mob_stats_win.winfo_exists():
            self._mob_stats_win.lift()
            self._mob_stats_win.focus_set()
            return

        win = tk.Toplevel(self.root)
        win.title("Mob Stats — Live")
        win.configure(bg='#0d1117')
        win.geometry('1100x540')
        win.protocol('WM_DELETE_WINDOW', self._close_mob_stats)

        self._mob_stats_count_lbl = tk.Label(
            win, text="", fg='#7d8590', bg='#0d1117', font=('Consolas', 10),
            anchor='w')
        self._mob_stats_count_lbl.pack(fill=tk.X, padx=10, pady=(8, 4))

        body = tk.Frame(win, bg='#0d1117')
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        txt = tk.Text(body, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
                      bd=0, wrap=tk.NONE)
        scroll = tk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(fill=tk.BOTH, expand=True)

        txt.tag_configure('hdr',       foreground='#d2a8ff')
        txt.tag_configure('row',       foreground='#c9d1d9')
        txt.tag_configure('dim',       foreground='#7d8590')
        txt.tag_configure('active',    background='#1f3a5a')   # highlight current fight

        self._mob_stats_win  = win
        self._mob_stats_text = txt
        # Initial render and start the live-refresh loop
        self._refresh_mob_stats_panel()
        self._schedule_mob_stats_refresh()

    def _close_mob_stats(self):
        if self._mob_stats_win:
            try:
                self._mob_stats_win.destroy()
            except Exception:
                pass
        self._mob_stats_win = None
        self._mob_stats_text = None

    def _schedule_mob_stats_refresh(self):
        """Re-render the Mob Stats panel every second while it's open."""
        if self._shutdown:
            return
        if not self._mob_stats_win or not self._mob_stats_win.winfo_exists():
            self._mob_stats_win = None
            self._mob_stats_text = None
            return
        self._refresh_mob_stats_panel()
        self.root.after(1000, self._schedule_mob_stats_refresh)

    def _refresh_mob_stats_panel(self):
        """Rebuild the Mob Stats text content while preserving the user's scroll position."""
        txt = self._mob_stats_text
        if not txt:
            return
        # Preserve scroll so live updates don't snap the viewport back to top
        try:
            yview_top = txt.yview()[0]
        except tk.TclError:
            return

        self._mob_stats_count_lbl.config(text=f"{len(self.mob_stats)} mobs tracked")

        txt.config(state=tk.NORMAL)
        txt.delete('1.0', tk.END)

        txt.insert(tk.END,
            f"{'Mob':<28} {'Kills':>5}  "
            f"{'Out hits':>8} {'Out dmg':>9} {'Avg':>5} {'BS':>4} {'BS%':>5}  "
            f"{'In hits':>7} {'In dmg':>8} {'In avg':>6}  "
            f"{'In types':<24} "
            f"{'XP':>8} {'Time':>6}\n", 'hdr')
        txt.insert(tk.END, "-" * 142 + "\n", 'dim')

        rows = sorted(self.mob_stats.items(),
                      key=lambda x: x[1]['damage'] + x[1]['damage_in'], reverse=True)
        for mob, st in rows:
            avg_out = st['damage']    / st['hits']    if st['hits']    else 0
            avg_in  = st['damage_in'] / st['hits_in'] if st['hits_in'] else 0
            bs_hits = st.get('backstab_hits', 0)
            bs_dmg  = st.get('backstab_damage', 0)
            bs_pct  = (bs_dmg / st['damage'] * 100) if st['damage'] > 0 else 0
            bs_pct_str = f"{bs_pct:>4.0f}%" if bs_hits > 0 else "  -"
            bs_hits_str = str(bs_hits) if bs_hits > 0 else "-"
            # Prefer accumulated per-fight time (entity attribution); fall
            # back to the first/last-seen wall span for text-only data.
            if st.get('fight_ms'):
                secs = st['fight_ms'] / 1000.0
            else:
                secs = (st['last_seen_ms'] - st['first_seen_ms']) / 1000.0 \
                       if st['first_seen_ms'] else 0
            time_str = f"{int(secs // 60)}:{int(secs % 60):02d}" if secs >= 60 \
                       else f"{secs:.1f}s"
            types_str = _format_in_types(st['in_types'], st['damage_in'])
            line = (
                f"{mob[:26]:<28} {st['kills']:>5}  "
                f"{st['hits']:>8} {st['damage']:>9,} {avg_out:>5.0f} "
                f"{bs_hits_str:>4} {bs_pct_str:>5}  "
                f"{st['hits_in']:>7} {st['damage_in']:>8,} {avg_in:>6.0f}  "
                f"{types_str:<24} "
                f"{st['xp']:>8,} {time_str:>6}\n")
            tags = ['row']
            # Highlight the row of whatever mob you traded damage with in
            # the last 3 seconds - "current fight" indicator.
            if mob == self.last_active_mob and \
               int(time.time() * 1000) - self.last_active_mob_ms < 3000:
                tags.append('active')
            txt.insert(tk.END, line, tuple(tags))

        txt.config(state=tk.DISABLED)
        # Restore scroll so the viewport doesn't jerk back to the top
        try:
            txt.yview_moveto(yview_top)
        except tk.TclError:
            pass

    def _dump_death_forensics(self, death_ts, death_msg):
        """On death, dump the last ~10s of events to the history panel for review."""
        window_start = death_ts - 10_000
        recent = [e for e in self.event_ring if e[0] >= window_start]
        bar = "─" * 60
        self._write_history(bar, 'info')
        self._write_history(f" DEATH — {datetime.fromtimestamp(death_ts/1000.0).strftime('%H:%M:%S')}", 'in')
        if death_msg:
            self._write_history(f"   {death_msg}", 'in')
        self._write_history(bar, 'info')
        if not recent:
            self._write_history("  (no events captured in last 10s)", 'info')
        else:
            in_total  = sum(e[2] for e in recent if e[1] == 'IN')
            out_total = sum(e[2] for e in recent if e[1] == 'OUT')
            xp_total  = sum(e[2] for e in recent if e[1] == 'EXP')
            self._write_history(
                f"  Last 10s: in {in_total:,} dmg | out {out_total:,} | xp +{xp_total:,}",
                'info')
            for ts, etype, dmg, cat, msg in recent[-15:]:
                t_off = (ts - death_ts) / 1000.0
                tag = 'in' if etype in ('IN', 'KILL', 'DEATH') else (
                      'out' if etype == 'OUT' else 'info')
                if etype == 'OUT':
                    self._write_history(f"  {t_off:>6.2f}s  OUT  {dmg:>5d} [{cat}]  {msg[:60]}", tag)
                elif etype == 'IN':
                    snippet = (msg[:60] + '...') if msg and len(msg) > 60 else (msg or '(bare HP drop)')
                    self._write_history(f"  {t_off:>6.2f}s   IN  {dmg:>5d} [{cat}]  {snippet}", tag)
                elif etype == 'KILL':
                    self._write_history(f"  {t_off:>6.2f}s  KILL  {msg[:60]}", tag)
                elif etype == 'EXP':
                    self._write_history(f"  {t_off:>6.2f}s  EXP  +{dmg:,}", tag)
        self._write_history(bar, 'info')
        self._write_history("", 'info')

    def _record_session_summary(self, direction, session):
        """Append a full summary block for a completed session to the history panel."""
        if not session or session.count == 0:
            return

        end_ts = session.end_ms or int(time.time() * 1000)
        tstr = datetime.fromtimestamp(end_ts / 1000.0).strftime("%H:%M:%S")
        dur  = session.elapsed_s
        m, s = divmod(dur, 60)
        dur_str = f"{int(m)}:{s:05.2f}" if dur >= 60 else f"{s:.2f}s"

        bar   = "─" * 60
        label = "OUTGOING" if direction == 'out' else "INCOMING"
        body_tag = 'out' if direction == 'out' else 'in'

        self._write_history(bar, 'info')
        self._write_history(f" {tstr}  {label}  —  {dur_str}", 'hdr')
        self._write_history(bar, 'info')
        self._write_history(f"  Damage      {session.total:>10,}", body_tag)
        self._write_history(f"  DPS         {session.dps:>10.1f}", body_tag)
        self._write_history(f"  Hits        {session.count:>10,}", body_tag)
        if session.count:
            self._write_history(f"  Avg hit     {session.avg:>10.0f}", body_tag)
        self._write_history(f"  Max hit     {session.max_hit:>10,}", body_tag)
        if session.crit_count:
            crit_pct = session.crit_count / session.count * 100
            self._write_history(
                f"  Crits       {session.crit_count:>10,}  "
                f"({crit_pct:.1f}% of hits, {session.crit_total:,} dmg)",
                body_tag)
        if direction == 'in' and session.healed > 0:
            net = session.healed - session.total
            self._write_history(f"  Healed      {session.healed:>10,}", body_tag)
            self._write_history(f"  Net HP      {net:>+10,}", body_tag)

        if direction == 'out':
            if session.backstab_count > 0:
                self._write_history("", body_tag)
                self._write_history(f"  Backstab hit statistics:", body_tag)
                self._write_history(f"      Mean: {session.backstab_mean};", body_tag)
                self._write_history(f"      Stdev: {session.backstab_stdev};", body_tag)
                self._write_history(f"      Min: {session.backstab_min};", body_tag)
                self._write_history(f"      Max: {session.backstab_max}", body_tag)
            if session.normal_count > 0 and session.backstab_count > 0:
                self._write_history("", body_tag)
                self._write_history(f"  Non-backstab hit statistics:", body_tag)
                self._write_history(f"      Mean: {session.normal_mean};", body_tag)
                self._write_history(f"      Stdev: {session.normal_stdev};", body_tag)
                self._write_history(f"      Min: {session.normal_min};", body_tag)
                self._write_history(f"      Max: {session.normal_max}", body_tag)

            # Per-type breakdown
            if session.cats:
                self._write_history("  " + "-" * 58, 'info')
                self._write_history(
                    f"  {'Type':<10}{'Hits':>6}{'Dmg':>10}{'Avg':>7}{'%':>6}",
                    'info')
                for name, st in sorted(
                        session.cats.items(), key=lambda x: x[1]['damage'], reverse=True):
                    cnt, dmg = st['count'], st['damage']
                    avg = dmg / cnt if cnt else 0
                    pct = dmg / session.total * 100 if session.total else 0
                    self._write_history(
                        f"  {name:<10}{cnt:>6}{dmg:>10,}{avg:>7.0f}{pct:>5.0f}%",
                        body_tag)

            # Cumulative XP context (XP spans multiple combat sessions)
            if self.exp_total:
                self._write_history("  " + "-" * 58, 'info')
                self._write_history(
                    f"  XP total    {self.exp_total:>10,}  "
                    f"({self.kill_count} kills, cumulative since tracking start)",
                    'info')
                rate = self._xp_rate()
                if rate:
                    self._write_history(f"  XP/hr       {rate:>10,.0f}", 'info')

        self._write_history("", 'info')

    def _poll_game_alive(self):
        """Close ourselves when the Wyvern client exits."""
        if self._shutdown:
            return
        capture_dead = WEB_CAPTURE is not None and WEB_CAPTURE.disconnected
        if capture_dead or (GAME_PID and not _pid_alive(GAME_PID)):
            # Print a final summary so the user sees their session before we close
            if not self._paused:
                self._print_summary()
            self.status.config(text="Wyvern closed. Exiting...", fg='#f0883e')
            self.root.after(1500, self._on_close)
            return
        self.root.after(2000, self._poll_game_alive)

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
        # Stats that start hidden and appear only when there's data for them.
        hidden_initially = {'Backstab', 'Healed', 'Net HP'}
        for key in ("Damage", "Hits", "Avg", "Max") + tuple(extra_stats or []):
            row = tk.Frame(sf, bg='#161b22')
            if key not in hidden_initially:
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

        # Drop stale active encounters - if no damage for SESSION_GAP, the mob
        # either escaped or wasn't killed by us. Don't pollute HP samples.
        if self.active_encounters:
            stale = [m for m, e in self.active_encounters.items()
                     if now - e['last_ts'] > gap]
            for m in stale:
                del self.active_encounters[m]

        self._settle_pending_kills(now)

        # Drop entity ledgers that went quiet without a KILL (fled, map
        # change, someone else's kill) so they can't be misattributed later.
        if self.entity_stats:
            stale = [e for e, es in self.entity_stats.items()
                     if now - es['last_ts'] > 60000]
            for e in stale:
                del self.entity_stats[e]

        self._refresh(self.out_session, self.out_dps, self.out_stats, self.out_bd, '#3fb950', '#58a6ff')
        self._refresh(self.in_session,  self.in_dps,  self.in_stats,  self.in_bd,  '#f85149', '#d29922')
        self._refresh_exp()
        self._refresh_mini()

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

    def _settle_pending_kills(self, now):
        """Attribute entity damage ledgers to named kills, ~1.2s after the
        KILL line. By then the dead entity has stopped receiving damage while
        AoE survivors kept taking hits, so 'no damage after the kill' singles
        out the right entity. 300ms grace covers in-flight hits."""
        # Settle all due kills together with closest-match assignment. Chain
        # kills <300ms apart share candidates: picking the FRESHEST entity
        # gave kill A the entity killed by B (its killing blow lands inside
        # A's grace window) - confirmed swaps in live logs. Closest |last_ts
        # - kill_ts| pairs each kill with its own entity.
        due = []
        while self.pending_kills and now - self.pending_kills[0][1] > 1200:
            due.append(self.pending_kills.popleft())
        for mob, kill_ts in due:
            cands = [e for e, es in self.entity_stats.items()
                     if es['last_ts'] <= kill_ts + 300
                     and kill_ts - es['last_ts'] < 5000]
            if not cands:
                continue
            eid = min(cands, key=lambda e: abs(
                self.entity_stats[e]['last_ts'] - kill_ts))
            es = self.entity_stats.pop(eid)
            ms = self.mob_stats[mob]
            ms['damage'] += es['damage']
            ms['hits']   += es['hits']
            ms['backstab_damage'] += es['backstab_damage']
            ms['backstab_hits']   += es['backstab_hits']
            for t, d in es['types'].items():
                ms['out_types'][t] = ms['out_types'].get(t, 0) + d
            ms['damage_in'] += es['damage_in']
            ms['hits_in']   += es['hits_in']
            for t, d in es['in_types'].items():
                ms['in_types'][t] = ms['in_types'].get(t, 0) + d
            # Exact damage-to-kill for this instance - an HP sample.
            if es['damage'] > 0:
                ms['encounter_damages'].append(es['damage'])
            # Fight time: accumulate this instance's first-to-last-hit span.
            ms['fight_ms'] += max(0, es['last_ts'] - es['first_ts'])
            if not ms['first_seen_ms'] or es['first_ts'] < ms['first_seen_ms']:
                ms['first_seen_ms'] = es['first_ts']
            if es['last_ts'] > ms['last_seen_ms']:
                ms['last_seen_ms'] = es['last_ts']

    def _refresh(self, session, dps_lbl, stats, bd, hi_color, mid_color):
        if not session or session.count == 0:
            # Clear every stat so stale numbers from a prior session don't leak
            # into what looks like the current one.
            dps_lbl.config(text="— DPS", fg='#484f58')
            for key in ("Damage", "Hits", "Avg", "Max"):
                stats[key].config(text="0")
            for extra in ("Backstab", "Healed", "Net HP"):
                if extra in stats:
                    row = stats[extra].master
                    if row.winfo_ismapped():
                        row.pack_forget()
            bd.config(state=tk.NORMAL)
            bd.delete('1.0', tk.END)
            bd.config(state=tk.DISABLED)
            return
        dps = session.dps
        if session.backstab_count > 0:
            dps_text = f"{dps:.0f} / {session.dps_no_backstab:.0f} DPS"
        else:
            dps_text = f"{dps:.1f} DPS"
        dps_lbl.config(
            text=dps_text,
            fg=hi_color if dps >= 100 else mid_color if dps > 0 else '#484f58')
        stats["Damage"].config(text=f"{session.total:,}")
        stats["Hits"].config(text=str(session.count))
        stats["Avg"].config(text=f"{session.avg:.0f}")
        stats["Max"].config(text=str(session.max_hit))

        if "Backstab" in stats:
            bs_row = stats["Backstab"].master
            if session.backstab_count > 0:
                bs_pct = session.backstab_total / session.total * 100 if session.total else 0
                bs_avg = session.backstab_total / session.backstab_count
                stats["Backstab"].config(
                    text=f"{session.backstab_count}× ({bs_pct:.0f}% / avg {bs_avg:.0f})")
                if not bs_row.winfo_ismapped():
                    bs_row.pack(fill=tk.X, padx=8)
            else:
                if bs_row.winfo_ismapped():
                    bs_row.pack_forget()

        if "Healed" in stats:
            heal_row = stats["Healed"].master
            net_row  = stats["Net HP"].master
            if session.healed > 0:
                net = session.healed - session.total
                stats["Healed"].config(text=f"{session.healed:,}")
                stats["Net HP"].config(
                    text=f"{net:+,}",
                    fg='#3fb950' if net >= 0 else '#f85149')
                for row in (heal_row, net_row):
                    if not row.winfo_ismapped():
                        row.pack(fill=tk.X, padx=8)
            else:
                for row in (heal_row, net_row):
                    if row.winfo_ismapped():
                        row.pack_forget()

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
            self._record_session_summary(direction, s)
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
            # Only init on first attach - re-attach must preserve accumulated
            # exp_total so the rate stays anchored to the session's actual start.
            # Anchor to the EVENT's timestamp (not wall-clock) so that when a
            # tracker relaunch replays an existing log, the rate is computed
            # against the original attach time, not "now".
            if self.exp_start_ms == 0:
                self.exp_start_ms = ts
            self.toggle_btn.config(text="Stop (F12)", bg='#da3633')
            self.status.config(text="Tracking...", fg='#3fb950')
        elif etype == "ERROR":
            self.status.config(text=f"Error: {data}", fg='#f85149')

        elif etype == "BACKSTAB":
            if self._paused:
                return
            self.backstab_pending_until_ms = ts + BACKSTAB_TIMEOUT_MS

        elif etype == "PLAYER":
            try:
                self.player_entity_id = int(data)
            except ValueError:
                return
            # Hits we took before detection were misrouted to the outgoing
            # branch and opened a ledger under our own entity id - drop it so
            # it can never be attributed to a kill.
            self.entity_stats.pop(self.player_entity_id, None)
            self._log(f"  Player entity identified: {data}", 'info')

        elif etype == "DMG":
            if self._paused:
                return
            parts = data.split('|')
            if len(parts) < 5:
                return
            try:
                eid, dmg = int(parts[0]), int(parts[1])
            except ValueError:
                return
            if dmg <= 0 or parts[4] == '1':   # ignore heals for now
                return
            crit = parts[3] == '1'
            cat  = parts[2].capitalize()
            self.dmg_live_ms = ts

            if self.player_entity_id and eid == self.player_entity_id:
                if not self.in_session or not self.in_session.active:
                    self._new_session('in')
                self.in_session.add(ts, dmg, cat, crit=crit)
                self.last_in_ms = ts
                # Attribute the hit to the entity we're actively fighting
                # (freshest target within 3s). The feed doesn't name the
                # attacker, so this is a fighting-it heuristic - exact in a
                # melee brawl, approximate in mixed packs.
                if self.entity_stats:
                    tgt = max(self.entity_stats,
                              key=lambda e: self.entity_stats[e]['last_ts'])
                    tes = self.entity_stats[tgt]
                    if ts - tes['last_ts'] < 3000:
                        tes['damage_in'] += dmg
                        tes['hits_in']   += 1
                        tes['in_types'][cat] = tes['in_types'].get(cat, 0) + dmg
                elapsed = (ts - self.in_session.start_ms) / 1000.0
                self.event_ring.append((ts, 'IN', dmg, cat, f'entity {eid}'))
                self._log(f"   IN {elapsed:6.2f}s {dmg:>5d} [{cat}]", 'in')
            else:
                is_backstab = ts <= self.backstab_pending_until_ms
                if is_backstab:
                    self.backstab_pending_until_ms = 0
                if not self.out_session or not self.out_session.active:
                    self._new_session('out')
                    self._out_dummy = False
                self.out_session.add(ts, dmg, cat,
                                     backstab=is_backstab, crit=crit)
                self.last_out_ms = ts
                es = self.entity_stats.get(eid)
                if es is None:
                    es = self.entity_stats[eid] = {
                        'damage': 0, 'hits': 0, 'types': {},
                        'backstab_damage': 0, 'backstab_hits': 0,
                        'damage_in': 0, 'hits_in': 0, 'in_types': {},
                        'first_ts': ts, 'last_ts': ts}
                es['damage'] += dmg
                es['hits']   += 1
                es['last_ts'] = ts
                es['types'][cat] = es['types'].get(cat, 0) + dmg
                if is_backstab:
                    es['backstab_damage'] += dmg
                    es['backstab_hits']   += 1
                elapsed = (ts - self.out_session.start_ms) / 1000.0
                self.event_ring.append((ts, 'OUT', dmg, cat, f'entity {eid}'))
                tag = ' CRIT' if crit else (' BS' if is_backstab else '')
                self._log(f"  OUT {elapsed:6.2f}s {dmg:>5d} [{cat}]{tag}", 'out')
                self.status.config(text="Tracking...", fg='#3fb950')

        elif etype == "OUT":
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

            # Passive procs (Justice, Dream Eater) shouldn't consume the
            # backstab flag - it's meant for the actual ninja swing.
            is_proc = msg.startswith('Justice ') or msg.startswith('Dream Eater ')
            is_backstab = (not is_proc) and ts <= self.backstab_pending_until_ms

            # While the DMG feed is live it owns session damage (and consumes
            # the backstab flag); the text line only does mob bookkeeping below.
            dmg_live = ts - self.dmg_live_ms < 10000
            if not dmg_live:
                if is_backstab:
                    self.backstab_pending_until_ms = 0
                session_type_changed = self.out_session and self.out_session.active and (is_dummy != self._out_dummy)
                if not self.out_session or not self.out_session.active or session_type_changed:
                    self._new_session('out')
                    self._out_dummy = is_dummy

                self.out_session.add(ts, dmg, cat, backstab=is_backstab)
                self.last_out_ms = ts
            # Per-mob accounting. With the DMG feed live, damage/hit totals
            # come from the per-entity ledger (attributed on KILL) - the text
            # line then contributes only what text uniquely has: verbs.
            mob = extract_mob_outgoing(msg)
            if mob:
                ms = self.mob_stats[mob]
                if not ms['first_seen_ms']:
                    ms['first_seen_ms'] = ts
                ms['last_seen_ms'] = ts
                self.last_active_mob    = mob
                self.last_active_mob_ms = ts
                if not dmg_live:
                    ms['damage'] += dmg
                    ms['hits']   += 1
                    if is_backstab:
                        ms['backstab_damage'] += dmg
                        ms['backstab_hits']   += 1
                    # Active encounter - accumulate damage; closed on KILL.
                    enc = self.active_encounters.get(mob)
                    if enc is None:
                        self.active_encounters[mob] = {'damage': dmg, 'last_ts': ts}
                    else:
                        enc['damage']  += dmg
                        enc['last_ts']  = ts
            if not dmg_live:
                elapsed = (ts - self.out_session.start_ms) / 1000.0
                self.event_ring.append((ts, 'OUT', dmg, cat, msg))
                tag = ' BS' if is_backstab else ''
                self._log(f"  OUT {elapsed:6.2f}s {dmg:>5d} [{cat}]{tag}", 'out')
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

            # DMG-to-player events own incoming session damage once the player
            # entity is known; the IN text then only does mob bookkeeping.
            dmg_live = (self.player_entity_id
                        and ts - self.dmg_live_ms < 10000)
            if not dmg_live:
                if not self.in_session or not self.in_session.active:
                    self._new_session('in')
                self.in_session.add(ts, dmg, cat)
                self.last_in_ms = ts
            # Per-mob incoming attribution (only when we can identify the source)
            src = extract_mob_incoming(msg) if msg else None
            if src:
                ms = self.mob_stats[src]
                if not ms['first_seen_ms']:
                    ms['first_seen_ms'] = ts
                ms['last_seen_ms'] = ts
                self.last_active_mob    = src
                self.last_active_mob_ms = ts
                # Numeric accounting only when the DMG feed isn't live -
                # while it is, DMG-to-player events own incoming attribution
                # (via the entity ledger) and text would double-count.
                if not dmg_live:
                    ms['damage_in'] += dmg
                    ms['hits_in']   += 1
                    ms['in_types'][cat] = ms['in_types'].get(cat, 0) + dmg
            if not dmg_live:
                elapsed = (ts - self.in_session.start_ms) / 1000.0
                cat_tag = f" [{cat}]" if msg else ""
                self.event_ring.append((ts, 'IN', dmg, cat, msg))
                self._log(f"   IN {elapsed:6.2f}s {dmg:>5d}{cat_tag}", 'in')

        elif etype == "HEAL":
            if self._paused:
                return
            try:
                amt = int(data)
            except ValueError:
                return
            if amt <= 0:
                return
            # Healing counts toward the active incoming session so Net HP is
            # damage-vs-regen over the same window. It deliberately does NOT
            # open or extend a session - constant regen ticks would otherwise
            # keep an INCOMING session alive forever.
            if self.in_session and self.in_session.active:
                self.in_session.healed += amt

        elif etype == "KILL":
            if not self._paused:
                # Only OUR kills count - KILL_RE also matches "is destroyed"/
                # "is dead" lines for deaths we didn't cause.
                if data.startswith('You '):
                    self.kill_count += 1
                    self._refresh_exp()
                mob = extract_mob_kill(data)
                if mob:
                    ms = self.mob_stats[mob]
                    ms['kills'] += 1
                    ms['last_seen_ms'] = ts
                    if not ms['first_seen_ms']:
                        ms['first_seen_ms'] = ts
                    self.last_killed_mob = mob
                    self.last_kill_ms    = ts
                    # Consume XP that arrived just ahead of this kill line.
                    # Stale amounts (unpaired quest/death-recovery XP) are
                    # discarded, never credited to a mob.
                    self.last_kill_xp_done = False
                    if self.pending_xp and ts - self.pending_xp[1] >= 2000:
                        self.pending_xp = None
                    if self.pending_xp:
                        ms['xp'] += self.pending_xp[0]
                        ms['xp_samples'].append(self.pending_xp[0])
                        self.exp_total += self.pending_xp[0]
                        self.pending_xp = None
                        self.last_kill_xp_done = True
                        self._refresh_exp()
                    # Entity attribution is deferred (see _settle_pending_kills)
                    # so AoE volleys don't credit a surviving entity.
                    self.pending_kills.append((mob, ts))
                    # Text-based encounter close (only accumulates when the
                    # DMG feed is not live).
                    enc = self.active_encounters.pop(mob, None)
                    if enc and enc['damage'] > 0:
                        ms['encounter_damages'].append(enc['damage'])
            self.event_ring.append((ts, 'KILL', 0, '', data))
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
            # Anchor to the event's timestamp so log replays compute against
            # the original event time, not wall-clock-now (would inflate rate).
            if self.exp_start_ms == 0:
                self.exp_start_ms = ts
            # XP only counts once it pairs with a kill. Unpaired receipts
            # exist and can be enormous - a death-penalty recovery of 6M XP
            # was observed, 19x a whole session's kill XP - and would wreck
            # Total XP / XP-hr if credited.
            # The web feed sends EXP just BEFORE its KILL line, so normally
            # the amount is held for the next KILL to consume. The old
            # ordering (KILL then EXP, seen in v1 log replays) is covered by
            # attributing directly when the last kill is fresh and unpaid.
            if (self.last_killed_mob and not self.last_kill_xp_done and
                    ts - self.last_kill_ms < 2000):
                ms = self.mob_stats[self.last_killed_mob]
                ms['xp'] += xp
                ms['xp_samples'].append(xp)
                self.exp_total += xp
                self.last_kill_xp_done = True
            else:
                self.pending_xp = (xp, ts)
            self.event_ring.append((ts, 'EXP', xp, '', ''))
            self._log(f"  EXP  +{xp:,}", 'info')
            self._refresh_exp()

        elif etype == "DEATH":
            self._log("  DIED", 'in')
            self._dump_death_forensics(ts, data)
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
        _trim_text_widget(self.log, COMBAT_LOG_MAX_LINES)
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
            self._print_summary()
        else:
            if self.exp_start_ms == 0:
                self.exp_start_ms = int(time.time() * 1000)
            self.toggle_btn.config(text="Stop (F12)", bg='#da3633')
            self.status.config(text="Tracking...", fg='#3fb950')

    def _xp_elapsed_s(self):
        """Wall-clock seconds since XP tracking began (0 if not started)."""
        if not self.exp_start_ms:
            return 0
        return (int(time.time() * 1000) - self.exp_start_ms) / 1000

    def _xp_rate(self, elapsed_s=None):
        """XP/hr over the session; 0 if not enough data."""
        if elapsed_s is None:
            elapsed_s = self._xp_elapsed_s()
        if elapsed_s <= 0 or self.exp_total <= 0:
            return 0
        return self.exp_total * 3600 / elapsed_s


    def _print_summary(self):
        elapsed_s = self._xp_elapsed_s()
        m, s = divmod(int(elapsed_s), 60)
        h, m = divmod(m, 60)
        dur = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        self._log("", 'info')
        self._log("─" * 52, 'info')
        self._log(f" SESSION SUMMARY — {dur}", 'kill')
        self._log("─" * 52, 'info')

        # EXP block
        if self.exp_total > 0:
            xp_per_k  = self.exp_total / self.kill_count if self.kill_count else 0
            self._log(f"  XP          {self.exp_total:>10,}", 'out')
            self._log(f"  XP/hr       {self._xp_rate(elapsed_s):>10,.0f}", 'out')
            self._log(f"  Kills       {self.kill_count:>10,}", 'out')
            if self.kill_count:
                self._log(f"  XP / kill   {xp_per_k:>10,.0f}", 'out')

        # Outgoing damage
        if self.out_session and self.out_session.count:
            s = self.out_session
            self._log(f"  OUT dmg     {s.total:>10,}  ({s.count} hits, {s.dps:.1f} DPS)", 'out')
            if s.backstab_count > 0:
                self._log("", 'out')
                self._log(f"  Backstab hit statistics:", 'out')
                self._log(f"      Mean: {s.backstab_mean};", 'out')
                self._log(f"      Stdev: {s.backstab_stdev};", 'out')
                self._log(f"      Min: {s.backstab_min};", 'out')
                self._log(f"      Max: {s.backstab_max}", 'out')
            if s.normal_count > 0 and s.backstab_count > 0:
                self._log("", 'out')
                self._log(f"  Non-backstab hit statistics:", 'out')
                self._log(f"      Mean: {s.normal_mean};", 'out')
                self._log(f"      Stdev: {s.normal_stdev};", 'out')
                self._log(f"      Min: {s.normal_min};", 'out')
                self._log(f"      Max: {s.normal_max}", 'out')

        # Incoming damage
        if self.in_session and self.in_session.count:
            s = self.in_session
            self._log(f"  IN  dmg     {s.total:>10,}  ({s.count} hits, {s.dps:.1f} DPS)", 'in')

        self._log("─" * 52, 'info')

    def _refresh_exp(self):
        self.exp_labels["Total XP"].config(text=f"{self.exp_total:,}")
        self.exp_labels["Kills"].config(text=str(self.kill_count))
        if self.exp_total <= 0 or not self.exp_start_ms:
            self.exp_rate_lbl.config(text="— XP/hr", fg='#484f58')
            return
        elapsed_s = self._xp_elapsed_s()
        # Don't report a rate until tracking has run for at least 10 seconds -
        # prevents burst kills in the first few seconds from spiking the rate.
        if elapsed_s < 10:
            self.exp_rate_lbl.config(text="… XP/hr", fg='#7d8590')
        else:
            life_rate = self._xp_rate(elapsed_s)
            self.exp_rate_lbl.config(
                text=f"{life_rate:,.0f} XP/hr", fg='#d2a8ff')

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
        self.mob_stats.clear()
        self.active_encounters.clear()
        self.last_killed_mob = None
        self.last_kill_ms    = 0
        self.event_ring.clear()
        self.entity_stats.clear()
        self.pending_kills.clear()
        self.pending_xp = None
        self.last_kill_xp_done = True
        self.time_labels["Session Start"].config(text="—")
        self.time_labels["Duration"].config(text="—")
        self.last_active_mob    = None
        self.last_active_mob_ms = 0
        self.backstab_pending_until_ms = 0
        # Note: history panel is intentionally NOT cleared here - survives Reset
        # so the user can review past sessions/deaths after wiping live state.
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


# ── Client helpers ────────────────────────────────────────────────────────────

def _res_dir():
    return Path(sys._MEIPASS) if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR

def _run_dir():
    return Path(sys.executable).parent if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR


CLIENT_EXE_NAME = "WyvernWindowsClient.exe"
CDP_PORT        = 9222


def find_client_exe():
    """Locate the Steam Wyvern client exe by walking Steam's library folders."""
    steam_paths = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam_paths.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
    except Exception:
        pass
    steam_paths.append(Path("C:/Program Files (x86)/Steam"))

    libraries = []
    for sp in steam_paths:
        libraries.append(sp)
        vdf = sp / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors='ignore')):
                    libraries.append(Path(m.group(1).replace('\\\\', '\\')))
            except Exception:
                pass

    for lib in libraries:
        exe = lib / "steamapps" / "common" / "Wyvern" / CLIENT_EXE_NAME
        if exe.exists():
            return exe
    return None


def client_pids():
    """PIDs of running WyvernWindowsClient processes (empty list if none)."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {CLIENT_EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        pids = []
        for line in (r.stdout or "").splitlines():
            parts = line.split('","')
            if len(parts) >= 2 and CLIENT_EXE_NAME.lower() in parts[0].lower():
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def _pid_alive(pid):
    """Windows-only: check if a process with the given PID is still running."""
    if not pid:
        return True  # Unknown PID — be permissive, don't self-close
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


WEB_CAPTURE = None  # the active web_capture.WebCapture, once attached


def _make_event_writer(log_path):
    """Thread-safe `ETYPE|ts_ms|data` appender (same format the agent wrote)."""
    lock = threading.Lock()

    def write_event(etype, data):
        line = f"{etype}|{int(time.time() * 1000)}|{data}\n"
        with lock:
            try:
                with open(log_path, 'a', encoding='utf-8') as fh:
                    fh.write(line)
            except OSError:
                pass
    return write_event


GAME_URL = "https://play.ghosttrack.com/game"

# Chromium-based browsers we can drive for browser play, in preference order.
_BROWSER_CANDIDATES = [
    ("Chrome", [
        Path(os.environ.get('ProgramFiles', 'C:/Program Files'))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get('LOCALAPPDATA', ''))
        / "Google/Chrome/Application/chrome.exe",
    ]),
    ("Edge", [
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get('ProgramFiles', 'C:/Program Files'))
        / "Microsoft/Edge/Application/msedge.exe",
    ]),
]


def find_browser_exe():
    """(name, path) of an installed Chromium browser, or (None, None)."""
    for name, paths in _BROWSER_CANDIDATES:
        for p in paths:
            if p.exists():
                return name, p
    return None, None


def launch_browser_client():
    """Launch Chrome/Edge with the debug port on the game URL.

    Modern Chrome only honors --remote-debugging-port together with a
    non-default --user-data-dir, so the tracker keeps its own browser profile
    (game login persists there between runs).

    Returns the Popen process, or an error string.
    """
    name, exe = find_browser_exe()
    if not exe:
        return "No Chrome or Edge installation found for browser play."
    profile = Path(os.environ.get('LOCALAPPDATA', str(_run_dir()))) \
        / "WyvernDPSTracker" / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    print(f"Launching {name} (tracker profile) at {GAME_URL}...")
    return subprocess.Popen([
        str(exe),
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        GAME_URL,
    ])


def attach_web(prefer_browser=False):
    """Attach to the game via CDP — the Steam Electron client or a Chromium
    browser playing play.ghosttrack.com — launching one if needed.

    Returns True on success, or an error string for main() to print.
    """
    import web_capture

    global LOG_FILE, GAME_PID, WEB_CAPTURE
    run = _run_dir()
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = run / f"dps_events_{ts_str}.log"

    if not web_capture.cdp_alive(CDP_PORT):
        running = client_pids()
        if running and not prefer_browser:
            print("Wyvern is running but without the debug port the tracker needs.")
            print("Close Wyvern and the tracker will relaunch it automatically,")
            print(f"or add  --remote-debugging-port={CDP_PORT}  to its Steam launch")
            print("options and restart it yourself.  Waiting...")
            while True:
                time.sleep(2)
                if web_capture.cdp_alive(CDP_PORT):
                    break
                if not client_pids():
                    running = []
                    break

        if not web_capture.cdp_alive(CDP_PORT):
            steam_exe = None if prefer_browser else find_client_exe()
            if steam_exe:
                print(f"Launching {steam_exe.name} with debug port {CDP_PORT}...")
                proc = subprocess.Popen(
                    [str(steam_exe), f"--remote-debugging-port={CDP_PORT}"],
                    cwd=str(steam_exe.parent))
                GAME_PID = proc.pid
            else:
                proc = launch_browser_client()
                if isinstance(proc, str):
                    return proc
                # No PID watch for browsers: the Chromium launcher process
                # hands off and exits immediately, which would read as "game
                # closed". The capture's CDP disconnect detection covers the
                # browser actually closing.
            deadline = time.time() + 30
            while time.time() < deadline:
                if web_capture.cdp_alive(CDP_PORT):
                    break
                time.sleep(0.5)
            else:
                return "Client launched but its debug port never came up."

    if not GAME_PID and not prefer_browser:
        pids = client_pids()
        if pids:
            GAME_PID = pids[0]

    print("Attaching to Wyvern...")
    WEB_CAPTURE = web_capture.WebCapture(
        CDP_PORT, _make_event_writer(LOG_FILE))
    WEB_CAPTURE.start()
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Wyvern DPS Tracker ===\n")

    ctypes.windll.kernel32.CreateMutexW(None, True, "WyvernDPSTracker_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("Another DPS Tracker is already running!")
        input("Press Enter to close...")
        sys.exit(1)

    result = attach_web(prefer_browser='--browser' in sys.argv)
    if result is not True:
        print("\nCould not attach to game.")
        if isinstance(result, str):
            print(f"\n  >> {result}")
        input("\nPress Enter to close...")
        sys.exit(1)

    def cleanup():
        if WEB_CAPTURE is not None:
            WEB_CAPTURE.stop()
    atexit.register(cleanup)

    print("\nStarting GUI...")
    app = DPSTrackerGUI()
    app.run()
    cleanup()
    sys.exit(0)


if __name__ == '__main__':
    main()
