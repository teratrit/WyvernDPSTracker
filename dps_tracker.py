"""
Wyvern DPS Tracker

Hooks into the running Wyvern game client via Java Attach API, captures
Training Dummy damage events with millisecond timestamps, and displays
real-time DPS with per-attack-type breakdown.

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
from tkinter import font as tkfont
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_FILE = SCRIPT_DIR / "dps_events_v2.log"
SESSION_GAP_SECONDS = 5

# The 5 elemental damage types — detected from flavor text keywords
ELEMENT_PATTERNS = [
    (re.compile(r'lightning|energy surge|shocked|electr', re.I), 'Shock'),
    (re.compile(r'flame|burn|fire|inferno|incinerat|scorch|sear|magma|lava', re.I), 'Fire'),
    (re.compile(r'arctic|glacial|frost|freeze|chill|frozen|cold|\bice\b|blizzard', re.I), 'Cold'),
    (re.compile(r'acid|corrosi|dissolv|caustic', re.I), 'Acid'),
    (re.compile(r'death|necrotic|drain|dark energy|shadow|unholy|wither', re.I), 'Death'),
]

# Physical attack type — verb-based
VERB_RE = re.compile(r'^You\s+(\w+)', re.IGNORECASE)

PHYSICAL_TYPES = {
    # Cut
    'cut': 'Cut', 'slice': 'Cut', 'sliced': 'Cut', 'slash': 'Cut', 'slashed': 'Cut',
    'carve': 'Cut', 'carved': 'Cut', 'cleave': 'Cut', 'cleaved': 'Cut',
    'hew': 'Cut', 'hewed': 'Cut',
    # Smash
    'smash': 'Smash', 'smashed': 'Smash', 'crush': 'Smash', 'crushed': 'Smash',
    'slam': 'Smash', 'slammed': 'Smash', 'bash': 'Smash', 'bashed': 'Smash',
    'pummel': 'Smash', 'pummeled': 'Smash',
    # Stab
    'stab': 'Stab', 'stabbed': 'Stab', 'pierce': 'Stab', 'pierced': 'Stab',
    'skewer': 'Stab', 'skewered': 'Stab', 'impale': 'Stab', 'impaled': 'Stab',
    # Generic verbs — when no element matched, map to best physical type
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

# Special case patterns
HOLE_RE = re.compile(r'make a hole|daylight through', re.I)
NEARLY_CUT_RE = re.compile(r'nearly cut.*in half', re.I)

CATEGORY_COLORS = {
    # Elements
    'Shock': '#87ceeb', 'Fire': '#ff6347', 'Cold': '#add8e6',
    'Acid': '#7fff00', 'Death': '#9370db',
    # Physical
    'Cut': '#ffa500', 'Smash': '#cd853f', 'Stab': '#daa520',
}


def categorize_message(message):
    """Detect damage type from the full message text.
    Elemental keywords (Fire/Cold/Shock/Acid/Death) take priority.
    Falls back to physical attack type (Cut/Smash/Stab)."""
    if not message:
        return 'Unknown'

    # 1. Check for elemental damage (flavor text keywords)
    for pattern, dtype in ELEMENT_PATTERNS:
        if pattern.search(message):
            return dtype

    # 2. Special cases
    if HOLE_RE.search(message):
        return 'Stab'
    if NEARLY_CUT_RE.search(message):
        return 'Cut'

    # 3. Fall back to physical type from verb
    m = VERB_RE.match(message)
    if m:
        verb = m.group(1).lower()
        return PHYSICAL_TYPES.get(verb, 'Other')

    return 'Unknown'


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
    # Per-category tracking: category -> {damage, count, max, min}
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

    def add_hit(self, timestamp_ms, damage, category='Other'):
        self.hits.append((timestamp_ms, damage, category))
        self.total_damage += damage
        self.hit_count += 1
        self.max_hit = max(self.max_hit, damage)
        self.min_hit = min(self.min_hit, damage)
        if not self.start_ms:
            self.start_ms = timestamp_ms
        # Category stats
        cat = self.categories[category]
        cat['damage'] += damage
        cat['count'] += 1
        cat['max'] = max(cat['max'], damage)
        cat['min'] = min(cat['min'], damage)

    def finalize(self, end_ms=None):
        self.end_ms = end_ms if end_ms else (self.hits[-1][0] if self.hits else 0)
        self.active = False


class DPSTrackerGUI:
    def __init__(self):
        self.current_session = None
        self.sessions = []
        self.agent_attached = False
        self.last_hit_time_ms = 0
        self._seen_events = set()  # for dedup
        self._shutdown = False
        self._v2_agent = False  # True once v2 agent confirms attachment

        self._build_ui()
        self._start_log_reader()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Wyvern DPS Tracker")
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#0d1117')
        self.root.geometry('400x620')
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        mono = tkfont.Font(family='Consolas', size=11)
        big_mono = tkfont.Font(family='Consolas', size=36, weight='bold')
        label_font = tkfont.Font(family='Consolas', size=10)
        small_font = tkfont.Font(family='Consolas', size=9)

        # -- Status --
        self.status_label = tk.Label(
            self.root, text="Connecting to game...", fg='#f0883e',
            bg='#0d1117', font=small_font, anchor='w')
        self.status_label.pack(fill=tk.X, padx=12, pady=(8, 0))

        # -- DPS --
        dps_frame = tk.Frame(self.root, bg='#0d1117')
        dps_frame.pack(fill=tk.X, padx=12, pady=(4, 0))
        tk.Label(dps_frame, text="DPS", fg='#7d8590', bg='#0d1117',
                 font=label_font, anchor='w').pack(anchor='w')
        self.dps_label = tk.Label(dps_frame, text="—", fg='#484f58',
                                   bg='#0d1117', font=big_mono, anchor='w')
        self.dps_label.pack(anchor='w')

        # -- Timing --
        time_frame = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        time_frame.pack(fill=tk.X, padx=12, pady=4)
        self.time_labels = {}
        for key in ["Session Start", "Session End", "Duration"]:
            row = tk.Frame(time_frame, bg='#161b22')
            row.pack(fill=tk.X, padx=8, pady=1)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=small_font, width=14, anchor='w').pack(side=tk.LEFT)
            val = tk.Label(row, text="—", fg='#c9d1d9', bg='#161b22',
                           font=small_font, anchor='e')
            val.pack(side=tk.RIGHT)
            self.time_labels[key] = val

        # -- Stats --
        stats_frame = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        stats_frame.pack(fill=tk.X, padx=12, pady=4)
        self.stats_labels = {}
        for key in ["Total Damage", "Hits", "Avg Hit", "Max Hit", "Min Hit"]:
            row = tk.Frame(stats_frame, bg='#161b22')
            row.pack(fill=tk.X, padx=8, pady=1)
            tk.Label(row, text=key, fg='#7d8590', bg='#161b22',
                     font=small_font, width=14, anchor='w').pack(side=tk.LEFT)
            val = tk.Label(row, text="0", fg='#c9d1d9', bg='#161b22',
                           font=mono, anchor='e')
            val.pack(side=tk.RIGHT)
            self.stats_labels[key] = val

        # -- Attack Breakdown --
        breakdown_frame = tk.Frame(self.root, bg='#161b22', bd=1, relief='groove')
        breakdown_frame.pack(fill=tk.X, padx=12, pady=4)
        tk.Label(breakdown_frame, text="Attack Breakdown", fg='#7d8590',
                 bg='#161b22', font=label_font, anchor='w').pack(
                     fill=tk.X, padx=8, pady=(4, 2))
        self.breakdown_text = tk.Text(
            breakdown_frame, bg='#161b22', fg='#c9d1d9', font=('Consolas', 9),
            height=6, bd=0, state=tk.DISABLED, wrap=tk.NONE,
            highlightthickness=0)
        self.breakdown_text.pack(fill=tk.X, padx=8, pady=(0, 4))
        # Configure color tags
        for cat, color in CATEGORY_COLORS.items():
            self.breakdown_text.tag_configure(cat, foreground=color)
        self.breakdown_text.tag_configure('Other', foreground='#8b949e')
        self.breakdown_text.tag_configure('header', foreground='#7d8590')

        # -- Hit Log --
        log_frame = tk.Frame(self.root, bg='#0d1117')
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 4))
        tk.Label(log_frame, text="Hit Log", fg='#7d8590', bg='#0d1117',
                 font=label_font, anchor='w').pack(anchor='w')
        self.log_text = tk.Text(
            log_frame, bg='#161b22', fg='#8b949e', font=('Consolas', 9),
            height=6, bd=0, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_configure('hit', foreground='#58a6ff')
        self.log_text.tag_configure('kill', foreground='#f85149')
        self.log_text.tag_configure('session', foreground='#3fb950')

        # -- Session History --
        hist_frame = tk.Frame(self.root, bg='#0d1117')
        hist_frame.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(hist_frame, text="Past Sessions", fg='#7d8590', bg='#0d1117',
                 font=small_font, anchor='w').pack(anchor='w')
        self.history_label = tk.Label(
            hist_frame, text="None yet", fg='#484f58', bg='#0d1117',
            font=small_font, anchor='w', justify=tk.LEFT)
        self.history_label.pack(anchor='w')

        self._schedule_display_update()

    def _on_close(self):
        """Shutdown when window is closed."""
        self._shutdown = True
        self.root.destroy()

    def _schedule_display_update(self):
        if self._shutdown:
            return
        self._update_display()
        self.root.after(100, self._schedule_display_update)

    def _update_display(self):
        s = self.current_session

        # Auto-end session on gap
        if s and s.active and self.last_hit_time_ms:
            gap = time.time() * 1000 - self.last_hit_time_ms
            if gap > SESSION_GAP_SECONDS * 1000:
                self._end_session()
                s = self.current_session

        if s and s.hit_count > 0:
            dps = s.dps
            self.dps_label.config(text=f"{dps:.1f}")
            if dps >= 100:
                self.dps_label.config(fg='#f85149')
            elif dps >= 50:
                self.dps_label.config(fg='#d29922')
            elif dps > 0:
                self.dps_label.config(fg='#3fb950')

            start_dt = datetime.fromtimestamp(s.start_ms / 1000.0)
            self.time_labels["Session Start"].config(
                text=start_dt.strftime("%H:%M:%S.") + f"{start_dt.microsecond // 1000:03d}")
            if s.end_ms:
                end_dt = datetime.fromtimestamp(s.end_ms / 1000.0)
                self.time_labels["Session End"].config(
                    text=end_dt.strftime("%H:%M:%S.") + f"{end_dt.microsecond // 1000:03d}")
            else:
                self.time_labels["Session End"].config(text="(fighting)")

            elapsed = s.elapsed_s
            mins, secs = divmod(elapsed, 60)
            self.time_labels["Duration"].config(text=f"{int(mins)}:{secs:05.2f}")

            self.stats_labels["Total Damage"].config(text=f"{s.total_damage:,}")
            self.stats_labels["Hits"].config(text=f"{s.hit_count}")
            self.stats_labels["Avg Hit"].config(text=f"{s.avg_hit:.1f}")
            self.stats_labels["Max Hit"].config(text=f"{s.max_hit}")
            self.stats_labels["Min Hit"].config(
                text=f"{s.min_hit}" if s.min_hit < 999999 else "0")

            # Attack breakdown
            self._update_breakdown(s)
        else:
            self.dps_label.config(text="—", fg='#484f58')

        # History
        if self.sessions:
            lines = []
            for i, sess in enumerate(reversed(self.sessions[-5:])):
                lines.append(
                    f"#{len(self.sessions) - i}: {sess.dps:.1f} DPS | "
                    f"{sess.total_damage:,} dmg | {sess.elapsed_s:.2f}s | "
                    f"{sess.hit_count} hits")
            self.history_label.config(text="\n".join(lines))

    def _update_breakdown(self, session):
        self.breakdown_text.config(state=tk.NORMAL)
        self.breakdown_text.delete('1.0', tk.END)

        # Header
        header = f"{'Type':<10} {'Hits':>5} {'Dmg':>7} {'Avg':>6} {'DPS':>7} {'%':>5}\n"
        self.breakdown_text.insert(tk.END, header, 'header')

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
            tag = cat_name if cat_name in CATEGORY_COLORS else 'Other'
            self.breakdown_text.insert(tk.END, line, tag)

        self.breakdown_text.config(state=tk.DISABLED)

    def _log_message(self, text, tag='hit'):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _start_session(self):
        if self.current_session and self.current_session.active:
            self._end_session()
        self.current_session = Session()
        self._log_message("--- New Session ---", 'session')

    def _end_session(self):
        if self.current_session and self.current_session.hit_count > 0:
            self.current_session.finalize()
            self.sessions.append(self.current_session)
            dps = self.current_session.dps
            elapsed = self.current_session.elapsed_s
            self._log_message(
                f"--- Session End: {dps:.1f} DPS over {elapsed:.2f}s ---", 'session')
        self.current_session = None

    def _handle_event(self, event_type, timestamp_ms, data):
        if self._shutdown:
            return

        if event_type == "AGENT_READY":
            if "v2" in data:
                self._v2_agent = True
            self.status_label.config(text="Agent loaded...", fg='#f0883e')
        elif event_type == "ATTACHED":
            self.agent_attached = True
            if "v2" in data:
                self._v2_agent = True
            self.status_label.config(text="Attached! Hit a Training Dummy.", fg='#3fb950')
        elif event_type == "ERROR":
            self.status_label.config(text=f"Error: {data}", fg='#f85149')

        elif event_type == "HIT":
            # New format: "damage|full_message_line" or old format: "damage"
            parts = data.split('|', 1)
            try:
                damage = int(parts[0])
            except ValueError:
                return
            message = parts[1] if len(parts) > 1 else ""

            # If v2 agent is active, skip events without message text
            # (those come from old agent listeners still firing)
            if self._v2_agent and not message:
                return

            # Dedup on (timestamp, damage) to collapse remaining duplicates
            event_key = (timestamp_ms, damage)
            if event_key in self._seen_events:
                return
            self._seen_events.add(event_key)
            if len(self._seen_events) > 5000:
                self._seen_events.clear()

            # Parse damage type from full message
            category = categorize_message(message)

            # Auto-start session
            if not self.current_session or not self.current_session.active:
                self._start_session()

            self.current_session.add_hit(timestamp_ms, damage, category)
            self.last_hit_time_ms = timestamp_ms

            elapsed = (timestamp_ms - self.current_session.start_ms) / 1000.0
            cat_tag = category if category in CATEGORY_COLORS else 'hit'
            self._log_message(
                f"  {elapsed:7.3f}s  {damage:>5d} dmg  [{category}]", 'hit')
            self.status_label.config(text="Tracking...", fg='#3fb950')

        elif event_type == "KILL":
            if self.current_session and self.current_session.active:
                self.current_session.finalize(end_ms=timestamp_ms)
                self.sessions.append(self.current_session)
                dps = self.current_session.dps
                elapsed = self.current_session.elapsed_s
                self._log_message(
                    f"  KILLED! {dps:.1f} DPS over {elapsed:.3f}s", 'kill')
                self.current_session = None
                self.status_label.config(
                    text="Dummy killed. Hit another to start.", fg='#58a6ff')

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
# Resource helpers (works both as script and as PyInstaller exe)
# ============================================================

def _resource_dir():
    """Return the directory containing bundled resources.
    When running as a PyInstaller exe, resources are in sys._MEIPASS.
    When running as a script, they're next to the .py file."""
    if getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS)
    return SCRIPT_DIR

def _runtime_dir():
    """Writable directory for log files etc. Always next to the exe/script."""
    if getattr(sys, '_MEIPASS', None):
        return Path(sys.executable).parent
    return SCRIPT_DIR


# ============================================================
# Find Java
# ============================================================

def find_java():
    """Find a JDK java.exe that has the jdk.attach module."""
    candidates = []

    # 1. JAVA_HOME
    jh = os.environ.get('JAVA_HOME')
    if jh:
        candidates.append(Path(jh) / "bin" / "java.exe")

    # 2. Known JDK locations
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

    # 3. PATH
    import shutil
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))

    # Test each candidate for jdk.attach
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
    """Inject the DPS agent into the running Wyvern JVM."""
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

    # Update LOG_FILE global so GUI reads from the right place
    global LOG_FILE
    LOG_FILE = Path(log_file)

    if not Path(agent_jar).exists():
        print(f"ERROR: agent.jar not found at {agent_jar}")
        return False

    # Clear old log
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

    # Prevent multiple instances using a Windows named mutex
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "WyvernDPSTracker_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
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

    # Keep lock file handle open — Windows releases it when process dies
    lock_file = Path(str(LOG_FILE) + ".lock")
    lock_handle = open(lock_file, 'w')
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    # Don't close lock_handle — held open for process lifetime

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
