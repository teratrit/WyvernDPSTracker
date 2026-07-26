"""
End-to-end smoke test: instantiate just enough of DPSTrackerGUI to fire
_close_frostbite_sample, confirm both the deque and the log file get the
expected rows. Then re-confirm the three close paths still produce
3.2s / 1.4s / 0.7s on the fabricated stream.
"""
import os
import sys
import tempfile
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import dps_tracker as t


class FakeGUI:
    """Mimics enough of DPSTrackerGUI for _close_frostbite_sample."""
    def __init__(self, log_path):
        self.frostbite_start_ms = 0
        self.frostbite_last_ms  = 0
        self.frostbite_samples  = deque(maxlen=200)
        self.frostbite_log_path = Path(log_path)

    # Bind the real method onto our fake
    _close_frostbite_sample = t.DPSTrackerGUI._close_frostbite_sample


# Use a temp file so we don't clobber the real frostbite_durations.log
with tempfile.NamedTemporaryFile(delete=False, suffix='.log') as tf:
    log_path = tf.name

try:
    gui = FakeGUI(log_path)

    # App 1: 1000..4200, then untagged at 4500 -> 3200ms
    gui.frostbite_start_ms = 1000
    gui.frostbite_last_ms  = 4200
    gui._close_frostbite_sample(4500, 'untagged')

    # App 2: 6000..7400, KILL at 7900 -> 1400ms
    gui.frostbite_start_ms = 6000
    gui.frostbite_last_ms  = 7400
    gui._close_frostbite_sample(7900, 'kill')

    # App 3: 9000..9700, timeout swept at 16000 -> 700ms
    gui.frostbite_start_ms = 9000
    gui.frostbite_last_ms  = 9700
    gui._close_frostbite_sample(9700, 'timeout')  # close_ts = last_ms for timeout

    # Zero-duration sample (single-hit application): should NOT log
    gui.frostbite_start_ms = 20000
    gui.frostbite_last_ms  = 20000
    gui._close_frostbite_sample(20001, 'untagged')

    print(f"In-memory samples: {list(gui.frostbite_samples)}")
    print(f"Trailing state:    start={gui.frostbite_start_ms} last={gui.frostbite_last_ms}")

    with open(log_path, encoding='utf-8') as fh:
        rows = [line.strip() for line in fh if line.strip()]
    print(f"Log file rows:     {rows}")

    expected_samples = [3200, 1400, 700]
    expected_rows = ['4500|3200|untagged', '7900|1400|kill', '9700|700|timeout']
    ok = (list(gui.frostbite_samples) == expected_samples
          and rows == expected_rows
          and gui.frostbite_start_ms == 0
          and gui.frostbite_last_ms  == 0)
    print()
    print("PASS" if ok else "FAIL")
finally:
    os.unlink(log_path)
