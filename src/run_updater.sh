#!/bin/bash
# Wingflight Lua EdgeTX/OpenTX Updater Launcher

echo "========================================"
echo "Wingflight EdgeTX/OpenTX Updater"
echo "========================================"
echo ""

die() {
    echo ""
    echo "ERROR: $1"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
}

check_import() {
    local module="$1"
    python3 -c "import ${module}" &> /dev/null
}

if ! command -v python3 &> /dev/null; then
    die "Python 3 is not installed. Please install Python 3.7 or higher."
fi

echo "Python found: $(python3 --version)"
echo ""

if ! check_import "tkinter"; then
    echo "tkinter is missing."
    echo "macOS: install a Python build with Tk support."
    echo "Linux: install your distro package for Tk, for example python3-tk."
    die "tkinter is required to run the updater GUI."
fi

echo "Starting updater..."
echo ""

python3 update_radio_gui.py

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Failed to start updater"
    read -p "Press Enter to exit..."
    exit 1
fi

exit 0
