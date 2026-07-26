# Wyvern DPS Tracker

DPS meter for Wyvern (Steam client). Tracks outgoing and incoming damage with
exact per-type breakdown, server-authoritative crits, backstab stats, EXP/hr,
kill counts, and per-mob stats with wiki export.

v2.0 reads the game's own combat data feed instead of parsing chat text: every
hit arrives with its damage type (fire, cold, shock, cut, stab, smash, holy,
poison, magic, ...), the exact target, and a crit flag. Incoming damage is
typed too. `hitmsgs` can stay off for DPS tracking - turn it on only if you
want per-mob name stats and the wiki export.

## Download

Grab `WyvernDPSTracker.exe` from the [latest release](https://github.com/teratrit/WyvernDPSTracker/releases) or from `dist/`.

## Requirements

- Wyvern on Steam (the current client). No JDK needed anymore.

## Usage

One-time setup: add `--remote-debugging-port=9222` to Wyvern's Steam
launch options (Library > Wyvern > Properties > Launch Options). The
tracker then binds to your already-running game every time.

1. Run `WyvernDPSTracker.exe`
2. It binds to the running game. Without the launch option it can't
   attach to a game that's already open (debuggers only attach to
   processes started with the debug port) - it'll show the setup steps
   and offer to relaunch the game itself.
3. Fight stuff

Sessions auto-start when you deal or take damage and auto-end after quiet
time. F12 toggles tracking. Session summaries include crit rate and a
damage-type table.

## How it works

The Steam client is an Electron app. The tracker connects to its DevTools
port and watches the game's WebSocket traffic read-only, decoding the Thrift
messages for combat text, floating damage numbers, and stat updates. Nothing
is injected into the game and no commands are ever sent.

## Building from source

```
pip install pyinstaller websocket-client pynput
pyinstaller --onefile --name WyvernDPSTracker --add-data "wyvern_mobs.txt;." dps_tracker.py
```

`web_capture.py` is the capture layer; `dps_tracker.py` is the GUI.
`test_web_capture.py` runs offline against synthesized protocol frames.

## License

MIT
