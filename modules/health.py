#!/usr/bin/env python3
"""
Health monitoring module with disk space and process checks
"""

import logging
import time
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List, Dict
from threading import Thread, Event
from datetime import datetime

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, monitoring_config: dict, storage_config: dict, 
                 recorders: list, sync_manager, notifier=None):
        self.check_interval = monitoring_config.get('check_interval_seconds', 60)
        self.file_age_threshold = monitoring_config.get('file_age_threshold_seconds', 120)
        self.check_disk_space = monitoring_config.get('check_disk_space', True)
        self.check_processes = monitoring_config.get('check_processes', True)
        self.ping_hosts = monitoring_config.get('ping_hosts', [])
        
        # Cronitor config
        self.cronitor_enabled = monitoring_config.get('cronitor', {}).get('enabled', False)
        self.cronitor_base = monitoring_config.get('cronitor', {}).get('base_url', '')
        self.cronitor_monitors = monitoring_config.get('cronitor', {}).get('monitors', {})
        
        # Storage config
        self.temp_dir = Path(storage_config['temp_dir'])
        self.archive_dir = Path(storage_config['archive_dir'])
        self.min_free_space_gb = storage_config.get('min_free_space_gb', 10)
        self.max_storage_gb = storage_config.get('max_local_storage_gb', 50)
        
        # Components to monitor
        self.recorders = recorders
        self.sync_manager = sync_manager
        self.notifier = notifier
        
        # State
        self.is_running = False
        self.stop_event = Event()
        self.thread: Optional[Thread] = None
        
        # Track issues to avoid spam
        self.low_space_notified = False
        self.critical_space_notified = False
        
    def _get_disk_usage(self, path: Path) -> Dict[str, float]:
        """Get disk usage statistics"""
        try:
            usage = shutil.disk_usage(path)
            total_gb = usage.total / (1024**3)
            used_gb = usage.used / (1024**3)
            free_gb = usage.free / (1024**3)
            used_percent = (usage.used / usage.total) * 100
            
            return {
                'total_gb': total_gb,
                'used_gb': used_gb,
                'free_gb': free_gb,
                'used_percent': used_percent
            }
        except Exception as e:
            logger.error(f"Error getting disk usage: {e}")
            return {
                'total_gb': 0,
                'used_gb': 0,
                'free_gb': 0,
                'used_percent': 0
            }
    
    def _check_disk_space_status(self) -> bool:
        """Check disk space and send notifications if needed"""
        disk_info = self._get_disk_usage(self.archive_dir)
        free_gb = disk_info['free_gb']
        used_percent = disk_info['used_percent']
        
        # Critical space check
        if free_gb < 2:  # Critical threshold: 2GB
            if not self.critical_space_notified and self.notifier:
                self.notifier.notify_disk_space_critical(
                    str(self.archive_dir), free_gb
                )
                self.critical_space_notified = True
            logger.critical(f"CRITICAL: Only {free_gb:.2f} GB free!")
            return False
        
        # Low space warning
        elif free_gb < self.min_free_space_gb:
            if not self.low_space_notified and self.notifier:
                self.notifier.notify_disk_space_low(
                    str(self.archive_dir), free_gb, used_percent
                )
                self.low_space_notified = True
            logger.warning(f"Low disk space: {free_gb:.2f} GB free")
            return True
        
        else:
            # Reset notification flags when space is OK
            self.low_space_notified = False
            self.critical_space_notified = False
            return True
    
    def _ping_host(self, host: str, count: int = 2) -> bool:
        """Ping a host to check connectivity"""
        try:
            cmd = ['ping', '-c', str(count), '-W', '5', '-q', host]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error pinging {host}: {e}")
            return False
    
    def _check_network_connectivity(self) -> Dict[str, bool]:
        """Check network connectivity to configured hosts"""
        results = {}
        
        for host in self.ping_hosts:
            reachable = self._ping_host(host)
            results[host] = reachable
            
            if not reachable:
                logger.warning(f"Cannot reach {host}")
            else:
                logger.debug(f"Successfully pinged {host}")
        
        return results
    
    def _send_cronitor_ping(self, monitor_name: str, state: str = 'run', 
                           msg: str = '') -> bool:
        """Send ping to Cronitor"""
        if not self.cronitor_enabled:
            return False
        
        monitor_id = self.cronitor_monitors.get(monitor_name)
        if not monitor_id:
            return False
        
        try:
            url = f"{self.cronitor_base}/{monitor_id}"
            params = []
            if state:
                params.append(f"state={state}")
            if msg:
                params.append(f"msg={msg}")
            
            if params:
                url += "?" + "&".join(params)
            
            cmd = ['curl', '-s', '-m', '5', url]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
            
        except Exception as e:
            logger.error(f"Error sending cronitor ping: {e}")
            return False
    
    def _health_check_loop(self):
        """Main health check loop"""
        logger.info("Starting health monitoring loop")
        
        while not self.stop_event.is_set():
            try:
                # Disk space check
                if self.check_disk_space:
                    space_ok = self._check_disk_space_status()
                    disk_info = self._get_disk_usage(self.archive_dir)
                    logger.debug(f"Disk: {disk_info['free_gb']:.2f} GB free ({disk_info['used_percent']:.1f}% used)")
                
                # Check camera recorders
                for recorder in self.recorders:
                    status = recorder.get_status()
                    if status['enabled']:
                        if status['healthy']:
                            logger.debug(f"Camera {status['name']}: Healthy")
                        else:
                            logger.warning(f"Camera {status['name']}: Unhealthy (failures: {status['consecutive_failures']})")
                
                # Check sync status
                if self.sync_manager:
                    sync_status = self.sync_manager.get_status()
                    if sync_status['enabled']:
                        logger.debug(f"Sync: {sync_status['status']}, pending: {sync_status['pending_files']}")
                
                # Network connectivity check
                if self.ping_hosts:
                    connectivity = self._check_network_connectivity()
                    online_count = sum(1 for v in connectivity.values() if v)
                    logger.debug(f"Network: {online_count}/{len(connectivity)} hosts reachable")
                
                # Send health ping to Cronitor
                self._send_cronitor_ping('health', 'complete')
                
            except Exception as e:
                logger.error(f"Error in health check loop: {e}", exc_info=True)
            
            # Wait for next check
            self.stop_event.wait(self.check_interval)
        
        logger.info("Health monitoring loop stopped")
    
    def start(self):
        """Start health monitoring"""
        if self.is_running:
            logger.warning("Health monitor already running")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.thread = Thread(target=self._health_check_loop, name="health-monitor")
        self.thread.daemon = True
        self.thread.start()
        logger.info("Started health monitoring thread")
    
    def stop(self):
        """Stop health monitoring"""
        if not self.is_running:
            return
        
        logger.info("Stopping health monitor")
        self.is_running = False
        self.stop_event.set()
        
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Health monitor thread did not stop gracefully")
        
        logger.info("Health monitor stopped")
    
    def get_health_report(self) -> dict:
        """Generate comprehensive health report"""
        # Camera status
        cameras_status = {}
        for recorder in self.recorders:
            status = recorder.get_status()
            cameras_status[status['name']] = {
                'online': status['online'],
                'status': 'Online' if status['online'] else f"Offline ({status['consecutive_failures']} failures)"
            }
        
        # Disk info
        disk_info = self._get_disk_usage(self.archive_dir)
        
        # Sync status
        sync_status = {}
        if self.sync_manager:
            sync_status = self.sync_manager.get_status()
        
        return {
            'cameras_status': cameras_status,
            'disk_info': disk_info,
            'sync_status': sync_status
        }
