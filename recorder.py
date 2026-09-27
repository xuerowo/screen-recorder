import os
import sys
import json
import time
import signal
import threading
import datetime
from pathlib import Path
import numpy as np
import win32api
import win32gui
import win32ui
import win32con
import ctypes
import ctypes.wintypes

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", ctypes.wintypes.POINT)]

class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.wintypes.BOOL),
                ("xHotspot", ctypes.wintypes.DWORD),
                ("yHotspot", ctypes.wintypes.DWORD),
                ("hbmMask", ctypes.wintypes.HBITMAP),
                ("hbmColor", ctypes.wintypes.HBITMAP)]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.wintypes.WORD),
                ("biBitCount", ctypes.wintypes.WORD),
                ("biCompression", ctypes.wintypes.DWORD),
                ("biSizeImage", ctypes.wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.wintypes.DWORD),
                ("biClrImportant", ctypes.wintypes.DWORD)]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", ctypes.wintypes.DWORD * 3)]

class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long),
                ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.wintypes.WORD),
                ("bmBitsPixel", ctypes.wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

try:
    # Undocumented but present since Windows XP; used to read animated cursor frames.
    _GetCursorFrameInfo = user32.GetCursorFrameInfo
    _GetCursorFrameInfo.restype = ctypes.c_void_p
    _GetCursorFrameInfo.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                    ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.POINTER(ctypes.wintypes.DWORD)]
except AttributeError:
    _GetCursorFrameInfo = None

user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.DestroyCursor.argtypes = [ctypes.c_void_p]

# System cursor IDs and their value names under HKCU\Control Panel\Cursors.
SYSTEM_CURSOR_NAMES = {
    32512: "Arrow", 32513: "IBeam", 32514: "Wait", 32515: "Crosshair", 32516: "UpArrow",
    32631: "NWPen", 32642: "SizeNWSE", 32643: "SizeNESW", 32644: "SizeWE", 32645: "SizeNS",
    32646: "SizeAll", 32648: "No", 32649: "Hand", 32650: "AppStarting", 32651: "Help",
    32671: "Pin", 32672: "Person",
}

class CursorOverlay:
    """
    Draws the mouse cursor onto captured frames.

    Rendering a cursor through GDI is expensive, so each cursor frame is cached
    per cursor handle and refreshed every CACHE_TTL seconds (system cursors keep
    their handle when the cursor scheme/size changes).

    Cursors come in two kinds, reproduced the same way DrawIconEx does:
    - alpha cursors (32bpp with an alpha channel) are alpha-blended;
    - classic cursors are applied as (screen AND mask) XOR image, which also
      covers inverting pixels (e.g. the classic I-beam).
    Animated cursors (busy spinners, .ani files) are played at their own frame rate.

    Windows shows system cursors at "pointer size" (CursorBaseSize) x display
    scale, while the cursor handle only holds a bitmap at the default size. So
    system cursors are reloaded from the cursor scheme's file at the displayed
    size (or stretched when the scheme has no file).
    """
    MIN_SIZE = 128
    CACHE_TTL = 1.0
    MAX_CACHE_ENTRIES = 256
    JIFFY = 1.0 / 60

    def __init__(self):
        self._cache = {}
        self._frame_timing = {}
        self._system_cursors = None # (timestamp, displayed size, {handle: (file path or None, bitmap size)})
        self._scaled_cursors = {} # system handle -> (file path, size, loaded handle)

    def close(self):
        for _, _, handle in self._scaled_cursors.values():
            user32.DestroyCursor(handle)
        self._scaled_cursors.clear()
        self._cache.clear()
        self._frame_timing.clear()

    @staticmethod
    def _displayed_cursor_size():
        base_size = 32
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors") as key:
                value, _ = winreg.QueryValueEx(key, "CursorBaseSize")
                if isinstance(value, int) and 16 <= value <= 512:
                    base_size = value
        except OSError:
            pass
        try:
            dpi = user32.GetDpiForSystem()
        except AttributeError:
            dpi = 96
        return max(1, round(base_size * (dpi or 96) / 96))

    def _get_system_cursors(self, now):
        state = self._system_cursors
        if state is not None and now - state[0] <= self.CACHE_TTL:
            return state[1], state[2]

        paths = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors") as key:
                for name in SYSTEM_CURSOR_NAMES.values():
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value:
                        path = os.path.expandvars(value)
                        if os.path.isfile(path):
                            paths[name] = path
        except OSError:
            pass

        handles = {}
        hdc = win32gui.GetDC(0)
        try:
            for cursor_id, name in SYSTEM_CURSOR_NAMES.items():
                handle = user32.LoadCursorW(None, ctypes.c_void_p(cursor_id))
                if handle:
                    _, _, width, height, _ = self._read_icon_info(hdc, handle)
                    handles[handle] = (paths.get(name), max(width, height))
        finally:
            win32gui.ReleaseDC(0, hdc)

        size = self._displayed_cursor_size()
        self._system_cursors = (now, size, handles)
        return size, handles

    def _resolve_render_source(self, hcursor, now):
        """
        Returns (handle to render, forced draw size or 0). Cursors that are not
        system cursors, or are already at the displayed size, are drawn as-is.
        """
        size, handles = self._get_system_cursors(now)
        source = handles.get(hcursor)
        if source is None:
            return hcursor, 0
        path, bitmap_size = source
        if bitmap_size == size:
            return hcursor, 0
        if path is None:
            return hcursor, size

        scaled = self._scaled_cursors.get(hcursor)
        if scaled is None or scaled[0] != path or scaled[1] != size:
            if scaled is not None:
                user32.DestroyCursor(scaled[2])
                del self._scaled_cursors[hcursor]
                # The destroyed handle value may be reused, so drop anything rendered from it.
                self._cache.clear()
                self._frame_timing.clear()
            loaded = user32.LoadImageW(None, path, 2, size, size, 0x10) # IMAGE_CURSOR, LR_LOADFROMFILE
            if not loaded:
                return hcursor, size
            scaled = (path, size, loaded)
            self._scaled_cursors[hcursor] = scaled
        return scaled[2], 0

    @staticmethod
    def _bitmap_info(hbitmap):
        bm = BITMAP()
        if not gdi32.GetObjectW(ctypes.c_void_p(hbitmap), ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        return bm

    @staticmethod
    def _dib_info(width, height):
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height # Top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        return bmi

    def _read_icon_info(self, hdc, hcursor):
        """Returns (hotspot_x, hotspot_y, width, height, has_alpha)."""
        icon_info = ICONINFO()
        if not user32.GetIconInfo(ctypes.c_void_p(hcursor), ctypes.byref(icon_info)):
            return 0, 0, 0, 0, True
        width = height = 0
        has_alpha = False
        try:
            if icon_info.hbmColor:
                bm = self._bitmap_info(icon_info.hbmColor)
                if bm:
                    width, height = bm.bmWidth, abs(bm.bmHeight)
                    if bm.bmBitsPixel == 32 and width > 0 and height > 0:
                        buffer = ctypes.create_string_buffer(width * height * 4)
                        bmi = self._dib_info(width, height)
                        if gdi32.GetDIBits(hdc, ctypes.c_void_p(icon_info.hbmColor), 0, height, buffer, ctypes.byref(bmi), 0):
                            pixels = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
                            has_alpha = bool(pixels[..., 3].any())
            elif icon_info.hbmMask:
                # Monochrome cursor: the mask holds the AND and XOR bitmaps stacked vertically.
                bm = self._bitmap_info(icon_info.hbmMask)
                if bm:
                    width, height = bm.bmWidth, abs(bm.bmHeight) // 2
        finally:
            if icon_info.hbmMask:
                gdi32.DeleteObject(ctypes.c_void_p(icon_info.hbmMask))
            if icon_info.hbmColor:
                gdi32.DeleteObject(ctypes.c_void_p(icon_info.hbmColor))
        return icon_info.xHotspot, icon_info.yHotspot, width, height, has_alpha

    def _render_cursor(self, hcursor, step=0, draw_size=0):
        hdc = win32gui.GetDC(0)
        try:
            hotspot_x, hotspot_y, width, height, has_alpha = self._read_icon_info(hdc, hcursor)
            if draw_size and width and height:
                hotspot_x = round(hotspot_x * draw_size / width)
                hotspot_y = round(hotspot_y * draw_size / height)
            size_x = size_y = max(self.MIN_SIZE, width, height, draw_size)
            hdc_mem = win32gui.CreateCompatibleDC(hdc)
            try:
                hbitmap = win32gui.CreateCompatibleBitmap(hdc, size_x, size_y)
                try:
                    old_bmp = win32gui.SelectObject(hdc_mem, hbitmap)
                    bmi = self._dib_info(size_x, size_y)

                    def draw_on(stock_brush):
                        win32gui.FillRect(hdc_mem, (0, 0, size_x, size_y), win32gui.GetStockObject(stock_brush))
                        win32gui.DrawIconEx(hdc_mem, 0, 0, hcursor, draw_size, draw_size, step, 0, 3) # DI_NORMAL
                        buffer = ctypes.create_string_buffer(size_x * size_y * 4)
                        gdi32.GetDIBits(hdc, int(hbitmap), 0, size_y, buffer, ctypes.byref(bmi), 0)
                        return np.frombuffer(buffer, dtype=np.uint8).reshape((size_y, size_x, 4))[..., :3][..., ::-1].astype(np.int32)

                    # 1. Draw on Black background, 2. Draw on White background
                    img_black = draw_on(4) # BLACK_BRUSH
                    img_white = draw_on(0) # WHITE_BRUSH
                    win32gui.SelectObject(hdc_mem, old_bmp)
                finally:
                    win32gui.DeleteObject(hbitmap)
            finally:
                win32gui.DeleteDC(hdc_mem)
        finally:
            win32gui.ReleaseDC(0, hdc)

        if has_alpha:
            # 3. Calculate Alpha
            alpha = 255 - (img_white - img_black)
            alpha = np.mean(alpha, axis=2)
            alpha = np.clip(alpha, 0, 255).astype(np.uint8)
            # Only pixels with alpha > 1% are drawn.
            visible = alpha > 2.55
        else:
            # Black background shows the XOR image; white background shows AND ^ XOR.
            and_bits = (img_white ^ img_black).astype(np.uint8)
            xor_bits = img_black.astype(np.uint8)
            visible = ((and_bits != 255) | (xor_bits != 0)).any(axis=2)

        # Crop to the visible bounding box.
        rows = np.flatnonzero(visible.any(axis=1))
        cols = np.flatnonzero(visible.any(axis=0))
        if rows.size == 0:
            return None
        top, bottom = rows[0], rows[-1] + 1
        left, right = cols[0], cols[-1] + 1
        box = (slice(top, bottom), slice(left, right))
        cursor = {
            "offset_x": int(left) - int(hotspot_x),
            "offset_y": int(top) - int(hotspot_y),
            "mask": visible[box][..., None],
            "has_alpha": has_alpha,
        }
        if has_alpha:
            cursor["inv_alpha"] = (1.0 - alpha[box] / 255.0)[..., None]
            cursor["rgb"] = np.clip(img_black, 0, 255).astype(np.uint8)[box]
        else:
            cursor["and_bits"] = and_bits[box]
            cursor["xor_bits"] = xor_bits[box]
        return cursor

    def _get_frame_timing(self, hcursor, now):
        """Returns the per-frame durations (seconds) of an animated cursor, or None."""
        entry = self._frame_timing.get(hcursor)
        if entry is None or now - entry[0] > self.CACHE_TTL:
            durations = None
            if _GetCursorFrameInfo is not None:
                rate = ctypes.wintypes.DWORD()
                steps = ctypes.wintypes.DWORD()
                try:
                    if _GetCursorFrameInfo(hcursor, 0, 0, ctypes.byref(rate), ctypes.byref(steps)) and steps.value > 1:
                        durations = []
                        for step in range(steps.value):
                            _GetCursorFrameInfo(hcursor, 0, step, ctypes.byref(rate), ctypes.byref(steps))
                            durations.append(rate.value * self.JIFFY)
                        if sum(durations) <= 0:
                            durations = None
                except Exception:
                    durations = None
            if len(self._frame_timing) >= self.MAX_CACHE_ENTRIES:
                self._frame_timing.clear()
            entry = (now, durations)
            self._frame_timing[hcursor] = entry
        return entry[1]

    def _current_step(self, hcursor, now):
        durations = self._get_frame_timing(hcursor, now)
        if not durations:
            return 0
        t = now % sum(durations)
        for step, duration in enumerate(durations):
            if t < duration:
                return step
            t -= duration
        return len(durations) - 1

    def _get_cursor(self, hcursor):
        now = time.monotonic()
        render_handle, draw_size = self._resolve_render_source(hcursor, now)
        key = (render_handle, self._current_step(render_handle, now), draw_size)
        entry = self._cache.get(key)
        if entry is None or now - entry[0] > self.CACHE_TTL:
            if len(self._cache) >= self.MAX_CACHE_ENTRIES:
                self._cache.clear()
            entry = (now, self._render_cursor(*key))
            self._cache[key] = entry
        return entry[1]

    def draw(self, img_array):
        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not (user32.GetCursorInfo(ctypes.byref(info)) and info.flags == 1):
            return img_array
        x, y = info.ptScreenPos.x, info.ptScreenPos.y
        try:
            cursor = self._get_cursor(info.hCursor)
            if cursor is None:
                return img_array

            h, w = img_array.shape[:2]
            x0 = x + cursor["offset_x"]
            y0 = y + cursor["offset_y"]
            ch, cw = cursor["mask"].shape[:2]
            # Clip the cursor box to the frame.
            src_x0, src_y0 = max(0, -x0), max(0, -y0)
            src_x1, src_y1 = min(cw, w - x0), min(ch, h - y0)
            if src_x0 >= src_x1 or src_y0 >= src_y1:
                return img_array

            src = (slice(src_y0, src_y1), slice(src_x0, src_x1))
            region = img_array[y0 + src_y0:y0 + src_y1, x0 + src_x0:x0 + src_x1]
            if cursor["has_alpha"]:
                drawn = cursor["rgb"][src] + region * cursor["inv_alpha"][src]
                drawn = np.clip(drawn, 0, 255).astype(np.uint8)
            else:
                drawn = (region & cursor["and_bits"][src]) ^ cursor["xor_bits"][src]
            np.copyto(region, drawn, where=cursor["mask"][src])
        except Exception as e:
            print(f"Fallback cursor due to: {e}")
            cv2.circle(img_array, (x, y), 5, (0, 0, 255), -1)
        return img_array

# Monkey-patch numpy fromstring for older soundcard versions
if not hasattr(np, '_old_fromstring'):
    np._old_fromstring = np.fromstring
    def _patched_fromstring(*args, **kwargs):
        try:
            return np.frombuffer(*args, **kwargs).copy()
        except TypeError:
            return np._old_fromstring(*args, **kwargs)
    np.fromstring = _patched_fromstring

import dxcam
import comtypes
import cv2
import soundcard as sc
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="soundcard")
try:
    from soundcard import SoundcardRuntimeWarning
    warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
except ImportError:
    pass
import av
from fractions import Fraction
import winreg

# Enable DPI awareness (fixes cursor position and resolution issues on high-DPI displays)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1) # 1 = Process_System_DPI_Aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorder.log")
RETENTION_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
RUNNING = True
shutdown_complete_event = threading.Event()
single_instance_mutex = None

def load_config():
    default_config = {
        "fps": 30, 
        "resolution": {"width": 1920, "height": 1080}, 
        "mic_volume": 1.0, 
        "sys_volume": 1.0, 
        "start_on_boot": True, 
        "auto_pause": True, 
        "idle_threshold": 5.0, 
        "silence_threshold": 0.01,
        "retention_days": 0
    }
    if not os.path.exists(CONFIG_FILE):
        safe_log(f"Config file not found at {CONFIG_FILE}, using defaults.")
        return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # Ensure all keys exist by merging with defaults
            merged_config = default_config.copy()
            for k, v in config.items():
                if k == "resolution" and isinstance(v, dict):
                    merged_config[k].update(v)
                else:
                    merged_config[k] = v
            safe_log(f"Config loaded successfully: {merged_config['resolution']['width']}x{merged_config['resolution']['height']} @ {merged_config['fps']}fps")
            return merged_config
    except Exception as e:
        safe_log(f"Error loading config: {e}. Using defaults.")
        return default_config

def quote_windows_arg(value):
    escaped_value = str(value).replace('"', r'\"')
    return f'"{escaped_value}"'

def get_startup_command():
    cmd_path = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
    if getattr(sys, "frozen", False):
        app_command = quote_windows_arg(os.path.abspath(sys.executable))
        return f'{quote_windows_arg(cmd_path)} /k "{app_command}"'

    python_path = sys.executable
    if os.path.basename(python_path).lower() == "pythonw.exe":
        python_console_path = os.path.join(os.path.dirname(python_path), "python.exe")
        if os.path.exists(python_console_path):
            python_path = python_console_path

    script_path = os.path.abspath(__file__)
    app_command = f"{quote_windows_arg(python_path)} {quote_windows_arg(script_path)}"
    return f'{quote_windows_arg(cmd_path)} /k "{app_command}"'

def setup_startup(enable):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "AutoScreenRecorder"
    startup_command = get_startup_command()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, startup_command)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Failed to configure startup: {e}")

def acquire_single_instance_lock():
    global single_instance_mutex
    mutex_name = r"Local\AutoScreenRecorder"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.GetLastError.restype = ctypes.wintypes.DWORD

    single_instance_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if not single_instance_mutex:
        safe_log("Failed to create single-instance lock.")
        return True
    if kernel32.GetLastError() == 183:
        safe_log("Another recorder instance is already running. Exiting.")
        return False
    return True

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
        sys.stdout.flush()
    except Exception:
        pass

def safe_log(message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] [LOG] {message}"
    safe_print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except Exception:
        pass

def get_output_filepath(ext=".mkv"):
    now = datetime.datetime.now()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    folder_path = os.path.join(base_dir, "Recordings", f"{now.year}", f"{now.month:02d}", f"{now.day:02d}")
    os.makedirs(folder_path, exist_ok=True)
    filename = now.strftime("%Y%m%d_%H%M%S") + ext
    return os.path.join(folder_path, filename)

def remux_mkv_to_mp4(mkv_path):
    mp4_path = mkv_path.rsplit('.', 1)[0] + '.mp4'
    safe_log(f"Remuxing (轉檔) {mkv_path} -> {mp4_path}...")
    
    import shutil
    import subprocess
    if shutil.which("ffmpeg"):
        try:
            # We use subprocess.Popen to capture output and show a simple progress indicator
            # or just let it show its own stats by not hiding stderr.
            # But let's try to keep it clean.
            cmd = ["ffmpeg", "-y", "-i", mkv_path, "-c", "copy", mp4_path, "-stats", "-loglevel", "info"]
            subprocess.run(cmd, check=True)
            os.remove(mkv_path)
            safe_log(f"Remux completed successfully via ffmpeg: {mp4_path}")
            return
        except Exception as e:
            safe_log(f"ffmpeg remux failed: {e}. Falling back to PyAV...")
            
    try:
        with av.open(mkv_path, 'r') as in_container:
            duration = in_container.duration
            with av.open(mp4_path, 'w') as out_container:
                out_streams = []
                for in_stream in in_container.streams:
                    out_stream = out_container.add_stream_from_template(in_stream)
                    if in_stream.type == 'video':
                        out_stream.codec_context.codec_tag = 'avc1'
                    elif in_stream.type == 'audio':
                        out_stream.codec_context.codec_tag = 'mp4a'
                    out_streams.append(out_stream)
                
                last_reported_progress = -1
                try:
                    for packet in in_container.demux():
                        if packet.pts is None:
                            continue
                        if packet.dts is None:
                            packet.dts = packet.pts
                        packet.stream = out_streams[packet.stream.index]
                        out_container.mux(packet)
                        
                        # Calculate and display progress
                        if duration and duration > 0:
                            current_time_sec = float(packet.pts * packet.stream.time_base)
                            total_duration_sec = duration / 1000000.0
                            progress = int((current_time_sec / total_duration_sec) * 100)
                            if progress > last_reported_progress:
                                sys.stdout.write(f"\r[LOG] Remuxing progress: {progress}%")
                                sys.stdout.flush()
                                last_reported_progress = progress
                except (av.error.EOFError, av.error.InvalidDataError) as demux_err:
                    print() # New line after progress
                    safe_log(f"Reached end of unfinalized MKV: {demux_err}")
                except Exception as unexpected_err:
                    print() # New line after progress
                    safe_log(f"Unexpected remux error: {unexpected_err}")
                    raise unexpected_err
                
                if last_reported_progress != -1:
                    print() # New line after progress
                    
        os.remove(mkv_path)
        safe_log(f"Remux completed successfully: {mp4_path}")
    except Exception as e:
        safe_log(f"Failed to remux {mkv_path}: {e}")
        try: os.remove(mp4_path)
        except: pass

def process_unfinalized_recordings():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    recordings_dir = os.path.join(base_dir, "Recordings")
    if not os.path.exists(recordings_dir):
        return
        
    for root, _, files in os.walk(recordings_dir):
        for file in files:
            if file.endswith(".mkv"):
                mkv_path = os.path.join(root, file)
                safe_log(f"Found unfinalized MKV recording from previous session: {mkv_path}")
                try:
                    remux_mkv_to_mp4(mkv_path)
                except Exception as e:
                    safe_log(f"Error processing {mkv_path}: {e}")

def parse_recording_timestamp(file_path):
    """
    Returns the recording start time encoded in a recording file name
    (YYYYMMDD_HHMMSS.mp4), or None when the name does not follow that pattern.
    """
    try:
        return datetime.datetime.strptime(Path(file_path).stem, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def get_retention_days(config):
    """
    Returns the retention window in days. 0 (or any invalid/non-positive value)
    means "keep recordings forever", which is also the safe default.
    """
    raw_value = config.get("retention_days", 0)
    if isinstance(raw_value, bool):
        return 0
    try:
        days = float(raw_value)
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0


def cleanup_old_recordings(config, exclude_paths=None, recordings_dir=None):
    """
    Deletes recordings older than config["retention_days"] and prunes the
    date folders that become empty. Returns the number of deleted files.

    Files are dated by the timestamp in their name, falling back to the
    modification time when the name does not match the expected pattern.
    Only .mp4/.mkv files inside the Recordings folder are ever touched, and
    files listed in exclude_paths (e.g. the recording in progress) are skipped.
    """
    retention_days = get_retention_days(config)
    if retention_days <= 0:
        return 0

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if recordings_dir is None:
        recordings_dir = os.path.join(base_dir, "Recordings")
    recordings_dir = os.path.abspath(recordings_dir)
    if not os.path.isdir(recordings_dir):
        return 0

    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    excluded = {
        os.path.normcase(os.path.abspath(str(path)))
        for path in (exclude_paths or [])
        if path
    }
    # Never prune the folders that hold an excluded file: the recording in
    # progress may not exist on disk yet (PyAV creates it on the first write),
    # so its date folder can look empty.
    protected_dirs = set()
    for path in excluded:
        parent = os.path.dirname(path)
        while parent and parent not in protected_dirs:
            protected_dirs.add(parent)
            next_parent = os.path.dirname(parent)
            if next_parent == parent:
                break
            parent = next_parent

    deleted_count = 0
    freed_bytes = 0
    failed_count = 0

    for root, _, files in os.walk(recordings_dir, topdown=False):
        for file_name in files:
            if not file_name.lower().endswith((".mp4", ".mkv")):
                continue

            file_path = os.path.join(root, file_name)
            if os.path.normcase(os.path.abspath(file_path)) in excluded:
                continue

            recorded_at = parse_recording_timestamp(file_path)
            if recorded_at is None:
                try:
                    recorded_at = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                except OSError:
                    continue

            if recorded_at >= cutoff:
                continue

            try:
                file_size = os.path.getsize(file_path)
                os.remove(file_path)
                deleted_count += 1
                freed_bytes += file_size
            except OSError as e:
                failed_count += 1
                safe_log(f"Retention cleanup could not delete {file_path}: {e}")

        # Prune date folders that are now empty (bottom-up, so nested folders die first).
        normalized_root = os.path.normcase(os.path.abspath(root))
        if normalized_root == os.path.normcase(recordings_dir) or normalized_root in protected_dirs:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass

    if deleted_count or failed_count:
        freed_mb = freed_bytes / (1024 * 1024)
        summary = (f"Retention cleanup: removed {deleted_count} recording(s) older than "
                   f"{retention_days:g} day(s), freed {freed_mb:.1f} MB.")
        if failed_count:
            summary += f" {failed_count} file(s) could not be deleted (in use?)."
        safe_log(summary)
    else:
        safe_log(f"Retention cleanup: no recordings older than {retention_days:g} day(s).")

    return deleted_count


def graceful_shutdown(*args, **kwargs):
    global RUNNING
    safe_log(f"Received shutdown signal: args={args}, kwargs={kwargs}")
    safe_log("Shutting down gracefully... Please wait for video file to finalize.")
    RUNNING = False
    
    if args and args[0] in (0, 1, 2, 5, 6):
        if threading.current_thread() != threading.main_thread():
            safe_log(f"Handling Windows Console Event {args[0]}, waiting for threads to finish...")
            shutdown_complete_event.wait(timeout=4.5)
            safe_log("Console Handler exiting.")
    return True

class ScreenAudioRecorder:
    def __init__(self, config):
        self.config = config
        self.fps = config.get("fps", 30)
        self.width = config.get("resolution", {}).get("width", 1920)
        self.height = config.get("resolution", {}).get("height", 1080)
        self.mic_vol = config.get("mic_volume", 1.0)
        self.sys_vol = config.get("sys_volume", 1.0)
        
        # Auto-pause settings
        self.auto_pause = config.get("auto_pause", True)
        self.idle_threshold = config.get("idle_threshold", 5.0)
        self.silence_threshold = config.get("silence_threshold", 0.01)
        
        self.is_paused = False
        self.pause_start_time = None
        self.total_paused_duration = 0
        self.last_activity_time = time.time()
        
        self.output_file = get_output_filepath(".mkv")
        self.container = av.open(self.output_file, mode='w')
        
        # Setup Video Stream
        self.video_stream = self.container.add_stream('libx264', rate=self.fps)
        self.video_stream.width = self.width
        self.video_stream.height = self.height
        self.video_stream.pix_fmt = 'yuv420p'
        self.video_stream.options = {'crf': '23', 'preset': 'veryfast', 'bframes': '0'}
        
        # Setup Audio Stream
        self.sample_rate = 48000
        self.audio_stream = self.container.add_stream('aac', rate=self.sample_rate)
        self.audio_stream.options = {'b:a': '128k'}
        self.audio_stream.layout = 'stereo'
        
        self.mux_lock = threading.Lock()
        self.start_event = threading.Event()
        self.pause_lock = threading.Lock()  # Protects is_paused, pause_start_time, total_paused_duration
        
        # Track PTS
        self.v_pts = 0
        self.a_pts = 0
        
        self.video_start_time = None
        
    def get_elapsed_recording_time(self):
        """
        Thread-safe: returns wall-clock seconds of *active* recording time,
        i.e. total elapsed minus all paused durations (including any ongoing pause).
        """
        if self.video_start_time is None:
            return 0.0
        with self.pause_lock:
            paused = self.total_paused_duration
            if self.is_paused and self.pause_start_time is not None:
                paused += time.time() - self.pause_start_time
        return time.time() - self.video_start_time - paused

    def record_video(self):
        global RUNNING
        comtypes.CoInitialize()

        cursor_overlay = CursorOverlay()
        camera = dxcam.create(output_idx=0, output_color="RGB")
        camera.start(target_fps=self.fps, video_mode=True)
        
        self.start_event.wait(timeout=10)
        self.video_start_time = time.time()
        
        try:
            while RUNNING:
                with self.pause_lock:
                    currently_paused = self.is_paused
                if currently_paused:
                    time.sleep(0.1)
                    continue

                start_time = time.time()
                img = camera.get_latest_frame()
                
                if img is None:
                    time.sleep(0.005)
                    continue
                    
                # dxcam >= 0.3 already returns a private copy; older versions return a
                # view into their ring buffer, which must not be drawn on.
                if img.base is not None:
                    img = img.copy()
                img_with_cursor = cursor_overlay.draw(img)
                if img_with_cursor.shape[1] == self.width and img_with_cursor.shape[0] == self.height:
                    img_resized = img_with_cursor
                else:
                    img_resized = cv2.resize(img_with_cursor, (self.width, self.height))
                
                frame = av.VideoFrame.from_ndarray(img_resized, format='rgb24')
                
                elapsed_since_start = self.get_elapsed_recording_time()
                
                expected_frame_index = int(elapsed_since_start * self.fps)
                if expected_frame_index <= self.v_pts:
                    expected_frame_index = self.v_pts + 1
                
                frame.pts = expected_frame_index
                self.v_pts = expected_frame_index
                frame.time_base = Fraction(1, self.fps)
                
                for packet in self.video_stream.encode(frame):
                    with self.mux_lock:
                        self.container.mux(packet)
                
                elapsed = time.time() - start_time
                sleep_time = (1.0 / self.fps) - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        finally:
            camera.stop()
            cursor_overlay.close()
                
    def record_audio(self):
        global RUNNING
        comtypes.CoInitialize()
        
        from queue import Queue, Empty
        block_size = 2048
        mic_queue = Queue(maxsize=5)
        sys_queue = Queue(maxsize=5)
        zero_audio_block = np.zeros((block_size, 2), dtype=np.float32)
        audio_issue_log_times = {}

        def log_audio_issue(key, message, min_interval=10.0):
            now = time.time()
            if now - audio_issue_log_times.get(key, 0) >= min_interval:
                audio_issue_log_times[key] = now
                safe_log(message)

        def close_recorder(recorder):
            if recorder:
                try:
                    recorder.__exit__(None, None, None)
                except:
                    pass
        
        def mic_capture_thread(mic, block_size, queue):
            while RUNNING:
                if self.mic_vol <= 0:
                    time.sleep(0.1)
                    continue
                try:
                    data = mic.record(numframes=block_size)
                    # Even when paused, we put into queue so main thread can check for RMS/activity
                    if queue.full():
                        try: queue.get_nowait()
                        except: pass
                    queue.put(data)
                except:
                    break

        def sys_capture_thread(sys_audio, block_size, queue):
            while RUNNING:
                try:
                    data = sys_audio.record(numframes=block_size)
                    # Even when paused, we put into queue so main thread can check for RMS/activity
                    if queue.full():
                        try: queue.get_nowait()
                        except: pass
                    queue.put(data)
                except:
                    break

        def get_recorders():
            mic = None
            sys_audio = None
            mic_id = None
            speaker_id = None
            mic_name = "unavailable"
            speaker_name = "unavailable"

            if self.mic_vol > 0:
                try:
                    default_mic = sc.default_microphone()
                    mic = default_mic.recorder(samplerate=self.sample_rate)
                    mic.__enter__()
                    mic_id = default_mic.id
                    mic_name = default_mic.name
                except Exception as e:
                    close_recorder(mic)
                    mic = None
                    log_audio_issue("mic_open", f"Microphone unavailable; recording without microphone: {e}")
            else:
                mic_name = "disabled"

            try:
                default_speaker = sc.default_speaker()
                loopback_mic = sc.get_microphone(id=default_speaker.id, include_loopback=True)
                sys_audio = loopback_mic.recorder(samplerate=self.sample_rate)
                sys_audio.__enter__()
                speaker_id = default_speaker.id
                speaker_name = default_speaker.name
            except Exception as e:
                close_recorder(sys_audio)
                sys_audio = None
                log_audio_issue("speaker_open", f"System audio unavailable; recording without system audio: {e}")

            if mic or sys_audio:
                safe_log(f"Audio devices connected - Mic: {mic_name}, Speaker: {speaker_name}")

            return mic, sys_audio, mic_id, speaker_id

        def get_idle_time():
            try:
                return (win32api.GetTickCount() - win32api.GetLastInputInfo()) / 1000.0
            except:
                return 0

        mic, sys_audio, current_mic_id, current_speaker_id = get_recorders()

        m_thread = None
        s_thread = None

        def start_capture_threads(mic_recorder, sys_recorder):
            nonlocal m_thread, s_thread
            if mic_recorder:
                m_thread = threading.Thread(target=mic_capture_thread, args=(mic_recorder, block_size, mic_queue), daemon=True)
                m_thread.start()
            else:
                m_thread = None
            if sys_recorder:
                s_thread = threading.Thread(target=sys_capture_thread, args=(sys_recorder, block_size, sys_queue), daemon=True)
                s_thread.start()
            else:
                s_thread = None

        def join_capture_threads(timeout=0.2):
            nonlocal m_thread, s_thread
            for capture_thread in (m_thread, s_thread):
                if capture_thread and capture_thread.is_alive():
                    capture_thread.join(timeout=timeout)
            m_thread = None
            s_thread = None

        if mic or sys_audio:
            start_capture_threads(mic, sys_audio)

        if not mic and not sys_audio:
            safe_log("No audio devices available; recording silent audio until a device is available.")

        last_had_audio = bool(mic or sys_audio)
        last_no_audio_retry_time = time.time()

        last_device_check_time = time.time()

        try:
            self.start_event.set()
            
            while RUNNING:
                current_time = time.time()
                if current_time - last_device_check_time > 2.0:
                    last_device_check_time = current_time
                    try:
                        check_mic_id = None
                        check_speaker_id = None
                        if self.mic_vol > 0:
                            try:
                                check_mic = sc.default_microphone()
                                check_mic_id = check_mic.id
                            except Exception:
                                pass

                        try:
                            check_speaker = sc.default_speaker()
                            check_speaker_id = check_speaker.id
                        except Exception as e:
                            log_audio_issue("speaker_check", f"System audio still unavailable: {e}")
                        
                        # Check if threads are dead
                        threads_dead = False
                        if m_thread and not m_thread.is_alive():
                            threads_dead = True
                        if s_thread and not s_thread.is_alive():
                            threads_dead = True
                            
                        if check_mic_id != current_mic_id or check_speaker_id != current_speaker_id or threads_dead:
                            if threads_dead:
                                safe_log("Audio capture thread died! Attempting automatic reconnect...")
                            else:
                                safe_log("Default audio device changed! Attempting automatic reconnect...")
                                
                            # Force re-initialization
                            close_recorder(mic)
                            mic = None
                            close_recorder(sys_audio)
                            sys_audio = None
                            join_capture_threads()
                            # Clear queues
                            while not mic_queue.empty(): 
                                try: mic_queue.get_nowait()
                                except Empty: break
                            while not sys_queue.empty(): 
                                try: sys_queue.get_nowait()
                                except Empty: break

                            mic, sys_audio, current_mic_id, current_speaker_id = get_recorders()
                            start_capture_threads(mic, sys_audio)
                    except Exception as e:
                        safe_log(f"Error checking audio devices: {e}")

                if not mic and not sys_audio:
                    time.sleep(block_size / self.sample_rate)
                    if time.time() - last_no_audio_retry_time >= 2.0:
                        last_no_audio_retry_time = time.time()
                        mic, sys_audio, current_mic_id, current_speaker_id = get_recorders()
                        if mic or sys_audio:
                            start_capture_threads(mic, sys_audio)
                    mic_data = zero_audio_block
                    sys_data = zero_audio_block
                else:
                    # Get data from queues
                    sys_data = None
                    mic_data = None

                    if sys_audio:
                        try:
                            sys_data = sys_queue.get(timeout=0.1)
                        except Empty:
                            if self.is_paused:
                                time.sleep(0.1)
                                continue
                            continue
                    if mic and self.mic_vol > 0:
                        try:
                            mic_data = mic_queue.get(timeout=0.05)
                        except Empty:
                            if not sys_audio:
                                if self.is_paused:
                                    time.sleep(0.1)
                                    continue
                                continue

                    if sys_data is None:
                        if mic_data is not None:
                            sys_data = np.zeros_like(mic_data)
                        else:
                            sys_data = zero_audio_block
                    if mic_data is None:
                        mic_data = np.zeros_like(sys_data)

                has_audio = bool(mic or sys_audio)
                if has_audio != last_had_audio:
                    if has_audio:
                        safe_log("Audio device available again; resuming live audio capture.")
                    else:
                        safe_log("All audio devices unavailable; recording silent audio.")
                    last_had_audio = has_audio

                # Auto-pause logic
                if self.auto_pause:
                    sys_rms = np.sqrt(np.mean(sys_data**2))
                    mic_rms = np.sqrt(np.mean(mic_data**2)) if self.mic_vol > 0 else 0
                    idle_time_kb_mouse = get_idle_time()
                    
                    if sys_rms > self.silence_threshold or mic_rms > self.silence_threshold or idle_time_kb_mouse < 0.1:
                        self.last_activity_time = time.time()
                    
                    total_idle_time = time.time() - self.last_activity_time
                    
                    with self.pause_lock:
                        if total_idle_time > self.idle_threshold:
                            if not self.is_paused:
                                self.is_paused = True
                                self.pause_start_time = time.time()
                                safe_log(f"Auto-paused (Total Idle: {total_idle_time:.1f}s)")
                                # Clear queues when pausing to avoid old data on resume
                                while not mic_queue.empty(): mic_queue.get()
                                while not sys_queue.empty(): sys_queue.get()
                        else:
                            if self.is_paused:
                                self.total_paused_duration += time.time() - self.pause_start_time
                                self.is_paused = False
                                self.pause_start_time = None
                                safe_log("Auto-resumed (Activity detected!)")

                with self.pause_lock:
                    currently_paused = self.is_paused
                if currently_paused:
                    time.sleep(0.1)
                    # Clear queues while paused to ensure we only have fresh data on resume
                    # We do this after the auto-pause logic has a chance to see current data
                    while not mic_queue.empty(): 
                        try: mic_queue.get_nowait()
                        except: break
                    while not sys_queue.empty(): 
                        try: sys_queue.get_nowait()
                        except: break
                    continue

                raw_mixed = (mic_data * self.mic_vol) + (sys_data * self.sys_vol)
                mixed = np.clip(raw_mixed, -1.0, 1.0)
                
                if len(mixed.shape) == 1:
                    mixed = np.reshape(mixed, (-1, 1))
                if mixed.shape[1] == 1:
                    mixed = np.repeat(mixed, 2, axis=1)
                
                mixed_int16 = (mixed * 32767).astype(np.int16)
                audio_data = np.ascontiguousarray(mixed_int16.reshape(1, -1), dtype=np.int16)
                frame = av.AudioFrame.from_ndarray(audio_data, format='s16', layout='stereo')
                frame.sample_rate = self.sample_rate
                
                if self.video_start_time is None:
                    time.sleep(0.01)
                    continue
                    
                elapsed_audio_time = self.get_elapsed_recording_time()
                expected_a_pts = int(elapsed_audio_time * self.sample_rate)
                
                # Drift correction: threshold = 100ms (was 50ms; tighter threshold caused
                # spurious corrections when total_paused_duration was momentarily stale)
                if abs(expected_a_pts - self.a_pts) > self.sample_rate * 0.1:
                    if expected_a_pts > self.a_pts:
                        # Audio behind: skip stale queued data to catch up
                        while not mic_queue.empty(): 
                            try: mic_queue.get_nowait()
                            except: break
                        while not sys_queue.empty(): 
                            try: sys_queue.get_nowait()
                            except: break
                        self.a_pts = expected_a_pts
                    else:
                        # Audio ahead: let wall clock catch up; keep recording sound.
                        pass
                
                frame.pts = self.a_pts
                self.a_pts += sys_data.shape[0]
                frame.time_base = Fraction(1, self.sample_rate)
                
                for packet in self.audio_stream.encode(frame):
                    with self.mux_lock:
                        try:
                            if packet.pts is not None and packet.dts is not None:
                                if packet.dts < 0: packet.dts = 0
                                if packet.pts < 0: packet.pts = 0
                            self.container.mux(packet)
                        except Exception as mux_err:
                            safe_log(f"Audio muxing error: {mux_err}")
                            # If it's a PTS error, we must reset a_pts to be monotonic
                            if "pts" in str(mux_err).lower() or "dts" in str(mux_err).lower():
                                # Reset to a safe future PTS to maintain monotonicity
                                self.a_pts = max(self.a_pts + 1, expected_a_pts)
        except Exception as e:
            import traceback
            traceback.print_exc()
            safe_log(f"Major audio thread error: {e}")
        finally:
            close_recorder(mic)
            close_recorder(sys_audio)
            join_capture_threads()

    def finalize(self):
        with self.mux_lock:
            try:
                for packet in self.video_stream.encode():
                    if packet.dts is None: packet.dts = packet.pts
                    self.container.mux(packet)
            except: pass
            try:
                for packet in self.audio_stream.encode():
                    if packet.dts is None: packet.dts = packet.pts
                    self.container.mux(packet)
            except: pass
            try:
                self.container.close()
            except: pass
        safe_print(f"File saved: {self.output_file}")

def main():
    config = load_config()
    setup_startup(config.get("start_on_boot", True))
    if not acquire_single_instance_lock():
        return
    process_unfinalized_recordings()
    
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    
    try:
        import win32api
        win32api.SetConsoleCtrlHandler(graceful_shutdown, True)
    except:
        pass
        
    print("Starting screen recording... Press Ctrl+C to stop.")
    recorder = ScreenAudioRecorder(config)

    retention_days = get_retention_days(config)
    if retention_days > 0:
        safe_log(f"Retention policy enabled: only recordings from the last {retention_days:g} day(s) are kept.")
    else:
        safe_log("Retention policy disabled (retention_days = 0): all recordings are kept.")
    cleanup_old_recordings(config, exclude_paths=[recorder.output_file])

    v_thread = threading.Thread(target=recorder.record_video)
    a_thread = threading.Thread(target=recorder.record_audio)
    
    v_thread.start()
    a_thread.start()
    
    last_cleanup_time = time.monotonic()
    while RUNNING:
        time.sleep(0.2)
        if retention_days > 0 and time.monotonic() - last_cleanup_time >= RETENTION_CHECK_INTERVAL_SECONDS:
            last_cleanup_time = time.monotonic()
            cleanup_old_recordings(config, exclude_paths=[recorder.output_file])
        
    v_thread.join(timeout=1.5)
    a_thread.join(timeout=1.5)
    
    try:
        recorder.finalize()
    except Exception as e:
        safe_print(f"Finalize error: {e}")
    finally:
        shutdown_complete_event.set()
        
    if os.path.exists(recorder.output_file):
        remux_mkv_to_mp4(recorder.output_file)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        safe_log("Fatal error:")
        safe_log(traceback.format_exc())
        raise
