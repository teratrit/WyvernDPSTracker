"""Combat-event capture for the Wyvern web/Steam (Electron) client.

Replaces the old Java-agent injection (dead since the client moved to
Electron + WebSocket). Connects to the client's Chrome DevTools Protocol
endpoint (--remote-debugging-port), watches the game's WebSocket frames,
decodes the Thrift COMPACT messages we care about, and emits the same
`ETYPE|ts_ms|data` event lines the old DPSAgent wrote, so the tracker GUI
is unchanged.

Wire format (verified against the beautified web bundle):
  frame   = [opcode i32 BE][len i32 BE][payload]   -- reassembled across WS messages
  payload = deflate-compressed for opcodes in DEFLATED_OPCODES (zlib format),
            bare struct at offset 0 after inflation; NON-deflated payloads
            carry the struct at offset 4 (4 junk/length bytes first).

Messages used:
  TEXT_OUT=11        TextMessage{1:style i32, 2:text str, ...}       not deflated
  MIXED_TEXT_OUT=12  MixedText{1: list<TextMessage>, 2: pane}        deflated
  ZIP_TEXT_OUT=51    TextMessage                                     deflated
  STAT_UPDATE=120    StatUpdate{1:hp, 2:maxHp, ...}                  not deflated

Text styles: TEXT_DAMAGE=3 (incoming, was Swing "damage"),
             TEXT_HIT=6    (outgoing, was Swing "hit").
"""

import base64
import json
import re
import threading
import time
import urllib.request
import zlib

import websocket  # websocket-client

# ── Protocol constants ────────────────────────────────────────────────────────

OP_TEXT_OUT       = 11
OP_MIXED_TEXT_OUT = 12
OP_ZIP_TEXT_OUT   = 51
OP_STAT_UPDATE    = 120
OP_CLIENT_ACTION  = 124

# PERFORM_CLIENT_ACTION sub-opcodes we care about. SHOW_DAMAGE is the floating
# combat text feed: JSON {id, dmg, type, crit, heal} — exact damage with target
# entityId, damage-type string and server-authoritative crit flag.
ACTION_SHOW_DAMAGE = 16
ACTION_DEBUFFS = {3: ('petrify', True),   4: ('petrify', False),
                  5: ('poison', True),    6: ('poison', False),
                  11: ('berserk', True),  12: ('berserk', False),
                  17: ('mana_burn', True), 18: ('mana_burn', False)}

# Opcodes whose payload is deflate-compressed (from the bundle's isDeflated()).
# We only ever inflate the two we parse; the rest are listed for completeness.
DEFLATED_OPCODES = {8, 9, 12, 17, 19, 24, 35, 42, 51, 52, 60, 70, 72, 90,
                    111, 112, 113, 114, 115, 116, 117, 128, 130, 131}

# Bytes to skip before the struct in a NON-deflated payload (i32 length).
PLAIN_STRUCT_OFFSET = 4
# Bytes to skip before the zlib stream in a deflated payload (i32 length +
# i32 uncompressed size). Verified against live capture.
DEFLATE_HEADER_OFFSET = 8

STYLE_DAMAGE = 3   # incoming (red)
STYLE_HIT    = 6   # outgoing (blue)

FRAME_HEADER_LEN = 8
MAX_FRAME_LEN    = 10 * 1024 * 1024

# How long an HP-drop IN waits for its matching DMG floating text before
# being emitted as text-invisible damage (DOTs, drains).
PENDING_IN_MS = 600

# ── Line classification (port of DPSAgent.processLine) ────────────────────────

DAMAGE_RE = re.compile(r'for\s+(\d+)\s+damage', re.I)
KILL_RE   = re.compile(r'(?:You killed |You destroy |is destroyed|is dead)', re.I)
EXP_RE    = re.compile(r'You receive\s+(\d[\d,]*)\s+xp', re.I)

# Weapon-ability procs whose damage line is rendered in the incoming (red)
# style; they must be treated as outgoing (same short-circuit as the agent).
NAMED_PROCS = ('Justice ', 'Dream Eater ')

# ── Thrift COMPACT reader (generic, schema applied by callers) ────────────────

T_STOP, T_TRUE, T_FALSE, T_BYTE, T_I16, T_I32, T_I64 = 0, 1, 2, 3, 4, 5, 6
T_DOUBLE, T_BINARY, T_LIST, T_SET, T_MAP, T_STRUCT   = 7, 8, 9, 10, 11, 12


class CompactReader:
    """Reads a TCompactProtocol struct into {field_id: value}."""

    def __init__(self, buf, offset=0):
        self.buf = buf
        self.pos = offset

    def _varint(self):
        result = 0
        shift = 0
        while True:
            if self.pos >= len(self.buf):
                raise ValueError('varint overflow')
            b = self.buf[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7
            if shift > 70:
                raise ValueError('varint too long')

    def _zigzag(self):
        n = self._varint()
        return (n >> 1) ^ -(n & 1)

    def read_struct(self):
        fields = {}
        last_id = 0
        while True:
            header = self.buf[self.pos]
            self.pos += 1
            if header == 0:
                return fields
            ctype = header & 0x0F
            delta = (header & 0xF0) >> 4
            fid = self._zigzag() if delta == 0 else last_id + delta
            last_id = fid
            fields[fid] = self._read_value(ctype)

    def _read_value(self, ctype):
        if ctype == T_TRUE:
            return True
        if ctype == T_FALSE:
            return False
        if ctype == T_BYTE:
            b = self.buf[self.pos]
            self.pos += 1
            return b - 256 if b >= 128 else b
        if ctype in (T_I16, T_I32, T_I64):
            return self._zigzag()
        if ctype == T_DOUBLE:
            import struct as _s
            v = _s.unpack_from('<d', self.buf, self.pos)[0]
            self.pos += 8
            return v
        if ctype == T_BINARY:
            n = self._varint()
            if n < 0 or n > len(self.buf) - self.pos:
                raise ValueError(f'binary length out of range: {n}')
            raw = self.buf[self.pos:self.pos + n]
            self.pos += n
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw
        if ctype in (T_LIST, T_SET):
            header = self.buf[self.pos]
            self.pos += 1
            size = (header & 0xF0) >> 4
            elem = header & 0x0F
            if size == 0x0F:
                size = self._varint()
            out = []
            for _ in range(size):
                if elem in (T_TRUE, T_FALSE):
                    b = self.buf[self.pos]
                    self.pos += 1
                    out.append(b != 0)
                else:
                    out.append(self._read_value(elem))
            return out
        if ctype == T_MAP:
            size = self._varint()
            m = {}
            if size == 0:
                return m
            kv = self.buf[self.pos]
            self.pos += 1
            ktype, vtype = (kv & 0xF0) >> 4, kv & 0x0F
            for _ in range(size):
                k = self._read_value(ktype)
                m[k] = self._read_value(vtype)
            return m
        if ctype == T_STRUCT:
            return self.read_struct()
        raise ValueError(f'unknown compact type: {ctype}')


# ── Frame reassembly (one buffer per WebSocket) ───────────────────────────────

class FrameBuffer:
    def __init__(self):
        self.buf = b''
        self.desynced = False   # set when a bogus length forced a buffer drop

    def add(self, data):
        """Append raw WS bytes, return list of (opcode, payload) frames.

        On desync the buffer is dropped but frames already parsed from this
        chunk are still returned; `desynced` is set for the caller to report.
        """
        self.buf += data
        frames = []
        while len(self.buf) >= FRAME_HEADER_LEN:
            opcode = int.from_bytes(self.buf[0:4], 'big', signed=True)
            length = int.from_bytes(self.buf[4:8], 'big', signed=True)
            if length < 0 or length > MAX_FRAME_LEN:
                # Desynced — drop the buffer rather than poison every
                # subsequent parse.
                self.buf = b''
                self.desynced = True
                break
            end = FRAME_HEADER_LEN + length
            if len(self.buf) < end:
                break
            frames.append((opcode, self.buf[FRAME_HEADER_LEN:end]))
            self.buf = self.buf[end:]
        return frames


# ── CDP plumbing ──────────────────────────────────────────────────────────────

def cdp_targets(port):
    """Page targets from the client's DevTools endpoint, [] if unreachable."""
    try:
        with urllib.request.urlopen(
                f'http://127.0.0.1:{port}/json/list', timeout=2) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return []


def cdp_alive(port):
    return bool(cdp_targets(port))


def find_game_target(port):
    """The game page's webSocketDebuggerUrl, or None."""
    for t in cdp_targets(port):
        if t.get('type') != 'page':
            continue
        url = t.get('url', '')
        if url.startswith('app://') or 'ghosttrack.com' in url:
            return t.get('webSocketDebuggerUrl')
    return None


# ── Capture ───────────────────────────────────────────────────────────────────

class WebCapture:
    """Watches the game's WebSocket via CDP and emits DPS events.

    write_event(etype, data) is called from the capture thread; the caller's
    writer must be thread-safe (the tracker appends to a log file).
    """

    def __init__(self, port, write_event):
        self.port = port
        self.write_event = write_event
        self.disconnected = False   # true once the client is gone for good
        self._ws = None
        self._msg_id = 0
        self._shutdown = False
        # All binary sockets are decoded (per-requestId buffers isolate them,
        # and non-game streams die as contained desync/decode errors). A URL
        # allowlist here went deaf on MIGRATE redials to new hosts.
        self._buffers = {}          # requestId -> FrameBuffer
        # HP pairing state (port of DPSAgent.onHpReading)
        self._last_hp = -1
        self._last_max_hp = -1
        self._pending_in_msg = None
        self._pending_in_ts = 0
        # Player entity detection: the DMG feed carries damage to every
        # on-screen entity including us. An entity whose DMG amounts repeatedly
        # equal our own HP drops at the same instant is the player. Emitted
        # as a PLAYER event so the GUI can classify DMG in/out; re-armed on
        # reconnect and death since ids can change.
        self._player_entity = 0
        self._player_candidates = {}
        self._last_hp_drop = (0, 0.0)   # (amount, ts_ms)
        # HP-drop INs are held briefly: the matching DMG-to-player floating
        # text arrives a few ms later and is the authoritative (typed) record
        # of the same hit. Only drops with no match within PENDING_IN_MS
        # (DOTs, drains — no floating text) are emitted as IN.
        self._pending_ins = []          # [{'dmg', 'msg', 'ts'}]

    # -- lifecycle --

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name='WebCapture').start()

    def stop(self):
        self._shutdown = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        self.write_event('AGENT_READY', 'web-v1')
        misses = 0
        while not self._shutdown:
            target = find_game_target(self.port)
            if not target:
                misses += 1
                # ~20 s of no debuggable client at all = client closed.
                if misses > 10 and not cdp_alive(self.port):
                    self.disconnected = True
                    return
                time.sleep(2)
                continue
            misses = 0
            try:
                self._attach(target)   # blocks until the CDP socket closes
            except Exception as e:
                self.write_event('DBG', f'cdp_error|{e}')
            if not self._shutdown:
                time.sleep(1)

    def _attach(self, ws_url):
        self._buffers = {}
        # Reset per-connection game state: after a capture gap the old HP
        # anchor would emit one giant fabricated IN/HEAL, and the player
        # entity id may have changed (respawn/server migration) — a stale id
        # silently inverts damage direction downstream. Re-detection takes a
        # couple of hits and re-emits PLAYER.
        self._last_hp = -1
        self._last_max_hp = -1
        self._pending_in_msg = None
        self._pending_ins = []
        self._player_entity = 0
        self._player_candidates = {}
        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message)
        # suppress_origin: Chrome 111+ rejects CDP websocket connections that
        # carry a browser Origin header unless --remote-allow-origins is set.
        # Pings detect half-open CDP pipes that would otherwise hang forever.
        self._ws.run_forever(suppress_origin=True,
                             ping_interval=30, ping_timeout=10)

    def _send(self, method, params=None):
        self._msg_id += 1
        self._ws.send(json.dumps(
            {'id': self._msg_id, 'method': method, 'params': params or {}}))

    def _on_open(self, ws):
        self._send('Network.enable')
        self.write_event('ATTACHED', 'web-v1')

    # -- CDP events --

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except ValueError:
            return
        method = msg.get('method')
        params = msg.get('params', {})

        if method == 'Network.webSocketClosed':
            self._buffers.pop(params.get('requestId'), None)
        elif method == 'Network.webSocketFrameReceived':
            rid = params.get('requestId')
            resp = params.get('response', {})
            if resp.get('opcode') != 2:   # binary frames only
                return
            try:
                data = base64.b64decode(resp.get('payloadData', ''))
            except Exception:
                return
            self._feed(rid, data)

    def _feed(self, rid, data):
        self._flush_pending_ins()
        fb = self._buffers.setdefault(rid, FrameBuffer())
        frames = fb.add(data)
        if fb.desynced:
            fb.desynced = False
            self.write_event('DBG', f'frame_desync|rid={rid}')
        for opcode, payload in frames:
            try:
                self._handle_frame(opcode, payload)
            except Exception as e:
                self.write_event('DBG', f'decode_error|op={opcode}|{e}')

    # -- game messages --

    @staticmethod
    def _inflate(payload):
        return zlib.decompress(payload[DEFLATE_HEADER_OFFSET:])

    def _handle_frame(self, opcode, payload):
        if opcode == OP_TEXT_OUT:
            msg = CompactReader(payload, PLAIN_STRUCT_OFFSET).read_struct()
            self._handle_text(msg)
        elif opcode == OP_ZIP_TEXT_OUT:
            msg = CompactReader(self._inflate(payload)).read_struct()
            self._handle_text(msg)
        elif opcode == OP_MIXED_TEXT_OUT:
            mixed = CompactReader(self._inflate(payload)).read_struct()
            runs = mixed.get(1) or []
            text = ''.join(r.get(2, '') for r in runs
                           if isinstance(r, dict))
            styles = {r.get(1) for r in runs if isinstance(r, dict)}
            if STYLE_HIT in styles:
                style = STYLE_HIT
            elif STYLE_DAMAGE in styles:
                style = STYLE_DAMAGE
            else:
                style = next(iter(styles), 0)
            self._handle_text({1: style, 2: text})
        elif opcode == OP_STAT_UPDATE:
            stats = CompactReader(payload, PLAIN_STRUCT_OFFSET).read_struct()
            hp, max_hp = stats.get(1), stats.get(2)
            if isinstance(hp, int):
                self._on_hp(hp, max_hp if isinstance(max_hp, int) else None)
            elif isinstance(max_hp, int):
                # maxHp-only update (buff/level with hp unchanged): record the
                # new baseline so a later hp change isn't misread.
                self._last_max_hp = max_hp
        elif opcode == OP_CLIENT_ACTION:
            action = CompactReader(payload, PLAIN_STRUCT_OFFSET).read_struct()
            self._handle_client_action(action)

    def _handle_client_action(self, action):
        sub = action.get(1)
        if sub == ACTION_SHOW_DAMAGE:
            try:
                d = json.loads(action.get(2) or '')
            except ValueError:
                return
            if not isinstance(d, dict):
                return
            eid, dmg = d.get('id'), d.get('dmg')
            if not isinstance(eid, int) or not isinstance(dmg, (int, float)) \
                    or eid == 0 or dmg <= 0:
                return
            heal = d.get('heal') is True
            crit = (not heal) and d.get('crit') is True
            dtype = d.get('type') if isinstance(d.get('type'), str) else 'hit'
            self.write_event(
                'DMG', f'{eid}|{int(dmg)}|{dtype}|{int(crit)}|{int(heal)}')
            # This floating-text hit supersedes the HP-drop IN for the same
            # damage — cancel the held IN so the GUI doesn't count it twice.
            if not heal and self._player_entity and eid == self._player_entity:
                now = time.time() * 1000
                for p in self._pending_ins:
                    if p['dmg'] == int(dmg) and now - p['ts'] < PENDING_IN_MS:
                        self._pending_ins.remove(p)
                        break
            if not heal and not self._player_entity:
                drop_amt, drop_ts = self._last_hp_drop
                if drop_amt == int(dmg) and \
                        abs(time.time() * 1000 - drop_ts) < 400:
                    n = self._player_candidates.get(eid, 0) + 1
                    self._player_candidates[eid] = n
                    if n >= 2:
                        self._player_entity = eid
                        self.write_event('PLAYER', str(eid))
        elif sub in ACTION_DEBUFFS:
            name, started = ACTION_DEBUFFS[sub]
            if started:
                total = action.get(4, action.get(3, 0))
                self.write_event('DEBUFF', f'{name}|START|{total}')
            else:
                self.write_event('DEBUFF', f'{name}|STOP|0')

    def _handle_text(self, msg):
        style = msg.get(1, 0)
        text = msg.get(2, '')
        if not isinstance(text, str):
            return
        for line in text.split('\n'):
            line = line.strip()
            if line:
                self._process_line(line, style)

    # Port of DPSAgent.processLine. Style ints replace the Swing style names:
    # 6=hit (out), 3=damage (in); anything else is neither.
    def _process_line(self, line, style):
        if 'You have died' in line or 'You die' in line:
            self.write_event('DEATH', line)
            # Invalidate the HP anchor (respawn jumps to full HP - not a
            # heal) and re-arm player detection (respawn may change our
            # entity id; a stale id inverts damage direction).
            self._last_hp = -1
            self._player_entity = 0
            self._player_candidates = {}
            self._pending_ins = []
            return

        if KILL_RE.search(line):
            self.write_event('KILL', line)

        exp = EXP_RE.search(line)
        if exp:
            self.write_event('EXP', exp.group(1).replace(',', ''))
            return

        if line.startswith(NAMED_PROCS):
            dmg = DAMAGE_RE.search(line)
            if dmg:
                self.write_event('OUT', f'{dmg.group(1)}|{line}')
            else:
                self.write_event('DBG', f'named_proc_no_damage|{line}')
            return

        if line == 'You perform a ninja backstab.':
            self.write_event('BACKSTAB', 'ninja')
            return

        if style == STYLE_DAMAGE:
            self._pending_in_msg = line
            self._pending_in_ts = time.time() * 1000
            return

        if style == STYLE_HIT:
            dmg = DAMAGE_RE.search(line)
            if dmg:
                self.write_event('OUT', f'{dmg.group(1)}|{line}')
            return

        # Neither style but looks like a damage line — surface it so gaps in
        # the style mapping show up in the log during live testing.
        if DAMAGE_RE.search(line):
            self.write_event('DBG', f'unstyled_damage|{style}|{line}')

    # Port of DPSAgent.onHpReading: incoming damage = HP drop, paired with
    # the last red-styled line if it arrived within 500 ms. HP rises are
    # healing (regen/potions/spells) — reported as HEAL events unless maxHp
    # changed in the same update (level-up/buff/drain shifts the baseline,
    # not a heal or a hit). Drops are held briefly and suppressed when the
    # matching DMG-to-player floating text arrives (it is the authoritative
    # typed record of the same hit — emitting both double-counted).
    def _on_hp(self, hp, max_hp=None):
        prev = self._last_hp
        max_changed = max_hp is not None and max_hp != self._last_max_hp
        if max_hp is not None:
            self._last_max_hp = max_hp
        self._last_hp = hp
        if prev <= 0 or hp == prev or max_changed:
            return
        if hp > prev:
            self.write_event('HEAL', str(hp - prev))
            return
        dmg = prev - hp
        msg = self._pending_in_msg
        now = time.time() * 1000
        self._last_hp_drop = (dmg, now)
        if msg is not None and (now - self._pending_in_ts) < 500:
            self._pending_in_msg = None
        else:
            msg = None
        self._pending_ins.append({'dmg': dmg, 'msg': msg, 'ts': now})

    def _flush_pending_ins(self, now=None):
        """Emit held HP-drop INs whose DMG-match window has expired."""
        if not self._pending_ins:
            return
        if now is None:
            now = time.time() * 1000
        keep = []
        for p in self._pending_ins:
            if now - p['ts'] >= PENDING_IN_MS:
                if p['msg'] is not None:
                    self.write_event('IN', f"{p['dmg']}|{p['msg']}")
                else:
                    self.write_event('IN', str(p['dmg']))
            else:
                keep.append(p)
        self._pending_ins = keep
