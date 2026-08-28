import asyncio
import csv
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import psutil
except ImportError:
    print("psutil is missing. Run install.bat first.")
    sys.exit(1)

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    WINRT_MEDIA_OK = True
    WINRT_MEDIA_ERROR = None
except Exception as e:
    WINRT_MEDIA_OK = False
    WINRT_MEDIA_ERROR = str(e)

VERSION = "2.4"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "src" / "web"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
ADB_EXE = RUNTIME_DIR / "platform-tools" / "adb.exe"
PRESENTMON_EXE = RUNTIME_DIR / "presentmon" / "PresentMon.exe"
PRESENTMON_SESSION_NAME = "AndroidHomeDashboard"
LEGACY_PRESENTMON_SESSION_PREFIX = "A51Dashboard"
ADB_KEY = RUNTIME_DIR / "adb-keys" / "adbkey"
ANDROID_HOME = RUNTIME_DIR / "android-home"
PWA_PACKAGE_FILE = RUNTIME_DIR / "pwa-package.txt"
LAST_WIRELESS_ENDPOINT_FILE = RUNTIME_DIR / "last-wireless-endpoint.txt"
PHONE_CONFIG_FILE = PROJECT_ROOT / "phone_config.json"
AUTOMATION_CONFIG_FILE = PROJECT_ROOT / "dashboard_automation.json"
os.environ["ADB_VENDOR_KEYS"] = str(ADB_KEY)
os.environ["ANDROID_USER_HOME"] = str(ANDROID_HOME)
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bridge.log"
LOG_MAX_BYTES = 2_000_000
PID_FILE = PROJECT_ROOT / "bridge.pid"
HOST = "127.0.0.1"
PORT = 8765
API_TOKEN = secrets.token_urlsafe(32)
TRUSTED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TRUSTED_ORIGINS = {f"http://{host}" for host in TRUSTED_HOSTS}
PHONE_SCREEN_TIMEOUT_ACTIVE_MS = 2_147_483_647
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_stop = threading.Event()
_server = None
_clipboard_owner_hwnd = None
_clipboard_owner_ready = threading.Event()
_session_active = threading.Event()
_session_active.set()
_session_state_lock = threading.Lock()
_session_locked = False
_session_changed_at = datetime.now().isoformat(timespec="seconds")
_session_generation = 0
_phone_resume_requested = threading.Event()

PAGE_IDS = ("clipboard", "today", "now_playing", "game_performance", "diagnostics")
CONTEXT_STATES = ("GAME", "VIDEO", "IDLE", "WORK", "DEFAULT")

if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
else:
    user32 = kernel32 = wtsapi32 = None

KEYEVENTF_KEYUP = 0x0002
VK_LWIN = 0x5B
VK_L = 0x4C
WM_APPCOMMAND = 0x0319
APPCOMMAND_VOLUME_MUTE = 8
APPCOMMAND_VOLUME_DOWN = 9
APPCOMMAND_VOLUME_UP = 10
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
APPCOMMAND_MEDIA_PLAY_PAUSE = 14

CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
CLIPBOARD_HISTORY_LIMIT = 10
CLIPBOARD_TEXT_MAX_BYTES = 2 * 1024 * 1024
CLIPBOARD_IMAGE_MAX_BYTES = 48 * 1024 * 1024
CLIPBOARD_FILES_MAX_BYTES = 2 * 1024 * 1024
CLIPBOARD_FILES_MAX_COUNT = 512
CLIPBOARD_IMAGE_MAX_PIXELS = 40_000_000
CLIPBOARD_HISTORY_MAX_BYTES = 128 * 1024 * 1024
CLIPBOARD_API_TEXT_CHARS = 4096
CLIPBOARD_THUMBNAIL_MAX_EDGE = 360


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


if os.name == "nt":
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GetTickCount64.argtypes = []
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
    try:
        # Cap the log so a tight error loop cannot fill the disk over a long run.
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            try:
                LOG_FILE.replace(LOG_DIR / "bridge.log.1")
            except OSError:
                pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_session_locked():
    with _session_state_lock:
        return _session_locked


def get_session_state():
    with _session_state_lock:
        return {
            "locked": _session_locked,
            "mode": "standby" if _session_locked else "active",
            "changed_at": _session_changed_at,
        }


def get_session_generation():
    with _session_state_lock:
        return _session_generation


def wait_for_active_session():
    """Wait without busy polling until the Windows session is usable again."""
    while not _stop.is_set():
        if _session_active.wait(0.5):
            return True
    return False


def set_session_locked(locked):
    """Atomically enter lock standby or request a phone resume on unlock."""
    global _session_locked, _session_changed_at, _session_generation
    locked = bool(locked)
    with _session_state_lock:
        if _session_locked == locked:
            return False
        _session_locked = locked
        _session_changed_at = datetime.now().isoformat(timespec="seconds")
        _session_generation += 1

    if locked:
        # Clear this before starting any cleanup so the watcher cannot race the
        # lock transition and wake the phone again.
        _session_active.clear()
        _phone_resume_requested.clear()
        threading.Thread(target=enter_session_standby, daemon=True).start()
        log("Windows session locked: entering standby")
    else:
        # phone_watcher owns the normal reconnect/open path. Asking it to reopen
        # the UI keeps unlock behavior identical for USB and wireless devices.
        _phone_resume_requested.set()
        _session_active.set()
        log("Windows session unlocked: resuming dashboard")
    return True


def run(cmd, timeout=8):
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def adb(*args, timeout=8):
    return run([str(ADB_EXE), *args], timeout=timeout)


def send_app_command(command):
    """Send a shell-level media/volume command without targeting the foreground game."""
    if not user32:
        return {"accepted": False, "method": "not-windows"}
    user32.GetShellWindow.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    hwnd = user32.GetShellWindow()
    if not hwnd:
        return {"accepted": False, "method": "WM_APPCOMMAND", "error": "shell window unavailable"}
    ctypes.set_last_error(0)
    user32.SendMessageW(hwnd, WM_APPCOMMAND, hwnd, command << 16)
    error = ctypes.get_last_error()
    if error:
        return {"accepted": False, "method": "WM_APPCOMMAND", "error": error}
    return {"accepted": True, "method": "WM_APPCOMMAND", "command": command}


def press_win_l():
    if not user32:
        return False
    try:
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_L, 0, 0, 0)
        time.sleep(0.06)
        user32.keybd_event(VK_L, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def launch_uri(uri):
    subprocess.Popen(
        ["cmd", "/c", "start", "", uri],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    return {"accepted": True, "method": "URI"}


def lock_pc():
    # 1) Native workstation lock.
    try:
        user32.LockWorkStation.restype = ctypes.c_bool
        if user32.LockWorkStation():
            return {"accepted": True, "method": "LockWorkStation"}
        native_error = ctypes.get_last_error()
    except Exception as e:
        native_error = str(e)

    # 2) Disconnect the current interactive session. This lands on the sign-in screen.
    try:
        pid = kernel32.GetCurrentProcessId()
        session_id = ctypes.c_ulong()
        if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
            wtsapi32.WTSDisconnectSession.restype = ctypes.c_bool
            if wtsapi32.WTSDisconnectSession(ctypes.c_void_p(0), session_id.value, False):
                return {
                    "accepted": True,
                    "method": "WTSDisconnectSession",
                    "native_error": native_error,
                }
    except Exception as e:
        wts_error = str(e)
    else:
        wts_error = ctypes.get_last_error()

    # 3) Last interactive fallback.
    if press_win_l():
        return {
            "accepted": True,
            "method": "Win+L fallback",
            "native_error": native_error,
            "wts_error": wts_error,
        }

    return {
        "accepted": False,
        "method": "failed",
        "native_error": native_error,
        "wts_error": wts_error,
    }


# ---------- CPU: cumulative CPU-time delta + EMA ----------
_cpu_lock = threading.Lock()
_cpu_state = {"percent": 0.0, "raw_percent": 0.0, "source": "cpu-times delta"}


def cpu_sampler():
    prev = psutil.cpu_times()
    ema = None
    generation = get_session_generation()
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        current_generation = get_session_generation()
        if current_generation != generation:
            prev = psutil.cpu_times()
            generation = current_generation
        if _stop.wait(1.0):
            break
        if is_session_locked():
            prev = psutil.cpu_times()
            continue
        try:
            cur = psutil.cpu_times()
            total = sum(cur) - sum(prev)
            idle = cur.idle - prev.idle
            raw = 0.0 if total <= 0 else 100.0 * max(0.0, min(1.0, 1.0 - idle / total))
            ema = raw if ema is None else 0.35 * raw + 0.65 * ema
            with _cpu_lock:
                _cpu_state["raw_percent"] = round(raw, 1)
                _cpu_state["percent"] = round(ema, 1)
            prev = cur
        except Exception as e:
            log(f"CPU sampler error: {e}")


def get_cpu():
    with _cpu_lock:
        return dict(_cpu_state)


# ---------- GPU ----------
_gpu_lock = threading.Lock()
_gpu_state = {
    "available": False,
    "name": "-",
    "util": None,
    "temp": None,
    "vram_used_mb": None,
    "vram_total_mb": None,
    "vram_percent": None,
    "power_w": None,
}


def gpu_sampler():
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        data = dict(_gpu_state)
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            ).strip().splitlines()[0]
            p = [x.strip() for x in out.split(",")]
            used = float(p[3])
            total = float(p[4])
            data = {
                "available": True,
                "name": p[0],
                "util": float(p[1]),
                "temp": float(p[2]),
                "vram_used_mb": used,
                "vram_total_mb": total,
                "vram_percent": round(used / total * 100.0, 1) if total else 0.0,
                "power_w": None if p[5] in ("N/A", "[N/A]") else float(p[5]),
            }
        except Exception:
            pass
        if not is_session_locked():
            with _gpu_lock:
                _gpu_state.update(data)
        _stop.wait(1.0)


def get_gpu():
    with _gpu_lock:
        value = dict(_gpu_state)
        value["utilization"] = value.get("util")
        value["temperature"] = value.get("temp")
        return value


# ---------- Network ----------
_net_lock = threading.Lock()
_net_state = {"down_mbps": 0.0, "up_mbps": 0.0}


def network_sampler():
    prev = psutil.net_io_counters()
    prev_t = time.monotonic()
    generation = get_session_generation()
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        current_generation = get_session_generation()
        if current_generation != generation:
            prev = psutil.net_io_counters()
            prev_t = time.monotonic()
            generation = current_generation
        if _stop.wait(1.0):
            break
        if is_session_locked():
            prev = psutil.net_io_counters()
            prev_t = time.monotonic()
            continue
        try:
            cur = psutil.net_io_counters()
            now = time.monotonic()
            dt = max(now - prev_t, 0.001)
            down = max(0.0, (cur.bytes_recv - prev.bytes_recv) * 8 / dt / 1_000_000)
            up = max(0.0, (cur.bytes_sent - prev.bytes_sent) * 8 / dt / 1_000_000)
            with _net_lock:
                _net_state.update(down_mbps=round(down, 1), up_mbps=round(up, 1))
            prev, prev_t = cur, now
        except Exception as e:
            log(f"Network sampler error: {e}")


def get_network():
    with _net_lock:
        return dict(_net_state)


# ---------- Foreground app ----------
def _foreground_window_info(hwnd):
    info = {"minimized": False, "fullscreen": False, "large": False, "coverage": 0.0}
    if not user32 or not hwnd:
        return info
    try:
        info["minimized"] = bool(user32.IsIconic(hwnd))
        rect = wintypes.RECT()
        monitor_info = MONITORINFO()
        monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or not monitor:
            return info
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
            return info
        mon = monitor_info.rcMonitor
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        mon_width = max(1, mon.right - mon.left)
        mon_height = max(1, mon.bottom - mon.top)
        coverage = min(1.0, (width * height) / float(mon_width * mon_height))
        tolerance = 8
        fullscreen = (
            abs(rect.left - mon.left) <= tolerance
            and abs(rect.top - mon.top) <= tolerance
            and abs(rect.right - mon.right) <= tolerance
            and abs(rect.bottom - mon.bottom) <= tolerance
        )
        info.update(
            fullscreen=fullscreen,
            large=not info["minimized"] and width >= mon_width * 0.72 and height >= mon_height * 0.72,
            coverage=round(coverage, 3),
        )
    except Exception:
        pass
    return info


def get_foreground_app():
    if not user32:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(length + 1, 2))
        user32.GetWindowTextW(hwnd, buf, len(buf))
        try:
            proc = psutil.Process(pid.value)
            proc_name = proc.name()
            started_at = proc.create_time()
            try:
                proc_path = proc.exe()
            except Exception:
                proc_path = None
        except Exception:
            proc_name = "-"
            started_at = None
            proc_path = None
        return {
            "pid": int(pid.value),
            "process": proc_name,
            "path": proc_path,
            "title": buf.value.strip() or proc_name,
            "started_at": started_at,
            "window": _foreground_window_info(hwnd),
        }
    except Exception:
        return None


def get_idle_seconds():
    if not user32 or not kernel32:
        return 0.0
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    try:
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now32 = int(kernel32.GetTickCount64()) & 0xFFFFFFFF
        elapsed_ms = (now32 - int(info.dwTime)) & 0xFFFFFFFF
        return round(min(elapsed_ms / 1000.0, 31 * 24 * 3600.0), 1)
    except Exception:
        return 0.0


# ---------- Context automation configuration / state machine ----------
_automation_lock = threading.RLock()
_automation_revision = 1


def _default_automation_config():
    return {
        "schema_version": 1,
        "automation": {"enabled": True, "manual_override": "until_state_change"},
        "detection": {
            "idle_timeout_sec": 300,
            "work_processes": ["code.exe", "devenv.exe", "clion64.exe", "windowsterminal.exe"],
            "app_classification": {},
            "stability": {
                "GAME": {"enter_ms": 2000, "exit_ms": 10000},
                "VIDEO": {"enter_ms": 1000, "exit_ms": 7000},
                "WORK": {"enter_ms": 1500, "exit_ms": 2500},
                "IDLE": {"enter_ms": 0, "exit_ms": 0},
            },
        },
        "profiles": {
            "GAME": {"pages": ["game_performance"], "rotation": {"enabled": False, "interval_sec": 10}},
            "VIDEO": {"pages": ["now_playing"], "rotation": {"enabled": False, "interval_sec": 10}},
            "WORK": {"pages": ["clipboard"], "rotation": {"enabled": False, "interval_sec": 10}},
            "IDLE": {
                "pages": ["clipboard", "diagnostics", "game_performance"],
                "rotation": {"enabled": True, "interval_sec": 8},
            },
            "DEFAULT": {"pages": ["today"], "rotation": {"enabled": True, "interval_sec": 10}},
        },
    }


def _json_clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _bounded_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _process_basename(value):
    if not isinstance(value, str):
        return ""
    name = value.strip().replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if not name or len(name) > 128 or not re.fullmatch(r"[a-z0-9._ ()+-]+", name):
        return ""
    return name


def validate_automation_config(value):
    defaults = _default_automation_config()
    value = value if isinstance(value, dict) else {}
    result = _json_clone(defaults)

    automation = value.get("automation") if isinstance(value.get("automation"), dict) else {}
    result["automation"]["enabled"] = bool(automation.get("enabled", defaults["automation"]["enabled"]))

    detection = value.get("detection") if isinstance(value.get("detection"), dict) else {}
    result["detection"]["idle_timeout_sec"] = _bounded_int(
        detection.get("idle_timeout_sec"), defaults["detection"]["idle_timeout_sec"], 60, 3600
    )
    work_processes = []
    for item in detection.get("work_processes", defaults["detection"]["work_processes"]):
        name = _process_basename(item)
        if name and name not in work_processes:
            work_processes.append(name)
        if len(work_processes) >= 100:
            break
    result["detection"]["work_processes"] = work_processes or list(defaults["detection"]["work_processes"])

    classifications = {}
    source_classifications = detection.get("app_classification", {})
    if isinstance(source_classifications, dict):
        for raw_name, raw_kind in list(source_classifications.items())[:100]:
            name = _process_basename(raw_name)
            kind = str(raw_kind).upper()
            if name and kind in ("GAME", "VIDEO", "WORK", "IGNORE"):
                classifications[name] = kind
    result["detection"]["app_classification"] = classifications

    stability = detection.get("stability") if isinstance(detection.get("stability"), dict) else {}
    for state, limits in (("GAME", (0, 15000, 0, 30000)), ("VIDEO", (0, 15000, 0, 30000)),
                          ("WORK", (0, 15000, 0, 30000)), ("IDLE", (0, 0, 0, 2000))):
        incoming = stability.get(state) if isinstance(stability.get(state), dict) else {}
        base = defaults["detection"]["stability"][state]
        result["detection"]["stability"][state] = {
            "enter_ms": _bounded_int(incoming.get("enter_ms"), base["enter_ms"], limits[0], limits[1]),
            "exit_ms": _bounded_int(incoming.get("exit_ms"), base["exit_ms"], limits[2], limits[3]),
        }

    profiles = value.get("profiles") if isinstance(value.get("profiles"), dict) else {}
    for state in CONTEXT_STATES:
        incoming = profiles.get(state) if isinstance(profiles.get(state), dict) else {}
        base = defaults["profiles"][state]
        pages = []
        for page in incoming.get("pages", base["pages"]):
            if page in PAGE_IDS and page not in pages:
                pages.append(page)
        if not pages:
            pages = list(base["pages"])
        rotation = incoming.get("rotation") if isinstance(incoming.get("rotation"), dict) else {}
        result["profiles"][state] = {
            "pages": pages,
            "rotation": {
                "enabled": bool(rotation.get("enabled", base["rotation"]["enabled"])),
                "interval_sec": _bounded_int(rotation.get("interval_sec"), base["rotation"]["interval_sec"], 5, 30),
            },
        }
    return result


def _load_automation_config():
    try:
        if AUTOMATION_CONFIG_FILE.exists():
            return validate_automation_config(json.loads(AUTOMATION_CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        log(f"Automation config load error: {e}")
    return _default_automation_config()


_automation_config = _load_automation_config()


def get_automation_config():
    with _automation_lock:
        return {"ok": True, "revision": _automation_revision, "config": _json_clone(_automation_config)}


def save_automation_config(value):
    global _automation_config, _automation_revision
    clean = validate_automation_config(value)
    try:
        temp_path = AUTOMATION_CONFIG_FILE.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(AUTOMATION_CONFIG_FILE)
    except Exception as e:
        return {"ok": False, "status": 500, "error": f"설정 저장 실패: {e}"}
    with _automation_lock:
        _automation_config = clean
        _automation_revision += 1
        revision = _automation_revision
    engine = globals().get("_context_engine")
    if engine is not None:
        engine_lock = globals().get("_context_engine_lock")
        if engine_lock is None:
            engine.configure(clean)
        else:
            with engine_lock:
                engine.configure(clean)
    return {"ok": True, "revision": revision, "config": _json_clone(clean)}


class _DetectorLatch:
    def __init__(self, name):
        self.name = name
        self.active = False
        self.raw = False
        self.true_since = None
        self.false_since = None
        self.enter_ms = 0
        self.exit_ms = 0

    def configure(self, values):
        self.enter_ms = int(values.get("enter_ms", 0))
        self.exit_ms = int(values.get("exit_ms", 0))

    def deactivate(self):
        self.active = False
        self.raw = False
        self.true_since = None
        self.false_since = None

    def update(self, raw, now):
        self.raw = bool(raw)
        if self.raw:
            self.false_since = None
            if self.active:
                self.true_since = None
                return
            if self.true_since is None:
                self.true_since = now
            if (now - self.true_since) * 1000.0 >= self.enter_ms:
                self.active = True
                self.true_since = None
        else:
            self.true_since = None
            if not self.active:
                self.false_since = None
                return
            if self.false_since is None:
                self.false_since = now
            if (now - self.false_since) * 1000.0 >= self.exit_ms:
                self.active = False
                self.false_since = None

    def snapshot(self, now):
        if self.raw and not self.active and self.true_since is not None:
            phase = "entering"
            remaining = max(0, self.enter_ms - int((now - self.true_since) * 1000.0))
        elif not self.raw and self.active and self.false_since is not None:
            phase = "exiting"
            remaining = max(0, self.exit_ms - int((now - self.false_since) * 1000.0))
        else:
            phase = "active" if self.active else "inactive"
            remaining = 0
        return {"raw": self.raw, "active": self.active, "phase": phase, "remaining_ms": remaining}


class ContextEngine:
    PRIORITY = ("GAME", "VIDEO", "IDLE", "WORK")
    BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}
    VIDEO_APPS = {"vlc.exe", "potplayer.exe", "potplayermini64.exe", "mpc-hc64.exe", "wmplayer.exe"}
    MEDIA_DISPLAY_APPS = {"spotify.exe"}
    VIDEO_TITLE_HINTS = ("youtube", "netflix", "twitch", "prime video", "disney+", "watcha", "wavve", "tving")
    MUSIC_TITLE_HINTS = ("spotify", "youtube music", "soundcloud", "apple music")
    GENERIC_GAME_EXCLUDES = {
        "applicationframehost.exe", "chatgpt.exe", "code.exe", "discord.exe", "dwm.exe", "explorer.exe",
        "firefox.exe", "chrome.exe", "msedge.exe", "obs64.exe", "potplayer.exe", "potplayermini64.exe",
        "searchhost.exe", "spotify.exe", "startmenuexperiencehost.exe", "taskmgr.exe", "vlc.exe",
        "windowsterminal.exe",
    }

    def __init__(self, config):
        self.instance_id = uuid.uuid4().hex[:12]
        self.sequence = 0
        self.state = "DEFAULT"
        self.entered_at = datetime.now().isoformat(timespec="seconds")
        self.reasons = ["fallback"]
        self.subject = None
        self.game_process = None
        self.last_idle_seconds = 0.0
        self.latches = {name: _DetectorLatch(name) for name in self.PRIORITY}
        self.configure(config)

    def configure(self, config):
        stability = config["detection"]["stability"]
        for name, latch in self.latches.items():
            latch.configure(stability[name])

    def _raw_matches(self, foreground, fps, media, idle_seconds, config):
        proc = _process_basename((foreground or {}).get("process", ""))
        window = (foreground or {}).get("window") or {}
        category = config["detection"]["app_classification"].get(proc)
        work_names = set(config["detection"]["work_processes"])
        present_samples = (fps or {}).get("frame_samples") or []
        present_active = len(present_samples) >= 3 and (fps or {}).get("sample_age_ms", 999999) <= 2000
        large_window = bool(window.get("large") or window.get("fullscreen"))

        media_status = str((media or {}).get("status") or "").lower()
        playback_type = str((media or {}).get("playback_type") or "").lower()
        source = str((media or {}).get("source") or "").lower()
        foreground_title = str((foreground or {}).get("title") or "").lower()
        media_active = media_status in ("playing", "paused") and bool((media or {}).get("title"))
        source_matches = bool(proc and proc.removesuffix(".exe") in source)
        browser_video_hint = (
            proc in self.BROWSERS and source_matches
            and not any(hint in foreground_title for hint in self.MUSIC_TITLE_HINTS)
            and (playback_type == "video" or bool(window.get("fullscreen"))
                 or any(hint in foreground_title for hint in self.VIDEO_TITLE_HINTS))
        )

        work_raw = bool(proc and (proc in work_names or category == "WORK"))
        explicit_video = category == "VIDEO"
        foreground_media_app = media_active and proc in self.MEDIA_DISPLAY_APPS and source_matches
        semantic_video = media_active and (
            browser_video_hint
            or (playback_type == "video" and source_matches)
        )
        video_app_fallback = media_active and proc in self.VIDEO_APPS and source_matches and playback_type != "music"
        video_raw = explicit_video or foreground_media_app or semantic_video or video_app_fallback

        explicit_game = category == "GAME"
        generic_excludes = self.GENERIC_GAME_EXCLUDES | work_names | self.BROWSERS | self.VIDEO_APPS
        generic_game = (
            present_active and large_window and proc not in generic_excludes
            and category not in ("VIDEO", "WORK", "IGNORE")
        )
        game_raw = explicit_game or generic_game
        idle_raw = idle_seconds >= config["detection"]["idle_timeout_sec"]

        reasons = {
            "GAME": (["explicit_app_game"] if explicit_game else ["foreground_present_activity", "large_window"]),
            "VIDEO": (["explicit_app_video"] if explicit_video else
                      ["foreground_media_app"] if foreground_media_app else ["playing_video_session"]),
            "WORK": ["registered_work_app"],
            "IDLE": ["idle_timeout"],
            "DEFAULT": ["fallback"],
        }
        facts = {
            "foreground_process": proc or None,
            "present_active": present_active,
            "large_window": large_window,
            "media_status": media_status or None,
            "media_playback_type": playback_type or None,
            "idle_seconds": idle_seconds,
            "app_classification": category,
        }
        return {"GAME": game_raw, "VIDEO": video_raw, "WORK": work_raw, "IDLE": idle_raw}, reasons, facts

    def evaluate(self, foreground, fps, media, idle_seconds, config, revision):
        now = time.monotonic()
        self.last_idle_seconds = idle_seconds
        raw, reasons, facts = self._raw_matches(foreground, fps, media, idle_seconds, config)
        current_process = facts.get("foreground_process")
        game_latch = self.latches["GAME"]
        if raw["GAME"]:
            self.game_process = current_process
        game_focus_left = bool(
            game_latch.active and self.game_process and current_process != self.game_process
        )
        for name, latch in self.latches.items():
            if name == "GAME" and game_focus_left:
                latch.deactivate()
            else:
                latch.update(raw[name], now)
        facts["game_focus_left"] = game_focus_left
        chosen = next((name for name in self.PRIORITY if self.latches[name].active), "DEFAULT")
        if chosen != self.state:
            if self.state == "GAME" and chosen != "GAME":
                self.game_process = None
            self.state = chosen
            self.sequence += 1
            self.entered_at = datetime.now().isoformat(timespec="seconds")
        self.reasons = reasons[chosen]
        self.subject = {
            "process": (foreground or {}).get("process"),
            "pid": (foreground or {}).get("pid"),
            "title": (foreground or {}).get("title"),
        }
        return self.snapshot(config, revision, facts=facts)

    def snapshot(self, config, revision, facts=None, suspended=False):
        now = time.monotonic()
        detectors = {name: latch.snapshot(now) for name, latch in self.latches.items()}
        pending = next(
            ({"state": name, **detectors[name]} for name in self.PRIORITY
             if detectors[name]["phase"] in ("entering", "exiting")),
            None,
        )
        return {
            "state": self.state,
            "token": f"{self.instance_id}:{self.sequence}",
            "entered_at": self.entered_at,
            "reasons": list(self.reasons),
            "subject": self.subject,
            "pending": pending,
            "detectors": detectors,
            "facts": facts or {"idle_seconds": self.last_idle_seconds},
            "suspended": bool(suspended),
            "automation_enabled": bool(config["automation"]["enabled"]),
            "config_revision": revision,
            "profile": _json_clone(config["profiles"][self.state]),
        }


_context_engine = ContextEngine(_automation_config)
_context_engine_lock = threading.RLock()


# ---------- FPS / PresentMon ----------
_fps_lock = threading.Lock()
_fps_frames = defaultdict(deque)
_fps_state = {"available": False, "status": "starting", "error": None}
_presentmon_process = None
_presentmon_job = None
_presentmon_lock = threading.Lock()
_presentmon_sessions_cleaned = False


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def create_kill_on_close_job(proc):
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    if not ok or not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(proc._handle)):
        kernel32.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def close_presentmon():
    global _presentmon_process, _presentmon_job
    with _presentmon_lock:
        proc = _presentmon_process
        job = _presentmon_job
        _presentmon_process = None
        _presentmon_job = None
    if proc:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
    if job:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass


def find_presentmon():
    if PRESENTMON_EXE.exists():
        return str(PRESENTMON_EXE)
    return None


def _csv_value(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, "", "NA"):
            return value
    return None


def presentmon_sampler():
    global _presentmon_process, _presentmon_job, _presentmon_sessions_cleaned
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        exe = find_presentmon()
        if not exe:
            with _fps_lock:
                _fps_state.update(available=False, status="not-installed", error="PresentMon not found")
            _stop.wait(10)
            continue

        if not _presentmon_sessions_cleaned:
            names = {PRESENTMON_SESSION_NAME, LEGACY_PRESENTMON_SESSION_PREFIX}
            try:
                sessions = run(["logman", "query", "-ets"], timeout=8)
                names.update(line.split()[0] for line in sessions.stdout.splitlines()
                             if line.strip().startswith((PRESENTMON_SESSION_NAME,
                                                         LEGACY_PRESENTMON_SESSION_PREFIX)))
            except Exception:
                pass
            try:
                session_pattern = rf'(?:{PRESENTMON_SESSION_NAME}|{LEGACY_PRESENTMON_SESSION_PREFIX})-\d+'
                names.update(re.findall(session_pattern,
                                        (LOG_DIR / "bridge.log").read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                pass
            for name in names:
                run([exe, "--session_name", name, "--terminate_existing_session"], timeout=5)
            _presentmon_sessions_cleaned = True

        cmd = [
            exe,
            "--output_stdout",
            "--no_console_stats",
            "--v2_metrics",
            "--track_frame_type",
            "--qpc_time_ms",
            "--set_circular_buffer_size", "65536",
            "--timed", "600",
            "--terminate_after_timed",
            "--session_name", PRESENTMON_SESSION_NAME,
            "--stop_existing_session",
        ]
        stderr_lines = deque(maxlen=8)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            job = create_kill_on_close_job(proc)
            with _presentmon_lock:
                _presentmon_process = proc
                _presentmon_job = job
            # The lock event can arrive between the active-session check and
            # process creation. Close this just-created capture immediately.
            if is_session_locked():
                close_presentmon()
                error = None
                continue

            def drain_stderr():
                for line in proc.stderr or ():
                    if line.strip():
                        stderr_lines.append(line.strip())

            threading.Thread(target=drain_stderr, daemon=True).start()
            with _fps_lock:
                _fps_state.update(available=True, status="capturing", error=None)

            reader = csv.DictReader(proc.stdout or ())
            with _fps_lock:
                _fps_state["columns"] = reader.fieldnames
            for row in reader:
                if _stop.is_set():
                    break
                try:
                    pid = int(float(_csv_value(row, "ProcessID", "ProcessId") or 0))
                    if pid <= 0:
                        continue
                    chain = _csv_value(row, "SwapChainAddress", "SwapChain") or "default"
                    present_ms = _csv_value(row, "MsBetweenPresents", "msBetweenPresents", "FrameTime", "CPUFrameTime")
                    app_ms = _csv_value(row, "MsBetweenAppStart", "msBetweenAppStart", "FrameTime")
                    display_ms = _csv_value(row, "MsBetweenDisplayChange", "msBetweenDisplayChange", "DisplayedTime")
                    present_ms = float(present_ms) if present_ms is not None else None
                    app_ms = float(app_ms) if app_ms is not None else None
                    display_ms = float(display_ms) if display_ms is not None else None
                    frame_type = (_csv_value(row, "FrameType", "frameType") or "").strip()
                    now = time.monotonic()
                    with _fps_lock:
                        q = _fps_frames[(pid, chain)]
                        q.append((now, present_ms, app_ms, display_ms, frame_type))
                        while q and now - q[0][0] > 2.0:
                            q.popleft()
                except (TypeError, ValueError):
                    continue
            code = proc.wait(timeout=3) if proc.poll() is None else proc.returncode
            with _fps_lock:
                _fps_state["stderr"] = list(stderr_lines)
            error = None if code == 0 else (" | ".join(stderr_lines) or f"PresentMon exited ({code})")
        except Exception as e:
            error = str(e)
        finally:
            close_presentmon()

        if is_session_locked():
            with _fps_lock:
                _fps_state.update(available=False, status="paused", error=None)
                _fps_frames.clear()
        elif error:
            with _fps_lock:
                _fps_state.update(available=False, status="error", error=error)
                _fps_frames.clear()
            log(f"PresentMon: {error}")
            _stop.wait(60 if "access denied" in error.lower() else 1)
        else:
            _stop.wait(0.15)


def get_fps(pid):
    now = time.monotonic()
    with _fps_lock:
        base = dict(_fps_state)
        candidates = []
        tracked_pids = set()
        for (frame_pid, chain), q in list(_fps_frames.items()):
            while q and now - q[0][0] > 2.0:
                q.popleft()
            if not q:
                if now - (q[-1][0] if q else 0) > 3:
                    _fps_frames.pop((frame_pid, chain), None)
                continue
            tracked_pids.add(frame_pid)
            if frame_pid == pid:
                candidates.append((len(q), chain, list(q)))
        base["tracked_pids"] = sorted(tracked_pids)

    if not candidates:
        return {**base, "fps": None, "frame_time_ms": None, "frame_samples": [], "pid": pid}

    _, chain, frames = max(candidates, key=lambda item: item[0])
    age = now - frames[-1][0]
    if age > 2.0:
        return {**base, "fps": None, "frame_time_ms": None, "frame_samples": [], "pid": pid}
    values = [[round(x[1], 3) if x[1] is not None else None,
               round(x[2], 3) if x[2] is not None else None,
               round(x[3], 3) if x[3] is not None else None,
               x[4]] for x in frames[-600:]]
    return {
        **base,
        "fps": None,
        "frame_time_ms": None,
        "frame_samples": values,
        "sample_age_ms": round(age * 1000.0),
        "computed_by": "phone",
        "pid": pid,
        "swapchain": chain,
    }


# ---------- Storage ----------
_storage_lock = threading.Lock()
_storage_state = {"volumes": [], "physical": [], "io": {"read_mbps": 0.0, "write_mbps": 0.0, "busy_percent": None}}


def _physical_disks():
    script = r'''Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object FriendlyName,MediaType,BusType,HealthStatus,Size | ConvertTo-Json -Compress'''
    try:
        r = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        x = json.loads(r.stdout.strip())
        if isinstance(x, dict):
            x = [x]
        out = []
        for d in x:
            size = d.get("Size")
            out.append({
                "name": d.get("FriendlyName") or "Disk",
                "media": str(d.get("MediaType") or "Unknown"),
                "bus": str(d.get("BusType") or "Unknown"),
                "health": str(d.get("HealthStatus") or "Unknown"),
                "size_gb": round(float(size) / 1024**3, 1) if size else None,
            })
        return out
    except Exception as e:
        log(f"Physical disk query error: {e}")
        return []


def storage_sampler():
    prev = psutil.disk_io_counters()
    prev_t = time.monotonic()
    physical = []
    next_phys = 0.0
    generation = get_session_generation()
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        current_generation = get_session_generation()
        if current_generation != generation:
            prev = psutil.disk_io_counters()
            prev_t = time.monotonic()
            next_phys = 0.0
            generation = current_generation
        if _stop.wait(1.0):
            break
        if is_session_locked():
            prev = psutil.disk_io_counters()
            prev_t = time.monotonic()
            continue
        try:
            now = time.monotonic()
            if now >= next_phys:
                physical = _physical_disks()
                next_phys = now + 60.0

            volumes = []
            seen = set()
            for p in psutil.disk_partitions(all=False):
                dev = p.device.rstrip("\\")
                if len(dev) != 2 or dev[1] != ":" or dev.upper() in seen:
                    continue
                seen.add(dev.upper())
                try:
                    u = psutil.disk_usage(dev + "\\")
                except Exception:
                    continue
                volumes.append({
                    "drive": dev.upper(),
                    "percent": round(u.percent, 1),
                    "used_gb": round(u.used / 1024**3, 1),
                    "total_gb": round(u.total / 1024**3, 1),
                    "free_gb": round(u.free / 1024**3, 1),
                })
            volumes.sort(key=lambda x: x["drive"])

            cur = psutil.disk_io_counters()
            io = {"read_mbps": 0.0, "write_mbps": 0.0, "busy_percent": None}
            if prev and cur:
                dt = max(now - prev_t, 0.001)
                io["read_mbps"] = round(max(0.0, (cur.read_bytes - prev.read_bytes) / dt / 1_000_000), 1)
                io["write_mbps"] = round(max(0.0, (cur.write_bytes - prev.write_bytes) / dt / 1_000_000), 1)
                if hasattr(cur, "busy_time") and hasattr(prev, "busy_time"):
                    io["busy_percent"] = round(max(0.0, min(100.0, (cur.busy_time - prev.busy_time) / (dt * 1000.0) * 100.0)), 1)
            prev, prev_t = cur, now

            with _storage_lock:
                _storage_state["volumes"] = volumes
                _storage_state["physical"] = physical
                _storage_state["io"] = io
        except Exception as e:
            log(f"Storage sampler error: {e}")


def get_storage():
    with _storage_lock:
        return json.loads(json.dumps(_storage_state))


# ---------- Windows GSMTC / Spotify ----------
_media_lock = threading.Lock()
_media_state = {
    "available": False,
    "source": None,
    "title": None,
    "artist": None,
    "album": None,
    "status": None,
    "playback_type": None,
    "position_sec": None,
    "duration_sec": None,
    "cover_version": None,
    "error": WINRT_MEDIA_ERROR,
}
_media_cover = b""
_media_cover_type = "image/jpeg"


def _seconds(x):
    if x is None:
        return None
    try:
        return float(x.total_seconds())
    except Exception:
        pass
    try:
        return float(x.seconds)
    except Exception:
        pass
    try:
        return float(x.duration) / 10_000_000.0
    except Exception:
        return None


def _status_name(x):
    try:
        return x.name.lower()
    except Exception:
        return str(x).split(".")[-1].lower()


def _playback_type_name(*values):
    for value in values:
        if value is None:
            continue
        name = _status_name(value)
        if name and name not in ("none", "unknown"):
            return name
    return None


async def _read_cover(thumbnail):
    if thumbnail is None:
        return None
    try:
        stream = await thumbnail.open_read_async()
        size = int(getattr(stream, "size", 0) or 0)
        if size <= 0 or size > 5 * 1024 * 1024:
            return None
        buffer = Buffer(size)
        await stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD)
        return bytes(buffer)
    except Exception as e:
        log(f"Cover read error: {e}")
        return None


async def _read_media_session(session, is_current=False):
    props = await session.try_get_media_properties_async()
    playback = session.get_playback_info()
    timeline = session.get_timeline_properties()
    start = _seconds(getattr(timeline, "start_time", None)) or 0.0
    position = _seconds(getattr(timeline, "position", None))
    end = _seconds(getattr(timeline, "end_time", None))
    position = None if position is None else max(0.0, position - start)
    duration = None if end is None else max(0.0, end - start)
    return {
        "session": session,
        "is_current": bool(is_current),
        "source": getattr(session, "source_app_user_model_id", None),
        "title": getattr(props, "title", "") or "",
        "artist": getattr(props, "artist", "") or getattr(props, "album_artist", "") or "",
        "album": getattr(props, "album_title", "") or "",
        "status": _status_name(getattr(playback, "playback_status", "unknown")),
        "playback_type": _playback_type_name(
            getattr(props, "playback_type", None), getattr(playback, "playback_type", None)
        ),
        "position_sec": position,
        "duration_sec": duration,
        "thumbnail": getattr(props, "thumbnail", None),
    }


def _media_session_rank(item, foreground_process=""):
    status_rank = 100 if item.get("status") == "playing" else 60 if item.get("status") == "paused" else 10
    source = str(item.get("source") or "").lower()
    process_stem = _process_basename(foreground_process).removesuffix(".exe")
    foreground_rank = 80 if process_stem and process_stem in source else 0
    return status_rank + foreground_rank + (30 if item.get("is_current") else 0) + (5 if str(item.get("title") or "").strip() else 0)


async def media_loop():
    global _media_cover, _media_cover_type
    if not WINRT_MEDIA_OK:
        log(f"WinRT media unavailable: {WINRT_MEDIA_ERROR}")
        return

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    last_key = None
    while not _stop.is_set():
        if is_session_locked():
            await asyncio.sleep(0.25)
            continue
        try:
            sessions = list(manager.get_sessions())
            current = manager.get_current_session()
            snapshots = []
            for session in sessions:
                try:
                    snapshots.append(await _read_media_session(session, session == current))
                except Exception as e:
                    log(f"Media session read error: {e}")

            foreground = get_foreground_app()
            foreground_process = (foreground or {}).get("process", "")
            selected = max(snapshots, key=lambda item: _media_session_rank(item, foreground_process)) if snapshots else None
            if selected is None:
                with _media_lock:
                    _media_state.update(
                        available=False, source=None, title=None, artist=None, album=None, status=None,
                        playback_type=None, position_sec=None, duration_sec=None, cover_version=None,
                        sessions=[], error=None,
                    )
                    _media_cover = b""
                last_key = None
            else:
                source = selected["source"]
                title = selected["title"]
                artist = selected["artist"]
                album = selected["album"]
                status = selected["status"]
                key = (source, title, artist, album)
                cover_version = None
                has_active_media = status in ("playing", "paused") and bool(title.strip())
                if not has_active_media:
                    with _media_lock:
                        _media_cover = b""
                    last_key = None
                else:
                    key_changed = key != last_key
                    if key_changed:
                        # Never carry the previous track's bytes into a new key.
                        with _media_lock:
                            _media_cover = b""
                    data = await _read_cover(selected["thumbnail"]) if key_changed else None
                    if data:
                        cover_version = hashlib.sha1(data).hexdigest()[:12]
                        with _media_lock:
                            _media_cover = data
                    elif not key_changed:
                        with _media_lock:
                            cover_version = _media_state.get("cover_version")
                    last_key = key

                with _media_lock:
                    _media_state.update(
                        available=True,
                        source=source,
                        title=title,
                        artist=artist,
                        album=album,
                        status=status,
                        playback_type=selected["playback_type"],
                        position_sec=selected["position_sec"],
                        duration_sec=selected["duration_sec"],
                        cover_version=cover_version,
                        sessions=[{
                            "source": item["source"], "title": item["title"], "status": item["status"],
                            "playback_type": item["playback_type"], "is_current": item["is_current"],
                        } for item in snapshots],
                        error=None,
                    )
        except Exception as e:
            with _media_lock:
                _media_state["error"] = str(e)
            log(f"Media sampler error: {e}")

        await asyncio.sleep(1.0)


def media_thread():
    try:
        asyncio.run(media_loop())
    except Exception as e:
        with _media_lock:
            _media_state["error"] = str(e)
        log(f"Media thread stopped: {e}")


def get_media():
    with _media_lock:
        return dict(_media_state)


def get_cover():
    with _media_lock:
        return bytes(_media_cover), _media_cover_type, _media_state.get("cover_version")


# ---------- Clipboard history ----------
_clipboard_lock = threading.Lock()
_clipboard_os_lock = threading.Lock()
_clipboard_history = []
_clipboard_version = 0
_clipboard_suppressed_restore = None


class _ClipboardTooLarge(Exception):
    pass


def _open_clipboard(owner_hwnd=None, retries=8):
    """Open the shared Windows clipboard, tolerating short-lived owner locks."""
    for attempt in range(retries):
        ctypes.set_last_error(0)
        if user32.OpenClipboard(owner_hwnd):
            return True
        time.sleep(0.01 * (attempt + 1))
    return False


def _read_clipboard_hglobal(fmt, max_bytes):
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = int(kernel32.GlobalSize(handle))
    if size <= 0:
        return None
    if size > max_bytes:
        raise _ClipboardTooLarge(f"clipboard format {fmt} is {size} bytes")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _dib_metadata(data):
    """Validate a packed DIB and return dimensions plus its pixel-data offset."""
    if len(data) < 12:
        return None
    header_size = struct.unpack_from("<I", data, 0)[0]
    if header_size == 12:  # BITMAPCOREHEADER
        width, height, planes, bits = struct.unpack_from("<HHHH", data, 4)
        if not width or not height or planes != 1 or bits not in (1, 4, 8, 24):
            return None
        colors = (1 << bits) if bits <= 8 else 0
        pixel_offset = header_size + colors * 3
        compression = 0
    elif header_size >= 40 and header_size <= len(data):
        width, height = struct.unpack_from("<ii", data, 4)
        planes, bits = struct.unpack_from("<HH", data, 12)
        compression = struct.unpack_from("<I", data, 16)[0]
        colors_used = struct.unpack_from("<I", data, 32)[0]
        valid_bits = bits in (1, 4, 8, 16, 24, 32) or (bits == 0 and compression in (4, 5))
        valid_compression = compression in (0, 1, 2, 3, 4, 5, 6)
        compatible = not ((compression == 1 and bits != 8) or
                          (compression == 2 and bits != 4) or
                          (compression in (4, 5) and bits != 0) or
                          (compression in (3, 6) and bits not in (16, 32)))
        if width <= 0 or not height or planes != 1 or not valid_bits or not valid_compression or not compatible:
            return None
        colors = colors_used or ((1 << bits) if 1 <= bits <= 8 else 0)
        # BITMAPINFOHEADER keeps bit masks immediately after the 40-byte
        # header. V4/V5 headers contain those masks inside the header itself.
        masks = 0
        if header_size == 40 and compression == 3:  # BI_BITFIELDS
            masks = 12
        elif header_size == 40 and compression == 6:  # BI_ALPHABITFIELDS
            masks = 16
        pixel_offset = header_size + masks + colors * 4
        height = abs(height)
    else:
        return None

    if (width > 32768 or height > 32768 or width * height > CLIPBOARD_IMAGE_MAX_PIXELS or
            pixel_offset >= len(data)):
        return None
    if compression in (0, 3, 6):
        row_stride = ((width * bits + 31) // 32) * 4
        if pixel_offset + row_stride * height > len(data):
            return None
    return {"width": width, "height": height, "pixel_offset": pixel_offset}


def _dib_bmp_parts(data):
    info = _dib_metadata(data)
    if not info:
        return None
    file_size = len(data) + 14
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, info["pixel_offset"] + 14)
    return header, data


def _png_chunk(chunk_type, payload):
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _dib_thumbnail_png(data):
    """Create a small RGB PNG for common uncompressed clipboard DIBs."""
    info = _dib_metadata(data)
    if not info:
        return None
    header_size = struct.unpack_from("<I", data, 0)[0]
    if header_size == 12:
        signed_height = info["height"]
        bits = struct.unpack_from("<H", data, 10)[0]
        compression = 0
    elif header_size >= 40:
        signed_height = struct.unpack_from("<i", data, 8)[0]
        bits = struct.unpack_from("<H", data, 14)[0]
        compression = struct.unpack_from("<I", data, 16)[0]
    else:
        return None
    if bits not in (24, 32) or compression not in (0, 3, 6):
        return None

    width, height = info["width"], info["height"]
    scale = min(1.0, CLIPBOARD_THUMBNAIL_MAX_EDGE / max(width, height))
    thumb_width = max(1, int(round(width * scale)))
    thumb_height = max(1, int(round(height * scale)))
    row_stride = ((width * bits + 31) // 32) * 4
    bytes_per_pixel = bits // 8
    pixel_offset = info["pixel_offset"]
    top_down = signed_height < 0

    red_mask, green_mask, blue_mask = 0x00FF0000, 0x0000FF00, 0x000000FF
    standard_masks = True
    if compression in (3, 6):
        if header_size == 40:
            red_mask, green_mask, blue_mask = struct.unpack_from("<III", data, 40)
        elif header_size >= 52:
            red_mask, green_mask, blue_mask = struct.unpack_from("<III", data, 40)
        else:
            return None
        if (not red_mask or not green_mask or not blue_mask or
                red_mask & green_mask or red_mask & blue_mask or green_mask & blue_mask):
            return None
        standard_masks = (red_mask, green_mask, blue_mask) == (0x00FF0000, 0x0000FF00, 0x000000FF)

    def channel(pixel, mask):
        low_bit = mask & -mask
        if not low_bit:
            return 0
        shift = low_bit.bit_length() - 1
        maximum = mask >> shift
        return ((pixel & mask) >> shift) * 255 // maximum if maximum else 0

    rows = bytearray((thumb_width * 3 + 1) * thumb_height)
    out = 0
    x_positions = [min(width - 1, x * width // thumb_width) for x in range(thumb_width)]
    for y in range(thumb_height):
        rows[out] = 0  # PNG filter type: None
        out += 1
        source_y = min(height - 1, y * height // thumb_height)
        stored_y = source_y if top_down else height - 1 - source_y
        row_start = pixel_offset + stored_y * row_stride
        for source_x in x_positions:
            pos = row_start + source_x * bytes_per_pixel
            if bits == 24 or standard_masks:
                blue, green, red = data[pos], data[pos + 1], data[pos + 2]
            else:
                pixel = struct.unpack_from("<I", data, pos)[0]
                red = channel(pixel, red_mask)
                green = channel(pixel, green_mask)
                blue = channel(pixel, blue_mask)
            rows[out], rows[out + 1], rows[out + 2] = red, green, blue
            out += 3

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", thumb_width, thumb_height, 8, 2, 0, 0, 0)
    return signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(rows, 6)) + _png_chunk(b"IEND", b"")


def _new_clipboard_entry(kind, data, **extra):
    digest = hashlib.sha256(kind.encode("ascii") + b"\0" + data).hexdigest()[:24]
    return {
        "id": digest,
        "type": kind,
        "created_at": datetime.now().isoformat(timespec="microseconds"),
        "size_bytes": len(data),
        "data": data,
        **extra,
    }


def _canonical_file_paths(paths):
    """Validate a CF_HDROP path list and return stable UTF-8 JSON bytes."""
    if not isinstance(paths, list) or not paths or len(paths) > CLIPBOARD_FILES_MAX_COUNT:
        raise ValueError("invalid clipboard file list")
    clean = []
    seen = set()
    for value in paths:
        if not isinstance(value, str) or not value or "\0" in value or len(value) > 32767:
            raise ValueError("invalid clipboard file path")
        path = os.path.normpath(value)
        if not os.path.isabs(path):
            raise ValueError("clipboard file path must be absolute")
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            clean.append(path)
    if not clean:
        raise ValueError("empty clipboard file list")
    data = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > CLIPBOARD_FILES_MAX_BYTES:
        raise ValueError("clipboard file list is too large")
    return clean, data


def _parse_hdrop_payload(data):
    """Decode the DROPFILES header and its double-NUL-terminated path list."""
    if not data or len(data) < 20 or len(data) > CLIPBOARD_FILES_MAX_BYTES:
        raise ValueError("invalid CF_HDROP payload size")
    offset, _x, _y, _non_client, wide = struct.unpack_from("<IiiII", data, 0)
    if offset < 20 or offset >= len(data):
        raise ValueError("invalid CF_HDROP file offset")
    tail = data[offset:]
    if wide:
        if len(tail) % 2:
            tail = tail[:-1]
        decoded = tail.decode("utf-16-le", errors="strict")
    else:
        decoded = tail.decode("mbcs", errors="strict")
    end = decoded.find("\0\0")
    if end < 0:
        raise ValueError("unterminated CF_HDROP file list")
    paths = [path for path in decoded[:end].split("\0") if path]
    return _canonical_file_paths(paths)


def _files_clipboard_payload(paths):
    clean, canonical = _canonical_file_paths(paths)
    names = "\0".join(clean) + "\0\0"
    # DROPFILES is five 32-bit fields on Windows. fWide=TRUE makes the path
    # list UTF-16LE, which avoids losing non-ASCII file names.
    payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16-le")
    return canonical, payload


def _clipboard_sequence_after(sequence, reference):
    """Compare wrapping DWORD clipboard sequence numbers."""
    distance = (int(sequence) - int(reference)) & 0xFFFFFFFF
    return 0 < distance < 0x80000000


def _store_clipboard_entry(entry, captured_sequence=None):
    global _clipboard_version, _clipboard_suppressed_restore
    with _clipboard_lock:
        if captured_sequence is not None and _clipboard_suppressed_restore is not None:
            restored_sequence, restored_id = _clipboard_suppressed_restore
            if entry["id"] == restored_id:
                # One restore can produce multiple Windows clipboard sequence
                # changes. Keep the restored value consumed for as long as it
                # remains the current clipboard content; a genuinely different
                # copy below clears the marker. This also covers an older
                # capture that finishes after the restore request.
                return False
            elif (int(captured_sequence) == restored_sequence or
                  _clipboard_sequence_after(captured_sequence, restored_sequence)):
                _clipboard_suppressed_restore = None

        # Re-copying an existing value makes it recent instead of consuming a
        # second slot. IDs consequently stay stable while the process runs.
        _clipboard_history[:] = [x for x in _clipboard_history if x["id"] != entry["id"]]
        _clipboard_history.insert(0, entry)
        del _clipboard_history[CLIPBOARD_HISTORY_LIMIT:]
        while len(_clipboard_history) > 1 and sum(x["size_bytes"] for x in _clipboard_history) > CLIPBOARD_HISTORY_MAX_BYTES:
            _clipboard_history.pop()
        _clipboard_version += 1
        return True


def _remove_restored_clipboard_entry(item_id, sequence):
    global _clipboard_version, _clipboard_suppressed_restore
    with _clipboard_lock:
        _clipboard_suppressed_restore = (int(sequence), item_id)
        original_length = len(_clipboard_history)
        _clipboard_history[:] = [entry for entry in _clipboard_history if entry["id"] != item_id]
        removed = len(_clipboard_history) != original_length
        if removed:
            _clipboard_version += 1
        return removed


def delete_clipboard_item(item_id):
    global _clipboard_version
    with _clipboard_lock:
        original_length = len(_clipboard_history)
        _clipboard_history[:] = [entry for entry in _clipboard_history if entry["id"] != item_id]
        removed = len(_clipboard_history) != original_length
        if removed:
            _clipboard_version += 1
    return {"ok": True, "id": item_id, "removed": removed}


def clear_clipboard_history():
    global _clipboard_version
    with _clipboard_lock:
        removed = len(_clipboard_history)
        if removed:
            _clipboard_history.clear()
            _clipboard_version += 1
    return {"ok": True, "removed": removed}


def _capture_clipboard():
    """Return (outcome, entry, sequence); only a busy clipboard is retried."""
    file_data = None
    image_data = None
    image_format = None
    image_info = None
    text_data = None
    warnings = []
    with _clipboard_os_lock:
        if not _open_clipboard():
            return "busy", None, None
        try:
            captured_sequence = int(user32.GetClipboardSequenceNumber())
            if user32.IsClipboardFormatAvailable(CF_HDROP):
                try:
                    file_data = _read_clipboard_hglobal(CF_HDROP, CLIPBOARD_FILES_MAX_BYTES)
                except _ClipboardTooLarge as e:
                    warnings.append(f"Clipboard file list ignored: {e}")
            if file_data is None:
                for fmt in (CF_DIBV5, CF_DIB):
                    if not user32.IsClipboardFormatAvailable(fmt):
                        continue
                    try:
                        data = _read_clipboard_hglobal(fmt, CLIPBOARD_IMAGE_MAX_BYTES)
                    except _ClipboardTooLarge as e:
                        warnings.append(f"Clipboard image ignored: {e}")
                        continue
                    info = _dib_metadata(data) if data else None
                    if info:
                        image_data, image_format, image_info = data, fmt, info
                        break

            if file_data is None and image_data is None and user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                try:
                    text_data = _read_clipboard_hglobal(CF_UNICODETEXT, CLIPBOARD_TEXT_MAX_BYTES)
                except _ClipboardTooLarge as e:
                    warnings.append(f"Clipboard text ignored: {e}")
        finally:
            user32.CloseClipboard()

    # Parsing/compressing after CloseClipboard keeps the globally shared
    # clipboard locked only for the HGLOBAL copy (a few milliseconds).
    for warning in warnings:
        log(warning)
    if file_data is not None:
        try:
            paths, canonical = _parse_hdrop_payload(file_data)
            return "captured", _new_clipboard_entry("files", canonical, paths=paths), captured_sequence
        except (UnicodeError, ValueError, struct.error) as e:
            log(f"Clipboard file list ignored: {e}")
    if image_data is not None:
        thumbnail = _dib_thumbnail_png(image_data)
        return "captured", _new_clipboard_entry(
            "image", image_data, format=image_format, width=image_info["width"], height=image_info["height"],
            thumbnail=thumbnail,
        ), captured_sequence
    if text_data:
        # GlobalSize can include allocator padding. CF_UNICODETEXT is
        # NUL-terminated, so exclude the terminator and anything following it.
        text = text_data[:len(text_data) - (len(text_data) % 2)].decode("utf-16-le", errors="replace").split("\0", 1)[0]
        if text:
            canonical = text.encode("utf-8", errors="replace")
            return "captured", _new_clipboard_entry("text", canonical, text=text), captured_sequence
    return "ignored", None, captured_sequence


def clipboard_monitor():
    last_sequence = None
    log("Clipboard monitor ready")
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        if is_session_locked():
            continue
        sequence = int(user32.GetClipboardSequenceNumber())
        if last_sequence is None or sequence != last_sequence:
            try:
                outcome, entry, captured_sequence = _capture_clipboard()
                if outcome != "busy":
                    last_sequence = captured_sequence
                if entry:
                    _store_clipboard_entry(entry, captured_sequence)
            except Exception as e:
                # Mark this sequence consumed so a malformed third-party
                # clipboard payload cannot create a tight error/log loop.
                last_sequence = sequence
                log(f"Clipboard monitor error: {e}")
        _stop.wait(0.25)


def get_clipboard_history():
    with _clipboard_lock:
        version = _clipboard_version
        entries = list(_clipboard_history)
    items = []
    for entry in entries:
        item = {k: entry[k] for k in ("id", "type", "created_at", "size_bytes")}
        if entry["type"] == "text":
            text = entry["text"]
            item.update(text=text[:CLIPBOARD_API_TEXT_CHARS], truncated=len(text) > CLIPBOARD_API_TEXT_CHARS)
        elif entry["type"] == "image":
            item.update(width=entry["width"], height=entry["height"], image_url=f"/api/clipboard/image?id={entry['id']}")
        else:
            names = [os.path.basename(path.rstrip("\\/")) or path for path in entry["paths"]]
            item.update(file_count=len(names), names=names[:8], truncated=len(names) > 8)
        item["content_url"] = f"/api/clipboard/content?id={entry['id']}"
        items.append(item)
    return {"ok": True, "version": version, "limit": CLIPBOARD_HISTORY_LIMIT, "items": items}


def get_clipboard_image(item_id):
    with _clipboard_lock:
        entry = next((x for x in _clipboard_history if x["id"] == item_id and x["type"] == "image"), None)
        data = entry["data"] if entry else None
        thumbnail = entry.get("thumbnail") if entry else None
    if thumbnail:
        return "image/png", (thumbnail,)
    # Uncommon DIB encodings that the thumbnailer cannot safely decode retain a
    # BMP fallback. Return it in chunks to avoid duplicating the full DIB merely
    # to prepend the 14-byte file header.
    parts = _dib_bmp_parts(data) if data else None
    return ("image/bmp", parts) if parts else None


def get_clipboard_content(item_id):
    with _clipboard_lock:
        entry = next((x for x in _clipboard_history if x["id"] == item_id), None)
        if entry:
            entry = dict(entry)
    if not entry:
        return None
    headers = {"ETag": f'"{item_id}"', "X-Clipboard-Type": entry["type"]}
    if entry["type"] == "text":
        return "text/plain; charset=utf-8", entry["data"], headers
    if entry["type"] == "image":
        headers["X-Clipboard-Format"] = str(entry["format"])
        return "application/x-windows-dib", entry["data"], headers
    return "application/json; charset=utf-8", json.dumps(
        {"paths": entry["paths"]}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8"), headers


def _entry_clipboard_payload(entry):
    if entry["type"] == "text":
        return CF_UNICODETEXT, entry["text"].encode("utf-16-le") + b"\0\0"
    if entry["type"] == "image":
        return entry["format"], entry["data"]
    missing = [path for path in entry["paths"] if not os.path.exists(path)]
    if missing:
        preview = os.path.basename(missing[0]) or missing[0]
        raise FileNotFoundError(f"original file not found: {preview}")
    _canonical, payload = _files_clipboard_payload(entry["paths"])
    return CF_HDROP, payload


def _restore_clipboard_entry(entry):
    item_id = entry["id"]
    try:
        fmt, payload = _entry_clipboard_payload(entry)
    except FileNotFoundError as e:
        return {"ok": False, "status": 409, "error": str(e)}

    # EmptyClipboard assigns ownership to the HWND passed to OpenClipboard.
    # Passing NULL can leave the clipboard owner NULL and make SetClipboardData
    # fail, so restoration uses our long-lived shutdown-watcher window.
    _clipboard_owner_ready.wait(2.0)
    owner_hwnd = _clipboard_owner_hwnd
    if not owner_hwnd:
        return {"ok": False, "status": 503, "error": "clipboard owner window is unavailable"}

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return {"ok": False, "status": 500, "error": "clipboard memory allocation failed"}
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return {"ok": False, "status": 500, "error": "clipboard memory lock failed"}
    try:
        ctypes.memmove(ptr, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)

    transferred = False
    restored_sequence = None
    removed = False
    with _clipboard_os_lock:
        if not _open_clipboard(owner_hwnd):
            kernel32.GlobalFree(handle)
            return {"ok": False, "status": 503, "error": "clipboard is busy"}
        try:
            if not user32.EmptyClipboard():
                error = ctypes.get_last_error()
                return {"ok": False, "status": 500, "error": f"EmptyClipboard failed: {error}"}
            ctypes.set_last_error(0)
            if not user32.SetClipboardData(fmt, handle):
                error = ctypes.get_last_error()
                return {"ok": False, "status": 500, "error": f"SetClipboardData failed: {error}"}
            transferred = True  # Windows owns the HGLOBAL after this point.
            restored_sequence = int(user32.GetClipboardSequenceNumber())
        finally:
            user32.CloseClipboard()
            if not transferred:
                kernel32.GlobalFree(handle)

        # Register the sequence before releasing the OS lock, otherwise the
        # monitor could capture and reinsert this value between these steps.
        removed = _remove_restored_clipboard_entry(item_id, restored_sequence)

    return {"ok": True, "id": item_id, "type": entry["type"], "removed": removed}


def restore_clipboard_item(item_id):
    with _clipboard_lock:
        entry = next((x for x in _clipboard_history if x["id"] == item_id), None)
    if not entry:
        return {"ok": False, "status": 404, "error": "clipboard item not found"}
    return _restore_clipboard_entry(entry)


def restore_clipboard_content(kind, data, requested_id="", image_format=None):
    if kind == "text":
        if not data or len(data) > CLIPBOARD_TEXT_MAX_BYTES:
            return {"ok": False, "status": 413, "error": "clipboard text is too large or empty"}
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return {"ok": False, "status": 400, "error": "clipboard text is not valid UTF-8"}
        entry = _new_clipboard_entry("text", data, text=text)
    elif kind == "image":
        if not data or len(data) > CLIPBOARD_IMAGE_MAX_BYTES:
            return {"ok": False, "status": 413, "error": "clipboard image is too large or empty"}
        info = _dib_metadata(data)
        if not info:
            return {"ok": False, "status": 400, "error": "invalid clipboard DIB image"}
        if image_format not in (CF_DIB, CF_DIBV5):
            image_format = CF_DIBV5 if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] >= 124 else CF_DIB
        entry = _new_clipboard_entry("image", data, format=image_format, width=info["width"], height=info["height"])
    elif kind == "files":
        if not data or len(data) > CLIPBOARD_FILES_MAX_BYTES:
            return {"ok": False, "status": 413, "error": "clipboard file list is too large or empty"}
        try:
            payload = json.loads(data.decode("utf-8", errors="strict"))
            paths, canonical = _canonical_file_paths(payload.get("paths") if isinstance(payload, dict) else None)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return {"ok": False, "status": 400, "error": "invalid clipboard file list"}
        entry = _new_clipboard_entry("files", canonical, paths=paths)
    else:
        return {"ok": False, "status": 400, "error": "unsupported clipboard type"}

    if requested_id and requested_id != entry["id"]:
        return {"ok": False, "status": 409, "error": "clipboard content id mismatch"}
    return _restore_clipboard_entry(entry)


# ---------- Crash / dump ----------
_dump_cache = {"t": 0.0, "data": None}
_event_cache = {"t": 0.0, "data": []}


def newest_dump():
    now = time.time()
    if now - _dump_cache["t"] < 10:
        return _dump_cache["data"]
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    files = []
    try:
        mini = windir / "Minidump"
        if mini.exists():
            files.extend(mini.glob("*.dmp"))
        mem = windir / "MEMORY.DMP"
        if mem.exists():
            files.append(mem)
    except Exception:
        pass
    data = None
    if files:
        try:
            p = max(files, key=lambda x: x.stat().st_mtime)
            st = p.stat()
            data = {"name": p.name, "path": str(p), "time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), "size_mb": round(st.st_size / 1024**2, 1)}
        except Exception:
            pass
    _dump_cache.update(t=now, data=data)
    return data


def last_events():
    now = time.time()
    if now - _event_cache["t"] < 30:
        return _event_cache["data"]
    script = r'''[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $e=Get-WinEvent -FilterHashtable @{LogName='System';Id=41,1001} -MaxEvents 8 -ErrorAction SilentlyContinue | Select-Object TimeCreated,Id,ProviderName,Message; $e | ConvertTo-Json -Compress -Depth 3'''
    out = []
    try:
        r = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            x = json.loads(r.stdout.strip())
            if isinstance(x, dict):
                x = [x]
            for e in x[:8]:
                msg = (e.get("Message") or "").replace("\r", " ").replace("\n", " ")
                out.append({"id": e.get("Id"), "provider": e.get("ProviderName"), "message": msg[:260], "time": e.get("TimeCreated")})
    except Exception:
        pass
    _event_cache.update(t=now, data=out)
    return out


# ---------- Phone / ADB ----------
_phone_lock = threading.Lock()
_phone_state = {
    "connected": False,
    "last_seen": None,
    "browser": None,
    "last_error": None,
    "model": None,
    "transport": None,
}
_phone_transport_serial = None
_phone_identity_cache = {}
_phone_seen = False
_phone_seen_transport = None
_phone_pwa_package = None
_phone_shutdown_lock = threading.Lock()
_phone_shutdown_done = False
_screen_guardian_lock = threading.Lock()
_screen_guardian_process = None
_screen_guardian_job = None
_screen_guardian_serial = None
_phone_power_lock = threading.Lock()


def set_phone(**kw):
    with _phone_lock:
        _phone_state.update(kw)


def get_phone():
    with _phone_lock:
        return dict(_phone_state)


def _valid_adb_endpoint(value):
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return False
    try:
        host, port = value.rsplit(":", 1)
        return bool(host) and 1 <= int(port) <= 65535
    except (ValueError, TypeError):
        return False


def load_phone_config():
    config = {
        "mode": "auto",
        "prefer_wireless": True,
        "usb_fallback": True,
        "preferred_hardware_serial": "",
        "preferred_model": "",
        "legacy_endpoints": [],
    }
    try:
        if PHONE_CONFIG_FILE.exists():
            saved = json.loads(PHONE_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
    except Exception as e:
        config["config_error"] = str(e)

    if config.get("mode") not in ("auto", "wireless", "usb"):
        config["mode"] = "auto"
    config["prefer_wireless"] = bool(config.get("prefer_wireless", True))
    config["usb_fallback"] = bool(config.get("usb_fallback", True))
    for key in ("preferred_hardware_serial", "preferred_model"):
        value = config.get(key, "")
        config[key] = value.strip() if isinstance(value, str) else ""
    endpoints = config.get("legacy_endpoints", [])
    if not isinstance(endpoints, list):
        endpoints = []
    config["legacy_endpoints"] = [x for x in endpoints if _valid_adb_endpoint(x)]
    return config


def _connection_kind(serial):
    return "wireless" if ":" in serial and not serial.startswith("emulator-") else "usb"


def _normalize_model(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def list_adb_devices():
    try:
        result = adb("devices", "-l", timeout=5)
    except Exception as e:
        return [], str(e)
    if result.returncode != 0:
        return [], (result.stderr or result.stdout or "adb devices failed").strip()

    devices = []
    for raw_line in result.stdout.splitlines()[1:]:
        parts = raw_line.strip().split()
        if len(parts) < 2 or parts[1] not in ("device", "offline", "unauthorized"):
            continue
        serial, state = parts[0], parts[1]
        if serial.startswith("emulator-"):
            continue
        details = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                details[key] = value
        devices.append({
            "serial": serial,
            "state": state,
            "model": details.get("model", "").replace("_", " "),
            "product": details.get("product", ""),
            "connection": _connection_kind(serial),
        })
    return devices, None


def device_adb(serial, *args, timeout=8):
    if not serial:
        raise RuntimeError("no ADB phone transport selected")
    return adb("-s", serial, *args, timeout=timeout)


def device_adb_ok(serial, *args, timeout=8):
    try:
        return device_adb(serial, *args, timeout=timeout).returncode == 0
    except Exception:
        return False


def _stop_phone_screen_guardian_locked():
    global _screen_guardian_process, _screen_guardian_job, _screen_guardian_serial
    proc = _screen_guardian_process
    job = _screen_guardian_job
    _screen_guardian_process = None
    _screen_guardian_job = None
    _screen_guardian_serial = None
    if proc:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                pass
    if job:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass


def stop_phone_screen_guardian():
    with _screen_guardian_lock:
        _stop_phone_screen_guardian_locked()


def start_phone_screen_guardian(serial):
    """Bind the phone's screen timeout to this bridge process.

    The remote shell stores the user's original timeout, applies a long timeout,
    then waits on stdin. A normal stop closes stdin; an abnormal bridge exit
    closes the Windows job and its adb client. Either path makes the remote shell
    restore the original value. The explicit shutdown path then sleeps the phone.
    """
    global _screen_guardian_process, _screen_guardian_job, _screen_guardian_serial
    if is_session_locked():
        return False
    with _screen_guardian_lock:
        if is_session_locked():
            return False
        if (_screen_guardian_process and _screen_guardian_process.poll() is None
                and _screen_guardian_serial == serial):
            return True
        _stop_phone_screen_guardian_locked()
        state_file = "/data/local/tmp/a51_dashboard_screen_timeout"
        script = (
            f'state={state_file}; '
            'if [ -s "$state" ]; then original=$(cat "$state" 2>/dev/null); '
            'else original=$(settings get system screen_off_timeout 2>/dev/null); '
            'case "$original" in ""|*[!0-9]*) original=30000;; esac; '
            'printf "%s\\n" "$original" > "$state"; fi; '
            'case "$original" in ""|*[!0-9]*) original=30000;; esac; '
            'cleanup() { settings put system screen_off_timeout "$original"; '
            'rm -f "$state"; input keyevent KEYCODE_SLEEP; }; '
            'trap cleanup EXIT HUP TERM; '
            f'settings put system screen_off_timeout {PHONE_SCREEN_TIMEOUT_ACTIVE_MS}; '
            'input keyevent KEYCODE_WAKEUP; '
            'while read dashboard_keepalive; do :; done'
        )
        try:
            proc = subprocess.Popen(
                [str(ADB_EXE), "-s", serial, "shell", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            job = create_kill_on_close_job(proc)
        except Exception as e:
            try:
                proc.terminate()
            except Exception:
                pass
            log(f"Phone screen guardian failed: {e}")
            return False
        _screen_guardian_process = proc
        _screen_guardian_job = job
        _screen_guardian_serial = serial
        return True


def _hardware_serial(device):
    serial = device["serial"]
    if device["connection"] == "usb":
        return serial
    if serial in _phone_identity_cache:
        return _phone_identity_cache[serial]
    try:
        result = device_adb(serial, "shell", "getprop", "ro.serialno", timeout=4)
        identity = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        identity = ""
    if identity:
        _phone_identity_cache[serial] = identity
    return identity


def _parse_mdns_services(output):
    services = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[-2].startswith("_adb") and _valid_adb_endpoint(parts[-1]):
            services.append({"name": parts[0], "service": parts[-2], "endpoint": parts[-1]})
    return services


def _last_wireless_endpoint():
    try:
        endpoint = LAST_WIRELESS_ENDPOINT_FILE.read_text(encoding="ascii").strip()
        return endpoint if _valid_adb_endpoint(endpoint) else None
    except Exception:
        return None


def _remember_wireless_endpoint(endpoint):
    if not _valid_adb_endpoint(endpoint) or endpoint == _last_wireless_endpoint():
        return
    try:
        temp = LAST_WIRELESS_ENDPOINT_FILE.with_suffix(".tmp")
        temp.write_text(endpoint, encoding="ascii")
        temp.replace(LAST_WIRELESS_ENDPOINT_FILE)
    except Exception:
        pass


def connect_configured_wireless(config, connected_serials=()):
    if config["mode"] == "usb":
        return
    connected = set(connected_serials)
    endpoints = []
    last_endpoint = _last_wireless_endpoint()
    if last_endpoint:
        endpoints.append(last_endpoint)
    endpoints.extend(config["legacy_endpoints"])
    try:
        result = adb("mdns", "services", timeout=5)
        if result.returncode == 0:
            endpoints.extend(
                item["endpoint"] for item in _parse_mdns_services(result.stdout)
                if item["service"] == "_adb-tls-connect._tcp"
            )
    except Exception:
        pass

    for endpoint in dict.fromkeys(endpoints):
        if endpoint in connected:
            continue
        try:
            adb("connect", endpoint, timeout=6)
        except Exception:
            pass


def _active_phone_transport():
    with _phone_lock:
        return _phone_transport_serial


def _activate_phone_transport(device):
    global _phone_transport_serial
    with _phone_lock:
        _phone_transport_serial = device["serial"]
        _phone_state.update(
            connected=True,
            last_seen=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            model=device.get("model") or None,
            transport=device["connection"],
        )
    if device["connection"] == "wireless":
        _remember_wireless_endpoint(device["serial"])


def _clear_phone_transport(error=None):
    global _phone_transport_serial
    with _phone_lock:
        _phone_transport_serial = None
        _phone_state.update(connected=False, transport=None)
        if error:
            _phone_state["last_error"] = error


def select_phone_device(devices, config):
    candidates = [item for item in devices if item["state"] == "device"]
    if not candidates:
        return None, "ADB phone is not connected"

    preferred_serial = config["preferred_hardware_serial"]
    if preferred_serial:
        matching = [item for item in candidates if _hardware_serial(item) == preferred_serial]
        if matching:
            candidates = matching
        else:
            return None, "preferred phone is not connected"
    elif config["preferred_model"]:
        wanted = _normalize_model(config["preferred_model"])
        matching = [item for item in candidates if _normalize_model(item["model"]) == wanted]
        if matching:
            candidates = matching
        else:
            return None, "preferred phone model is not connected"
    else:
        models = {_normalize_model(item["model"]) for item in candidates if item["model"]}
        current = _active_phone_transport()
        if len(models) > 1:
            existing = next((item for item in candidates if item["serial"] == current), None)
            if existing:
                return existing, None
            return None, "multiple ADB phones found; run wireless_setup.bat to choose one"

    mode = config["mode"]
    if mode == "usb":
        candidates = [item for item in candidates if item["connection"] == "usb"]
    elif mode == "wireless" and not config["usb_fallback"]:
        candidates = [item for item in candidates if item["connection"] == "wireless"]
    if not candidates:
        return None, f"no {mode} ADB transport available"

    prefer_wireless = config["prefer_wireless"] and mode != "usb"
    candidates.sort(key=lambda item: (
        0 if (item["connection"] == "wireless") == prefer_wireless else 1,
        0 if item["serial"] == _active_phone_transport() else 1,
        item["serial"],
    ))
    return candidates[0], None


def resolve_phone_device(connect_wireless=False):
    config = load_phone_config()
    devices, error = list_adb_devices()
    if connect_wireless:
        connect_configured_wireless(config, (item["serial"] for item in devices))
        devices, error = list_adb_devices()
    target, select_error = select_phone_device(devices, config)
    return target, select_error or error


def reverse_exists(serial):
    try:
        r = device_adb(serial, "reverse", "--list", timeout=5)
        return r.returncode == 0 and "tcp:8765 tcp:8765" in r.stdout
    except Exception:
        return False


def ensure_reverse(serial):
    return device_adb(serial, "reverse", "tcp:8765", "tcp:8765", timeout=8).returncode == 0


def package_exists(serial, pkg):
    try:
        r = device_adb(serial, "shell", "pm", "list", "packages", pkg, timeout=5)
        return r.returncode == 0 and pkg in r.stdout
    except Exception:
        return False


def installed_pwa_package(serial):
    # The package name is stable after the PWA is installed. Reusing the cache
    # avoids several package-manager queries on every Windows logon.
    try:
        if PWA_PACKAGE_FILE.exists():
            cached = PWA_PACKAGE_FILE.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[A-Za-z0-9._]+", cached) and package_exists(serial, cached):
                return cached
    except Exception:
        pass
    try:
        result = device_adb(serial, "shell", "pm", "list", "packages", "webapk", timeout=5)
        packages = [line.split(":", 1)[1].strip() for line in result.stdout.splitlines() if line.startswith("package:")]
        if len(packages) == 1:
            return packages[0]

        dashboard_packages = []
        for pkg in packages:
            details = device_adb(serial, "shell", "dumpsys", "package", pkg, timeout=5)
            if details.returncode == 0 and 'Authority: "127.0.0.1"' in details.stdout:
                dashboard_packages.append(pkg)
        return dashboard_packages[0] if len(dashboard_packages) == 1 else None
    except Exception:
        return None


def dashboard_is_foreground(serial):
    try:
        window = device_adb(serial, "shell", "dumpsys", "window", timeout=6)
        focused = next((line for line in window.stdout.splitlines() if "mCurrentFocus=" in line), "")
        if "com.android.chrome" not in focused:
            return False
        activity = device_adb(serial, "shell", "dumpsys", "activity", "top", timeout=6)
        return activity.returncode == 0 and "127.0.0.1:8765" in activity.stdout
    except Exception:
        return False


def open_phone_ui(serial):
    global _phone_pwa_package
    if is_session_locked():
        raise RuntimeError("PC session is locked")
    # This keeps the screen awake without periodic traffic while externally
    # powered. phone_watcher also injects a harmless WAKEUP key every ten
    # seconds, which covers battery operation without changing the user's
    # persistent screen timeout.
    device_adb_ok(serial, "shell", "svc", "power", "stayon", "true")
    device_adb_ok(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    device_adb_ok(serial, "shell", "wm", "dismiss-keyguard")
    pwa = installed_pwa_package(serial)
    if pwa:
        _phone_pwa_package = pwa
        try:
            PWA_PACKAGE_FILE.write_text(pwa, encoding="ascii")
        except Exception:
            pass
        device_adb_ok(serial, "shell", "settings", "put", "global", "policy_control", f"immersive.full={pwa}")
        r = device_adb(serial, "shell", "monkey", "-p", pwa, "-c", "android.intent.category.LAUNCHER", "1", timeout=10)
        if r.returncode == 0:
            return "Dashboard PWA"
    device_adb_ok(serial, "shell", "settings", "put", "global", "policy_control", "immersive.full=com.android.chrome")
    url = "http://127.0.0.1:8765"
    browser = None
    if package_exists(serial, "com.android.chrome"):
        if dashboard_is_foreground(serial):
            return "Chrome current"
        r = device_adb(serial, "shell", "am", "start", "-W", "--activity-clear-top", "--activity-single-top",
                       "-a", "android.intent.action.VIEW", "-d", url, "com.android.chrome", timeout=10)
        if r.returncode == 0:
            browser = "Chrome"
    if browser is None and package_exists(serial, "com.sec.android.app.sbrowser"):
        r = device_adb(serial, "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url,
                       "com.sec.android.app.sbrowser", timeout=10)
        if r.returncode == 0:
            browser = "Samsung Internet"
    if browser is None:
        r = device_adb(serial, "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url,
                       timeout=10)
        if r.returncode == 0:
            browser = "Default browser"
    if browser is None:
        raise RuntimeError("browser launch failed")
    return browser


def setup_phone(serial, open_browser=True):
    if not ensure_reverse(serial):
        raise RuntimeError("adb reverse failed")
    browser = open_phone_ui(serial) if open_browser else None
    set_phone(connected=True, last_seen=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), browser=browser, last_error=None)
    return browser


def phone_watcher():
    global _phone_seen, _phone_seen_transport
    next_discovery = 0.0
    discovery_backoff = 2.0
    last_reverse_check = 0.0
    while not _stop.is_set():
        if not wait_for_active_session():
            break
        try:
            now = time.monotonic()
            config = load_phone_config()
            devices, adb_error = list_adb_devices()
            if now >= next_discovery:
                connect_configured_wireless(config, (item["serial"] for item in devices))
                devices, adb_error = list_adb_devices()
                next_discovery = now + discovery_backoff

            target, select_error = select_phone_device(devices, config)
            if target:
                previous = _active_phone_transport()
                _activate_phone_transport(target)
                changed = previous != target["serial"] or _phone_seen_transport != target["serial"]
                reopen = changed or not _phone_seen or _phone_resume_requested.is_set()
                with _phone_power_lock:
                    if not is_session_locked():
                        start_phone_screen_guardian(target["serial"])
                        if reopen:
                            try:
                                browser = setup_phone(target["serial"], True)
                                log(f"Phone attached via {target['connection']}: reverse configured; opened {browser}")
                                _phone_seen = True
                                _phone_seen_transport = target["serial"]
                                _phone_resume_requested.clear()
                                last_reverse_check = now
                            except Exception as e:
                                set_phone(last_error=str(e))
                                _phone_seen = False
                if not reopen and not is_session_locked() and now - last_reverse_check >= 30.0:
                    if not reverse_exists(target["serial"]):
                        ensure_reverse(target["serial"])
                    last_reverse_check = now

                # While USB is active, look more often for the preferred Wi-Fi
                # transport. Once Wi-Fi is active, a 30-second discovery pass is
                # enough to follow dynamic TLS ports without constant ADB churn.
                discovery_backoff = 10.0 if target["connection"] == "usb" else 30.0
                next_discovery = min(next_discovery, now + discovery_backoff)
            else:
                _clear_phone_transport(select_error or adb_error)
                _phone_seen = False
                _phone_seen_transport = None
                discovery_backoff = min(30.0, max(2.0, discovery_backoff * 1.7))
        except Exception as e:
            _clear_phone_transport(str(e))
            _phone_seen = False
            _phone_seen_transport = None
        if _stop.wait(2.5):
            break


def enter_session_standby():
    """Pause expensive work and sleep the phone without dropping ADB reverse."""
    close_presentmon()
    with _fps_lock:
        _fps_state.update(available=False, status="paused", error=None)
        _fps_frames.clear()

    # Serialize against an in-flight PWA launch. If the lock arrives while the
    # browser is opening, this worker runs last and guarantees a dark screen.
    with _phone_power_lock:
        if not is_session_locked():
            return
        stop_phone_screen_guardian()
        serial = _active_phone_transport()
        if not serial:
            log("Session standby: no active phone transport; collectors paused")
            return
        try:
            result = device_adb(serial, "shell", "svc power stayon false; input keyevent KEYCODE_SLEEP", timeout=4)
            log(f"Session standby: phone asleep; rc={result.returncode}")
        except Exception as e:
            log(f"Session standby: phone sleep failed: {e}")


def shutdown_phone():
    """Common phone shutdown path for manual stop and Windows shutdown."""
    global _phone_shutdown_done
    with _phone_shutdown_lock:
        if _phone_shutdown_done:
            return
        _phone_shutdown_done = True
        stop_phone_screen_guardian()

        pwa = _phone_pwa_package
        if not pwa and PWA_PACKAGE_FILE.exists():
            try:
                candidate = PWA_PACKAGE_FILE.read_text(encoding="ascii").strip()
                if re.fullmatch(r"[A-Za-z0-9._]+", candidate):
                    pwa = candidate
            except Exception:
                pass

        # Run every step in a single adb shell round-trip. Separate calls each
        # cost a round-trip and `svc power stayon false` alone exceeds a second,
        # so per-command 1s timeouts were firing during the short Windows
        # shutdown window before the screen-off ever ran. Clear stay-awake before
        # SLEEP or the display will not stay off while USB is attached.
        steps = []
        if pwa:
            steps.append(f"am force-stop {pwa}")
        steps.append("am force-stop com.android.chrome")
        steps.append("svc power stayon false")
        steps.append("input keyevent KEYCODE_SLEEP")
        cmd = "; ".join(steps)
        target, target_error = resolve_phone_device(connect_wireless=True)
        if not target:
            log(f"Phone shutdown skipped: {target_error}")
            return
        serial = target["serial"]
        try:
            result = device_adb(serial, "shell", cmd, timeout=4)
            if result.returncode != 0:
                retry_target, _ = resolve_phone_device(connect_wireless=True)
                if retry_target:
                    result = device_adb(retry_target["serial"], "shell", cmd, timeout=4)
            log(f"Phone shutdown: rc={result.returncode}")
        except Exception as e:
            try:
                retry_target, _ = resolve_phone_device(connect_wireless=True)
                if not retry_target:
                    raise RuntimeError("no phone transport available")
                result = device_adb(retry_target["serial"], "shell", cmd, timeout=4)
                log(f"Phone shutdown (retry): rc={result.returncode}")
            except Exception as retry_error:
                log(f"Phone shutdown failed: {retry_error}")


def shutdown_message_watcher():
    """Own a hidden window for Windows shutdown and session lock events."""
    global _clipboard_owner_hwnd
    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016
    WM_DESTROY = 0x0002
    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x0007
    WTS_SESSION_UNLOCK = 0x0008
    NOTIFY_FOR_THIS_SESSION = 0
    class_name = "A51PcDashboardShutdownWindow"
    wts_registered = False

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    # Without explicit argtypes ctypes guesses a 32-bit int and overflows on the
    # 64-bit hwnd/lparam, flooding stderr with "int too long to convert".
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                set_session_locked(True)
            elif wparam == WTS_SESSION_UNLOCK:
                set_session_locked(False)
            return 0
        if message == WM_QUERYENDSESSION:
            shutdown_phone()
            return 1
        if message == WM_ENDSESSION and wparam:
            # Windows is logging off / shutting down. Sleep the phone, then wind
            # the bridge down cleanly so the next boot starts from a clean state
            # instead of finding a stale PID file or a half-dead process.
            shutdown_phone()
            _stop.set()
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            if _server is not None:
                threading.Thread(target=_server.shutdown, daemon=True).start()
            return 0
        if message == WM_DESTROY:
            if wts_registered:
                wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    hinstance = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = window_proc
    wc.hInstance = hinstance
    wc.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:
        log(f"Shutdown watcher registration failed: {ctypes.get_last_error()}")
        _clipboard_owner_ready.set()
        return
    hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                  None, None, hinstance, None)
    if not hwnd:
        log(f"Shutdown watcher window failed: {ctypes.get_last_error()}")
        _clipboard_owner_ready.set()
        return
    _clipboard_owner_hwnd = hwnd
    _clipboard_owner_ready.set()
    try:
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
        wts_registered = bool(wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION))
        if not wts_registered:
            log(f"Windows session notification registration failed: {ctypes.get_last_error()}")
    except Exception as e:
        log(f"Windows session notification unavailable: {e}")
    # Be notified first at shutdown, before console children like the adb server
    # are torn down, so the phone commands still reach a live device.
    try:
        kernel32.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.SetProcessShutdownParameters.restype = wintypes.BOOL
        kernel32.SetProcessShutdownParameters(0x3FF, 0)
    except Exception:
        pass
    log(f"Windows shutdown watcher ready; session notifications={'ready' if wts_registered else 'unavailable'}")
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    _clipboard_owner_hwnd = None


def relaunch_phone_async():
    def worker():
        try:
            if is_session_locked():
                raise RuntimeError("PC session is locked")
            target, error = resolve_phone_device(connect_wireless=True)
            if not target:
                raise RuntimeError(error or "phone is not connected")
            _activate_phone_transport(target)
            with _phone_power_lock:
                if is_session_locked():
                    raise RuntimeError("PC session is locked")
                ensure_reverse(target["serial"])
                start_phone_screen_guardian(target["serial"])
                b = open_phone_ui(target["serial"])
            set_phone(browser=b, last_error=None)
        except Exception as e:
            set_phone(last_error=str(e))
    threading.Thread(target=worker, daemon=True).start()


# ---------- Status / HTTP ----------
def format_uptime(sec):
    sec = max(0, int(sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def get_status():
    vm = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    session = get_session_state()
    foreground = None if session["locked"] else get_foreground_app()
    fps = get_fps(foreground["pid"] if foreground else 0)
    media = get_media()
    idle_seconds = get_idle_seconds()
    with _automation_lock:
        automation_config = _json_clone(_automation_config)
        automation_revision = _automation_revision
    with _context_engine_lock:
        if session["locked"]:
            context = _context_engine.snapshot(
                automation_config, automation_revision,
                facts={"idle_seconds": idle_seconds}, suspended=True,
            )
        else:
            context = _context_engine.evaluate(
                foreground, fps, media, idle_seconds, automation_config, automation_revision
            )
    return {
        "ok": True,
        "version": VERSION,
        "pc_name": socket.gethostname(),
        "cpu": {**get_cpu(), "freq_mhz": round(freq.current, 0) if freq else None},
        "ram": {"percent": round(vm.percent, 1), "used_gb": round(vm.used / 1024**3, 1), "total_gb": round(vm.total / 1024**3, 1)},
        "gpu": get_gpu(),
        "network": get_network(),
        "foreground": foreground,
        "fps": fps,
        "storage": get_storage(),
        "media": media,
        "context": context,
        "uptime": format_uptime(time.time() - psutil.boot_time()),
        "dump": None if session["locked"] else newest_dump(),
        "events": [] if session["locked"] else last_events(),
        "phone": get_phone(),
        "session": session,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


ACTIONS = {
    "media_prev": lambda: send_app_command(APPCOMMAND_MEDIA_PREVIOUSTRACK),
    "media_play_pause": lambda: send_app_command(APPCOMMAND_MEDIA_PLAY_PAUSE),
    "media_next": lambda: send_app_command(APPCOMMAND_MEDIA_NEXTTRACK),
    "volume_down": lambda: send_app_command(APPCOMMAND_VOLUME_DOWN),
    "volume_up": lambda: send_app_command(APPCOMMAND_VOLUME_UP),
    "volume_mute": lambda: send_app_command(APPCOMMAND_VOLUME_MUTE),
    "launch_spotify": lambda: launch_uri("spotify:"),
    "launch_youtube": lambda: launch_uri("https://www.youtube.com/"),
    "lock_pc": lock_pc,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_body(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_parts(self, code, parts, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(sum(len(part) for part in parts)))
        self.end_headers()
        for part in parts:
            self.wfile.write(part)

    def reject_locked_mutation(self):
        if not is_session_locked():
            return False
        # Do not leave an unread request body on a persistent HTTP connection.
        # Closing it also makes the PWA retry cleanly after Windows unlocks.
        self.close_connection = True
        self.send_body(423, {
            "ok": False,
            "error": "PC session is locked",
            "session": get_session_state(),
        }, headers={"Connection": "close"})
        return True

    def reject_untrusted_mutation(self):
        host = self.headers.get("Host", "").strip().lower()
        origin = self.headers.get("Origin", "").strip().lower()
        token = self.headers.get("X-A51-Token", "")
        if host not in TRUSTED_HOSTS or (origin and origin not in TRUSTED_ORIGINS):
            self.close_connection = True
            self.send_body(403, {"ok": False, "error": "untrusted request origin"},
                           headers={"Connection": "close"})
            return True
        if not token or not secrets.compare_digest(token, API_TOKEN):
            self.close_connection = True
            self.send_body(403, {"ok": False, "error": "missing or invalid request token"},
                           headers={"Connection": "close"})
            return True
        return False

    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.startswith("/?"):
            self.send_body(200, (WEB_DIR / "dashboard.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/health"):
            self.send_body(200, {"ok": True, "version": VERSION, "pid": os.getpid()})
            return
        if self.path == "/api/security":
            self.send_body(200, {"ok": True, "token": API_TOKEN})
            return
        static_files = {
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
            "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
            "/icon.svg": ("assets/icon.svg", "image/svg+xml; charset=utf-8"),
            "/icon-192.png": ("assets/icon-192.png", "image/png"),
            "/icon-512.png": ("assets/icon-512.png", "image/png"),
        }
        if self.path in static_files:
            name, ctype = static_files[self.path]
            self.send_body(200, (WEB_DIR / name).read_bytes(), ctype)
            return
        if self.path.startswith("/api/status"):
            try:
                self.send_body(200, get_status())
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/automation/config":
            self.send_body(200, get_automation_config())
            return
        if self.path == "/api/automation/defaults":
            self.send_body(200, {"ok": True, "config": _default_automation_config()})
            return
        if self.path.startswith("/api/media/cover"):
            data, ctype, ver = get_cover()
            if data:
                self.send_body(200, data, ctype, {"ETag": f'"{ver}"'} if ver else None)
            else:
                self.send_body(404, b"", "application/octet-stream")
            return
        if self.path.startswith("/api/clipboard/content"):
            query = parse_qs(urlsplit(self.path).query)
            item_id = (query.get("id") or [""])[0]
            if not re.fullmatch(r"[0-9a-f]{24}", item_id):
                self.send_body(400, {"ok": False, "error": "invalid clipboard item id"})
                return
            content = get_clipboard_content(item_id)
            if content:
                ctype, body, headers = content
                self.send_body(200, body, ctype, headers)
            else:
                self.send_body(404, {"ok": False, "error": "clipboard content not found"})
            return
        if self.path.startswith("/api/clipboard/image"):
            query = parse_qs(urlsplit(self.path).query)
            item_id = (query.get("id") or [""])[0]
            if not re.fullmatch(r"[0-9a-f]{24}", item_id):
                self.send_body(400, {"ok": False, "error": "invalid clipboard item id"})
                return
            image = get_clipboard_image(item_id)
            if image:
                ctype, parts = image
                self.send_parts(200, parts, ctype, {"ETag": f'"{item_id}"'})
            else:
                self.send_body(404, {"ok": False, "error": "clipboard image not found"})
            return
        if self.path == "/api/clipboard" or self.path.startswith("/api/clipboard?"):
            self.send_body(200, get_clipboard_history())
            return
        self.send_body(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.reject_untrusted_mutation():
            return
        if self.reject_locked_mutation():
            return
        if self.path == "/api/shutdown":
            # Finish the phone actions before acknowledging the request. The
            # caller stops this bridge after the response, so an early 202 can
            # otherwise kill us between force-stop and KEYCODE_SLEEP.
            shutdown_phone()
            self.send_body(200, {"ok": True, "stopping": True, "phone_shutdown_complete": True})
            _stop.set()
            threading.Thread(target=_server.shutdown, daemon=True).start()
            return
        if self.path == "/api/automation/config":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 64 * 1024:
                    self.send_body(413, {"ok": False, "error": "설정 요청 크기가 올바르지 않음"})
                    return
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                value = req.get("config", req) if isinstance(req, dict) else {}
                result = save_automation_config(value)
                status = result.pop("status", 200 if result.get("ok") else 500)
                self.send_body(status, result)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self.send_body(400, {"ok": False, "error": "잘못된 자동화 설정 요청"})
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/action":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n < 0 or n > 4096:
                    self.send_body(413, {"ok": False, "error": "request body too large"})
                    return
                req = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))
                action = req.get("action", "")
                fn = ACTIONS.get(action)
                if not fn:
                    self.send_body(400, {"ok": False, "error": f"unknown action: {action}"})
                    return
                result = fn()
                log(f"action {action}: {result}")
                accepted = not isinstance(result, dict) or result.get("accepted", True)
                self.send_body(200 if accepted else 500, {"ok": accepted, "action": action, "result": result, "error": None if accepted else result.get("error", "action failed")})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self.send_body(400, {"ok": False, "error": "invalid JSON request"})
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/clipboard/delete":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n < 0 or n > 4096:
                    self.send_body(413, {"ok": False, "error": "request body too large"})
                    return
                req = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))
                item_id = req.get("id", "")
                if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-f]{24}", item_id):
                    self.send_body(400, {"ok": False, "error": "invalid clipboard item id"})
                    return
                self.send_body(200, delete_clipboard_item(item_id))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self.send_body(400, {"ok": False, "error": "invalid JSON request"})
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/clipboard/clear":
            self.send_body(200, clear_clipboard_history())
            return
        if self.path == "/api/clipboard/restore-content":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > max(CLIPBOARD_TEXT_MAX_BYTES, CLIPBOARD_IMAGE_MAX_BYTES,
                                     CLIPBOARD_FILES_MAX_BYTES):
                    self.send_body(413, {"ok": False, "error": "clipboard content is too large or empty"})
                    return
                kind = self.headers.get("X-Clipboard-Type", "").strip().lower()
                requested_id = self.headers.get("X-Clipboard-Id", "").strip().lower()
                if requested_id and not re.fullmatch(r"[0-9a-f]{24}", requested_id):
                    self.send_body(400, {"ok": False, "error": "invalid clipboard item id"})
                    return
                try:
                    image_format = int(self.headers.get("X-Clipboard-Format", ""))
                except ValueError:
                    image_format = None
                result = restore_clipboard_content(kind, self.rfile.read(n), requested_id, image_format)
                status = result.pop("status", 200 if result.get("ok") else 500)
                self.send_body(status, result)
            except (ValueError, TypeError):
                self.send_body(400, {"ok": False, "error": "invalid clipboard restore request"})
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/clipboard/restore":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n < 0 or n > 4096:
                    self.send_body(413, {"ok": False, "error": "request body too large"})
                    return
                req = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))
                item_id = req.get("id", "")
                if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-f]{24}", item_id):
                    self.send_body(400, {"ok": False, "error": "invalid clipboard item id"})
                    return
                result = restore_clipboard_item(item_id)
                status = result.pop("status", 200 if result.get("ok") else 500)
                self.send_body(status, result)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self.send_body(400, {"ok": False, "error": "invalid JSON request"})
            except Exception as e:
                self.send_body(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/relaunch_phone":
            self.send_body(202, {"ok": True, "queued": True})
            relaunch_phone_async()
            return
        self.send_body(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    if os.name != "nt":
        print("Windows only.")
        sys.exit(1)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\A51PcDashboardBridge")
    if not _instance_mutex or ctypes.get_last_error() == 183:
        log("Bridge startup blocked: another instance is already running")
        sys.exit(2)

    # Bind first. A stale bridge must not start samplers or an ETW session when
    # another process already owns the dashboard port.
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        server.daemon_threads = True
        _server = server
    except OSError as e:
        log(f"Bridge startup blocked: {e}")
        sys.exit(2)

    try:
        pid_tmp = PID_FILE.with_suffix(".pid.tmp")
        pid_tmp.write_text(json.dumps({"pid": os.getpid(), "version": VERSION,
                                       "started": datetime.now().isoformat(timespec="seconds"),
                                       "exe": sys.executable}), encoding="ascii")
        pid_tmp.replace(PID_FILE)
    except Exception:
        pass

    for target in (cpu_sampler, gpu_sampler, network_sampler, presentmon_sampler, storage_sampler, media_thread,
                   shutdown_message_watcher, clipboard_monitor, phone_watcher):
        threading.Thread(target=target, daemon=True).start()
    log(f"Android Home Dashboard v{VERSION}: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        if _clipboard_owner_hwnd:
            user32.PostMessageW(_clipboard_owner_hwnd, 0x0010, 0, 0)  # WM_CLOSE
        close_presentmon()
        stop_phone_screen_guardian()
        server.server_close()
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
