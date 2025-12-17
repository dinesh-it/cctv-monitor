#!/usr/bin/env python3
"""
Camera recorder module with proper resource management and error handling
"""

import subprocess
import time
import logging
import os
import signal
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
from threading import Thread, Event

logger = logging.getLogger(__name__)


class CameraRecorder:
    def __init__(self, camera_config: dict, storage_config: dict, notifier=None):
        self.cronitor_config = None
        self.name = camera_config['name']
        self.rtsp_url = camera_config['rtsp_url']
        self.enabled = camera_config.get('enabled', True)
        self.segment_duration = camera_config.get('segment_duration', 900)
        self.connection_timeout = camera_config.get('connection_timeout', 30)
        self.retry_delay = camera_config.get('retry_delay', 60)
        self.max_failures = camera_config.get('max_failures', 3)
        self.extended_retry_delay = camera_config.get('extended_retry_delay', 300)
        
        # Video settings
        self.video_codec = camera_config.get('video_codec', 'copy')
        self.audio_codec = camera_config.get('audio_codec', 'copy')
        self.format = camera_config.get('format', 'mkv')
        self.extra_opts = camera_config.get('ffmpeg_extra_opts', '')
        
        # Storage paths
        self.temp_dir = Path(storage_config['temp_dir']) / self.name
        self.archive_dir = Path(storage_config['archive_dir'])
        self.incomplete_dir = Path(storage_config['incomplete_dir'])
        
        # Create directories
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.incomplete_dir.mkdir(parents=True, exist_ok=True)
        
        # State management
        self.process: Optional[subprocess.Popen] = None
        self.is_running = False
        self.stop_event = Event()
        self.thread: Optional[Thread] = None
        self.consecutive_failures = 0
        self.last_failure_time = 0
        self.last_success_time = time.time()
        self.notifier = notifier
        self.was_online = True
        
    def _get_ffmpeg_command(self) -> list:
        """Build ffmpeg command with all options"""
        output_pattern = str(self.temp_dir / f"%Y-%m-%d_%H-%M-%S.{self.format}")
        
        cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-rtsp_transport', 'tcp',
            '-timeout', str(self.connection_timeout * 1000000),  # microseconds
            '-use_wallclock_as_timestamps', '1',
            '-i', self.rtsp_url,
            '-vcodec', self.video_codec,
            '-acodec', self.audio_codec,
            '-f', 'segment',
            '-reset_timestamps', '1',
            '-segment_time', str(self.segment_duration),
            '-segment_format', self.format,
            '-segment_atclocktime', '1',
            '-strftime', '1'
        ]
        
        # Add extra options if specified
        if self.extra_opts:
            cmd.extend(self.extra_opts.split())
        
        cmd.append(output_pattern)
        
        return cmd
    
    def _move_completed_files(self):
        """Move completed video segments to archive directory"""
        try:
            # Get current date for directory
            date_dir = self.archive_dir / datetime.now().strftime('%Y-%m-%d') / self.name
            date_dir.mkdir(parents=True, exist_ok=True)
            
            # Find files not modified in last 30 seconds (completed segments)
            current_time = time.time()
            for file_path in self.temp_dir.glob(f"*.{self.format}"):
                if current_time - file_path.stat().st_mtime > 30:
                    dest = date_dir / file_path.name
                    try:
                        file_path.rename(dest)
                        logger.debug(f"Moved {file_path.name} to {date_dir}")
                    except Exception as e:
                        logger.error(f"Failed to move {file_path.name}: {e}")
        
        except Exception as e:
            logger.error(f"Error moving completed files for {self.name}: {e}")
    
    def _check_recording_health(self) -> bool:
        """Check if recording is healthy based on file creation"""
        try:
            files = sorted(self.temp_dir.glob(f"*.{self.format}"), 
                         key=lambda p: p.stat().st_mtime, reverse=True)
            
            if not files:
                # No files yet, check if we just started
                if time.time() - self.last_success_time < 60:
                    return True
                return False
            
            latest_file = files[0]
            age = time.time() - latest_file.stat().st_mtime
            
            # File should be updated within segment_duration + grace period
            if age < self.segment_duration + 60:
                return True
            
            logger.warning(f"Latest file for {self.name} is {age:.0f}s old")
            return False
            
        except Exception as e:
            logger.error(f"Error checking recording health for {self.name}: {e}")
            return False
    
    def _record_loop(self):
        """Main recording loop"""
        logger.info(f"Starting recording loop for {self.name}")
        
        while not self.stop_event.is_set() and self.enabled:
            try:
                # Check if we should wait due to failures
                if self.consecutive_failures >= self.max_failures:
                    delay = self.extended_retry_delay
                    logger.warning(f"{self.name}: {self.consecutive_failures} failures, waiting {delay}s")
                    if self.stop_event.wait(delay):
                        break
                    self.consecutive_failures = 0  # Reset after extended wait
                elif self.consecutive_failures > 0:
                    delay = self.retry_delay
                    logger.info(f"{self.name}: Waiting {delay}s before retry")
                    if self.stop_event.wait(delay):
                        break
                
                # Start ffmpeg process
                cmd = self._get_ffmpeg_command()
                logger.info(f"Starting ffmpeg for {self.name}")
                logger.debug(f"Command: {' '.join(cmd[:15])}...")  # Don't log full URL with credentials
                
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=os.setsid  # Create new process group
                )
                
                # Monitor process
                check_interval = 30
                last_health_check = time.time()
                
                while not self.stop_event.is_set():
                    # Check if process is still running
                    retcode = self.process.poll()
                    
                    if retcode is not None:
                        # Process ended
                        stderr = self.process.stderr.read().decode('utf-8', errors='ignore')
                        logger.error(f"{self.name}: ffmpeg exited with code {retcode}")
                        if stderr:
                            logger.error(f"{self.name}: {stderr[:500]}")
                        
                        self.consecutive_failures += 1
                        self.last_failure_time = time.time()
                        
                        # Notify if camera went offline
                        if self.notifier and self.was_online:
                            self.notifier.notify_camera_offline(self.name, stderr[:200])
                            self.was_online = False
                        
                        break
                    
                    # Periodic health check
                    if time.time() - last_health_check > check_interval:
                        if self._check_recording_health():
                            self._send_cronitor("complete", "Recording_healthy")
                            if self.consecutive_failures > 0:
                                logger.info(f"{self.name}: Recording healthy, resetting failure count")
                                self.consecutive_failures = 0
                                
                                # Notify camera back online
                                if self.notifier and not self.was_online:
                                    self.notifier.notify_camera_online(self.name)
                                    self.was_online = True
                            
                            self.last_success_time = time.time()
                            self._move_completed_files()
                        else:
                            logger.warning(f"{self.name}: Recording appears unhealthy")
                            self._send_cronitor("fail", "Recording_unhealthy")
                        
                        last_health_check = time.time()
                    
                    # Wait a bit before next check
                    if self.stop_event.wait(5):
                        break
                
            except Exception as e:
                logger.error(f"Error in recording loop for {self.name}: {e}", exc_info=True)
                self.consecutive_failures += 1
                self.last_failure_time = time.time()
                
                if self.notifier and self.was_online:
                    self.notifier.notify_camera_offline(self.name, str(e))
                    self.was_online = False
            
            finally:
                # Ensure process is terminated
                if self.process:
                    try:
                        # Kill process group to ensure all child processes are terminated
                        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                        self.process.wait(timeout=5)
                    except Exception as e:
                        logger.warning(f"Error terminating ffmpeg for {self.name}: {e}")
                        try:
                            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                        except:
                            pass
                    
                    self.process = None
        
        logger.info(f"Recording loop stopped for {self.name}")
    
    def start(self):
        """Start recording in background thread"""
        if self.is_running:
            logger.warning(f"{self.name}: Already running")
            return
        
        if not self.enabled:
            logger.info(f"{self.name}: Disabled in config")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.thread = Thread(target=self._record_loop, name=f"recorder-{self.name}")
        self.thread.daemon = True
        self.thread.start()
        logger.info(f"Started recorder thread for {self.name}")
    
    def stop(self):
        """Stop recording gracefully"""
        if not self.is_running:
            return
        
        logger.info(f"Stopping recorder for {self.name}")
        self.is_running = False
        self.stop_event.set()
        
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning(f"{self.name}: Thread did not stop gracefully")
        
        # Final cleanup of completed files
        self._move_completed_files()
        
        logger.info(f"Recorder stopped for {self.name}")
    
    def get_status(self) -> dict:
        """Get current status of recorder"""
        is_healthy = self._check_recording_health()
        
        return {
            'name': self.name,
            'enabled': self.enabled,
            'running': self.is_running,
            'healthy': is_healthy,
            'online': is_healthy and self.consecutive_failures == 0,
            'consecutive_failures': self.consecutive_failures,
            'last_success': self.last_success_time,
            'last_failure': self.last_failure_time
        }
    def _send_cronitor(self, state='run', msg=''):
        """Send Cronitor heartbeat for recorder"""
        if not self.cronitor_config or not self.cronitor_config.get('enabled'):
            return
        
        monitor_id = self.cronitor_config.get('monitors', {}).get('recorder')
        if not monitor_id:
            return
        
        try:
            import subprocess
            url = f"{self.cronitor_config['base_url']}/{monitor_id}"
            params = []
            if state:
                params.append(f"state={state}")
            if msg:
                params.append(f"msg={msg.replace(' ', '_')}")
            if params:
                url += "?" + "&".join(params)
            
            subprocess.run(['curl', '-s', '-m', '3', url], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass


