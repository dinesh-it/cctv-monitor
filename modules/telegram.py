#!/usr/bin/env python3
"""
Telegram notification module with rate limiting and smart notifications
"""

import json
import time
import logging
from typing import Dict, Optional
from urllib import request, parse, error
from pathlib import Path

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, rate_limit_seconds: int = 300):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.rate_limit_seconds = rate_limit_seconds
        self.last_sent: Dict[str, float] = {}
        self.api_base_url = f"https://api.telegram.org/bot{bot_token}"
        
        # Track consecutive failures to avoid spam
        self.failure_counts: Dict[str, int] = {}
        self.last_failure_time: Dict[str, float] = {}
        
    def _can_send(self, message_key: str) -> bool:
        """Check if message can be sent based on rate limiting"""
        current_time = time.time()
        last_time = self.last_sent.get(message_key, 0)
        
        if current_time - last_time >= self.rate_limit_seconds:
            return True
        
        logger.debug(f"Rate limit active for {message_key}, skipping notification")
        return False
    
    def _should_notify_failure(self, key: str, max_consecutive: int = 3) -> bool:
        """
        Implement smart failure notifications:
        - Notify on 1st failure
        - Notify on 3rd consecutive failure
        - Then notify every 10th failure
        """
        count = self.failure_counts.get(key, 0) + 1
        self.failure_counts[key] = count
        
        if count == 1:
            return True
        elif count == max_consecutive:
            return True
        elif count % 10 == 0:
            return True
        return False
    
    def _reset_failure_count(self, key: str):
        """Reset failure count (call when issue is resolved)"""
        if key in self.failure_counts:
            self.failure_counts[key] = 0
    
    def _send_request(self, method: str, data: dict) -> Optional[dict]:
        """Send request to Telegram API with error handling"""
        url = f"{self.api_base_url}/{method}"
        
        try:
            encoded_data = parse.urlencode(data).encode('utf-8')
            req = request.Request(url, data=encoded_data, method='POST')
            
            with request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('ok'):
                    return result
                else:
                    logger.error(f"Telegram API error: {result.get('description')}")
                    return None
                    
        except error.URLError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return None
    
    def _log_for_agent(self, text: str):
        """Append to the shared sent-messages log read by the Claude agent"""
        try:
            entry = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'source': 'cctv-monitor',
                     'kind': 'text', 'text': text}
            with open('/opt/tel-bot/sent_messages.jsonl', 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.debug(f"Could not log message for agent: {e}")

    def send_message(self, text: str, message_key: Optional[str] = None,
                    force: bool = False, parse_mode: str = "HTML") -> bool:
        """
        Send a Telegram message with rate limiting
        
        Args:
            text: Message text
            message_key: Key for rate limiting (use same key for similar messages)
            force: Skip rate limiting
            parse_mode: HTML or Markdown
        
        Returns:
            True if sent successfully
        """
        # Use text hash as key if not provided
        if message_key is None:
            message_key = str(hash(text[:100]))
        
        # Check rate limiting
        if not force and not self._can_send(message_key):
            return False
        
        data = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode
        }
        
        result = self._send_request('sendMessage', data)
        
        if result:
            self.last_sent[message_key] = time.time()
            logger.info(f"Telegram notification sent: {message_key}")
            self._log_for_agent(text)
            return True
        
        return False
    
    def notify_camera_offline(self, camera_name: str, error_msg: str = "") -> bool:
        """Notify about camera going offline"""
        key = f"camera_offline_{camera_name}"
        
        if not self._should_notify_failure(key):
            return False
        
        failure_count = self.failure_counts.get(key, 0)
        
        text = f"🔴 <b>Camera Offline</b>\n"
        text += f"Camera: {camera_name}\n"
        text += f"Consecutive failures: {failure_count}\n"
        if error_msg:
            text += f"Error: {error_msg}\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key=key, force=True)
    
    def notify_camera_online(self, camera_name: str) -> bool:
        """Notify about camera coming back online"""
        key = f"camera_online_{camera_name}"
        failure_key = f"camera_offline_{camera_name}"
        
        # Reset failure count
        self._reset_failure_count(failure_key)
        
        text = f"✅ <b>Camera Online</b>\n"
        text += f"Camera: {camera_name}\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key=key, force=True)
    
    def notify_sync_error(self, error_msg: str, retry_count: int = 0) -> bool:
        """Notify about cloud sync errors"""
        key = "sync_error"
        
        if not self._should_notify_failure(key):
            return False
        
        failure_count = self.failure_counts.get(key, 0)
        
        text = f"☁️ <b>Cloud Sync Error</b>\n"
        text += f"Consecutive failures: {failure_count}\n"
        text += f"Current retry: {retry_count}\n"
        text += f"Error: {error_msg}\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key=key)
    
    def notify_sync_success(self, files_synced: int = 0) -> bool:
        """Notify about successful sync (only after failures)"""
        key = "sync_error"
        
        # Only notify if there were previous failures
        if self.failure_counts.get(key, 0) == 0:
            return False
        
        self._reset_failure_count(key)
        
        text = f"✅ <b>Cloud Sync Restored</b>\n"
        text += f"Files synced: {files_synced}\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key="sync_success", force=True)
    
    def notify_disk_space_low(self, path: str, free_gb: float, used_percent: float) -> bool:
        """Notify about low disk space"""
        key = "disk_space_low"
        
        if not self._can_send(key):
            return False
        
        text = f"💾 <b>Low Disk Space Warning</b>\n"
        text += f"Path: {path}\n"
        text += f"Free space: {free_gb:.2f} GB\n"
        text += f"Used: {used_percent:.1f}%\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key=key, force=True)
    
    def notify_disk_space_critical(self, path: str, free_gb: float) -> bool:
        """Notify about critical disk space"""
        key = "disk_space_critical"
        
        text = f"🚨 <b>CRITICAL: Disk Space</b>\n"
        text += f"Path: {path}\n"
        text += f"Free space: {free_gb:.2f} GB\n"
        text += f"Action: Stopping recordings!\n"
        text += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key=key, force=True)
    
    def notify_health_report(self, cameras_status: dict, disk_info: dict, 
                           sync_status: dict) -> bool:
        """Send daily health report"""
        text = f"📊 <b>Daily Health Report</b>\n\n"
        
        # Camera status
        text += "<b>Cameras:</b>\n"
        for camera, status in cameras_status.items():
            icon = "✅" if status['online'] else "🔴"
            text += f"{icon} {camera}: {status['status']}\n"
        
        text += f"\n<b>Storage:</b>\n"
        text += f"Local: {disk_info['free_gb']:.2f} GB free ({disk_info['used_percent']:.1f}% used)\n"
        
        text += f"\n<b>Cloud Sync:</b>\n"
        text += f"Status: {sync_status['status']}\n"
        text += f"Last sync: {sync_status['last_sync']}\n"
        text += f"Files pending: {sync_status['pending_files']}\n"
        
        text += f"\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(text, message_key="daily_report", force=True)
    
    def test_connection(self) -> bool:
        """Test Telegram connection"""
        text = "🧪 CCTV Monitor - Test notification"
        return self.send_message(text, message_key="test", force=True)


if __name__ == "__main__":
    # Simple test
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: telegram.py <bot_token> <chat_id>")
        sys.exit(1)
    
    notifier = TelegramNotifier(sys.argv[1], sys.argv[2])
    if notifier.test_connection():
        print("✅ Telegram connection successful!")
    else:
        print("❌ Failed to send Telegram message")
        sys.exit(1)
