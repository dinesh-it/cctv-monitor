# CCTV Monitor

A reliable, production-ready CCTV monitoring system with multi-camera support, cloud sync, and Telegram notifications.

## Features

- **Multi-Camera Support**: Monitor multiple RTSP cameras simultaneously
- **Cloud Sync**: Automatic upload to Google Drive (or any rclone-supported cloud)
- **Telegram Notifications**: Smart alerts for camera failures, sync errors, disk space issues
- **Intelligent Resource Management**:
  - Exponential backoff on failures (no API bombardment)
  - Single ffmpeg process per camera (prevents camera overload)
  - Rate-limited notifications (no spam)
  - Automatic cleanup of old recordings
- **Health Monitoring**: 
  - Disk space monitoring
  - Camera connection health checks
  - Network connectivity checks
  - Optional Cronitor integration
- **Reliable**: Proper error handling, graceful shutdown, systemd integration

## Motion Detection Script (tele-notify.py)

This repository includes a **separate** motion detection script for different use cases:

**tele-notify.py** - Motion-triggered recording:
- Monitors single RTSP camera stream using OpenCV
- Records video ONLY when motion is detected
- Sends 5-second video clips to Telegram (including 2s before motion)
- Lightweight alternative to 24/7 recording

**When to use tele-notify.py vs main system:**
- Use **tele-notify.py** for: Instant motion alerts, low storage usage, testing motion detection
- Use **cctv_monitor.py** for: Complete 24/7 recording, multi-camera archival, cloud backup

**Running tele-notify.py:**
```bash
# Install additional dependency
pip3 install opencv-python

# Set credentials via environment variables (RECOMMENDED)
export RTSP_URL="rtsp://user:pass@ip:port/stream"
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_USER_ID="your_user_id"
python3 tele-notify.py

# Or edit credentials in tele-notify.py main() function (NOT recommended for version control)
```

**Security Note:** Remove hardcoded credentials from tele-notify.py before committing to version control.

## Prerequisites

- Python 3.8+
- ffmpeg for video recording
- rclone for cloud sync

## Quick Start

```bash
cd ~/cctv-monitor
./setup.sh
# Edit config.yaml with your settings
./cctv_monitor.py
```

## Installation

See setup.sh for automated installation or follow these steps manually:

1. Install dependencies: `pip3 install -r requirements.txt`
2. Copy `config.example.yaml` to `config.yaml`
3. Configure Telegram bot (get token from @BotFather)
4. Configure rclone: `rclone config`
5. Update config.yaml with your camera URLs and settings

## Configuration

Edit `config.yaml` to configure:
- Telegram bot token and chat ID
- Camera RTSP URLs (supports multiple cameras)
- Cloud sync settings (remote name, intervals, retention)
- Storage paths and retention policies
- Health monitoring settings

## Usage

**Manual run:**
```bash
./cctv_monitor.py
```

**As systemd service:**
```bash
sudo cp cctv-monitor.service /etc/systemd/system/
sudo systemctl enable cctv-monitor.service
sudo systemctl start cctv-monitor.service
```

**View logs:**
```bash
tail -f logs/cctv_monitor.log
sudo journalctl -u cctv-monitor.service -f
```

## How It Works

### Resource Protection

**Camera Protection:**
- Single ffmpeg process per camera
- Exponential backoff after failures (60s → 120s → 240s...)
- Extended backoff (5 min) after 3 consecutive failures

**Cloud API Protection:**
- Minimum 60s between sync attempts
- Exponential backoff on failures (up to 1 hour max)
- Limited concurrent transfers (4) and checkers (2)

**Notification Rate Limiting:**
- Same message type: max once per 5 minutes
- Smart failure notifications (1st, 3rd, then every 10th)
- Recovery notifications when back online

### File Management

1. **Recording**: Segments saved to `/tmp/cctv/{camera}/`
2. **Archive**: Completed files moved to `/tmp/cctv_store/{date}/{camera}/`
3. **Sync**: Only syncs files older than 60s (avoids incomplete files)
4. **Cleanup**: Local files deleted after 6h, cloud after 5d (configurable)

## Notifications

Telegram notifications sent for:
- Camera online/offline status
- Cloud sync errors and recovery
- Low/critical disk space
- Daily health reports
- System startup/shutdown

## Troubleshooting

**Camera won't connect:**
```bash
ffmpeg -i "rtsp://user:pass@ip:port/stream" -t 10 test.mkv
tail -f logs/cctv_monitor.log | grep camera
```

**Cloud sync failing:**
```bash
rclone ls RemoteName:/CCTV
grep sync logs/cctv_monitor.log
```

**Telegram not working:**
```bash
cd ~/cctv-monitor
python3 modules/telegram.py BOT_TOKEN CHAT_ID
```

## Performance

Typical per-camera usage:
- CPU: 5-15%
- Memory: 50-100MB
- Disk I/O: 5-10 MB/s
- Network: Depends on stream bitrate

## Security

```bash
chmod 600 config.yaml  # Protect passwords
```

Consider using environment variables for credentials or rclone encryption for cloud storage.

## License

MIT License
