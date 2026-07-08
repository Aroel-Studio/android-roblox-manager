#!/bin/bash
# ============================================================
#  ARM v3 (Android Roblox Manager) - Termux Installer
# ============================================================
#
#  Install / Update:
#    . <(curl -sL https://raw.githubusercontent.com/Aroel-Studio/android-roblox-manager/main/setup.sh)
#
#  Run ARM:
#    su -c "python /sdcard/Download/start_arm.py"
#
#  Stop ARM:
#    su -c "pkill -f start_arm.py"
#
# ============================================================

set -e

# --- Auto-detect Android version for correct path ---
ANDROID_VER=$(getprop ro.build.version.sdk 2>/dev/null || echo "34")
if [ "$ANDROID_VER" -ge 31 ] 2>/dev/null; then
    ARM_DIR="/sdcard/Download"
else
    ARM_DIR="/sdcard/download"
fi

REPO_URL="https://raw.githubusercontent.com/Aroel-Studio/android-roblox-manager/main"

# --- Boot setup mode ---
if [ "$1" = "--boot" ]; then
    echo "[ARM] Setting up Termux:Boot autostart"
    mkdir -p ~/.termux/boot
    cat > ~/.termux/boot/start_arm.sh << BOOT_EOF
#!/bin/bash
su -c "export PATH=\\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && python ${ARM_DIR}/start_arm.py" << ANSWERS
1
ANSWERS
BOOT_EOF
    chmod +x ~/.termux/boot/start_arm.sh
    echo "[ARM] Auto-boot configured. ARM will start on device restart."
    echo "[ARM] To stop: su -c \"pkill -f start_arm.py\""
    exit 0
fi

# --- Detect fresh install vs update ---
MODE="INSTALL"
if [ -f "${ARM_DIR}/start_arm.py" ]; then
    MODE="UPDATE"
fi

echo "=========================================="
if [ "$MODE" = "UPDATE" ]; then
    echo "  ARM v3 - Updater"
else
    echo "  ARM v3 - Installer"
fi
echo "=========================================="

# --- Install Termux packages (fresh install only) ---
if [ "$MODE" = "INSTALL" ]; then
    echo "[ARM] Setting up Termux"
    termux-setup-storage 2>/dev/null || true
    pkg update -y 2>/dev/null || true
    pkg upgrade -y 2>/dev/null || true
    pkg install -y python tsu 2>/dev/null || true
    pip install aiohttp 2>/dev/null || true
    echo "[ARM] Termux packages installed"
else
    echo "[ARM] UPDATE: Skipping Termux package install"
fi

# --- Download start_arm.py ---
echo "[ARM] Downloading start_arm.py"
curl -sL -o "${ARM_DIR}/start_arm.py" "${REPO_URL}/start_arm.py" || {
    echo "[ARM] ERROR: Failed to download start_arm.py"
    exit 1
}

echo ""
echo "=========================================="
if [ "$MODE" = "UPDATE" ]; then
    echo "  UPDATE COMPLETE"
else
    echo "  INSTALLATION COMPLETE"
fi
echo "=========================================="
echo ""
echo "  Run ARM:"
echo "    su -c \"python ${ARM_DIR}/start_arm.py\""
echo ""
echo "  Auto-boot (optional):"
echo "    bash ${ARM_DIR}/setup.sh --boot"
echo ""
echo "  Stop ARM:"
echo "    su -c \"pkill -f start_arm.py\""
echo "=========================================="
