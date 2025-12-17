#!/usr/bin/env python3
"""
Cloud sync module with exponential backoff and rate limiting
"""

import subprocess
import time
import logging
from pathlib import Path
from typing import Optional, List
from threading import Thread, Event
from datetime import datetime

logger = logging.getLogger(__name__)


class CloudSync:
    def __init__(self, cloud_config: dict, storage_config: dict, notifier=None):
        self.enabled = cloud_config.get('enabled', True)
        self.remote_name = cloud_config['remote_name']
        self.remote_path = cloud_config['remote_path']
        self.sync_interval = cloud_config.get('sync_interval_seconds', 120)
        self.min_file_age = cloud_config.get('min_file_age_seconds', 60)
        
        # Retry configuration
        self.max_retries = cloud_config.get('max_retries', 3)
        self.retry_backoff = cloud_config.get('retry_backoff_seconds', 60)
        self.backoff_multiplier = cloud_config.get('backoff_multiplier', 2)
        self.max_backoff = cloud_config.get('max_backoff_seconds', 3600)
        
        # Cloud retention
        self.cloud_retention_days = cloud_config.get('cloud_retention_days', 5)
        
        # Storage paths
        self.archive_dir = Path(storage_config['archive_dir'])
        
        # State management
        self.is_running = False
        self.stop_event = Event()
        self.thread: Optional[Thread] = None
        self.notifier = notifier
        
        # Track failures and backoff
        self.consecutive_failures = 0
        self.current_backoff = self.retry_backoff
        self.last_sync_time = 0
        self.last_success_time = 0
        self.files_synced_total = 0
        
        # Rate limiting for rclone API calls
        self.min_time_between_syncs = 60  # Don't sync more than once per minute
        
    def _run_rclone(self, args: List[str], timeout: int = 600) -> tuple[bool, str, str]:
        """
        Run rclone command with timeout and error handling
        
        Returns:
            (success, stdout, stderr)
        """
        cmd = ['rclone'] + args
        
        try:
            logger.debug(f"Running rclone: {' '.join(args[:5])}...")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                success = process.returncode == 0
                
                if not success:
                    logger.error(f"rclone failed with code {process.returncode}: {stderr[:500]}")
                
                return success, stdout, stderr
                
            except subprocess.TimeoutExpired:
                logger.error(f"rclone timeout after {timeout}s")
                process.kill()
                stdout, stderr = process.communicate()
                return False, stdout, f"Timeout after {timeout}s"
                
        except Exception as e:
            logger.error(f"Error running rclone: {e}", exc_info=True)
            return False, "", str(e)
    
    def _sync_to_cloud(self) -> tuple[bool, int]:
        """
        Sync local archive to cloud
        
        Returns:
            (success, files_synced_count)
        """
        if not self.archive_dir.exists():
            logger.warning(f"Archive directory does not exist: {self.archive_dir}")
            return True, 0
        
        # Build rclone command
        # Using copy instead of sync to be safer (won't delete from destination)
        # Using --max-age to only sync recent files (avoids incomplete files)
        max_age = f"{self.min_file_age}s"
        
        args = [
            'copy',
            '--min-age', max_age,
            '--transfers', '4',  # Limit concurrent transfers
            '--checkers', '2',   # Limit concurrent checks
            '--retries', '3',
            '--low-level-retries', '3',
            '--stats', '0',      # Disable stats output
            '--no-update-modtime',
            str(self.archive_dir),
            f"{self.remote_name}:{self.remote_path}"
        ]
        
        success, stdout, stderr = self._run_rclone(args, timeout=600)
        
        # Try to count synced files from output
        files_synced = 0
        if stdout:
            # rclone doesn't output count by default in copy
            # We'll count files in archive as estimate
            try:
                files = list(self.archive_dir.rglob('*'))
                files_synced = len([f for f in files if f.is_file()])
            except:
                pass
        
        return success, files_synced
    
    def _cleanup_cloud_old_files(self) -> bool:
        """Delete files older than retention period from cloud"""
        if self.cloud_retention_days <= 0:
            return True
        
        max_age = f"{self.cloud_retention_days}d"
        
        args = [
            'delete',
            '--min-age', max_age,
            '--drive-use-trash=false',  # Permanent delete
            '--retries', '2',
            f"{self.remote_name}:{self.remote_path}"
        ]
        
        success, stdout, stderr = self._run_rclone(args, timeout=300)
        
        if success:
            logger.info(f"Cleaned up cloud files older than {self.cloud_retention_days} days")
        
        # Clean up empty directories
        args_rmdirs = [
            'rmdirs',
            f"{self.remote_name}:{self.remote_path}",
            '--leave-root'
        ]
        
        self._run_rclone(args_rmdirs, timeout=60)
        
        return success
    
    def _sync_loop(self):
        """Main sync loop with exponential backoff"""
        logger.info("Starting cloud sync loop")
        
        # Do initial cleanup
        try:
            self._cleanup_cloud_old_files()
        except Exception as e:
            logger.error(f"Error in initial cloud cleanup: {e}")
        
        while not self.stop_event.is_set():
            try:
                # Calculate wait time based on failures
                if self.consecutive_failures > 0:
                    wait_time = min(self.current_backoff, self.max_backoff)
                    logger.info(f"Backing off for {wait_time}s after {self.consecutive_failures} failures")
                else:
                    wait_time = self.sync_interval
                
                # Wait for interval or stop event
                if self.stop_event.wait(wait_time):
                    break
                
                # Rate limiting check
                time_since_last = time.time() - self.last_sync_time
                if time_since_last < self.min_time_between_syncs:
                    logger.debug(f"Rate limiting: waiting {self.min_time_between_syncs - time_since_last:.0f}s")
                    continue
                
                self.last_sync_time = time.time()
                
                # Perform sync
                logger.info("Starting cloud sync")
                success, files_synced = self._sync_to_cloud()
                
                if success:
                    logger.info(f"Cloud sync successful (~{files_synced} files)")
                    self.last_success_time = time.time()
                    self.files_synced_total += files_synced
                    
                    # Notify if recovering from failure
                    if self.consecutive_failures > 0 and self.notifier:
                        self.notifier.notify_sync_success(files_synced)
                    
                    # Reset backoff
                    self.consecutive_failures = 0
                    self.current_backoff = self.retry_backoff
                    
                    # Periodic cleanup (every 10 successful syncs)
                    if self.files_synced_total % 10 == 0:
                        try:
                            self._cleanup_cloud_old_files()
                        except Exception as e:
                            logger.error(f"Error in cloud cleanup: {e}")
                
                else:
                    # Sync failed
                    self.consecutive_failures += 1
                    
                    # Notify about failure
                    if self.notifier:
                        self.notifier.notify_sync_error(
                            f"Failed to sync to {self.remote_name}",
                            self.consecutive_failures
                        )
                    
                    # Calculate new backoff
                    self.current_backoff = min(
                        self.current_backoff * self.backoff_multiplier,
                        self.max_backoff
                    )
                    
                    logger.error(f"Sync failed, next retry in {self.current_backoff}s")
                
            except Exception as e:
                logger.error(f"Error in sync loop: {e}", exc_info=True)
                self.consecutive_failures += 1
                
                if self.notifier:
                    self.notifier.notify_sync_error(str(e), self.consecutive_failures)
        
        logger.info("Cloud sync loop stopped")
    
    def start(self):
        """Start sync in background thread"""
        if not self.enabled:
            logger.info("Cloud sync disabled in config")
            return
        
        if self.is_running:
            logger.warning("Sync already running")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.thread = Thread(target=self._sync_loop, name="cloud-sync")
        self.thread.daemon = True
        self.thread.start()
        logger.info("Started cloud sync thread")
    
    def stop(self):
        """Stop sync gracefully"""
        if not self.is_running:
            return
        
        logger.info("Stopping cloud sync")
        self.is_running = False
        self.stop_event.set()
        
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning("Sync thread did not stop gracefully")
        
        logger.info("Cloud sync stopped")
    
    def get_status(self) -> dict:
        """Get current sync status"""
        # Count pending files
        pending_files = 0
        try:
            if self.archive_dir.exists():
                pending_files = len(list(self.archive_dir.rglob('*')))
        except:
            pass
        
        last_sync = "Never"
        if self.last_success_time > 0:
            last_sync = datetime.fromtimestamp(self.last_success_time).strftime('%Y-%m-%d %H:%M:%S')
        
        return {
            'enabled': self.enabled,
            'running': self.is_running,
            'status': 'OK' if self.consecutive_failures == 0 else f'Failing ({self.consecutive_failures} failures)',
            'last_sync': last_sync,
            'pending_files': pending_files,
            'total_synced': self.files_synced_total,
            'consecutive_failures': self.consecutive_failures,
            'current_backoff': self.current_backoff
        }
