"""Offline test for web_capture: synthesizes Thrift-COMPACT frames the way the
server encodes them and checks the emitted DPS events. No client needed."""

import zlib

from web_capture import (WebCapture, FrameBuffer, CompactReader,
                         OP_TEXT_OUT, OP_MIXED_TEXT_OUT, OP_ZIP_TEXT_OUT,
                         OP_STAT_UPDATE, OP_CLIENT_ACTION,
                         STYLE_HIT, STYLE_DAMAGE)


# ── Minimal compact writer (test-side only) ───────────────────────────────────

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(n):
    return _varint((n << 1) ^ (n >> 63))


def field_i32(delta, value):
    return bytes([(delta << 4) | 5]) + _zigzag(value)


def field_str(delta, s):
    raw = s.encode('utf-8')
    return bytes([(delta << 4) | 8]) + _varint(len(raw)) + raw


def text_message(style, text):
    return field_i32(1, style) + field_str(1, text) + b'\x00'


def stat_update(hp, max_hp):
    return field_i32(1, hp) + field_i32(1, max_hp) + b'\x00'


def mixed_message(runs):
    body = bytes([(1 << 4) | 9])                      # field 1, LIST
    body += bytes([(len(runs) << 4) | 12])            # list header, STRUCT elems
    for style, text in runs:
        body += text_message(style, text)
    return body + b'\x00'


def frame(opcode, struct_bytes, deflate=False):
    if deflate:
        z = zlib.compress(struct_bytes)
        # inner header: i32 length + i32 uncompressed size (as on the wire)
        payload = ((len(z) + 4).to_bytes(4, 'big')
                   + len(struct_bytes).to_bytes(4, 'big') + z)
    else:
        payload = len(struct_bytes).to_bytes(4, 'big') + struct_bytes
    return (opcode.to_bytes(4, 'big') + len(payload).to_bytes(4, 'big')
            + payload)


# ── Tests ─────────────────────────────────────────────────────────────────────

def make_capture():
    events = []
    cap = WebCapture(0, lambda etype, data: events.append((etype, data)))
    return cap, events


def feed(cap, *frames):
    for opcode, payload in FrameBuffer().add(b''.join(frames)):
        cap._handle_frame(opcode, payload)


def test_roundtrip_reader():
    msg = CompactReader(text_message(6, 'hello')).read_struct()
    assert msg == {1: 6, 2: 'hello'}, msg


def test_outgoing():
    cap, events = make_capture()
    feed(cap, frame(OP_TEXT_OUT,
                    text_message(STYLE_HIT, 'You slash the goblin for 42 damage.')))
    assert events == [('OUT', '42|You slash the goblin for 42 damage.')], events


def test_incoming_pairing():
    cap, events = make_capture()
    feed(cap,
         frame(OP_STAT_UPDATE, stat_update(100, 100)),
         frame(OP_TEXT_OUT,
               text_message(STYLE_DAMAGE, 'The goblin bites you for 13 damage.')),
         frame(OP_STAT_UPDATE, stat_update(87, 100)))
    assert events == [('IN', '13|The goblin bites you for 13 damage.')], events


def test_kill_and_exp():
    cap, events = make_capture()
    feed(cap,
         frame(OP_TEXT_OUT, text_message(0, 'You killed the goblin!')),
         frame(OP_TEXT_OUT, text_message(0, 'You receive 1,234 xp.')))
    assert events == [('KILL', 'You killed the goblin!'), ('EXP', '1234')], events


def test_zip_text():
    cap, events = make_capture()
    feed(cap, frame(OP_ZIP_TEXT_OUT,
                    text_message(STYLE_HIT, 'Your arrow hits the orc for 7 damage.'),
                    deflate=True))
    assert events == [('OUT', '7|Your arrow hits the orc for 7 damage.')], events


def test_mixed_text():
    cap, events = make_capture()
    feed(cap, frame(OP_MIXED_TEXT_OUT,
                    mixed_message([(0, 'Your fireball engulfs the troll '),
                                   (STYLE_HIT, 'for 99 damage.')]),
                    deflate=True))
    assert events == [('OUT', '99|Your fireball engulfs the troll for 99 damage.')], events


def test_split_frames():
    raw = frame(OP_TEXT_OUT,
                text_message(STYLE_HIT, 'You punch the rat for 3 damage.'))
    cap, events = make_capture()
    fb = FrameBuffer()
    for opcode, payload in fb.add(raw[:5]) + fb.add(raw[5:20]) + fb.add(raw[20:]):
        cap._handle_frame(opcode, payload)
    assert events == [('OUT', '3|You punch the rat for 3 damage.')], events


def field_i16(delta, value):
    return bytes([(delta << 4) | 4]) + _zigzag(value)


def client_action(sub, data=None, remaining=None, total=None):
    body = field_i16(1, sub)
    delta_base = 1
    if data is not None:
        body += field_str(1, data)
    else:
        delta_base = 2
    if remaining is not None:
        body += field_i32(delta_base, remaining)
        body += field_i32(1, total if total is not None else remaining)
    return body + b'\x00'


def test_show_damage():
    cap, events = make_capture()
    feed(cap, frame(OP_CLIENT_ACTION, client_action(
        16, '{"id":4242,"dmg":137.0,"type":"fire","crit":true}')))
    assert events == [('DMG', '4242|137|fire|1|0')], events


def test_debuff_cycle():
    cap, events = make_capture()
    feed(cap,
         frame(OP_CLIENT_ACTION, client_action(5, remaining=8000, total=12000)),
         frame(OP_CLIENT_ACTION, client_action(6)))
    assert events == [('DEBUFF', 'poison|START|12000'),
                      ('DEBUFF', 'poison|STOP|0')], events


def test_backstab_and_death():
    cap, events = make_capture()
    feed(cap,
         frame(OP_TEXT_OUT, text_message(0, 'You perform a ninja backstab.')),
         frame(OP_TEXT_OUT, text_message(0, 'You have died.')))
    assert events == [('BACKSTAB', 'ninja'), ('DEATH', 'You have died.')], events


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
