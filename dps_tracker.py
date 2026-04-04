"""
Wyvern DPS Tracker

Hooks into the Wyvern game client via Java Attach API to capture all combat
damage (outgoing and incoming) with millisecond timestamps and per-type breakdown.
"""

import atexit
import os
import re
import shutil
import sys
import subprocess
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_FILE = SCRIPT_DIR / "dps_events_v2.log"
SESSION_GAP = 15     # seconds of no hits before session ends
SESSION_BACKTRACK = True  # snap end time back to last hit (not the gap timeout)

# ============================================================
# Damage categorization
# ============================================================

ELEMENT_PATTERNS = [
    (re.compile(r'poison|venom', re.I), 'Poison'),
    (re.compile(r'lightning|energy surge|shocked|electr', re.I), 'Shock'),
    (re.compile(r'flame|burn|fire|inferno|incinerat|scorch|sear|magma|lava|\bhot\b', re.I), 'Fire'),
    (re.compile(r'arctic|glacial|frost|freeze|froze|chill|\bice\b|blizzard', re.I), 'Cold'),
    (re.compile(r'acid|corrosi|dissolv|caustic', re.I), 'Acid'),
    (re.compile(r'death|necrotic|drain|dark energy|shadow|unholy|wither', re.I), 'Death'),
    (re.compile(r'holy|radiance|divinity|divine|vengeance|sacred', re.I), 'Holy'),
    (re.compile(r'spirit|rend', re.I), 'Magic'),
]

OUTGOING_VERB_RE = re.compile(r'^You\s+(\w+)', re.I)
OUTGOING_VERBS = {
    'cut': 'Cut', 'slice': 'Cut', 'sliced': 'Cut', 'slash': 'Cut', 'slashed': 'Cut',
    'carve': 'Cut', 'carved': 'Cut', 'cleave': 'Cut', 'cleaved': 'Cut',
    'hew': 'Cut', 'hewed': 'Cut',
    'smash': 'Smash', 'smashed': 'Smash', 'crush': 'Smash', 'crushed': 'Smash',
    'slam': 'Smash', 'slammed': 'Smash', 'bash': 'Smash', 'bashed': 'Smash',
    'pummel': 'Smash', 'pummeled': 'Smash', 'smote': 'Smash', 'smite': 'Smash',
    'drown': 'Smash', 'drowned': 'Smash', 'stagger': 'Smash', 'staggered': 'Smash',
    'hit': 'Smash', 'strike': 'Smash', 'struck': 'Smash', 'graze': 'Smash', 'grazed': 'Smash',
    'blast': 'Smash', 'blasted': 'Smash', 'zap': 'Smash', 'zapped': 'Smash',
    'overwhelm': 'Smash', 'overwhelmed': 'Smash', 'engulf': 'Smash', 'engulfed': 'Smash',
    'scorch': 'Smash', 'scorched': 'Smash', 'shock': 'Smash', 'shocked': 'Smash',
    'decimate': 'Smash', 'decimated': 'Smash',
    'condemn': 'Smash', 'condemned': 'Smash',
    'stab': 'Stab', 'stabbed': 'Stab', 'pierce': 'Stab', 'pierced': 'Stab',
    'skewer': 'Stab', 'skewered': 'Stab', 'impale': 'Stab', 'impaled': 'Stab',
}

INCOMING_VERB_RE = re.compile(
    r'(hits|damages|slashes|stabs|bites|claws|burns|zaps|smashes|crushes|'
    r'strikes|blasts|freezes|shocks|drowns|staggers|cuts|pierces|impales|grazed)\s+you', re.I)
INCOMING_VERBS = {
    'hits': 'Smash', 'damages': 'Smash', 'strikes': 'Smash', 'blasts': 'Smash',
    'smashes': 'Smash', 'crushes': 'Smash', 'drowns': 'Smash', 'staggers': 'Smash',
    'grazed': 'Smash',
    'slashes': 'Cut', 'cuts': 'Cut', 'claws': 'Cut',
    'stabs': 'Stab', 'pierces': 'Stab', 'impales': 'Stab', 'bites': 'Stab',
    'burns': 'Fire', 'zaps': 'Shock', 'shocks': 'Shock', 'freezes': 'Cold',
}

HOLE_RE = re.compile(r'make a hole|daylight through', re.I)
NEARLY_CUT_RE = re.compile(r'nearly cut.*in half', re.I)

TYPE_COLORS = {
    'Shock': '#87ceeb', 'Fire': '#ff6347', 'Cold': '#add8e6',
    'Acid': '#7fff00', 'Death': '#9370db', 'Poison': '#00ff7f',
    'Holy': '#da70d6', 'Magic': '#9966ff',
    'Cut': '#ffa500', 'Smash': '#cd853f', 'Stab': '#daa520',
}


def _check_elements(text):
    """Check for elemental keywords. Returns type or None."""
    for pat, dtype in ELEMENT_PATTERNS:
        if pat.search(text):
            return dtype
    return None


# Verbs that unambiguously indicate a damage type
VERB_TYPES = {
    'poisoned': 'Poison', 'poison': 'Poison',
    'froze': 'Cold', 'freeze': 'Cold',
    'burned': 'Fire', 'burn': 'Fire', 'scorched': 'Fire', 'scorch': 'Fire',
    'shocked': 'Shock', 'shock': 'Shock', 'zapped': 'Shock', 'zap': 'Shock',
    'corroded': 'Acid', 'corrode': 'Acid',
}

# Extract flavor text: the part after prepositions (with/in) before "for X damage"
FLAVOR_RE = re.compile(r'\b(?:with|in)\s+(.+?)\s+for\s+\d+\s+damage', re.I)


def categorize_outgoing(msg):
    if not msg:
        return 'Unknown'

    # 1. Check verb first — unambiguous type verbs beat everything
    m = OUTGOING_VERB_RE.match(msg)
    if m:
        verb = m.group(1).lower()
        if verb in VERB_TYPES:
            return VERB_TYPES[verb]

    # 2. Check element keywords in flavor text only (avoids monster name matches)
    #    Flavor = text after "with/in" preposition before "for X damage"
    flavor = FLAVOR_RE.search(msg)
    if flavor:
        elem = _check_elements(flavor.group(1))
        if elem:
            return elem

    # 3. Check "'s spirit" / "rend" patterns (for "Your claws rend X's spirit")
    if re.search(r"'s spirit\b", msg, re.I) or re.search(r'\brend\b', msg, re.I):
        return 'Magic'

    # 4. Physical type from verb
    if HOLE_RE.search(msg):
        return 'Stab'
    if NEARLY_CUT_RE.search(msg):
        return 'Cut'
    if m:
        return OUTGOING_VERBS.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


def categorize_incoming(msg):
    if not msg:
        return 'Unknown'
    elem = _check_elements(msg)
    if elem:
        return elem
    m = INCOMING_VERB_RE.search(msg)
    if m:
        return INCOMING_VERBS.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


# ============================================================
# Session tracking
# ============================================================

@dataclass
class Session:
    start_ms: int = 0
    end_ms: int = 0
    total_damage: int = 0
    hit_count: int = 0
    max_hit: int = 0
    min_hit: int = 999999
    hits: list = field(default_factory=list)
    active: bool = True
    categories: dict = field(default_factory=lambda: defaultdict(
        lambda: {'damage': 0, 'count': 0, 'max': 0, 'min': 999999}))

    @property
    def elapsed_s(self):
        if not self.start_ms:
            return 0.0
        end = self.end_ms or int(time.time() * 1000)
        return (end - self.start_ms) / 1000.0

    @property
    def dps(self):
        e = self.elapsed_s
        return self.total_damage / e if e > 0 else 0.0

    @property
    def avg_hit(self):
        return self.total_damage / self.hit_count if self.hit_count else 0.0

    def add_hit(self, ts, damage, category='Unknown'):
        self.hits.append((ts, damage, category))
        self.total_damage += damage
        self.hit_count += 1
        self.max_hit = max(self.max_hit, damage)
        self.min_hit = min(self.min_hit, damage)
        if not self.start_ms:
            self.start_ms = ts
        c = self.categories[category]
        c['damage'] += damage
        c['count'] += 1
        c['max'] = max(c['max'], damage)
        c['min'] = min(c['min'], damage)

    def finalize(self, end_ms=None):
        self.end_ms = end_ms or (self.hits[-1][0] if self.hits else 0)
        self.active = False


# ============================================================
# GUI
# ============================================================

class DPSTrackerGUI:
    def __init__(self):
        self.out_session = None
        self.in_session = None
        self.sessions = []
        self.last_out_ms = 0
        self.last_in_ms = 0
        self._shutdown = False
        self._has_message_text = False
        self._out_is_dummy = False  # True when current outgoing session is vs Training Dummy

        self._build_ui()
        self._start_log_reader()

    # -- UI setup --

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Wyvern DPS Tracker")
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#0d1117')
        self.root.geometry('440x820')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        mono = tkfont.Font(family='Consolas', size=11)
        big = tkfont.Font(family='Consolas', size=28, weight='bold')
        lbl = tkfont.Font(family='Consolas', size=10)
        sm = tkfont.Font(family='Consolas', size=9)

        # Status
        self.status = tk.Label(self.root, text="Connecting...", fg='#f0883e',
                               bg='#0d1117', font=sm, anchor='w')
        self.status.pack(fill=tk.X, padx=12, pady=(8, 0))

        # Outgoing (with type breakdown)
        self.out_dps, self.out_stats, self.out_bd = self._build_section(
            "OUTGOING", '#3fb950', big, lbl, sm, mono, breakdown=True)

        # Incoming (with type breakdown — message text paired with HP deltas)
        self.in_dps, self.in_stats, self.in_bd = self._build_section(
            "INCOMING", '#f85149', big, lbl, sm, mono, breakdown=True)

        # Timing
        tf = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        tf.pack(fill=tk.X, padx=12, pady=4)
        self.time_labels = {}
        for key in ("Session Start", "Duration"):
            row = tk.Frame(tf, bg='#161b22')
            row.pack(fill=tk.X, padx=8)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=sm, width=14, anchor='w').pack(side=tk.LEFT)
            v = tk.Label(row, text="—", fg='#c9d1d9', bg='#161b22',
                         font=sm, anchor='e')
            v.pack(side=tk.RIGHT)
            self.time_labels[key] = v

        # Reset button
        btn_frame = tk.Frame(self.root, bg='#0d1117')
        btn_frame.pack(fill=tk.X, padx=12, pady=2)
        tk.Button(btn_frame, text="Reset", font=sm, bg='#21262d', fg='#c9d1d9',
                  bd=0, cursor='hand2', width=8, command=self._reset).pack(side=tk.LEFT)

        # Combat log
        lf = tk.Frame(self.root, bg='#0d1117')
        lf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
        tk.Label(lf, text="Combat Log", fg='#7d8590', bg='#0d1117',
                 font=lbl, anchor='w').pack(anchor='w')
        self.log = tk.Text(lf, bg='#161b22', fg='#8b949e', font=('Consolas', 9),
                           height=6, bd=0, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.tag_configure('out', foreground='#58a6ff')
        self.log.tag_configure('in', foreground='#f85149')
        self.log.tag_configure('kill', foreground='#3fb950')
        self.log.tag_configure('info', foreground='#7d8590')

        self._tick()

    def _build_section(self, title, color, big, lbl, sm, mono, breakdown=True):
        """Build an outgoing/incoming section. Returns (dps_label, stats_dict, breakdown_widget)."""
        hdr = tk.Frame(self.root, bg='#0d1117')
        hdr.pack(fill=tk.X, padx=12, pady=(6, 0))
        tk.Label(hdr, text=title, fg=color, bg='#0d1117',
                 font=lbl, anchor='w').pack(side=tk.LEFT)
        dps_lbl = tk.Label(hdr, text="— DPS", fg='#484f58',
                           bg='#0d1117', font=big, anchor='e')
        dps_lbl.pack(side=tk.RIGHT)

        sf = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        sf.pack(fill=tk.X, padx=12, pady=2)
        stats = {}
        for key in ("Damage", "Hits", "Avg", "Max"):
            row = tk.Frame(sf, bg='#161b22')
            row.pack(fill=tk.X, padx=8)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=sm, width=8, anchor='w').pack(side=tk.LEFT)
            v = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22',
                         font=sm, anchor='e')
            v.pack(side=tk.RIGHT)
            stats[key] = v

        bd = None
        if breakdown:
            bd = tk.Text(self.root, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
                         height=5, bd=1, relief='groove', state=tk.DISABLED,
                         wrap=tk.NONE, highlightthickness=0)
            bd.pack(fill=tk.X, padx=12, pady=2)
            for cat, c in TYPE_COLORS.items():
                bd.tag_configure(cat, foreground=c)
            bd.tag_configure('Unknown', foreground='#8b949e')
            bd.tag_configure('hdr', foreground='#7d8590')

        return dps_lbl, stats, bd

    # -- Update loop --

    def _tick(self):
        if self._shutdown:
            return
        now = int(time.time() * 1000)

        # Auto-end stale sessions
        if self.out_session and self.out_session.active and self.last_out_ms:
            if now - self.last_out_ms > SESSION_GAP * 1000:
                self._end('out')
        if self.in_session and self.in_session.active and self.last_in_ms:
            if now - self.last_in_ms > SESSION_GAP * 1000:
                self._end('in')

        self._refresh_section(self.out_session, self.out_dps, self.out_stats,
                              self.out_bd, '#3fb950', '#58a6ff')
        self._refresh_section(self.in_session, self.in_dps, self.in_stats,
                              self.in_bd, '#f85149', '#d29922')

        # Timing from earliest active session
        starts = [s.start_ms for s in (self.out_session, self.in_session)
                  if s and s.start_ms]
        if starts:
            earliest = min(starts)
            dt = datetime.fromtimestamp(earliest / 1000.0)
            self.time_labels["Session Start"].config(
                text=dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}")
            elapsed = (now - earliest) / 1000.0
            m, s = divmod(elapsed, 60)
            self.time_labels["Duration"].config(text=f"{int(m)}:{s:05.2f}")

        self.root.after(100, self._tick)

    def _refresh_section(self, session, dps_lbl, stats, bd, hi_color, mid_color):
        if not session or session.hit_count == 0:
            dps_lbl.config(text="— DPS", fg='#484f58')
            return

        dps = session.dps
        dps_lbl.config(text=f"{dps:.1f} DPS")
        dps_lbl.config(fg=hi_color if dps >= 100 else mid_color if dps > 0 else '#484f58')

        stats["Damage"].config(text=f"{session.total_damage:,}")
        stats["Hits"].config(text=str(session.hit_count))
        stats["Avg"].config(text=f"{session.avg_hit:.0f}")
        stats["Max"].config(text=str(session.max_hit))

        # Breakdown table (only if widget exists)
        if bd is None:
            return
        bd.config(state=tk.NORMAL)
        bd.delete('1.0', tk.END)
        bd.insert(tk.END, f"{'Type':<10}{'Hits':>5}{'Dmg':>8}{'Avg':>6}{'DPS':>7}{'%':>5}\n", 'hdr')
        elapsed = session.elapsed_s
        for name, st in sorted(session.categories.items(),
                                key=lambda x: x[1]['damage'], reverse=True):
            cnt, dmg = st['count'], st['damage']
            avg = dmg / cnt if cnt else 0
            cdps = dmg / elapsed if elapsed > 0 else 0
            pct = dmg / session.total_damage * 100 if session.total_damage else 0
            tag = name if name in TYPE_COLORS else 'Unknown'
            bd.insert(tk.END,
                      f"{name:<10}{cnt:>5}{dmg:>8,}{avg:>6.0f}{cdps:>7.1f}{pct:>4.0f}%\n", tag)
        bd.config(state=tk.DISABLED)

    # -- Session management --

    def _start(self, direction):
        if direction == 'out':
            if self.out_session and self.out_session.active:
                self._end('out')
            self.out_session = Session()
        else:
            if self.in_session and self.in_session.active:
                self._end('in')
            self.in_session = Session()

    def _end(self, direction):
        s = self.out_session if direction == 'out' else self.in_session
        tag = "OUT" if direction == 'out' else "IN"
        if s and s.hit_count > 0:
            s.finalize()
            self.sessions.append((direction, s))
            self._log(f"--- {tag}: {s.dps:.1f} DPS, {s.total_damage:,} dmg, {s.elapsed_s:.1f}s ---", 'info')
        if direction == 'out':
            self.out_session = None
        else:
            self.in_session = None

    # -- Event handling --

    def _handle(self, etype, ts, data):
        if self._shutdown:
            return

        if etype == "AGENT_READY":
            self.status.config(text="Agent loaded...", fg='#f0883e')
        elif etype == "ATTACHED":
            self.status.config(text="Attached! Start fighting.", fg='#3fb950')
        elif etype == "ERROR":
            self.status.config(text=f"Error: {data}", fg='#f85149')

        elif etype in ("OUT", "HIT"):
            parts = data.split('|', 1)
            try:
                dmg = int(parts[0])
            except ValueError:
                return
            msg = parts[1] if len(parts) > 1 else ""
            if msg:
                self._has_message_text = True
            elif self._has_message_text:
                return  # skip legacy events without text

            cat = categorize_outgoing(msg)
            is_dummy = 'Training Dummy' in msg

            # Training Dummy: force new session on first hit
            if is_dummy and (not self.out_session or not self.out_session.active
                             or not self._out_is_dummy):
                self._start('out')
                self._out_is_dummy = True
            elif not is_dummy and (not self.out_session or not self.out_session.active):
                self._start('out')
                self._out_is_dummy = False

            self.out_session.add_hit(ts, dmg, cat)
            self.last_out_ms = ts

            elapsed = (ts - self.out_session.start_ms) / 1000.0
            self._log(f"  OUT {elapsed:6.2f}s {dmg:>5d} [{cat}]", 'out')
            self.status.config(text="Tracking...", fg='#3fb950')

        elif etype == "IN":
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
                self._start('in')
            self.in_session.add_hit(ts, dmg, cat)
            self.last_in_ms = ts

            elapsed = (ts - self.in_session.start_ms) / 1000.0
            cat_tag = f" [{cat}]" if msg else ""
            self._log(f"   IN {elapsed:6.2f}s {dmg:>5d}{cat_tag}", 'in')

        elif etype == "KILL":
            self._log(f"  KILL  {data}", 'kill')
            # End outgoing session if Training Dummy killed
            if 'Training Dummy' in data:
                if self.out_session and self.out_session.active:
                    self._end('out')
                self._out_is_dummy = False

        elif etype == "DEATH":
            self._log(f"  DIED", 'in')
            if self.out_session and self.out_session.active:
                self._end('out')
            if self.in_session and self.in_session.active:
                self._end('in')

    # -- Log reader --

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
                line = line.strip().rstrip('\r')
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
                data = parts[2] if len(parts) > 2 else ""
                self.root.after(0, self._handle, etype, ts, data)

    # -- Helpers --

    def _log(self, text, tag='out'):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n", tag)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _reset(self):
        """Reset all sessions and clear the log."""
        if self.out_session and self.out_session.active:
            self._end('out')
        if self.in_session and self.in_session.active:
            self._end('in')
        self.out_session = None
        self.in_session = None
        self.last_out_ms = 0
        self.last_in_ms = 0
        self._out_is_dummy = False
        self.log.config(state=tk.NORMAL)
        self.log.delete('1.0', tk.END)
        self.log.config(state=tk.DISABLED)
        self.status.config(text="Reset. Start fighting.", fg='#3fb950')

    def _on_close(self):
        self._shutdown = True
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================================================
# Resource / Java helpers
# ============================================================

def _res_dir():
    return Path(sys._MEIPASS) if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR

def _run_dir():
    return Path(sys.executable).parent if getattr(sys, '_MEIPASS', None) else SCRIPT_DIR


def find_java():
    """Find a JDK with jdk.attach module."""
    candidates = []
    jh = os.environ.get('JAVA_HOME')
    if jh:
        candidates.append(Path(jh) / "bin" / "java.exe")
    for base in (Path("C:/Program Files/Java"), Path("C:/Program Files/Eclipse Adoptium"),
                 Path("C:/Program Files/Android/Android Studio1/jbr"),
                 Path("C:/Program Files/Microsoft"), Path("C:/Program Files/Zulu")):
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
    """Inject agent into the running Wyvern JVM."""
    java = find_java()
    if not java:
        print("ERROR: No JDK with jdk.attach found.")
        print("Install a JDK (Java 11+) — https://adoptium.net/")
        return False

    res, run = _res_dir(), _run_dir()
    attacher = str(res / "attacher")
    agent = str(res / "agent.jar")
    log = str(run / "dps_events_v2.log")

    global LOG_FILE
    LOG_FILE = Path(log)

    if not Path(agent).exists():
        print(f"ERROR: agent.jar not found at {agent}")
        return False

    try:
        LOG_FILE.unlink(missing_ok=True)
    except PermissionError:
        pass

    print(f"Using Java: {java}")
    print("Attaching to Wyvern JVM...")
    try:
        r = subprocess.run([java, "-cp", attacher, "dps.DPSAttacher", agent, log],
                           capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("Attach timed out.")
        return False

    print(r.stdout)
    if r.returncode != 0:
        print(f"Attach failed:\n{r.stderr}")
        time.sleep(1)
        if LOG_FILE.exists():
            print("Agent may already be loaded. Continuing.")
            return True
        return False
    return True


# ============================================================
# Main
# ============================================================

def main():
    print("=== Wyvern DPS Tracker ===\n")

    # Single instance
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, True, "WyvernDPSTracker_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("Another DPS Tracker is already running!")
        input("Press Enter to close...")
        sys.exit(1)

    if not attach_agent():
        print("\nCould not attach to game.")
        print("  1. Is Wyvern running?")
        print("  2. JDK 11+ installed? (not JRE) — https://adoptium.net/")
        java = find_java()
        if not java:
            print("\n  >> No JDK found!")
        else:
            print(f"\n  >> Java found: {java}")
            print("  >> Game might not be running.")
        input("\nPress Enter to close...")
        sys.exit(1)

    # Lock file for agent cleanup
    lock = Path(str(LOG_FILE) + ".lock")
    lh = open(lock, 'w')
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
