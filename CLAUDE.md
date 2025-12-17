# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A production-ready CCTV monitoring system that records from multiple RTSP cameras, syncs recordings to cloud storage via rclone, and sends Telegram notifications. Key features include intelligent resource management (exponential backoff, rate limiting), health monitoring, and automatic cleanup.

## Core Architecture

The system follows a modular architecture with thread-based components:

```
cctv_monitor.py (Main orchestrator)
├── modules/recorder.py      - CameraRecorder: Manages ffmpeg processes per camera
├── modules/sync.py          - CloudSync: Handles rclone uploads with backoff
├── modules/health.py        - HealthMonitor: Monitors disk, processes, network
├── modules/cleanup.py       - CleanupManager: Deletes old files (local/cloud)
└── modules/telegram.py      - TelegramNotifier: Sends notifications with rate limiting
```

### Component Communication

- **Main Orchestrator** (`CCTVMonitor`): Initializes all components with shared config and notifier, manages lifecycle
- **Thread-based Components**: Each component runs in its own thread with `start()`, `stop()`, and status methods
- **Shared Notifier**: All components use the same `TelegramNotifier` instance for consistent rate limiting
- **Storage Flow**: temp_dir → archive_dir → cloud → cleanup

### Key Design Patterns

1. **Resource Protection**:
   - One ffmpeg process per camera (prevents camera overload)
   - Exponential backoff after failures (60s → 120s → 240s...)
   - Rate-limited Telegram notifications (5 min cooldown per message type)
   - Minimum 60s between rclone sync attempts

2. **State Management**:
   - Each component tracks consecutive failures and implements backoff
   - Components maintain `is_running` flag and `stop_event` for graceful shutdown
   - Status methods return component health for monitoring

3. **Notification Strategy**:
   - Smart failure notifications: 1st, 3rd, then every 10th failure
   - Recovery notifications when service comes back online
   - Message keys for rate limiting (e.g., "camera_offline_front-door")

## Development Commands

### Setup and Installation
```bash
./setup.sh                    # Interactive setup (dependencies, config, systemd)
pip3 install -r requirements.txt
cp config.example.yaml config.yaml
rclone config                 # Configure cloud storage
```

### Running and Testing
```bash
./cctv_monitor.py            # Run manually (Ctrl+C to stop)
./cctv_monitor.py /path/to/config.yaml  # Custom config

# Test Telegram notifications
python3 modules/telegram.py BOT_TOKEN CHAT_ID

# Test ffmpeg camera connection
ffmpeg -i "rtsp://user:pass@ip:port/stream" -t 10 test.mkv

# Test rclone sync
rclone ls RemoteName:/CCTV
```

### Systemd Service
```bash
sudo cp cctv-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cctv-monitor.service
sudo systemctl start cctv-monitor.service
sudo systemctl status cctv-monitor.service
sudo journalctl -u cctv-monitor.service -f
```

### Log Analysis
```bash
tail -f logs/cctv_monitor.log
tail -f logs/cctv_monitor.log | grep camera
tail -f logs/cctv_monitor.log | grep sync
sudo journalctl -u cctv-monitor.service -f
```

## Configuration Structure

**config.yaml** sections (see config.example.yaml for full reference):
- `telegram`: bot_token, chat_id, rate_limit_seconds, daily_report settings
- `storage`: temp_dir, archive_dir, retention policies, disk space thresholds
- `cloud`: rclone remote configuration, sync intervals, retry/backoff settings
- `cameras`: list of camera configs (name, rtsp_url, segment_duration, codec settings)
- `monitoring`: health check intervals, disk/process checks, cronitor integration
- `logging`: log level, rotation, console output

## Important Implementation Details

### Recording Flow (recorder.py)

1. ffmpeg writes segments to `/tmp/cctv/{camera}/` using strftime pattern
2. `_move_completed_files()` moves files >30s old to `archive_dir/{date}/{camera}/`
3. Incomplete recordings on crash → moved to `incomplete_dir` on restart
4. Thread monitors process, implements exponential backoff on failures
5. Extended backoff (5 min) after 3 consecutive failures

**Critical**: Only one ffmpeg process per camera at any time. Check for existing process before starting new one.

### Cloud Sync Flow (sync.py)

1. Scans `archive_dir` for files older than `min_file_age_seconds` (default 60s)
2. Uses `rclone copy` with limited transfers (4) and checkers (2)
3. Rate limited: minimum 60s between sync attempts
4. Exponential backoff on failures (up to 1 hour max)
5. Separate thread handles cloud cleanup based on retention days

**Critical**: Always filter files by age to avoid syncing incomplete recordings. Use `--max-age` flag with rclone.

### Notification Rate Limiting (telegram.py)

- `message_key` groups similar messages for rate limiting (e.g., "camera_offline_front-door")
- Default 5-minute cooldown per message key
- `force=True` bypasses rate limiting (use for startup/shutdown)
- Smart failure counting: notify on 1st, 3rd, then every 10th consecutive failure
- Reset failure count on recovery

### Signal Handling

All components respond to SIGINT/SIGTERM for graceful shutdown:
1. Main catches signal, calls `stop()` on all components
2. Each component sets `stop_event`, waits for threads to finish
3. CameraRecorder terminates ffmpeg with SIGTERM (allows segment finalization)
4. Shutdown notification sent before exit

## Adding New Features

### Adding a New Camera
1. Edit config.yaml, add entry to `cameras` list
2. Restart service - new CameraRecorder will be auto-initialized

### Adding a New Notification Type
1. Add method to `TelegramNotifier` (e.g., `notify_custom()`)
2. Use `send_message()` with appropriate `message_key` for rate limiting
3. Consider using `_should_notify_failure()` for recurring issues

### Extending Health Checks
1. Add check method to `HealthMonitor` (e.g., `_check_network_latency()`)
2. Call from `_run_health_checks()` in main loop
3. Send notifications via `self.notifier` with proper message keys

## Dependencies

**System**: Python 3.8+, ffmpeg, rclone
**Python**: PyYAML>=6.0 (see requirements.txt)

## File Retention and Cleanup

- **Local recordings**: Deleted after 6 hours (configurable via `local_retention_hours`)
- **Cloud recordings**: Deleted after 5 days (configurable via `cloud_retention_days`)
- **Cleanup runs**: Periodic (check code for interval)
- **Disk space alerts**: Warning at 10GB free, critical at 2GB free

## Troubleshooting Tips

- **Camera won't connect**: Check RTSP URL, test with ffmpeg directly, verify network access
- **Cloud sync failing**: Test rclone manually (`rclone ls remote:/path`), check credentials
- **High CPU usage**: Check number of cameras × ffmpeg processes, consider lowering resolution/bitrate
- **Telegram spam**: Adjust `rate_limit_seconds` or notification thresholds in code
- **Logs filling disk**: Adjust `max_log_size_mb` and `backup_count` in config.yaml

## Security Considerations

- `config.yaml` contains sensitive credentials (RTSP URLs, bot tokens) - set permissions: `chmod 600 config.yaml`
- Consider using environment variables for secrets (requires code changes)
- rclone supports encryption for cloud storage (`rclone config` → crypt)
- Service file includes security hardening: NoNewPrivileges, PrivateTmp
