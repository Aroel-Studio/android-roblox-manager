# ARM v3 - Android Roblox Manager

Single-file CLI application that monitors and auto-recovers multiple Roblox instances on Android.

## Features

- Multi-instance monitoring (com.roblox.client, client2, client3)
- Crash detection via /proc filesystem (zero-subprocess)
- Auto-rejoin with exponential backoff retry
- Discord webhook notifications with screenshot
- Cookie/account management
- Live status table with ANSI colors
- Adaptive polling (30s stable, 3s during recovery)

## Requirements

- Android device with root access (Magisk / KernelSU)
- Termux installed from F-Droid
- Python 3.8+

## Installation

### Step 1: Enable Freeform Windows

1. Enable Developer Options: Settings > About Phone > Tap Build Number 7 times
2. Go to System > Developer Options > Under Apps, enable:
   - Enable freeform windows
   - Force activities to be resizable
   - Enable non-resizable in multi-window
3. Reboot your device

### Step 2: Configure Root and Termux

1. Enable Magisk (or KernelSU) and LSPosed or Root Permission
2. Open Magisk/Kitsune/Root Permission > Superuser > Ensure Termux is granted root access

### Step 3: Termux Setup

Run this single command in Termux (https://f-droid.org/packages/com.termux/):

```
termux-setup-storage && pkg update -y && pkg upgrade -y && pkg install -y python tsu && pip install aiohttp
```

Grant storage permission when prompted and type "y" if asked.

### Step 4: Install ARM v3

```
curl -L -o /sdcard/Download/start_arm.py https://raw.githubusercontent.com/Aroel-Studio/android-roblox-manager/main/start_arm.py
```

Note: For Android 10, replace /sdcard/Download/ with /sdcard/download/ (lowercase).

### Step 5: Run ARM v3

```
su -c "/data/data/com.termux/files/usr/bin/python /sdcard/Download/start_arm.py"
```

## Update / Reinstall

Re-run Step 4 (curl command). The old file is overwritten automatically.

## Stop ARM

```
su -c "pkill -f start_arm.py"
```

## Auto-Boot (Termux:Boot)

```
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start_arm.sh << 'EOF'
#!/bin/bash
su -c "/data/data/com.termux/files/usr/bin/python /sdcard/Download/start_arm.py" << ANSWERS
1
ANSWERS
EOF
chmod +x ~/.termux/boot/start_arm.sh
```

## License

MIT
