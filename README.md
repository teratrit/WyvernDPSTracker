# Wyvern DPS Tracker

DPS meter for Wyvern. Tracks outgoing and incoming damage separately with per-type breakdown.

## Download

Grab `WyvernDPSTracker.exe` from the [latest release](https://github.com/teratrit/WyvernDPSTracker/releases) or from `dist/`.

## Requirements

- Wyvern running
- JDK 11+ installed (not JRE). Grab one from https://adoptium.net if you don't have one.

## Usage

1. Open Wyvern, log in
2. Make sure hit messages are on (`hitmsgs on` in the command bar)
3. Run `WyvernDPSTracker.exe`
4. Fight stuff

Outgoing and incoming damage are tracked separately. Sessions auto-start when you deal or take damage, and auto-end after 5 seconds of no combat.

Damage is broken down by type: Cut, Smash, Stab, Fire, Cold, Shock, Acid, Death.

## Building from source

Source is in `src/` if you want to poke around or build it yourself.

```
pip install pyinstaller
pyinstaller --onefile --name WyvernDPSTracker --add-data "agent.jar;." --add-data "attacher;attacher" dps_tracker.py
```

Needs `javac` to compile the agent. See the Java files in `src/` for details.

## License

MIT
