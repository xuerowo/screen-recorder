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

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

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

CONFIG_FILE = "config.json"
RUNNING = True
shutdown_complete_event = threading.Event()

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"fps": 30, "resolution": {"width": 1920, "height": 1080}, "mic_volume": 1.0, "sys_volume": 1.0, "start_on_boot": True, "auto_pause": True, "idle_threshold": 5.0, "silence_threshold": 0.01}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def setup_startup(enable):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "AutoScreenRecorder"
    exe_path = os.path.abspath(sys.argv[0])
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Failed to configure startup: {e}")

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
        sys.stdout.flush()
    except Exception:
        pass

def safe_log(message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    safe_print(f"[{now}] [LOG] {message}")

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
            subprocess.run(["ffmpeg", "-y", "-i", mkv_path, "-c", "copy", mp4_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(mkv_path)
            safe_log(f"Remux completed successfully via ffmpeg: {mp4_path}")
            return
        except Exception as e:
            safe_log(f"ffmpeg remux failed: {e}. Falling back to PyAV...")
            
    try:
        with av.open(mkv_path, 'r') as in_container:
            with av.open(mp4_path, 'w') as out_container:
                out_streams = []
                for in_stream in in_container.streams:
                    out_stream = out_container.add_stream_from_template(in_stream)
                    if in_stream.type == 'video':
                        out_stream.codec_context.codec_tag = 'avc1'
                    elif in_stream.type == 'audio':
                        out_stream.codec_context.codec_tag = 'mp4a'
                    out_streams.append(out_stream)
                
                try:
                    for packet in in_container.demux():
                        if packet.pts is None:
                            continue
                        if packet.dts is None:
                            packet.dts = packet.pts
                        packet.stream = out_streams[packet.stream.index]
                        out_container.mux(packet)
                except (av.error.EOFError, av.error.InvalidDataError) as demux_err:
                    safe_log(f"Reached end of unfinalized MKV: {demux_err}")
                except Exception as unexpected_err:
                    safe_log(f"Unexpected remux error: {unexpected_err}")
                    raise unexpected_err
                    
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
        
        # Track PTS
        self.v_pts = 0
        self.a_pts = 0
        
        self.video_start_time = None
        
    def record_video(self):
        global RUNNING
        comtypes.CoInitialize()
        
        def draw_cursor(img_array):
            info = CURSORINFO()
            info.cbSize = ctypes.sizeof(CURSORINFO)
            if user32.GetCursorInfo(ctypes.byref(info)) and info.flags == 1:
                hcursor = info.hCursor
                x, y = info.ptScreenPos.x, info.ptScreenPos.y
                
                icon_info = ICONINFO()
                if user32.GetIconInfo(hcursor, ctypes.byref(icon_info)):
                    x -= icon_info.xHotspot
                    y -= icon_info.yHotspot
                    
                    if icon_info.hbmMask:
                        gdi32.DeleteObject(ctypes.c_void_p(icon_info.hbmMask))
                    if icon_info.hbmColor:
                        gdi32.DeleteObject(ctypes.c_void_p(icon_info.hbmColor))
                
                try:
                    size_x = 128
                    size_y = 128
                    
                    hdc = win32gui.GetDC(0)
                    hdc_mem = win32gui.CreateCompatibleDC(hdc)
                    hbitmap = win32gui.CreateCompatibleBitmap(hdc, size_x, size_y)
                    old_bmp = win32gui.SelectObject(hdc_mem, hbitmap)
                    
                    # 1. Draw on Black background
                    win32gui.FillRect(hdc_mem, (0, 0, size_x, size_y), win32gui.GetStockObject(4)) # BLACK_BRUSH
                    win32gui.DrawIconEx(hdc_mem, 0, 0, hcursor, 0, 0, 0, 0, 3) # DI_NORMAL
                    
                    bmi = BITMAPINFO()
                    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                    bmi.bmiHeader.biWidth = size_x
                    bmi.bmiHeader.biHeight = -size_y # Top-down
                    bmi.bmiHeader.biPlanes = 1
                    bmi.bmiHeader.biBitCount = 32
                    bmi.bmiHeader.biCompression = 0
                    
                    buffer_black = ctypes.create_string_buffer(size_x * size_y * 4)
                    gdi32.GetDIBits(hdc, int(hbitmap), 0, size_y, buffer_black, ctypes.byref(bmi), 0)
                    img_black = np.frombuffer(buffer_black, dtype=np.uint8).reshape((size_y, size_x, 4))[..., :3][..., ::-1].astype(np.int32)
                    
                    # 2. Draw on White background
                    win32gui.FillRect(hdc_mem, (0, 0, size_x, size_y), win32gui.GetStockObject(0)) # WHITE_BRUSH
                    win32gui.DrawIconEx(hdc_mem, 0, 0, hcursor, 0, 0, 0, 0, 3)
                    
                    buffer_white = ctypes.create_string_buffer(size_x * size_y * 4)
                    gdi32.GetDIBits(hdc, int(hbitmap), 0, size_y, buffer_white, ctypes.byref(bmi), 0)
                    img_white = np.frombuffer(buffer_white, dtype=np.uint8).reshape((size_y, size_x, 4))[..., :3][..., ::-1].astype(np.int32)
                    
                    # 3. Calculate Alpha
                    alpha = 255 - (img_white - img_black)
                    alpha = np.mean(alpha, axis=2)
                    alpha = np.clip(alpha, 0, 255).astype(np.uint8)
                    
                    cursor_rgb = np.clip(img_black, 0, 255).astype(np.uint8)
                    
                    h, w = img_array.shape[:2]
                    for cy in range(size_y):
                        for cx in range(size_x):
                            if y + cy < 0 or y + cy >= h or x + cx < 0 or x + cx >= w:
                                continue
                            
                            alpha_val = alpha[cy, cx] / 255.0
                            if alpha_val > 0.01:
                                curr_bgr = img_array[y + cy, x + cx]
                                cursor_bgr = cursor_rgb[cy, cx]
                                new_bgr = cursor_bgr + curr_bgr * (1.0 - alpha_val)
                                img_array[y + cy, x + cx] = np.clip(new_bgr, 0, 255).astype(np.uint8)
                    
                    win32gui.SelectObject(hdc_mem, old_bmp)
                    win32gui.DeleteObject(hbitmap)
                    win32gui.DeleteDC(hdc_mem)
                    win32gui.ReleaseDC(0, hdc)
                except Exception as e:
                    print(f"Fallback cursor due to: {e}")
                    cv2.circle(img_array, (x, y), 5, (0, 0, 255), -1)
            return img_array

        camera = dxcam.create(output_idx=0, output_color="RGB")
        camera.start(target_fps=self.fps, video_mode=True)
        
        self.start_event.wait(timeout=10)
        self.video_start_time = time.time()
        
        try:
            while RUNNING:
                if self.is_paused:
                    time.sleep(0.1)
                    continue

                start_time = time.time()
                img = camera.get_latest_frame()
                
                if img is None:
                    time.sleep(0.005)
                    continue
                    
                img_with_cursor = draw_cursor(img.copy())
                img_resized = cv2.resize(img_with_cursor, (self.width, self.height))
                
                frame = av.VideoFrame.from_ndarray(img_resized, format='rgb24')
                
                elapsed_since_start = time.time() - self.video_start_time - self.total_paused_duration
                
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
                
    def record_audio(self):
        global RUNNING
        comtypes.CoInitialize()
        
        from queue import Queue, Empty
        block_size = 2048
        mic_queue = Queue(maxsize=5)
        sys_queue = Queue(maxsize=5)
        
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
            try:
                default_mic = sc.default_microphone()
                default_speaker = sc.default_speaker()
                loopback_mic = sc.get_microphone(id=default_speaker.id, include_loopback=True)
                
                mic = default_mic.recorder(samplerate=self.sample_rate)
                sys_audio = loopback_mic.recorder(samplerate=self.sample_rate)
                
                mic.__enter__()
                sys_audio.__enter__()
                
                safe_log(f"Audio devices connected - Mic: {default_mic.name}, Speaker: {default_speaker.name}")
                return mic, sys_audio, default_mic.id, default_speaker.id
            except Exception as e:
                safe_log(f"Failed to open audio devices: {e}")
                return None, None, None, None

        def get_idle_time():
            try:
                return (win32api.GetTickCount() - win32api.GetLastInputInfo()) / 1000.0
            except:
                return 0

        mic, sys_audio, current_mic_id, current_speaker_id = get_recorders()
        
        m_thread = None
        s_thread = None
        if mic and sys_audio:
            m_thread = threading.Thread(target=mic_capture_thread, args=(mic, block_size, mic_queue), daemon=True)
            s_thread = threading.Thread(target=sys_capture_thread, args=(sys_audio, block_size, sys_queue), daemon=True)
            m_thread.start()
            s_thread.start()

        last_device_check_time = time.time()

        try:
            self.start_event.set()
            
            while RUNNING:
                current_time = time.time()
                if current_time - last_device_check_time > 2.0:
                    last_device_check_time = current_time
                    try:
                        check_mic = sc.default_microphone()
                        check_speaker = sc.default_speaker()
                        
                        # Check if threads are dead
                        threads_dead = False
                        if m_thread and not m_thread.is_alive():
                            threads_dead = True
                        if s_thread and not s_thread.is_alive():
                            threads_dead = True
                            
                        if check_mic.id != current_mic_id or check_speaker.id != current_speaker_id or threads_dead:
                            if threads_dead:
                                safe_log("Audio capture thread died! Attempting automatic reconnect...")
                            else:
                                safe_log("Default audio device changed! Attempting automatic reconnect...")
                                
                            # Force re-initialization
                            if mic:
                                try: mic.__exit__(None, None, None)
                                except: pass
                                mic = None
                            if sys_audio:
                                try: sys_audio.__exit__(None, None, None)
                                except: pass
                                sys_audio = None
                            # Clear queues
                            while not mic_queue.empty(): 
                                try: mic_queue.get_nowait()
                                except Empty: break
                            while not sys_queue.empty(): 
                                try: sys_queue.get_nowait()
                                except Empty: break
                    except Exception as e:
                        safe_log(f"Error checking audio devices: {e}")

                if not mic or not sys_audio:
                    time.sleep(0.5)
                    mic, sys_audio, current_mic_id, current_speaker_id = get_recorders()
                    if mic and sys_audio:
                        m_thread = threading.Thread(target=mic_capture_thread, args=(mic, block_size, mic_queue), daemon=True)
                        s_thread = threading.Thread(target=sys_capture_thread, args=(sys_audio, block_size, sys_queue), daemon=True)
                        m_thread.start()
                        s_thread.start()
                    else:
                        # Fallback to silence if still no devices
                        mic_data = np.zeros((block_size, 2), dtype=np.float32)
                        sys_data = np.zeros((block_size, 2), dtype=np.float32)
                else:
                    # Get data from queues
                    try:
                        sys_data = sys_queue.get(timeout=0.1)
                        if self.mic_vol > 0:
                            try:
                                mic_data = mic_queue.get(timeout=0.05)
                            except Empty:
                                mic_data = np.zeros_like(sys_data)
                        else:
                            mic_data = np.zeros_like(sys_data)
                    except Empty:
                        if self.is_paused:
                            time.sleep(0.1)
                            continue
                        continue

                # Auto-pause logic
                if self.auto_pause:
                    sys_rms = np.sqrt(np.mean(sys_data**2))
                    mic_rms = np.sqrt(np.mean(mic_data**2)) if self.mic_vol > 0 else 0
                    idle_time_kb_mouse = get_idle_time()
                    
                    if sys_rms > self.silence_threshold or mic_rms > self.silence_threshold or idle_time_kb_mouse < 0.1:
                        self.last_activity_time = time.time()
                    
                    total_idle_time = time.time() - self.last_activity_time
                    
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

                if self.is_paused:
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
                    
                elapsed_audio_time = time.time() - self.video_start_time - self.total_paused_duration
                expected_a_pts = int(elapsed_audio_time * self.sample_rate)
                
                # Check for drift more aggressively (50ms)
                if abs(expected_a_pts - self.a_pts) > self.sample_rate * 0.05:
                    if expected_a_pts > self.a_pts:
                        # Audio behind: clear queue to drop old data
                        while not mic_queue.empty(): 
                            try: mic_queue.get_nowait()
                            except: break
                        while not sys_queue.empty(): 
                            try: sys_queue.get_nowait()
                            except: break
                        self.a_pts = expected_a_pts
                    else:
                        # Audio ahead: instead of dropping blocks (which caused silence), 
                        # just don't jump self.a_pts. This allows wall clock to catch up
                        # while still recording the sound. We accept a bit of "lead" here
                        # to prevent the "no sound" issue.
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
            if mic:
                try: mic.__exit__(None, None, None)
                except: pass
            if sys_audio:
                try: sys_audio.__exit__(None, None, None)
                except: pass

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
    
    v_thread = threading.Thread(target=recorder.record_video)
    a_thread = threading.Thread(target=recorder.record_audio)
    
    v_thread.start()
    a_thread.start()
    
    while RUNNING:
        time.sleep(0.2)
        
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
    main()
