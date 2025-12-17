#!/bin/bash
set -euo pipefail

echo "========================================"
echo "CCTV Monitor - Installation Script"
echo "========================================"
echo

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   echo "ERROR: Do not run this script as root"
   exit 1
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "[1/7] Checking system dependencies..."
# Check for required commands
MISSING_DEPS=()

if ! command -v python3 &> /dev/null; then
    MISSING_DEPS+=("python3")
fi

if ! command -v ffmpeg &> /dev/null; then
    MISSING_DEPS+=("ffmpeg")
fi

if ! command -v rclone &> /dev/null; then
    MISSING_DEPS+=("rclone")
fi

if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
    echo "ERROR: Missing required dependencies: ${MISSING_DEPS[*]}"
    echo "Please install them first:"
    echo "  sudo apt install python3 python3-pip ffmpeg"
    echo "  For rclone: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

echo "✓ All system dependencies found"

echo
echo "[2/7] Installing Python dependencies..."
if [ -f requirements.txt ]; then
    pip3 install --user -r requirements.txt
    echo "✓ Python dependencies installed"
else
    echo "WARNING: requirements.txt not found"
fi

echo
echo "[3/7] Creating directory structure..."
mkdir -p logs data
mkdir -p /tmp/cctv /tmp/cctv_store /tmp/cctv_store/incomplete
echo "✓ Directories created"

echo
echo "[4/7] Setting up configuration..."
if [ ! -f config.yaml ]; then
    if [ -f config.example.yaml ]; then
        echo "Creating config.yaml from example..."
        cp config.example.yaml config.yaml
        echo "⚠  IMPORTANT: Edit config.yaml with your settings before starting!"
    else
        echo "ERROR: config.example.yaml not found"
        exit 1
    fi
else
    echo "✓ config.yaml already exists"
fi

echo
echo "[5/7] Making scripts executable..."
chmod +x cctv_monitor.py
chmod +x modules/*.py
echo "✓ Scripts are executable"

echo
echo "[6/7] Testing Telegram connection..."
read -p "Do you want to test Telegram connection now? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f config.yaml ]; then
        echo "Attempting to send test message..."
        python3 -c "
import yaml
import sys
sys.path.insert(0, 'modules')
from telegram import TelegramNotifier

with open('config.yaml') as f:
    config = yaml.safe_load(f)

notifier = TelegramNotifier(
    config['telegram']['bot_token'],
    config['telegram']['chat_id']
)

if notifier.test_connection():
    print('✓ Telegram test successful!')
else:
    print('✗ Telegram test failed. Check your bot token and chat ID.')
" || echo "Could not test Telegram (check config.yaml)"
    fi
fi

echo
echo "[7/7] Installing systemd service (optional)..."
read -p "Do you want to install systemd service? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f cctv-monitor.service ]; then
        echo "Installing service..."
        sudo cp cctv-monitor.service /etc/systemd/system/
        sudo systemctl daemon-reload
        echo "✓ Service installed"
        echo
        echo "To enable and start the service:"
        echo "  sudo systemctl enable cctv-monitor.service"
        echo "  sudo systemctl start cctv-monitor.service"
        echo
        echo "To check status:"
        echo "  sudo systemctl status cctv-monitor.service"
        echo "  sudo journalctl -u cctv-monitor.service -f"
    else
        echo "ERROR: cctv-monitor.service not found"
    fi
fi

echo
echo "========================================"
echo "Installation complete!"
echo "========================================"
echo
echo "Next steps:"
echo "1. Edit config.yaml with your settings"
echo "2. Configure rclone: rclone config"
echo "3. Test manually: ./cctv_monitor.py"
echo "4. Or start service: sudo systemctl start cctv-monitor.service"
echo
echo "For help, see README.md"
echo
