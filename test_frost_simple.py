"""
Smoke-test the simplified single-global-state Frostbite tracker.

Replays a fabricated mini-stream that mimics the screenshot the user sent:
three distinct Frostbite applications on a fire imp, ending in death. We
verify the new logic produces sensible durations.
"""
import re
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import dps_tracker as t


def replay(events):
    """events: list of (ts, etype, dmg, msg)"""
    start_ms = 0
    last_ms  = 0
    last_out_ms = 0
    last_in_ms  = 0
    samples = deque(maxlen=200)

    def maybe_close_on_tick(now):
        nonlocal start_ms, last_ms
        latest = max(last_out_ms, last_in_ms)
        if start_ms and latest and latest - last_ms > t.FROSTBITE_TIMEOUT_MS:
            dur = last_ms - start_ms
            if dur > 0:
                samples.append(dur)
            start_ms = 0
            last_ms  = 0

    for ts, etype, dmg, msg in events:
        if etype == 'OUT':
            last_out_ms = ts
            if t.FROSTBITTEN_RE.search(msg):
                if start_ms == 0:
                    start_ms = ts
                last_ms = ts
            elif start_ms:
                dur = last_ms - start_ms
                if dur > 0:
                    samples.append(dur)
                start_ms = 0
                last_ms  = 0
        elif etype == 'IN':
            last_in_ms = ts
        elif etype == 'KILL':
            if start_ms:
                dur = last_ms - start_ms
                if dur > 0:
                    samples.append(dur)
                start_ms = 0
                last_ms  = 0
        maybe_close_on_tick(ts)
    return list(samples), start_ms, last_ms


# Application 1: 0s start, 4 tagged hits over 3.2s, 5th hit untagged -> close at 3.2s.
# Application 2: 5s start, 3 tagged hits over 1.4s, then KILL -> close at 1.4s.
# Application 3: 8s start, 2 tagged hits over 0.7s, then 6s gap, timeout closes at 0.7s.
events = [
    (1000, 'OUT', 12, 'You hit fire imp (Frostbitten) for 12.'),
    (1800, 'OUT', 11, 'You hit fire imp (Frostbitten) for 11.'),
    (2900, 'OUT', 13, 'You hit fire imp (Frostbitten) for 13.'),
    (4200, 'OUT', 10, 'You hit fire imp (Frostbitten) for 10.'),
    (4500, 'OUT',  9, 'You hit fire imp for 9.'),  # closes app 1 at 3200ms
    (6000, 'OUT', 12, 'You hit fire imp (Frostbitten) for 12.'),  # app 2 start
    (6700, 'OUT', 11, 'You hit fire imp (Frostbitten) for 11.'),
    (7400, 'OUT', 13, 'You hit fire imp (Frostbitten) for 13.'),
    (7900, 'KILL', 0, 'You killed fire imp.'),  # closes app 2 at 1400ms
    (9000, 'OUT', 14, 'You hit goblin (Frostbitten) for 14.'),    # app 3 start
    (9700, 'OUT', 15, 'You hit goblin (Frostbitten) for 15.'),
    (16000, 'IN', 4, 'goblin bites you for 4.'),  # 6.3s gap from last_ms=9700 > 5s
]

samples, start, last = replay(events)
print(f"Samples (ms): {samples}")
print(f"In seconds:   {[round(s/1000.0, 2) for s in samples]}")
print(f"Trailing state: start={start} last={last}")
print()

expected = [3200, 1400, 700]
ok = samples == expected and start == 0 and last == 0
print("PASS" if ok else f"FAIL - expected {expected}, trailing 0/0")
