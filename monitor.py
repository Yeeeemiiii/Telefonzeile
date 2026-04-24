import os
import signal
import serial
import time
import subprocess
import threading
import socket
import json
from datetime import datetime

# ── Serial ────────────────────────────────────────────────────────────────────
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE   = 9600

# ── Screen output names ───────────────────────────────────────────────────────
HDMI0_OUTPUT = "HDMI-A-1"   # Physical HDMI0 → video (mpv)
HDMI1_OUTPUT = "HDMI-A-2"   # Physical HDMI1 → stream / recording (rpicam)

# ── Camera resolution (HDMI1) ─────────────────────────────────────────────────
HDMI_WIDTH  = 1920
HDMI_HEIGHT = 540

# ── Paths ─────────────────────────────────────────────────────────────────────
VIDEO_PATH      = "/home/jjven/StudiaEuropa_2.mp4"
RECORDINGS_DIR  = "/home/jjven/recordings"
MPV_IPC_SOCKET      = "/tmp/mpvsocket"
REC_OVERLAY_SCRIPT  = "/tmp/rec_overlay.py"
WELCOME_SCRIPT      = "/tmp/welcome_display.py"
# ─────────────────────────────────────────────────────────────────────────────

# Track current states
state = {
    "MP3":    "STOPPED",
    "PHONE":  "ON_HOOK",
    "RECORD": "IDLE"
}

cam_process         = None   # rpicam-vid preview
rec_process         = None   # rpicam-vid recording
rec_overlay_process = None   # REC indicator overlay
welcome_process     = None   # welcome image display
video_process = None   # mpv
cam_lock          = threading.Lock()
rec_lock          = threading.Lock()
rec_overlay_lock  = threading.Lock()
welcome_lock      = threading.Lock()
video_lock    = threading.Lock()


# ─── Ensure recordings directory exists ──────────────────────────────────────

os.makedirs(RECORDINGS_DIR, exist_ok=True)


# ─── Base environment ─────────────────────────────────────────────────────────

def base_env(wayland_socket=None):
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    if wayland_socket:
        env["WAYLAND_DISPLAY"] = wayland_socket
    return env


# ─── Camera stream — HDMI1 preview ───────────────────────────────────────────

def start_camera_stream():
    """Launch rpicam-vid in live preview mode on HDMI1. No-op if already running."""
    global cam_process
    with cam_lock:
        if cam_process and cam_process.poll() is None:
            return
        cmd = [
            "rpicam-vid",
            "--timeout",  "0",
            "--fullscreen",
            "--width",    str(HDMI_WIDTH),
            "--height",   str(HDMI_HEIGHT),
            "--roi",      "0.15,0.2,0.7,0.6",
            "--hflip",
        ]
        try:
            cam_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=base_env("wayland-1"),
            )
            print(f"[CAM] Stream started on {HDMI1_OUTPUT} (PID {cam_process.pid})")
            threading.Thread(
                target=_log_stderr, args=(cam_process, "CAM"), daemon=True
            ).start()
        except FileNotFoundError:
            print("[CAM] rpicam-vid not found — install with: sudo apt install rpicam-apps")
        except Exception as e:
            print(f"[CAM] Failed to start: {e}")


def stop_camera_stream():
    """Terminate rpicam-vid preview. No-op if not running."""
    global cam_process
    with cam_lock:
        if cam_process and cam_process.poll() is None:
            cam_process.terminate()
            try:
                cam_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cam_process.kill()
            print("[CAM] Stream stopped.")
        cam_process = None


# ─── Recording — HDMI1 + save to file ────────────────────────────────────────

def start_recording():
    """
    Stop the live preview and start rpicam-vid in recording mode.
    Shows a REC indicator on HDMI1 while saving to RECORDINGS_DIR.
    """
    global rec_process

    # Stop live stream first so camera is free
    stop_camera_stream()

    with rec_lock:
        if rec_process and rec_process.poll() is None:
            return

        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(RECORDINGS_DIR, f"recording_{timestamp}.mkv")

        cmd = [
            "rpicam-vid",
            "--timeout",      "0",               # record until stopped
            "--fullscreen",                       # show preview on HDMI1 while recording
            "--width",        str(HDMI_WIDTH),
            "--height",       str(HDMI_HEIGHT),
            "--roi",          "0,0.25,1,0.5",
            "--hflip",
            "--codec",        "libav",            # libav needed for audio muxing
            "--libav-format", "matroska",         # correct FFmpeg name for mkv container
            "--libav-audio",                      # REQUIRED: enable audio recording
            "--audio-codec",  "aac",              # audio track
            "--audio-source", "alsa",             # use ALSA not PulseAudio
            "--audio-device", "plughw:2,0",       # USB Audio with rate conversion
            "--output",       output_path,
        ]
        try:
            rec_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=base_env("wayland-1"),
            )
            print(f"[REC] Recording started → {output_path} (PID {rec_process.pid})")
            start_rec_overlay()
            threading.Thread(
                target=_log_stderr, args=(rec_process, "REC"), daemon=True
            ).start()
        except FileNotFoundError:
            print("[REC] rpicam-vid not found — install with: sudo apt install rpicam-apps")
        except Exception as e:
            print(f"[REC] Failed to start recording: {e}")


def stop_recording():
    """Stop rpicam-vid recording gracefully so MP4 is properly finalised."""
    global rec_process
    stop_rec_overlay()              # remove REC indicator immediately
    with rec_lock:
        if rec_process and rec_process.poll() is None:
            rec_process.send_signal(signal.SIGINT)  # SIGINT = graceful stop for rpicam-vid
            try:
                rec_process.wait(timeout=8)         # give it time to write moov atom
            except subprocess.TimeoutExpired:
                rec_process.kill()                  # force kill only if it hangs
            print("[REC] Recording stopped.")
        rec_process = None


# ─── Video — HDMI0 (mpv) ─────────────────────────────────────────────────────

def start_video():
    """Play VIDEO_PATH fullscreen on HDMI0 via mpv. No-op if already running."""
    global video_process
    with video_lock:
        if video_process and video_process.poll() is None:
            return
        cmd = [
            "mpv",
            "--fullscreen",
            "--loop=inf",
            "--no-terminal",
            "--gpu-context=wayland",
            f"--screen-name={HDMI0_OUTPUT}",
            f"--fs-screen-name={HDMI0_OUTPUT}",
            "--hwdec=v4l2m2m-copy",
            "--video-sync=audio",
            "--interpolation=no",
            "--video-latency-hacks=yes",
            f"--input-ipc-server={MPV_IPC_SOCKET}",  # IPC socket for volume control
            VIDEO_PATH,
        ]
        try:
            video_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=base_env(),
            )
            print(f"[VID] Video started on HDMI0 / {HDMI0_OUTPUT} (PID {video_process.pid})")
            threading.Thread(
                target=_log_stderr, args=(video_process, "VID"), daemon=True
            ).start()
        except FileNotFoundError:
            print("[VID] mpv not found — install with: sudo apt install mpv")
        except Exception as e:
            print(f"[VID] Failed to start: {e}")


def stop_video():
    """Terminate mpv. No-op if not running."""
    global video_process
    with video_lock:
        if video_process and video_process.poll() is None:
            video_process.terminate()
            try:
                video_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                video_process.kill()
            print("[VID] Video stopped.")
        video_process = None


def set_video_volume(volume):
    """
    Set mpv volume via IPC socket (0 = mute, 100 = full).
    Retries briefly to allow mpv time to create the socket after launch.
    """
    cmd = json.dumps({"command": ["set_property", "volume", volume]}) + "\n"
    for _ in range(10):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(MPV_IPC_SOCKET)
            sock.sendall(cmd.encode())
            sock.close()
            print(f"[VID] Volume set to {volume}")
            return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.3)
    print(f"[VID] Could not set volume — IPC socket not ready")


# ─── Shared stderr logger ─────────────────────────────────────────────────────

def _log_stderr(proc, tag):
    for raw in proc.stderr:
        line = raw.decode("utf-8", errors="ignore").rstrip()
        if line:
            print(f"[{tag} stderr] {line}")




# ─── Welcome image display ────────────────────────────────────────────────────

WELCOME_IMAGE_PATH = "/home/jjven/welcome.png"

WELCOME_CODE = """
import tkinter as tk
import os

def dismiss(e=None):
    os._exit(0)

def make_window(root, x, y, w, h, photo):
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="black")
    win.geometry(f"{w}x{h}+{x}+{y}")
    lbl = tk.Label(win, image=photo, bg="black")
    lbl.pack(expand=True, fill="both")
    for widget in (win, lbl):
        widget.bind("<j>", dismiss)
        widget.bind("<J>", dismiss)
    win.focus_force()
    return win

IMG = "/home/jjven/welcome.png"
W, H = 1920, 1080

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg="black")
root.geometry(f"{W}x{H}+0+0")           # HDMI-A-1

photo = tk.PhotoImage(file=IMG)

lbl = tk.Label(root, image=photo, bg="black")
lbl.pack(expand=True, fill="both")

for widget in (root, lbl):
    widget.bind("<j>", dismiss)
    widget.bind("<J>", dismiss)

make_window(root, 1920, 0, W, H, photo)  # HDMI-A-2

root.focus_force()
root.mainloop()
"""

WELCOME_SCRIPT = "/tmp/welcome_display.py"

def write_welcome_script():
    with open(WELCOME_SCRIPT, "w") as f:
        f.write(WELCOME_CODE)

def start_welcome():
    global welcome_process
    write_welcome_script()
    with welcome_lock:
        if welcome_process and welcome_process.poll() is None:
            return
        try:
            env = base_env()
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"
            welcome_process = subprocess.Popen(
                ["python3", WELCOME_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
            print(f"[IMG] Welcome image started (PID {welcome_process.pid})")
            threading.Thread(
                target=_log_stderr, args=(welcome_process, "IMG"), daemon=True
            ).start()
        except Exception as e:
            print(f"[IMG] Failed to start welcome image: {e}")

def stop_welcome():
    global welcome_process
    with welcome_lock:
        if welcome_process and welcome_process.poll() is None:
            welcome_process.terminate()
            try:
                welcome_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                welcome_process.kill()
            print("[IMG] Welcome image stopped.")
        welcome_process = None


# ─── REC overlay ─────────────────────────────────────────────────────────────

REC_OVERLAY_CODE = """
import tkinter as tk

blink_state = [True]

def blink():
    color = "#FF0000" if blink_state[0] else "#880000"
    label1.config(fg=color)
    label2.config(fg=color)
    blink_state[0] = not blink_state[0]
    root.after(600, blink)

def make_window(root_or_top, x):
    win = tk.Toplevel(root_or_top) if root_or_top else None
    w = win if win else root_or_top
    w.overrideredirect(True)
    w.attributes("-topmost", True)
    w.configure(bg="#111111")
    w.geometry(f"620x70+{x}+10")
    lbl = tk.Label(w, text="● RECORDING IN PROG...",
                   font=("DejaVu Sans", 28, "bold"),
                   fg="#FF0000", bg="#111111")
    lbl.pack(expand=True, fill="both")
    return lbl

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg="#111111")
root.geometry("620x70+650+10")      # top-center of HDMI-A-1
label1 = tk.Label(root, text="● RECORDING IN PROG...",
                  font=("DejaVu Sans", 28, "bold"),
                  fg="#FF0000", bg="#111111")
label1.pack(expand=True, fill="both")

# Second window on HDMI-A-2 (offset by 1920px)
win2 = tk.Toplevel(root)
win2.overrideredirect(True)
win2.attributes("-topmost", True)
win2.configure(bg="#111111")
win2.geometry("620x70+2570+10")     # top-center of HDMI-A-2 (1920 + 650)
label2 = tk.Label(win2, text="● RECORDING IN PROG...",
                  font=("DejaVu Sans", 28, "bold"),
                  fg="#FF0000", bg="#111111")
label2.pack(expand=True, fill="both")

blink()
root.mainloop()
"""

def write_overlay_script():
    with open(REC_OVERLAY_SCRIPT, "w") as f:
        f.write(REC_OVERLAY_CODE)


def start_rec_overlay():
    """Launch a blinking REC indicator window on HDMI1 via GTK/Wayland."""
    global rec_overlay_process
    write_overlay_script()
    with rec_overlay_lock:
        if rec_overlay_process and rec_overlay_process.poll() is None:
            return
        try:
            env = base_env()
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"               # GTK needs DISPLAY (X/EGL system)
            rec_overlay_process = subprocess.Popen(
                ["python3", REC_OVERLAY_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
            print(f"[REC] Overlay started (PID {rec_overlay_process.pid})")
            threading.Thread(
                target=_log_stderr, args=(rec_overlay_process, "OVL"), daemon=True
            ).start()
        except Exception as e:
            print(f"[REC] Overlay failed to start: {e}")


def stop_rec_overlay():
    """Kill the REC indicator overlay."""
    global rec_overlay_process
    with rec_overlay_lock:
        if rec_overlay_process and rec_overlay_process.poll() is None:
            rec_overlay_process.terminate()
            try:
                rec_overlay_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                rec_overlay_process.kill()
            print("[REC] Overlay stopped.")
        rec_overlay_process = None


# ─── Serial monitor ───────────────────────────────────────────────────────────

def parse_and_update(line):
    updated = False
    parts = line.strip().split(",")
    for part in parts:
        if ":" in part:
            key, value = part.split(":", 1)
            if key in state and state[key] != value:
                state[key] = value
                updated = True
    return updated


def print_state():
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"[{timestamp}]  "
        f"MP3: {state['MP3']:<10}  "
        f"PHONE: {state['PHONE']:<10}  "
        f"RECORD: {state['RECORD']}"
    )


def handle_states():
    """
    Central state handler — called whenever any state changes.

    Logic:
      Phone ON_HOOK/RETURNED → stop everything
      Phone LIFTED + RECORD IDLE    → stream on HDMI1, video (full volume) on HDMI0
      Phone LIFTED + RECORD PRESSED → record on HDMI1 (with REC overlay),
                                       video continues on HDMI0 at volume 0
    """
    phone  = state["PHONE"]
    record = state["RECORD"]

    if phone in ("ON_HOOK", "RETURNED"):
        print("[PHONE] On hook — stopping everything...")
        stop_rec_overlay()
        stop_recording()
        stop_camera_stream()
        stop_video()
        
        start_welcome()

    elif phone == "LIFTED":
        stop_welcome()
        if record == "PRESSED":
            print("[REC] Record pressed — starting recording, muting video...")
            stop_camera_stream()       # free camera for recording
            start_recording()          # record + show REC on HDMI1
            start_video()              # ensure video is running
            time.sleep(0.5)              # give mpv a moment to open IPC socket
            set_video_volume(0)        # mute video during recording

        elif record == "IDLE":
            print("[PHONE] Phone lifted — starting stream and video...")
            stop_recording()           # stop recording + wait for file to finalise
            stop_camera_stream()       # ensure HDMI1 is dark
            stop_video()               # ensure HDMI0 is dark
            print("[REC] Screens idle — resuming in 0.5 seconds...")
            time.sleep(0.5)              # brief pause before restarting both screens
            start_camera_stream()      # resume live stream on HDMI1
            start_video()              # resume video on HDMI0
            time.sleep(0.5)
            set_video_volume(100)      # restore full volume


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to Arduino on {SERIAL_PORT} at {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        print("Connected. Listening for state updates...\n")
    except serial.SerialException as e:
        print(f"Failed to connect: {e}")
        print("Check your port with: ls /dev/ttyUSB* or ls /dev/ttyACM*")
        return

    print_state()
    handle_states()

    try:
        while True:
            try:
                if ser.in_waiting > 0:
                    raw = ser.readline().decode("utf-8", errors="ignore").strip()
                    if raw:
                        changed = parse_and_update(raw)
                        if changed:
                            print_state()
                            handle_states()
            except serial.SerialException as e:
                print(f"Serial error: {e}")
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ser.close()
        stop_rec_overlay()
        stop_recording()
        stop_camera_stream()
        stop_video()
        stop_welcome()


if __name__ == "__main__":
    main()
