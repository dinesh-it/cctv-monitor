#!/usr/bin/env python3
"""
CCTV Monitor - Main orchestrator
Reliable multi-camera CCTV monitoring with cloud sync and Telegram notifications
"""

import sys
import signal
import logging
import time
import yaml
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Add modules directory to path
sys.path.insert(0, str(Path(__file__).parent / 'modules'))

from telegram import TelegramNotifier
from recorder import CameraRecorder
from sync import CloudSync
from health import HealthMonitor
from cleanup import CleanupManager


class CCTVMonitor:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self._setup_logging()
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("="*60)
        self.logger.info("CCTV Monitor Starting")
        self.logger.info("="*60)
        
        # Initialize components
        self.notifier = None
        self.recorders = []
        self.sync_manager = None
        self.health_monitor = None
        self.cleanup_manager = None
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        self._initialize_components()
    
    def _load_config(self) -> dict:
        """Load and validate configuration"""
        if not self.config_path.exists():
            print(f"ERROR: Config file not found: {self.config_path}")
            print("Please copy config.example.yaml to config.yaml and update it.")
            sys.exit(1)
        
        try:
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Validate required sections
            required_sections = ['telegram', 'storage', 'cameras']
            for section in required_sections:
                if section not in config:
                    print(f"ERROR: Missing required config section: {section}")
                    sys.exit(1)
            
            # Validate at least one camera is enabled
            if not any(cam.get('enabled', True) for cam in config['cameras']):
                print("WARNING: No cameras are enabled in config")
            
            return config
            
        except yaml.YAMLError as e:
            print(f"ERROR: Failed to parse config file: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"ERROR: Failed to load config: {e}")
            sys.exit(1)
    
    def _setup_logging(self):
        """Setup logging with rotation"""
        log_config = self.config.get('logging', {})
        log_level = log_config.get('level', 'INFO')
        log_dir = Path(log_config.get('log_dir', 'logs'))
        max_size_mb = log_config.get('max_log_size_mb', 100)
        backup_count = log_config.get('backup_count', 5)
        console_logging = log_config.get('console', True)
        
        # Create log directory
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, log_level))
        
        # Remove existing handlers
        root_logger.handlers = []
        
        # File handler with rotation
        log_file = log_dir / 'cctv_monitor.log'
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count
        )
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
        
        # Console handler
        if console_logging:
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
            console_handler.setFormatter(console_formatter)
            root_logger.addHandler(console_handler)
    
    def _initialize_components(self):
        """Initialize all monitoring components"""
        try:
            # Initialize Telegram notifier
            telegram_config = self.config['telegram']
            self.notifier = TelegramNotifier(
                bot_token=telegram_config['bot_token'],
                chat_id=telegram_config['chat_id'],
                rate_limit_seconds=telegram_config.get('rate_limit_seconds', 300)
            )
            self.logger.info("Telegram notifier initialized")
            
            # Initialize camera recorders
            storage_config = self.config['storage']
            for camera_config in self.config['cameras']:
                if camera_config.get("enabled", True):
                    recorder = CameraRecorder(
                        camera_config=camera_config,
                        storage_config=storage_config,
                        notifier=self.notifier
                    )
                    if "monitoring" in self.config and "cronitor" in self.config["monitoring"]:
                        recorder.cronitor_config = self.config["monitoring"]["cronitor"]
                    self.recorders.append(recorder)
                    self.logger.info(f"Initialized recorder for camera: {camera_config['name']}")
                    self.logger.info(f"Initialized recorder for camera: {camera_config['name']}")
            
            # Initialize cloud sync
            if 'cloud' in self.config:
                self.sync_manager = CloudSync(
                    cloud_config=self.config['cloud'],
                    storage_config=storage_config,
                    notifier=self.notifier
                )
                self.logger.info("Cloud sync manager initialized")
            
            # Initialize cleanup manager
            self.cleanup_manager = CleanupManager(storage_config=storage_config)
            self.logger.info("Cleanup manager initialized")
            
            # Initialize health monitor
            if 'monitoring' in self.config:
                self.health_monitor = HealthMonitor(
                    monitoring_config=self.config['monitoring'],
                    storage_config=storage_config,
                    recorders=self.recorders,
                    sync_manager=self.sync_manager,
                    notifier=self.notifier
                )
                self.logger.info("Health monitor initialized")
            
            self.logger.info("All components initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize components: {e}", exc_info=True)
            sys.exit(1)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        signal_name = 'SIGINT' if signum == signal.SIGINT else 'SIGTERM'
        self.logger.info(f"Received {signal_name}, initiating graceful shutdown...")
        self.stop()
        sys.exit(0)
    
    def start(self):
        """Start all monitoring components"""
        try:
            self.logger.info("Starting all components...")
            
            # Start camera recorders
            for recorder in self.recorders:
                recorder.start()
            self.logger.info(f"Started {len(self.recorders)} camera recorder(s)")
            
            # Start cloud sync
            if self.sync_manager:
                self.sync_manager.start()
            
            # Start cleanup manager
            if self.cleanup_manager:
                self.cleanup_manager.start()
            
            # Start health monitor
            if self.health_monitor:
                self.health_monitor.start()
            
            self.logger.info("All components started successfully")
            
            # Send startup notification
            if self.notifier:
                text = "🚀 <b>CCTV Monitor Started</b>\n"
                text += f"Cameras: {len(self.recorders)}\n"
                text += f"Cloud sync: {'Enabled' if self.sync_manager and self.sync_manager.enabled else 'Disabled'}\n"
                text += f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                self.notifier.send_message(text, message_key="startup", force=True)
            
        except Exception as e:
            self.logger.error(f"Error starting components: {e}", exc_info=True)
            self.stop()
            sys.exit(1)
    
    def stop(self):
        """Stop all monitoring components gracefully"""
        # Signal handler and run()'s finally both call stop(); only run once
        if getattr(self, '_stopped', False):
            return
        self._stopped = True
        self.logger.info("Stopping all components...")
        
        # Stop health monitor first
        if self.health_monitor:
            self.health_monitor.stop()
        
        # Stop recorders
        for recorder in self.recorders:
            recorder.stop()
        
        # Stop sync manager
        if self.sync_manager:
            self.sync_manager.stop()
        
        # Stop cleanup manager
        if self.cleanup_manager:
            self.cleanup_manager.stop()
        
        self.logger.info("All components stopped")
        
        # Send shutdown notification
        if self.notifier:
            text = "🛑 <b>CCTV Monitor Stopped</b>\n"
            text += f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            self.notifier.send_message(text, message_key="shutdown", force=True)
    
    def run(self):
        """Main run loop"""
        self.start()
        
        try:
            self.logger.info("CCTV Monitor is running. Press Ctrl+C to stop.")
            
            # Keep running and log status periodically
            while True:
                time.sleep(300)  # Status update every 5 minutes
                
                # Log status
                self.logger.info("--- Status Update ---")
                for recorder in self.recorders:
                    status = recorder.get_status()
                    self.logger.info(f"Camera {status['name']}: {'Online' if status['online'] else 'Offline'}")
                
                if self.sync_manager:
                    sync_status = self.sync_manager.get_status()
                    self.logger.info(f"Cloud Sync: {sync_status['status']}")
                
        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt")
        finally:
            self.stop()


def main():
    """Main entry point"""
    # Determine config file path
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        # Default to config.yaml in same directory as script
        config_path = Path(__file__).parent / 'config.yaml'
    
    # Create and run monitor
    monitor = CCTVMonitor(str(config_path))
    monitor.run()


if __name__ == '__main__':
    main()
