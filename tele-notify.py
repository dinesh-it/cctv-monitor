"""
RTSP Motion Detection with Telegram Notifications

This is a STANDALONE motion detection script - separate from the main cctv_monitor.py system.

PURPOSE:
    - Monitors a single RTSP camera stream for motion
    - Records video clips ONLY when motion is detected (saves storage)
    - Sends video clips to Telegram for instant alerts

DIFFERENCE FROM MAIN SYSTEM:
    - Main system (cctv_monitor.py): Continuous 24/7 recording of all cameras
    - This script (tele-notify.py): Motion-triggered recording for instant alerts

USE CASES:
    - Lightweight motion alerts without storing 24/7 footage
    - Testing motion detection before integrating into main system
    - Secondary alert system for high-priority areas

SECURITY WARNING:
    - Remove hardcoded credentials from main() function before committing
    - Use environment variables: RTSP_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID

DEPENDENCIES:
    - opencv-python (cv2)
    - requests
    - numpy

USAGE:
    python3 tele-notify.py
    # or with environment variables:
    export RTSP_URL="rtsp://user:pass@ip:port/stream"
    export TELEGRAM_BOT_TOKEN="your_bot_token"
    export TELEGRAM_USER_ID="your_user_id"
    python3 tele-notify.py
"""

import cv2
import numpy as np
import requests
import time
import threading
from datetime import datetime
import logging
import os
from io import BytesIO
from collections import deque
import tempfile

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RTSPMotionDetector:
    def __init__(self, rtsp_url, bot_token, user_id, sensitivity=25, min_area=500, 
                 video_duration=5, pre_motion_buffer=2):
        """
        Initialize the RTSP motion detector
        
        Args:
            rtsp_url (str): RTSP stream URL
            bot_token (str): Telegram bot token
            user_id (str): Telegram user ID to send messages to
            sensitivity (int): Motion detection sensitivity (lower = more sensitive)
            min_area (int): Minimum area of motion to trigger alert
            video_duration (int): Duration of video to record (seconds)
            pre_motion_buffer (int): Seconds of video to include before motion detected
        """
        self.rtsp_url = rtsp_url
        self.bot_token = bot_token
        self.user_id = user_id
        self.sensitivity = sensitivity
        self.min_area = min_area
        self.video_duration = video_duration
        self.pre_motion_buffer = pre_motion_buffer
        self.telegram_api_url = f"https://api.telegram.org/bot{bot_token}"
        
        # Motion detection variables
        self.background_subtractor = cv2.createBackgroundSubtractorMOG2(
            detectShadows=True, varThreshold=16, history=500
        )
        self.last_motion_time = 0
        self.motion_cooldown = 15  # seconds between motion alerts (increased for video)
        
        # Stream variables
        self.cap = None
        self.running = False
        self.fps = 15  # Target FPS for recording
        
        # Video recording variables
        self.frame_buffer = deque(maxlen=self.fps * self.pre_motion_buffer)  # Pre-motion buffer
        self.is_recording = False
        self.current_writer = None
        self.record_start_time = 0
        
    def connect_to_stream(self):
        """Connect to the RTSP stream"""
        try:
            self.cap = cv2.VideoCapture(self.rtsp_url)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce buffer to minimize delay
            
            if not self.cap.isOpened():
                logger.error("Failed to open RTSP stream")
                return False
            
            logger.info("Successfully connected to RTSP stream")
            return True
            
        except Exception as e:
            logger.error(f"Error connecting to stream: {e}")
            return False
    
    def send_telegram_video(self, video_path, caption="Motion detected!"):
        """Send video to Telegram bot"""
        try:
            # Check if file exists and has content
            if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
                logger.error("Video file is empty or doesn't exist")
                return False
            
            # Telegram has a 50MB limit for videos
            file_size = os.path.getsize(video_path)
            if file_size > 50 * 1024 * 1024:  # 50MB
                logger.error(f"Video file too large: {file_size / (1024*1024):.1f}MB")
                return False
            
            with open(video_path, 'rb') as video_file:
                files = {
                    'video': ('motion_detected.mp4', video_file, 'video/mp4')
                }
                
                data = {
                    'chat_id': self.user_id,
                    'caption': caption,
                    'supports_streaming': True
                }
                
                # Send video to Telegram
                response = requests.post(
                    f"{self.telegram_api_url}/sendVideo",
                    files=files,
                    data=data,
                    timeout=60  # Longer timeout for video upload
                )
                
                if response.status_code == 200:
                    logger.info(f"Video sent successfully to Telegram ({file_size / (1024*1024):.1f}MB)")
                    return True
                else:
                    logger.error(f"Failed to send video: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error sending video to Telegram: {e}")
            return False
        finally:
            # Clean up the temporary file
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
            except Exception as e:
                logger.warning(f"Failed to delete temporary video file: {e}")
    
    def start_recording(self, first_frame):
        """Start recording video"""
        try:
            # Create temporary file for video
            temp_fd, temp_path = tempfile.mkstemp(suffix='.mp4', prefix='motion_')
            os.close(temp_fd)  # Close the file descriptor, we'll use the path
            
            # Get frame dimensions
            height, width = first_frame.shape[:2]
            
            # Initialize video writer
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            self.current_writer = cv2.VideoWriter(temp_path, fourcc, self.fps, (width, height))
            
            if not self.current_writer.isOpened():
                logger.error("Failed to initialize video writer")
                return None
            
            # Write buffered frames (pre-motion frames)
            for buffered_frame in self.frame_buffer:
                self.current_writer.write(buffered_frame)
            
            # Write the current frame
            self.current_writer.write(first_frame)
            
            self.is_recording = True
            self.record_start_time = time.time()
            
            logger.info(f"Started recording video: {temp_path}")
            return temp_path
            
        except Exception as e:
            logger.error(f"Error starting video recording: {e}")
            return None
    
    def stop_recording(self, video_path):
        """Stop recording and return the video file path"""
        try:
            if self.current_writer:
                self.current_writer.release()
                self.current_writer = None
            
            self.is_recording = False
            logger.info("Stopped recording video")
            
            # Check if video file was created successfully
            if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                return video_path
            else:
                logger.error("Video file was not created properly")
                return None
                
        except Exception as e:
            logger.error(f"Error stopping video recording: {e}")
            return None
    
    def detect_motion(self, frame):
        """Detect motion in the frame"""
        try:
            # Convert to grayscale
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Apply background subtraction
            fg_mask = self.background_subtractor.apply(gray)
            
            # Remove noise
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            
            # Find contours
            contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # Check if any contour is large enough to be considered motion
            for contour in contours:
                area = cv2.contourArea(contour)
                if area > self.min_area:
                    return True, fg_mask
                    
            return False, fg_mask
            
        except Exception as e:
            logger.error(f"Error in motion detection: {e}")
            return False, None
    
    def process_frame(self, frame):
        """Process a single frame for motion detection and recording"""
        # Always add frame to buffer for pre-motion recording
        self.frame_buffer.append(frame.copy())
        
        # Continue recording if already recording
        if self.is_recording:
            if self.current_writer:
                self.current_writer.write(frame)
            
            # Check if recording duration has been reached
            if time.time() - self.record_start_time >= self.video_duration:
                temp_path = getattr(self, 'current_video_path', None)
                if temp_path:
                    final_path = self.stop_recording(temp_path)
                    if final_path:
                        # Send video in a separate thread
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        caption = f"Motion detected at {timestamp}"
                        
                        threading.Thread(
                            target=self.send_telegram_video,
                            args=(final_path, caption),
                            daemon=True
                        ).start()
        
        # Check for new motion
        motion_detected, mask = self.detect_motion(frame)
        
        if motion_detected and not self.is_recording:
            current_time = time.time()
            
            # Check if enough time has passed since last motion alert
            if current_time - self.last_motion_time > self.motion_cooldown:
                self.last_motion_time = current_time
                
                logger.info("Motion detected, starting video recording...")
                
                # Start recording
                video_path = self.start_recording(frame)
                if video_path:
                    self.current_video_path = video_path
    
    def run(self):
        """Main loop to monitor the RTSP stream"""
        if not self.connect_to_stream():
            return
        
        self.running = True
        logger.info("Starting motion detection...")
        
        frame_skip = 0
        max_failures = 10
        failure_count = 0
        
        try:
            while self.running:
                ret, frame = self.cap.read()
                
                if not ret:
                    failure_count += 1
                    logger.warning(f"Failed to read frame ({failure_count}/{max_failures})")
                    
                    if failure_count >= max_failures:
                        logger.error("Too many failures, attempting to reconnect...")
                        self.cap.release()
                        time.sleep(5)
                        
                        if not self.connect_to_stream():
                            logger.error("Failed to reconnect, stopping...")
                            break
                        failure_count = 0
                    
                    time.sleep(1)
                    continue
                
                failure_count = 0  # Reset failure count on successful read
                
                # Skip frames to reduce processing load
                frame_skip += 1
                if frame_skip % 3 != 0:  # Process every 3rd frame
                    continue
                
                # Resize frame to reduce processing time
                height, width = frame.shape[:2]
                if width > 640:
                    scale = 640 / width
                    new_width = int(width * scale)
                    new_height = int(height * scale)
                    frame = cv2.resize(frame, (new_width, new_height))
                
                # Process frame for motion detection
                self.process_frame(frame)
                
                # Small delay to prevent excessive CPU usage
                time.sleep(0.1)
                
        except KeyboardInterrupt:
            logger.info("Stopping motion detection...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the motion detection"""
        self.running = False
        
        # Stop any ongoing recording
        if self.is_recording and hasattr(self, 'current_video_path'):
            self.stop_recording(self.current_video_path)
        
        if self.cap:
            self.cap.release()
        logger.info("Motion detection stopped")

def main():
    # Optional: Load from environment variables for security
    RTSP_URL = os.getenv('RTSP_URL', RTSP_URL)
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', BOT_TOKEN)
    USER_ID = os.getenv('TELEGRAM_USER_ID', USER_ID)
    
    # Validate configuration
    if not all([RTSP_URL, BOT_TOKEN, USER_ID]):
        logger.error("Please configure RTSP_URL, BOT_TOKEN, and USER_ID")
        return
    
    if RTSP_URL == "rtsp://username:password@ip_address:port/stream":
        logger.error("Please update RTSP_URL with your actual stream URL")
        return
    
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please update BOT_TOKEN with your actual bot token")
        return
    
    if USER_ID == "YOUR_USER_ID_HERE":
        logger.error("Please update USER_ID with your actual user ID")
        return
    
    # Create and start motion detector
    detector = RTSPMotionDetector(
        rtsp_url=RTSP_URL,
        bot_token=BOT_TOKEN,
        user_id=USER_ID,
        sensitivity=25,        # Adjust as needed
        min_area=500,         # Adjust as needed
        video_duration=5,     # Record 5 seconds of video
        pre_motion_buffer=2   # Include 2 seconds before motion
    )
    
    try:
        detector.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        detector.stop()

if __name__ == "__main__":
    main()
