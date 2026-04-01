"""
Wyvern DPS Tracker

Hooks into the running Wyvern game client via Java Attach API, captures
all combat damage (outgoing and incoming) with millisecond timestamps,
and displays real-time DPS with per-attack-type breakdown.

Usage: python dps_tracker.py
"""

import atexit
import os
import re
import sys
import time
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, font as tkfont
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_FILE = SCRIPT_DIR / "dps_events_v2.log"
SESSION_GAP_SECONDS = 5

# ============================================================
# Damage type categorization
# ============================================================

# Elemental — detected from flavor text keywords
ELEMENT_PATTERNS = [
    (re.compile(r'lightning|energy surge|shocked|electr', re.I), 'Shock'),
    (re.compile(r'flame|burn|fire|inferno|incinerat|scorch|sear|magma|lava', re.I), 'Fire'),
    (re.compile(r'arctic|glacial|frost|freeze|chill|frozen|cold|\bice\b|blizzard', re.I), 'Cold'),
    (re.compile(r'acid|corrosi|dissolv|caustic', re.I), 'Acid'),
    (re.compile(r'death|necrotic|drain|dark energy|shadow|unholy|wither', re.I), 'Death'),
]

VERB_RE = re.compile(r'^You\s+(\w+)', re.IGNORECASE)

PHYSICAL_TYPES = {
    'cut': 'Cut', 'slice': 'Cut', 'sliced': 'Cut', 'slash': 'Cut', 'slashed': 'Cut',
    'carve': 'Cut', 'carved': 'Cut', 'cleave': 'Cut', 'cleaved': 'Cut',
    'hew': 'Cut', 'hewed': 'Cut',
    'smash': 'Smash', 'smashed': 'Smash', 'crush': 'Smash', 'crushed': 'Smash',
    'slam': 'Smash', 'slammed': 'Smash', 'bash': 'Smash', 'bashed': 'Smash',
    'pummel': 'Smash', 'pummeled': 'Smash',
    'stab': 'Stab', 'stabbed': 'Stab', 'pierce': 'Stab', 'pierced': 'Stab',
    'skewer': 'Stab', 'skewered': 'Stab', 'impale': 'Stab', 'impaled': 'Stab',
    'hit': 'Smash', 'strike': 'Smash', 'struck': 'Smash',
    'blast': 'Smash', 'blasted': 'Smash',
    'zap': 'Smash', 'zapped': 'Smash',
    'overwhelm': 'Smash', 'overwhelmed': 'Smash',
    'engulf': 'Smash', 'engulfed': 'Smash',
    'scorch': 'Smash', 'scorched': 'Smash',
    'shock': 'Smash', 'shocked': 'Smash',
    'smote': 'Smash', 'smite': 'Smash',
    'drown': 'Smash', 'drowned': 'Smash',
    'stagger': 'Smash', 'staggered': 'Smash',
}

HOLE_RE = re.compile(r'make a hole|daylight through', re.I)
NEARLY_CUT_RE = re.compile(r'nearly cut.*in half', re.I)

CATEGORY_COLORS = {
    'Shock': '#87ceeb', 'Fire': '#ff6347', 'Cold': '#add8e6',
    'Acid': '#7fff00', 'Death': '#9370db',
    'Cut': '#ffa500', 'Smash': '#cd853f', 'Stab': '#daa520',
}

# For incoming damage — extract the monster verb
INCOMING_VERB_RE = re.compile(
    r'(?:hits|damages|slashes|stabs|bites|claws|burns|zaps|smashes|crushes|'
    r'strikes|blasts|freezes|shocks|drowns|staggers|cuts|pierces|impales)\s+you',
    re.IGNORECASE
)

INCOMING_VERB_MAP = {
    'hits': 'Smash', 'damages': 'Smash', 'strikes': 'Smash',
    'slashes': 'Cut', 'cuts': 'Cut',
    'stabs': 'Stab', 'pierces': 'Stab', 'impales': 'Stab',
    'bites': 'Stab', 'claws': 'Cut',
    'burns': 'Fire', 'blasts': 'Smash',
    'zaps': 'Shock', 'shocks': 'Shock',
    'smashes': 'Smash', 'crushes': 'Smash',
    'freezes': 'Cold', 'drowns': 'Smash', 'staggers': 'Smash',
}


def categorize_message(message):
    """Detect damage type from outgoing message text."""
    if not message:
        return 'Unknown'
    for pattern, dtype in ELEMENT_PATTERNS:
        if pattern.search(message):
            return dtype
    if HOLE_RE.search(message):
        return 'Stab'
    if NEARLY_CUT_RE.search(message):
        return 'Cut'
    m = VERB_RE.match(message)
    if m:
        return PHYSICAL_TYPES.get(m.group(1).lower(), 'Unknown')
    return 'Unknown'


def categorize_incoming(message):
    """Detect damage type from incoming message text."""
    if not message:
        return 'Unknown'
    # Check elemental keywords first
    for pattern, dtype in ELEMENT_PATTERNS:
        if pattern.search(message):
            return dtype
    # Fall back to verb
    m = INCOMING_VERB_RE.search(message)
    if m:
        verb = m.group(0).split()[0].lower()
        return INCOMING_VERB_MAP.get(verb, 'Unknown')
    return 'Unknown'


# ============================================================
# Session data
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
    categories: dict = field(default_factory=lambda: defaultdict(lambda: {
        'damage': 0, 'count': 0, 'max': 0, 'min': 999999
    }))

    @property
    def elapsed_ms(self):
        if not self.start_ms:
            return 0
        end = self.end_ms if self.end_ms else int(time.time() * 1000)
        return end - self.start_ms

    @property
    def elapsed_s(self):
        return self.elapsed_ms / 1000.0

    @property
    def dps(self):
        e = self.elapsed_s
        return self.total_damage / e if e > 0 else 0.0

    @property
    def avg_hit(self):
        return self.total_damage / self.hit_count if self.hit_count > 0 else 0.0

    def add_hit(self, timestamp_ms, damage, category='Unknown'):
        self.hits.append((timestamp_ms, damage, category))
        self.total_damage += damage
        self.hit_count += 1
        self.max_hit = max(self.max_hit, damage)
        self.min_hit = min(self.min_hit, damage)
        if not self.start_ms:
            self.start_ms = timestamp_ms
        cat = self.categories[category]
        cat['damage'] += damage
        cat['count'] += 1
        cat['max'] = max(cat['max'], damage)
        cat['min'] = min(cat['min'], damage)

    def finalize(self, end_ms=None):
        self.end_ms = end_ms if end_ms else (self.hits[-1][0] if self.hits else 0)
        self.active = False


# ============================================================
# GUI
# ============================================================

class DPSTrackerGUI:
    def __init__(self):
        self.out_session = None  # outgoing damage session
        self.in_session = None   # incoming damage session
        self.sessions = []       # completed sessions (both types)
        self.agent_attached = False
        self.last_out_time_ms = 0
        self.last_in_time_ms = 0
        self._seen_events = set()
        self._shutdown = False
        self._v2_agent = False

        self._build_ui()
        self._start_log_reader()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Wyvern DPS Tracker")
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#0d1117')
        self.root.geometry('440x700')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        mono = tkfont.Font(family='Consolas', size=11)
        big_mono = tkfont.Font(family='Consolas', size=28, weight='bold')
        label_font = tkfont.Font(family='Consolas', size=10)
        small_font = tkfont.Font(family='Consolas', size=9)

        # -- Status --
        self.status_label = tk.Label(
            self.root, text="Connecting to game...", fg='#f0883e',
            bg='#0d1117', font=small_font, anchor='w')
        self.status_label.pack(fill=tk.X, padx=12, pady=(8, 0))

        # -- Outgoing DPS --
        out_header = tk.Frame(self.root, bg='#0d1117')
        out_header.pack(fill=tk.X, padx=12, pady=(6, 0))
        tk.Label(out_header, text="OUTGOING", fg='#3fb950', bg='#0d1117',
                 font=label_font, anchor='w').pack(side=tk.LEFT)
        self.out_dps_label = tk.Label(out_header, text="— DPS", fg='#484f58',
                                       bg='#0d1117', font=big_mono, anchor='e')
        self.out_dps_label.pack(side=tk.RIGHT)

        # Outgoing stats
        out_stats = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        out_stats.pack(fill=tk.X, padx=12, pady=2)
        self.out_stats = {}
        for key in ["Damage", "Hits", "Avg", "Max"]:
            row = tk.Frame(out_stats, bg='#161b22')
            row.pack(fill=tk.X, padx=8, pady=0)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=small_font, width=8, anchor='w').pack(side=tk.LEFT)
            val = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22',
                           font=small_font, anchor='e')
            val.pack(side=tk.RIGHT)
            self.out_stats[key] = val

        # Outgoing breakdown
        self.out_breakdown = tk.Text(
            self.root, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
            height=5, bd=1, relief='groove', state=tk.DISABLED, wrap=tk.NONE,
            highlightthickness=0)
        self.out_breakdown.pack(fill=tk.X, padx=12, pady=2)
        for cat, color in CATEGORY_COLORS.items():
            self.out_breakdown.tag_configure(cat, foreground=color)
        self.out_breakdown.tag_configure('Unknown', foreground='#8b949e')
        self.out_breakdown.tag_configure('header', foreground='#7d8590')

        # -- Incoming DPS --
        in_header = tk.Frame(self.root, bg='#0d1117')
        in_header.pack(fill=tk.X, padx=12, pady=(8, 0))
        tk.Label(in_header, text="INCOMING", fg='#f85149', bg='#0d1117',
                 font=label_font, anchor='w').pack(side=tk.LEFT)
        self.in_dps_label = tk.Label(in_header, text="— DPS", fg='#484f58',
                                      bg='#0d1117', font=big_mono, anchor='e')
        self.in_dps_label.pack(side=tk.RIGHT)

        # Incoming stats
        in_stats = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        in_stats.pack(fill=tk.X, padx=12, pady=2)
        self.in_stats = {}
        for key in ["Damage", "Hits", "Avg", "Max"]:
            row = tk.Frame(in_stats, bg='#161b22')
            row.pack(fill=tk.X, padx=8, pady=0)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=small_font, width=8, anchor='w').pack(side=tk.LEFT)
            val = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22',
                           font=small_font, anchor='e')
            val.pack(side=tk.RIGHT)
            self.in_stats[key] = val

        # Incoming breakdown
        self.in_breakdown = tk.Text(
            self.root, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
            height=5, bd=1, relief='groove', state=tk.DISABLED, wrap=tk.NONE,
            highlightthickness=0)
        self.in_breakdown.pack(fill=tk.X, padx=12, pady=2)
        for cat, color in CATEGORY_COLORS.items():
            self.in_breakdown.tag_configure(cat, foreground=color)
        self.in_breakdown.tag_configure('Unknown', foreground='#8b949e')
        self.in_breakdown.tag_configure('header', foreground='#7d8590')

        # -- Timing --
        time_frame = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        time_frame.pack(fill=tk.X, padx=12, pady=4)
        self.time_labels = {}
        for key in ["Session Start", "Duration"]:
            row = tk.Frame(time_frame, bg='#161b22')
            row.pack(fill=tk.X, padx=8, pady=0)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=small_font, width=14, anchor='w').pack(side=tk.LEFT)
            val = tk.Label(row, text="—", fg='#c9d1d9', bg='#161b22',
                           font=small_font, anchor='e')
            val.pack(side=tk.RIGHT)
            self.time_labels[key] = val

        # -- Hit Log --
        log_frame = tk.Frame(self.root, bg='#0d1117')
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 4))
        tk.Label(log_frame, text="Combat Log", fg='#7d8590', bg='#0d1117',
                 font=label_font, anchor='w').pack(anchor='w')
        self.log_text = tk.Text(
            log_frame, bg='#161b22', fg='#8b949e', font=('Consolas', 9),
            height=6, bd=0, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_configure('out', foreground='#58a6ff')
        self.log_text.tag_configure('in', foreground='#f85149')
        self.log_text.tag_configure('kill', foreground='#3fb950')
        self.log_text.tag_configure('session', foreground='#7d8590')

        self._schedule_display_update()

    def _on_close(self):
        self._shutdown = True
        self.root.destroy()

    def _schedule_display_update(self):
        if self._shutdown:
            return
        self._update_display()
        self.root.after(100, self._schedule_display_update)

    def _update_display(self):
        now_ms = int(time.time() * 1000)

        # Auto-end sessions on gap
        if self.out_session and self.out_session.active and self.last_out_time_ms:
            if now_ms - self.last_out_time_ms > SESSION_GAP_SECONDS * 1000:
                self._end_session('out')
        if self.in_session and self.in_session.active and self.last_in_time_ms:
            if now_ms - self.last_in_time_ms > SESSION_GAP_SECONDS * 1000:
                self._end_session('in')

        # Update outgoing display
        s = self.out_session
        if s and s.hit_count > 0:
            dps = s.dps
            self.out_dps_label.config(text=f"{dps:.1f} DPS")
            if dps >= 100:
                self.out_dps_label.config(fg='#3fb950')
            elif dps > 0:
                self.out_dps_label.config(fg='#58a6ff')
            self.out_stats["Damage"].config(text=f"{s.total_damage:,}")
            self.out_stats["Hits"].config(text=f"{s.hit_count}")
            self.out_stats["Avg"].config(text=f"{s.avg_hit:.0f}")
            self.out_stats["Max"].config(text=f"{s.max_hit}")
            self._update_breakdown(self.out_breakdown, s)
        else:
            self.out_dps_label.config(text="— DPS", fg='#484f58')

        # Update incoming display
        s = self.in_session
        if s and s.hit_count > 0:
            dps = s.dps
            self.in_dps_label.config(text=f"{dps:.1f} DPS")
            if dps >= 100:
                self.in_dps_label.config(fg='#f85149')
            elif dps > 0:
                self.in_dps_label.config(fg='#d29922')
            self.in_stats["Damage"].config(text=f"{s.total_damage:,}")
            self.in_stats["Hits"].config(text=f"{s.hit_count}")
            self.in_stats["Avg"].config(text=f"{s.avg_hit:.0f}")
            self.in_stats["Max"].config(text=f"{s.max_hit}")
            self._update_breakdown(self.in_breakdown, s)
        else:
            self.in_dps_label.config(text="— DPS", fg='#484f58')

        # Timing — use earliest start from either session
        starts = []
        if self.out_session and self.out_session.start_ms:
            starts.append(self.out_session.start_ms)
        if self.in_session and self.in_session.start_ms:
            starts.append(self.in_session.start_ms)
        if starts:
            earliest = min(starts)
            start_dt = datetime.fromtimestamp(earliest / 1000.0)
            self.time_labels["Session Start"].config(
                text=start_dt.strftime("%H:%M:%S.") + f"{start_dt.microsecond // 1000:03d}")
            elapsed = (now_ms - earliest) / 1000.0
            mins, secs = divmod(elapsed, 60)
            self.time_labels["Duration"].config(text=f"{int(mins)}:{secs:05.2f}")

    def _update_breakdown(self, widget, session):
        widget.config(state=tk.NORMAL)
        widget.delete('1.0', tk.END)

        header = f"{'Type':<10} {'Hits':>5} {'Dmg':>7} {'Avg':>6} {'DPS':>7} {'%':>5}\n"
        widget.insert(tk.END, header, 'header')

        elapsed = session.elapsed_s
        cats = sorted(session.categories.items(),
                      key=lambda x: x[1]['damage'], reverse=True)
        for cat_name, stats in cats:
            count = stats['count']
            dmg = stats['damage']
            avg = dmg / count if count else 0
            cat_dps = dmg / elapsed if elapsed > 0 else 0
            pct = (dmg / session.total_damage * 100) if session.total_damage else 0
            line = f"{cat_name:<10} {count:>5} {dmg:>7,} {avg:>6.0f} {cat_dps:>7.1f} {pct:>4.0f}%\n"
            tag = cat_name if cat_name in CATEGORY_COLORS else 'Unknown'
            widget.insert(tk.END, line, tag)

        widget.config(state=tk.DISABLED)

    def _log_message(self, text, tag='out'):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _start_session(self, direction):
        if direction == 'out':
            if self.out_session and self.out_session.active:
                self._end_session('out')
            self.out_session = Session()
        else:
            if self.in_session and self.in_session.active:
                self._end_session('in')
            self.in_session = Session()

    def _end_session(self, direction):
        if direction == 'out':
            s = self.out_session
            label = "OUT"
        else:
            s = self.in_session
            label = "IN"

        if s and s.hit_count > 0:
            s.finalize()
            self.sessions.append((direction, s))
            self._log_message(
                f"--- {label} End: {s.dps:.1f} DPS, {s.total_damage:,} dmg, {s.elapsed_s:.1f}s ---",
                'session')

        if direction == 'out':
            self.out_session = None
        else:
            self.in_session = None

    def _handle_event(self, event_type, timestamp_ms, data):
        if self._shutdown:
            return

        if event_type == "AGENT_READY":
            if "v4" in data or "v2" in data:
                self._v2_agent = True
            self.status_label.config(text="Agent loaded...", fg='#f0883e')
        elif event_type == "ATTACHED":
            self.agent_attached = True
            if "v4" in data or "v2" in data:
                self._v2_agent = True
            self.status_label.config(text="Attached! Start fighting.", fg='#3fb950')
        elif event_type == "ERROR":
            self.status_label.config(text=f"Error: {data}", fg='#f85149')

        elif event_type in ("OUT", "HIT"):
            # OUT = outgoing damage, HIT = legacy format (also outgoing)
            parts = data.split('|', 1)
            try:
                damage = int(parts[0])
            except ValueError:
                return
            message = parts[1] if len(parts) > 1 else ""

            if self._v2_agent and not message:
                return

            event_key = ('out', timestamp_ms, damage)
            if event_key in self._seen_events:
                return
            self._seen_events.add(event_key)
            if len(self._seen_events) > 10000:
                self._seen_events.clear()

            category = categorize_message(message)

            if not self.out_session or not self.out_session.active:
                self._start_session('out')

            self.out_session.add_hit(timestamp_ms, damage, category)
            self.last_out_time_ms = timestamp_ms

            elapsed = (timestamp_ms - self.out_session.start_ms) / 1000.0
            self._log_message(
                f"  OUT {elapsed:6.2f}s  {damage:>5d} [{category}]", 'out')
            self.status_label.config(text="Tracking...", fg='#3fb950')

        elif event_type == "IN":
            parts = data.split('|', 1)
            try:
                damage = int(parts[0])
            except ValueError:
                return
            message = parts[1] if len(parts) > 1 else ""

            event_key = ('in', timestamp_ms, damage)
            if event_key in self._seen_events:
                return
            self._seen_events.add(event_key)
            if len(self._seen_events) > 10000:
                self._seen_events.clear()

            category = categorize_incoming(message)

            if not self.in_session or not self.in_session.active:
                self._start_session('in')

            self.in_session.add_hit(timestamp_ms, damage, category)
            self.last_in_time_ms = timestamp_ms

            elapsed = (timestamp_ms - self.in_session.start_ms) / 1000.0
            self._log_message(
                f"   IN {elapsed:6.2f}s  {damage:>5d} [{category}]", 'in')

        elif event_type == "KILL":
            self._log_message(f"  KILL  {data}", 'kill')

    def _start_log_reader(self):
        thread = threading.Thread(target=self._read_log_loop, daemon=True)
        thread.start()

    def _read_log_loop(self):
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
                event_type = parts[0]
                try:
                    timestamp_ms = int(parts[1])
                except ValueError:
                    continue
                data = parts[2] if len(parts) > 2 else ""

                if not self._shutdown:
                    self.root.after(0, self._handle_event, event_type, timestamp_ms, data)

    def run(self):
        self.root.mainloop()


# ============================================================
# Resource helpers
# ============================================================

def _resource_dir():
    if getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS)
    return SCRIPT_DIR

def _runtime_dir():
    if getattr(sys, '_MEIPASS', None):
        return Path(sys.executable).parent
    return SCRIPT_DIR


# ============================================================
# Find Java
# ============================================================

def find_java():
    candidates = []
    jh = os.environ.get('JAVA_HOME')
    if jh:
        candidates.append(Path(jh) / "bin" / "java.exe")

    for base in [Path("C:/Program Files/Java"), Path("C:/Program Files/Eclipse Adoptium"),
                 Path("C:/Program Files/Android/Android Studio1/jbr"),
                 Path("C:/Program Files/Microsoft"), Path("C:/Program Files/Zulu")]:
        if base.is_dir():
            if (base / "bin" / "java.exe").exists():
                candidates.append(base / "bin" / "java.exe")
            else:
                for child in base.iterdir():
                    j = child / "bin" / "java.exe"
                    if j.exists():
                        candidates.append(j)

    import shutil
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))

    for java in candidates:
        if not java.exists():
            continue
        try:
            r = subprocess.run(
                [str(java), "--list-modules"],
                capture_output=True, text=True, timeout=10)
            if "jdk.attach" in r.stdout:
                return str(java)
        except Exception:
            continue
    return None


# ============================================================
# Attach
# ============================================================

def attach_agent():
    java = find_java()
    if not java:
        print("ERROR: No JDK with jdk.attach found.")
        print("Install a JDK (Java 11+) and ensure it's on PATH or JAVA_HOME.")
        return False

    res = _resource_dir()
    run = _runtime_dir()

    attacher_classes = str(res / "attacher")
    agent_jar = str(res / "agent.jar")
    log_file = str(run / "dps_events_v2.log")

    global LOG_FILE
    LOG_FILE = Path(log_file)

    if not Path(agent_jar).exists():
        print(f"ERROR: agent.jar not found at {agent_jar}")
        return False

    try:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
    except PermissionError:
        pass

    print(f"Using Java: {java}")
    print("Attaching to Wyvern JVM...")
    try:
        result = subprocess.run(
            [java, "-cp", attacher_classes, "dps.DPSAttacher", agent_jar, log_file],
            capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("Attach timed out.")
        return False

    print(result.stdout)
    if result.returncode != 0:
        print(f"Attach failed:\n{result.stderr}")
        time.sleep(1)
        if LOG_FILE.exists():
            print("Agent may already be loaded. Continuing.")
            return True
        return False
    return True


def main():
    print("=== Wyvern DPS Tracker ===\n")

    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "WyvernDPSTracker_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("Another DPS Tracker is already running!")
        input("Press Enter to close...")
        sys.exit(1)

    if not attach_agent():
        print("\nCould not attach to game.")
        print("Make sure:")
        print("  1. Wyvern is running")
        print("  2. A JDK (Java 11+) is installed (not just JRE)")
        print("     Download from: https://adoptium.net/")
        java = find_java()
        if not java:
            print("\n  >> No JDK found! Install one and try again.")
        else:
            print(f"\n  >> Found Java: {java}")
            print("  >> Game might not be running or JVM not visible.")
        input("\nPress Enter to close...")
        sys.exit(1)

    lock_file = Path(str(LOG_FILE) + ".lock")
    lock_handle = open(lock_file, 'w')
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()

    def cleanup():
        try:
            lock_handle.close()
            lock_file.unlink(missing_ok=True)
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
