#!/usr/bin/env python3
"""
Cleanup module for managing local storage
"""

import logging
import time
from pathlib import Path
from typing import Optional
from threading import Thread, Event
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class CleanupManager:
    def __init__(self, storage_config: dict):
        self.temp_dir = Path(storage_config['temp_dir'])
        self.archive_dir = Path(storage_config['archive_dir'])
        self.incomplete_dir = Path(storage_config['incomplete_dir'])
        self.local_retention_hours = storage_config.get('local_retention_hours', 6)
        
        # Cleanup intervals
        self.incomplete_cleanup_interval = 300  # 5 minutes
        self.archive_cleanup_interval = 600     # 10 minutes
        
        # State
        self.is_running = False
        self.stop_event = Event()
        self.thread: Optional[Thread] = None
        
        # Statistics
        self.files_cleaned = 0
        self.bytes_freed = 0
        
    def _cleanup_incomplete_files(self) -> int:
        """
        Move incomplete files from temp directories to incomplete directory
        Returns number of files moved
        """
        moved_count = 0
        
        try:
            # Find all temp directories (one per camera)
            if not self.temp_dir.exists():
                return 0
            
            for camera_dir in self.temp_dir.iterdir():
                if not camera_dir.is_dir():
                    continue
                
                # Move files older than 2 minutes (likely incomplete/abandoned)
                cutoff_time = time.time() - 120
                
                for file_path in camera_dir.glob('*.*'):
                    try:
                        if file_path.stat().st_mtime < cutoff_time:
                            # File hasn't been modified in 2+ minutes, likely incomplete
                            dest = self.incomplete_dir / f"{camera_dir.name}_{file_path.name}"
                            
                            # Ensure destination directory exists
                            self.incomplete_dir.mkdir(parents=True, exist_ok=True)
                            
                            file_path.rename(dest)
                            moved_count += 1
                            logger.info(f"Moved incomplete file: {file_path.name}")
                            
                    except Exception as e:
                        logger.error(f"Error moving incomplete file {file_path}: {e}")
            
            return moved_count
            
        except Exception as e:
            logger.error(f"Error in incomplete file cleanup: {e}")
            return moved_count
    
    def _cleanup_old_archive_files(self) -> tuple[int, int]:
        """
        Delete archive files older than retention period
        Returns (files_deleted, bytes_freed)
        """
        deleted_count = 0
        bytes_freed = 0
        
        try:
            if not self.archive_dir.exists():
                return 0, 0
            
            # Calculate cutoff time
            cutoff_time = time.time() - (self.local_retention_hours * 3600)
            
            # Find and delete old files
            for file_path in self.archive_dir.rglob('*.*'):
                try:
                    if file_path.is_file() and file_path.stat().st_mtime < cutoff_time:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                        bytes_freed += file_size
                        logger.debug(f"Deleted old file: {file_path.name}")
                        
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")
            
            # Remove empty directories
            self._remove_empty_directories(self.archive_dir)
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old files, freed {bytes_freed / (1024**2):.2f} MB")
            
            return deleted_count, bytes_freed
            
        except Exception as e:
            logger.error(f"Error in archive cleanup: {e}")
            return deleted_count, bytes_freed
    
    def _cleanup_old_incomplete_files(self) -> tuple[int, int]:
        """
        Delete very old incomplete files (older than 24 hours)
        Returns (files_deleted, bytes_freed)
        """
        deleted_count = 0
        bytes_freed = 0
        
        try:
            if not self.incomplete_dir.exists():
                return 0, 0
            
            # Delete incomplete files older than 24 hours
            cutoff_time = time.time() - (24 * 3600)
            
            for file_path in self.incomplete_dir.glob('*.*'):
                try:
                    if file_path.stat().st_mtime < cutoff_time:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                        bytes_freed += file_size
                        
                except Exception as e:
                    logger.error(f"Error deleting incomplete file {file_path}: {e}")
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old incomplete files")
            
            return deleted_count, bytes_freed
            
        except Exception as e:
            logger.error(f"Error in incomplete file cleanup: {e}")
            return deleted_count, bytes_freed
    
    def _remove_empty_directories(self, base_path: Path):
        """Recursively remove empty directories"""
        try:
            for item in sorted(base_path.rglob('*'), reverse=True):
                if item.is_dir() and not any(item.iterdir()):
                    try:
                        item.rmdir()
                        logger.debug(f"Removed empty directory: {item}")
                    except Exception as e:
                        logger.debug(f"Could not remove directory {item}: {e}")
        except Exception as e:
            logger.error(f"Error removing empty directories: {e}")
    
    def _cleanup_loop(self):
        """Main cleanup loop"""
        logger.info("Starting cleanup loop")
        
        last_incomplete_cleanup = 0
        last_archive_cleanup = 0
        
        while not self.stop_event.is_set():
            try:
                current_time = time.time()
                
                # Cleanup incomplete files
                if current_time - last_incomplete_cleanup >= self.incomplete_cleanup_interval:
                    moved = self._cleanup_incomplete_files()
                    if moved > 0:
                        logger.info(f"Moved {moved} incomplete files")
                    last_incomplete_cleanup = current_time
                
                # Cleanup old archive files
                if current_time - last_archive_cleanup >= self.archive_cleanup_interval:
                    deleted, freed = self._cleanup_old_archive_files()
                    self.files_cleaned += deleted
                    self.bytes_freed += freed
                    
                    # Also cleanup very old incomplete files
                    inc_deleted, inc_freed = self._cleanup_old_incomplete_files()
                    self.files_cleaned += inc_deleted
                    self.bytes_freed += inc_freed
                    
                    last_archive_cleanup = current_time
                
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
            
            # Wait before next iteration
            self.stop_event.wait(60)  # Check every minute
        
        logger.info("Cleanup loop stopped")
    
    def start(self):
        """Start cleanup manager"""
        if self.is_running:
            logger.warning("Cleanup manager already running")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.thread = Thread(target=self._cleanup_loop, name="cleanup-manager")
        self.thread.daemon = True
        self.thread.start()
        logger.info("Started cleanup manager thread")
    
    def stop(self):
        """Stop cleanup manager"""
        if not self.is_running:
            return
        
        logger.info("Stopping cleanup manager")
        self.is_running = False
        self.stop_event.set()
        
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Cleanup thread did not stop gracefully")
        
        logger.info("Cleanup manager stopped")
    
    def force_cleanup(self):
        """Force immediate cleanup"""
        logger.info("Running forced cleanup")
        self._cleanup_incomplete_files()
        deleted, freed = self._cleanup_old_archive_files()
        self.files_cleaned += deleted
        self.bytes_freed += freed
        logger.info(f"Forced cleanup complete: {deleted} files, {freed / (1024**2):.2f} MB freed")
    
    def get_statistics(self) -> dict:
        """Get cleanup statistics"""
        return {
            'files_cleaned': self.files_cleaned,
            'mb_freed': self.bytes_freed / (1024**2)
        }
