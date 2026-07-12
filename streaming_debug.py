import os
import time

_DEBUG_LOG_PATH = os.path.join(os.path.dirname(__file__), "streaming_debug.log")
_MAX_LOG_BYTES = 256 * 1024  # keep last 256 KiB

def _trim_log():
    try:
        if not os.path.exists(_DEBUG_LOG_PATH):
            return
        size = os.path.getsize(_DEBUG_LOG_PATH)
        if size <= _MAX_LOG_BYTES:
            return
        with open(_DEBUG_LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        keep = content[-_MAX_LOG_BYTES:]
        # trim to next newline to avoid a broken first line
        nl = keep.find("\n")
        if nl != -1:
            keep = keep[nl + 1:]
        with open(_DEBUG_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(keep)
    except Exception:
        pass

def log_debug(msg):
    try:
        _trim_log()
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def clear_debug_log():
    try:
        with open(_DEBUG_LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
