"""
PathoCam Clone - Manual Whole Slide Imaging System
Phiên bản 7.0 - Image Registration để ghép ảnh chính xác

Thuật toán:
1. Position tracking (rough) - ước lượng vị trí
2. Image registration (precise) - tìm vị trí chính xác bằng template matching với canvas

Author: AI Assistant
"""

import sys
import cv2
import numpy as np
from typing import Optional, Tuple
import time

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QGridLayout, QComboBox, QSpinBox,
    QMessageBox, QFileDialog
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QImage, QPixmap


# ============================================================================
# STITCHING CANVAS - Ghép ảnh với Image Registration
# ============================================================================

class StitchingCanvas:
    """
    Canvas với image registration để ghép ảnh chính xác.
    Mỗi tile mới được match với canvas để tìm vị trí chính xác.
    """
    
    def __init__(self):
        # Main canvas
        self.canvas = None
        self.canvas_gray = None  # Grayscale version for matching
        
        # Canvas offset for negative coordinates
        self.offset_x = 0
        self.offset_y = 0
        
        # Current position estimate
        self.current_x = 0.0
        self.current_y = 0.0
        
        # Last tile info for tracking
        self.last_tile_gray = None
        self.last_tile_pos = (0, 0)
        
        # Bounds
        self.min_x = 0
        self.max_x = 0  
        self.min_y = 0
        self.max_y = 0
        
        # Stats
        self.tile_count = 0
        
        # Settings
        self.overlap_margin = 100  # Pixels to search for overlap
        
    def reset(self):
        self.canvas = None
        self.canvas_gray = None
        self.offset_x = 0
        self.offset_y = 0
        self.current_x = 0.0
        self.current_y = 0.0
        self.last_tile_gray = None
        self.last_tile_pos = (0, 0)
        self.min_x = self.max_x = 0
        self.min_y = self.max_y = 0
        self.tile_count = 0
        
    def _ensure_size(self, x: int, y: int, w: int, h: int):
        """Ensure canvas is large enough"""
        if self.canvas is None:
            # Initial size must be large enough for 5MP frames (2560x1920)
            # Plus room for expansion in all directions
            size = 8000
            self.canvas = np.zeros((size, size, 3), dtype=np.uint8)
            self.canvas_gray = np.zeros((size, size), dtype=np.uint8)
            self.offset_x = size // 2
            self.offset_y = size // 2
            return
            
        cx = x + self.offset_x
        cy = y + self.offset_y
        ch, cw = self.canvas.shape[:2]
        
        margin = 500
        if cx < margin or cy < margin or cx + w > cw - margin or cy + h > ch - margin:
            new_size = max(cw, ch) + 2000
            new_canvas = np.zeros((new_size, new_size, 3), dtype=np.uint8)
            new_gray = np.zeros((new_size, new_size), dtype=np.uint8)
            
            ox = (new_size - cw) // 2
            oy = (new_size - ch) // 2
            
            new_canvas[oy:oy+ch, ox:ox+cw] = self.canvas
            new_gray[oy:oy+ch, ox:ox+cw] = self.canvas_gray
            
            self.canvas = new_canvas
            self.canvas_gray = new_gray
            self.offset_x += ox
            self.offset_y += oy
            
    def _find_best_position(self, tile: np.ndarray, rough_x: int, rough_y: int) -> Tuple[int, int]:
        """
        Tìm vị trí chính xác bằng template matching với canvas.
        """
        if self.canvas is None or self.tile_count == 0:
            return rough_x, rough_y
            
        tile_h, tile_w = tile.shape[:2]
        
        # Convert tile to grayscale
        if len(tile.shape) == 3:
            tile_gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
        else:
            tile_gray = tile
            
        # Define search region on canvas (around rough position)
        search_margin = 150  # Search +/- 150 pixels from rough estimate
        
        cx = rough_x + self.offset_x
        cy = rough_y + self.offset_y
        
        # Search region bounds
        search_x1 = max(0, cx - search_margin)
        search_y1 = max(0, cy - search_margin)
        search_x2 = min(self.canvas.shape[1], cx + tile_w + search_margin)
        search_y2 = min(self.canvas.shape[0], cy + tile_h + search_margin)
        
        search_region = self.canvas_gray[search_y1:search_y2, search_x1:search_x2]
        
        if search_region.shape[0] < tile_h or search_region.shape[1] < tile_w:
            return rough_x, rough_y
            
        # Check if search region has content (not empty)
        if np.max(search_region) < 10:
            return rough_x, rough_y
            
        try:
            # Use template matching to find best position
            # Use a smaller template from center of tile for speed
            margin = tile_h // 4
            template = tile_gray[margin:-margin, margin:-margin] if margin > 0 else tile_gray
            
            if template.shape[0] < 50 or template.shape[1] < 50:
                template = tile_gray
                
            result = cv2.matchTemplate(search_region, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            
            # Only use result if confidence is high enough
            if max_val > 0.3:
                # Calculate offset from template margin
                if margin > 0 and template.shape == tile_gray[margin:-margin, margin:-margin].shape:
                    best_x = search_x1 + max_loc[0] - margin - self.offset_x
                    best_y = search_y1 + max_loc[1] - margin - self.offset_y
                else:
                    best_x = search_x1 + max_loc[0] - self.offset_x
                    best_y = search_y1 + max_loc[1] - self.offset_y
                    
                # Sanity check - don't allow huge jumps from rough estimate
                if abs(best_x - rough_x) < search_margin and abs(best_y - rough_y) < search_margin:
                    return best_x, best_y
                    
        except Exception as e:
            pass
            
        return rough_x, rough_y
        
    def add_tile(self, tile: np.ndarray, dx: float = 0, dy: float = 0) -> bool:
        """
        Add tile to canvas.
        dx, dy: displacement from last position (from tracker)
        """
        tile_h, tile_w = tile.shape[:2]
        
        # First tile - place at origin
        if self.tile_count == 0:
            self._ensure_size(0, 0, tile_w, tile_h)
            
            cx = self.offset_x
            cy = self.offset_y
            
            self.canvas[cy:cy+tile_h, cx:cx+tile_w] = tile
            
            tile_gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY) if len(tile.shape) == 3 else tile
            self.canvas_gray[cy:cy+tile_h, cx:cx+tile_w] = tile_gray
            
            self.last_tile_gray = tile_gray.copy()
            self.last_tile_pos = (0, 0)
            self.current_x = 0
            self.current_y = 0
            
            self.min_x = 0
            self.max_x = tile_w
            self.min_y = 0
            self.max_y = tile_h
            
            self.tile_count = 1
            return True
            
        # Subsequent tiles - use tracking + registration
        
        # Update rough position from tracker
        rough_x = int(self.current_x + dx)
        rough_y = int(self.current_y + dy)
        
        # Find precise position using image registration
        precise_x, precise_y = self._find_best_position(tile, rough_x, rough_y)
        
        # Update current position
        self.current_x = precise_x
        self.current_y = precise_y
        
        # Ensure canvas size
        self._ensure_size(precise_x, precise_y, tile_w, tile_h)
        
        # Place tile
        cx = precise_x + self.offset_x
        cy = precise_y + self.offset_y
        
        # Bounds check
        if cx < 0 or cy < 0:
            return False
        if cx + tile_w > self.canvas.shape[1] or cy + tile_h > self.canvas.shape[0]:
            return False
            
        # Simple placement (overwrite)
        self.canvas[cy:cy+tile_h, cx:cx+tile_w] = tile
        
        tile_gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY) if len(tile.shape) == 3 else tile
        self.canvas_gray[cy:cy+tile_h, cx:cx+tile_w] = tile_gray
        
        # Update state
        self.last_tile_gray = tile_gray.copy()
        self.last_tile_pos = (precise_x, precise_y)
        
        # Update bounds
        self.min_x = min(self.min_x, precise_x)
        self.max_x = max(self.max_x, precise_x + tile_w)
        self.min_y = min(self.min_y, precise_y)
        self.max_y = max(self.max_y, precise_y + tile_h)
        
        self.tile_count += 1
        return True
        
    def get_position(self) -> Tuple[float, float]:
        """Get current position"""
        return self.current_x, self.current_y
        
    def get_canvas(self) -> Optional[np.ndarray]:
        if self.canvas is None or self.tile_count == 0:
            return None
            
        x1 = max(0, self.min_x + self.offset_x)
        y1 = max(0, self.min_y + self.offset_y)
        x2 = min(self.canvas.shape[1], self.max_x + self.offset_x)
        y2 = min(self.canvas.shape[0], self.max_y + self.offset_y)
        
        if x2 <= x1 or y2 <= y1:
            return None
            
        return self.canvas[y1:y2, x1:x2].copy()


# ============================================================================
# SIMPLE TRACKER - Chỉ để ước lượng hướng di chuyển
# ============================================================================

class SimpleTracker:
    """Tracker đơn giản để ước lượng dx, dy giữa các frame"""
    
    def __init__(self):
        self.prev_gray = None
        
    def reset(self):
        self.prev_gray = None
        
    def get_displacement(self, frame: np.ndarray) -> Tuple[float, float]:
        """Tính displacement từ frame trước"""
        
        # Downscale for speed
        small = cv2.resize(frame, (320, 240))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small
        
        dx, dy = 0.0, 0.0
        
        if self.prev_gray is not None:
            try:
                # Phase correlation
                shift, response = cv2.phaseCorrelate(
                    np.float32(self.prev_gray),
                    np.float32(gray)
                )
                
                # Scale back to original size
                scale_x = frame.shape[1] / 320
                scale_y = frame.shape[0] / 240
                
                dx = -shift[0] * scale_x
                dy = -shift[1] * scale_y
                
                # Filter noise
                if abs(dx) < 5:
                    dx = 0
                if abs(dy) < 5:
                    dy = 0
                    
            except:
                pass
                
        self.prev_gray = gray.copy()
        return dx, dy


# ============================================================================
# CAMERA SETTINGS - Euromex CMEX-5f DC.5000f
# ============================================================================

# Camera resolution presets
CAMERA_RESOLUTIONS = {
    "5MP (2560x1920)": (2560, 1920, 50),   # Full resolution
    "3MP (2048x1536)": (2048, 1536, 50),   # Good balance
    "2MP (1600x1200)": (1600, 1200, 50),   # Higher FPS
    "HD (1280x720)": (1280, 720, 50),       # Standard HD
}

# Euromex DC.5000f specifications
CAMERA_SPECS = {
    "sensor": "CMOS 1/2.8 inch",
    "pixels": "2560 x 1920 (5.0 Mpix)",
    "pixel_size_um": 2.0,  # 2.0 μm x 2.0 μm
    "color_depth": 24,  # bits
    "interface": "USB 2.0",
}


# ============================================================================
# CAMERA THREAD
# ============================================================================

class CameraThread(QThread):
    frame_ready = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    
    def __init__(self, index: int = 0, resolution: str = "5MP (2560x1920)"):
        super().__init__()
        self.index = index
        self.resolution = resolution
        self.running = False
        self.actual_resolution = (0, 0)
        
    def run(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.index)
            
        if not cap.isOpened():
            self.error.emit("Không thể mở camera!")
            return
        
        # Get resolution settings
        res = CAMERA_RESOLUTIONS.get(self.resolution, (2560, 1920, 30))
        width, height, fps = res
        
        # Apply camera settings for Euromex DC.5000f
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        # Optimize image quality settings
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)  # Disable autofocus if available
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)  # Manual exposure mode
        
        # Get actual resolution
        self.actual_resolution = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        )
        
        self.running = True
        
        # Adjust sleep based on target FPS
        sleep_ms = max(20, int(1000 / fps) - 5)
        
        while self.running:
            ret, frame = cap.read()
            if ret:
                self.frame_ready.emit(frame)
            self.msleep(sleep_ms)
            
        cap.release()
        
    def stop(self):
        self.running = False
        self.wait(2000)


# ============================================================================
# MAIN WINDOW
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.canvas = StitchingCanvas()
        self.tracker = SimpleTracker()
        self.camera = None
        
        self.scanning = False
        self.capture_interval = 15  # Capture every N frames
        self.frame_counter = 0
        
        # Accumulated displacement
        self.accum_dx = 0.0
        self.accum_dy = 0.0
        
        # Stats
        self.fps_counter = 0
        self.last_fps_time = time.time()
        self.fps = 0.0
        
        self.setup_ui()
        
    def setup_ui(self):
        self.setWindowTitle("PathoCam Clone v7.0 - Image Registration")
        self.setMinimumSize(1200, 750)
        self.setStyleSheet("""
            QMainWindow { background-color: #1a1b26; }
            QGroupBox {
                color: #c0caf5;
                font-weight: bold;
                border: 2px solid #3b4261;
                border-radius: 8px;
                margin-top: 12px;
                padding: 8px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; }
            QPushButton {
                background-color: #3b4261;
                color: #c0caf5;
                border: none;
                border-radius: 6px;
                padding: 10px 18px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #545c7e; }
            QPushButton:disabled { background-color: #24283b; color: #565f89; }
            QLabel { color: #c0caf5; }
            QComboBox, QSpinBox {
                background-color: #24283b;
                color: #c0caf5;
                border: 1px solid #3b4261;
                border-radius: 4px;
                padding: 5px;
            }
        """)
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # === LEFT PANEL ===
        left = QWidget()
        left.setFixedWidth(360)
        left_layout = QVBoxLayout(left)
        
        # Camera
        cam_group = QGroupBox("📷 Camera (Euromex DC.5000f)")
        cam_layout = QGridLayout(cam_group)
        
        cam_layout.addWidget(QLabel("Camera:"), 0, 0)
        self.cam_combo = QComboBox()
        self.cam_combo.addItems([f"Camera {i}" for i in range(5)])
        cam_layout.addWidget(self.cam_combo, 0, 1)
        
        cam_layout.addWidget(QLabel("Resolution:"), 1, 0)
        self.res_combo = QComboBox()
        self.res_combo.addItems(list(CAMERA_RESOLUTIONS.keys()))
        self.res_combo.setCurrentIndex(0)  # Default: 5MP
        cam_layout.addWidget(self.res_combo, 1, 1)
        
        self.connect_btn = QPushButton("🔌 Kết nối Camera")
        self.connect_btn.clicked.connect(self.toggle_camera)
        cam_layout.addWidget(self.connect_btn, 2, 0, 1, 2)
        
        # Resolution info label
        self.res_info_label = QLabel("Sensor: CMOS 1/2.8\" | Pixel: 2.0μm")
        self.res_info_label.setStyleSheet("font-size: 10px; color: #7aa2f7;")
        cam_layout.addWidget(self.res_info_label, 3, 0, 1, 2)
        
        left_layout.addWidget(cam_group)
        
        # Live View
        live_group = QGroupBox("🎥 Live View")
        live_layout = QVBoxLayout(live_group)
        
        self.live_label = QLabel("Camera chưa kết nối")
        self.live_label.setFixedSize(340, 255)
        self.live_label.setAlignment(Qt.AlignCenter)
        self.live_label.setStyleSheet("background-color: #15161e; border-radius: 6px;")
        live_layout.addWidget(self.live_label)
        
        left_layout.addWidget(live_group)
        
        # Controls
        ctrl_group = QGroupBox("⚡ Điều khiển")
        ctrl_layout = QGridLayout(ctrl_group)
        
        self.start_btn = QPushButton("▶ Bắt đầu quét")
        self.start_btn.setStyleSheet("background-color: #9ece6a; color: #1a1b26;")
        self.start_btn.clicked.connect(self.start_scan)
        self.start_btn.setEnabled(False)
        ctrl_layout.addWidget(self.start_btn, 0, 0)
        
        self.stop_btn = QPushButton("⏹ Dừng")
        self.stop_btn.setStyleSheet("background-color: #f7768e; color: #1a1b26;")
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setEnabled(False)
        ctrl_layout.addWidget(self.stop_btn, 0, 1)
        
        self.reset_btn = QPushButton("🔄 Reset")
        self.reset_btn.clicked.connect(self.reset_all)
        ctrl_layout.addWidget(self.reset_btn, 1, 0)
        
        self.save_btn = QPushButton("💾 Lưu ảnh")
        self.save_btn.clicked.connect(self.save_result)
        ctrl_layout.addWidget(self.save_btn, 1, 1)
        
        left_layout.addWidget(ctrl_group)
        
        # Settings
        set_group = QGroupBox("⚙️ Cài đặt")
        set_layout = QGridLayout(set_group)
        
        set_layout.addWidget(QLabel("Capture interval:"), 0, 0)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 60)
        self.interval_spin.setValue(15)
        self.interval_spin.setSuffix(" frames")
        self.interval_spin.valueChanged.connect(lambda v: setattr(self, 'capture_interval', v))
        set_layout.addWidget(self.interval_spin, 0, 1)
        
        left_layout.addWidget(set_group)
        
        # Info
        info_group = QGroupBox("ℹ️ Hướng dẫn")
        info_layout = QVBoxLayout(info_group)
        info_label = QLabel(
            "1. Di chuyển CHẬM và ĐỀU\n"
            "2. Mỗi tile phải overlap ~50%\n"
            "   với tile trước\n"
            "3. Hệ thống sẽ tự động match\n"
            "   và ghép ảnh chính xác"
        )
        info_label.setStyleSheet("font-size: 11px;")
        info_layout.addWidget(info_label)
        left_layout.addWidget(info_group)
        
        # Stats
        stat_group = QGroupBox("📊 Thống kê")
        stat_layout = QVBoxLayout(stat_group)
        self.stat_label = QLabel("Tiles: 0\nPosition: (0, 0)\nFPS: 0")
        self.stat_label.setStyleSheet("font-family: Consolas; font-size: 12px;")
        stat_layout.addWidget(self.stat_label)
        left_layout.addWidget(stat_group)
        
        left_layout.addStretch()
        
        # === RIGHT PANEL ===
        right = QWidget()
        right_layout = QVBoxLayout(right)
        
        canvas_group = QGroupBox("🖼️ Canvas - Ghép ảnh tự động")
        canvas_layout = QVBoxLayout(canvas_group)
        
        self.canvas_label = QLabel("Di chuyển bàn kính để quét")
        self.canvas_label.setAlignment(Qt.AlignCenter)
        self.canvas_label.setMinimumSize(750, 620)
        self.canvas_label.setStyleSheet("background-color: #15161e; border-radius: 6px;")
        canvas_layout.addWidget(self.canvas_label)
        
        right_layout.addWidget(canvas_group)
        
        layout.addWidget(left)
        layout.addWidget(right, 1)
        
        # Timers
        self.canvas_timer = QTimer()
        self.canvas_timer.timeout.connect(self.update_canvas)
        self.canvas_timer.setInterval(100)
        
        self.stat_timer = QTimer()
        self.stat_timer.timeout.connect(self.update_stats)
        self.stat_timer.setInterval(500)
        
    def toggle_camera(self):
        if self.camera is None:
            self.connect_camera()
        else:
            self.disconnect_camera()
            
    def connect_camera(self):
        resolution = self.res_combo.currentText()
        self.camera = CameraThread(self.cam_combo.currentIndex(), resolution)
        self.camera.frame_ready.connect(self.on_frame)
        self.camera.error.connect(lambda m: QMessageBox.critical(self, "Lỗi", m))
        self.camera.start()
        
        self.connect_btn.setText("🔌 Ngắt kết nối")
        self.connect_btn.setStyleSheet("background-color: #e0af68; color: #1a1b26;")
        self.start_btn.setEnabled(True)
        
        # Disable resolution change while connected
        self.res_combo.setEnabled(False)
        self.cam_combo.setEnabled(False)
        
        self.canvas_timer.start()
        self.stat_timer.start()
        
    def disconnect_camera(self):
        if self.camera:
            self.camera.stop()
            self.camera = None
            
        self.connect_btn.setText("🔌 Kết nối Camera")
        self.connect_btn.setStyleSheet("background-color: #3b4261;")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        
        # Re-enable resolution change
        self.res_combo.setEnabled(True)
        self.cam_combo.setEnabled(True)
        
        self.canvas_timer.stop()
        self.stat_timer.stop()
        
    def on_frame(self, frame: np.ndarray):
        """Process camera frame"""
        self.fps_counter += 1
        
        # Track displacement
        dx, dy = self.tracker.get_displacement(frame)
        self.accum_dx += dx
        self.accum_dy += dy
        
        # Capture tile at interval
        if self.scanning:
            self.frame_counter += 1
            
            if self.frame_counter >= self.capture_interval:
                # Add tile with accumulated displacement
                self.canvas.add_tile(frame, self.accum_dx, self.accum_dy)
                
                # Reset accumulators
                self.accum_dx = 0.0
                self.accum_dy = 0.0
                self.frame_counter = 0
                
        # Update live view
        display = cv2.resize(frame, (340, 255))
        
        # Info overlay
        pos = self.canvas.get_position()
        cv2.putText(display, f"Pos: ({pos[0]:.0f}, {pos[1]:.0f})", (5, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(display, f"Tiles: {self.canvas.tile_count}", (5, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        if self.scanning:
            # Progress bar for next capture
            progress = self.frame_counter / self.capture_interval
            bar_w = int(100 * progress)
            cv2.rectangle(display, (5, 245), (5 + bar_w, 252), (0, 255, 0), -1)
            cv2.rectangle(display, (5, 245), (105, 252), (100, 100, 100), 1)
            
            cv2.putText(display, "● SCANNING", (120, 252),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            
        h, w = display.shape[:2]
        qimg = QImage(display.data, w, h, 3 * w, QImage.Format_RGB888).rgbSwapped()
        self.live_label.setPixmap(QPixmap.fromImage(qimg))
        
    def update_canvas(self):
        result = self.canvas.get_canvas()
        if result is not None:
            h, w = result.shape[:2]
            
            lw = self.canvas_label.width() - 10
            lh = self.canvas_label.height() - 10
            scale = min(lw / w, lh / h, 1.0)
            
            if scale < 1.0:
                result = cv2.resize(result, (int(w * scale), int(h * scale)))
                h, w = result.shape[:2]
                
            qimg = QImage(result.data, w, h, 3 * w, QImage.Format_RGB888).rgbSwapped()
            self.canvas_label.setPixmap(QPixmap.fromImage(qimg))
            
    def update_stats(self):
        now = time.time()
        elapsed = now - self.last_fps_time
        self.fps = self.fps_counter / elapsed if elapsed > 0 else 0
        self.fps_counter = 0
        self.last_fps_time = now
        
        pos = self.canvas.get_position()
        self.stat_label.setText(
            f"Tiles: {self.canvas.tile_count}\n"
            f"Position: ({pos[0]:.0f}, {pos[1]:.0f})\n"
            f"FPS: {self.fps:.1f}"
        )
        
    def start_scan(self):
        self.scanning = True
        self.frame_counter = self.capture_interval  # Capture first tile immediately
        self.accum_dx = 0.0
        self.accum_dy = 0.0
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        
    def stop_scan(self):
        self.scanning = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        
    def reset_all(self):
        self.canvas.reset()
        self.tracker.reset()
        self.accum_dx = 0.0
        self.accum_dy = 0.0
        self.frame_counter = 0
        self.canvas_label.setText("Di chuyển bàn kính để quét")
        
    def save_result(self):
        result = self.canvas.get_canvas()
        if result is None:
            QMessageBox.warning(self, "Cảnh báo", "Không có dữ liệu!")
            return
            
        path, _ = QFileDialog.getSaveFileName(
            self, "Lưu", f"scan_{time.strftime('%H%M%S')}.png", "PNG (*.png)"
        )
        if path:
            cv2.imwrite(path, result)
            QMessageBox.information(self, "OK", f"Đã lưu: {path}")
            
    def closeEvent(self, event):
        self.disconnect_camera()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
