"""Live diagnostic for web_capture: runs the real capture pipeline against the
client, printing events, per-opcode counts, and saving the first raw payload of
each opcode to probe_samples/ for offline verification."""

import sys
import time
from collections import Counter
from pathlib import Path

import web_capture
from web_capture import WebCapture

SAMPLES = Path(__file__).parent / 'probe_samples'
SAMPLES.mkdir(exist_ok=True)

counts = Counter()
seen = set()


class ProbeCapture(WebCapture):
    def _handle_frame(self, opcode, payload):
        counts[opcode] += 1
        if opcode not in seen:
            seen.add(opcode)
            (SAMPLES / f'op{opcode}.bin').write_bytes(payload)
            print(f"[probe] first frame op={opcode} len={len(payload)}")
        super()._handle_frame(opcode, payload)


def write_event(etype, data):
    line = f"{etype}|{int(time.time() * 1000)}|{data}"
    print(f"[event] {line}")
    with open(SAMPLES / 'probe_events.log', 'a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def main():
    port = 9222
    if not web_capture.cdp_alive(port):
        print("CDP not reachable on 9222")
        sys.exit(1)
    cap = ProbeCapture(port, write_event)
    cap.start()
    print("Probe attached. Waiting for game traffic... Ctrl+C to stop.")
    try:
        deadline = time.time() + 600
        while time.time() < deadline and not cap.disconnected:
            time.sleep(10)
            if counts:
                top = ', '.join(f'{op}:{n}' for op, n
                                in counts.most_common(8))
                print(f"[stats] frames by opcode: {top}")
    except KeyboardInterrupt:
        pass
    print(f"\nFinal opcode counts: {dict(counts.most_common())}")


if __name__ == '__main__':
    main()
