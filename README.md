# Wyvern DPS Tracker

Real-time DPS tracker for [Wyvern](https://www.wyvernrpg.com/) that hooks directly into the game client to capture combat damage with millisecond precision.

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![Java](https://img.shields.io/badge/Java-11+-orange)

## Features

- **Millisecond-accurate** damage tracking via Java Attach API (no OCR, no screen reading)
- **Per-attack-type breakdown**: Cut, Smash, Stab, Fire, Cold, Shock, Acid, Death
- **Auto-session detection**: starts on first hit, ends on kill or 5-second gap
- **Session history**: compare DPS across multiple runs
- **Single-instance guard**: prevents duplicate tracker windows
- **Clean attach/detach**: agent listener is removed when the tracker closes

## Quick Start (EXE)

1. Launch Wyvern and log in
2. Double-click `dist/WyvernDPSTracker.exe`
3. Hit a Training Dummy
4. Watch your DPS

## Requirements

- **Wyvern** must be running
- **JDK 11+** (not JRE) must be installed — the Attach API requires a full JDK
  - Download from [Adoptium](https://adoptium.net/) if needed
  - The tracker auto-detects Java from `JAVA_HOME`, `PATH`, or common install locations

## How It Works

1. The tracker finds the running Wyvern JVM using `VirtualMachine.list()`
2. It loads a Java agent (`agent.jar`) into the game's JVM via the Attach API
3. The agent adds a `DocumentListener` to the game's `ServerOutput` text component
4. Combat messages are captured with `System.currentTimeMillis()` timestamps and written to a log file
5. The Python GUI tails the log file and displays real-time DPS statistics

## Building from Source

### Prerequisites
- Python 3.8+
- JDK 11+ (for compiling and running the attacher)
- Wyvern game client installed

### Run from source
```bash
python dps_tracker.py
```

### Build the EXE
```bash
pip install pyinstaller
pyinstaller --onefile --name WyvernDPSTracker --add-data "agent.jar;." --add-data "attacher;attacher" dps_tracker.py
```

## Attack Type Detection

Damage is categorized by parsing the flavor text of combat messages:

| Category | Example Messages |
|----------|-----------------|
| **Cut** | carved, cleaved, sliced, hewed, nearly cut in half |
| **Smash** | smashed, crushed, slammed, staggered, smote, drown |
| **Stab** | stabbed, pierced, impaled, skewered, make a hole |
| **Fire** | burning flame, intense flame, scorched |
| **Cold** | arctic chill, glacial chill, frozen |
| **Shock** | bolt of lightning, energy surge, shocked |
| **Acid** | acid, corrosive, caustic |
| **Death** | necrotic, drain, dark energy |

## File Structure

```
dps_tracker.py          # Python GUI + orchestration
agent.jar               # Pre-built Java agent (injected into game)
attacher/dps/           # Pre-built Java attacher classes
src/dps3/DPSAgent.java  # Java agent source
src/dps/DPSAttacher.java # Java attacher source
MANIFEST.MF             # Agent JAR manifest
dist/WyvernDPSTracker.exe # Standalone Windows executable
```

## License

MIT
