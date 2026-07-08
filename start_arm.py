#!/usr/bin/env python3
"""
Module:
    start_arm

Description:
    ARM v3 (Android Roblox Manager) - Single-file CLI application.
    Monitors and auto-recovers multiple Roblox instances on Android.
    Runs in Termux with root access. Uses asyncio for concurrency,
    /proc filesystem for zero-subprocess process detection, and
    ANSI escape codes for live table display.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path

VERSION = "3.0.0"


# ============================================================
#  ENVIRONMENT
# ============================================================

def setup_environment():
    """
    Detect Android SDK version and return correct download path.
    Sets TERM environment variable for color support.

    Returns:
        str: The correct sdcard path for this Android version.
    """
    try:
        result = subprocess.run(
            ["getprop", "ro.build.version.sdk"],
            capture_output=True, text=True, timeout=5
        )
        sdk = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 34
    except Exception:
        #@ Logic: default to SDK 34 (Android 14) if detection fails
        sdk = 34

    arm_dir = "/sdcard/download" if sdk < 31 else "/sdcard/Download"
    os.environ.setdefault("TERM", "xterm-256color")
    return arm_dir


# ============================================================
#  CONFIGURATION
# ============================================================

CONFIG_DEFAULTS = {
    "packages":                 {"default": [],     "type": list},
    "place_id":                 {"default": 0,      "type": int},
    "private_server_code":      {"default": "",     "type": str},
    "server_id":                {"default": "",     "type": str},
    "check_interval_running":   {"default": 30,     "type": int},
    "check_interval_recovery":  {"default": 3,      "type": int},
    "max_retries":              {"default": 5,      "type": int},
    "startup_grace_seconds":    {"default": 25,     "type": int},
    "webhook_url":              {"default": "",     "type": str},
    "webhook_enabled":          {"default": False,  "type": bool},
    "webhook_interval":         {"default": 300,    "type": int},
    "screenshot_interval":      {"default": 3600,   "type": int},
    "auto_execute":             {"default": "",     "type": str},
}


def validate_config(data):
    """
    Validate and sanitize config data using CONFIG_DEFAULTS.
    Applies defaults for missing keys and coerces types.

    Args:
        data: Raw config dict from file.

    Returns:
        dict: Cleaned config with defaults applied for missing or invalid keys.
    """
    cleaned = {}
    for key, spec in CONFIG_DEFAULTS.items():
        value         = data.get(key, spec["default"])
        expected_type = spec["type"]

        #@ Logic: coerce types to match expected schema
        if expected_type is list and not isinstance(value, list):
            value = [value] if value else []
        elif expected_type is int and not isinstance(value, int):
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = spec["default"]
        elif expected_type is bool and not isinstance(value, bool):
            value = str(value).lower() in ("true", "1", "yes", "on")
        elif expected_type is str and not isinstance(value, str):
            value = str(value)

        cleaned[key] = value
    return cleaned


def get_config_path(arm_dir):
    """
    Return the config.json path for the given ARM directory.

    Args:
        arm_dir: Base directory where ARM is installed.

    Returns:
        Path: Absolute path to config.json.
    """
    return Path(arm_dir) / "config.json"


def load_config(config_path):
    """
    Load config from disk. Creates default config if file is missing.
    Backs up corrupt files before overwriting with defaults.

    Args:
        config_path: Path to config.json.

    Returns:
        dict: Validated config dict.
    """
    if not config_path.exists():
        config = validate_config({})
        save_config(config_path, config)
        return config

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return validate_config(raw)
    except json.JSONDecodeError:
        #@ Logic: backup corrupt config before overwriting
        backup = config_path.with_suffix(".json.bak")
        try:
            config_path.rename(backup)
        except OSError:
            pass
        config = validate_config({})
        save_config(config_path, config)
        return config


def save_config(config_path, data):
    """
    Atomically write config to disk to prevent corruption on crash.
    Writes to a temp file first, then renames to target path.

    Args:
        config_path: Path to config.json.
        data: Config dict to persist.

    Returns:
        None.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent), suffix=".tmp", prefix=".config_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(config_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================
#  STATE
# ============================================================

class ArmState(str, Enum):
    """ARM service states. str mixin allows JSON serialization."""
    IDLE       = "IDLE"
    STARTING   = "STARTING"
    RUNNING    = "RUNNING"
    RECOVERING = "RECOVERING"
    STOPPED    = "STOPPED"


_package_states = {}


def init_package_state(package):
    """
    Initialize default state tracking for a package.

    Args:
        package: Android package identifier string.

    Returns:
        None.
    """
    _package_states[package] = {
        "status":          ArmState.IDLE,
        "uptime_start":    0.0,
        "crash_count":     0,
        "reconnect_count": 0,
        "was_running":     False,
        "last_seen":       0.0,
        "retry_count":     0,
        "username":        "",
    }


def get_package_state(package):
    """
    Return state dict for a package. Initializes if missing.

    Args:
        package: Android package identifier string.

    Returns:
        dict: Mutable state dict for the package.
    """
    if package not in _package_states:
        init_package_state(package)
    return _package_states[package]


def record_crash(package):
    """
    Record a crash event for a package.

    Args:
        package: Package that crashed.

    Returns:
        None.
    """
    state = get_package_state(package)
    state["crash_count"] += 1


def record_reconnect(package):
    """
    Record a successful reconnect for a package.

    Args:
        package: Package that reconnected.

    Returns:
        None.
    """
    state = get_package_state(package)
    state["reconnect_count"] = state.get("reconnect_count", 0) + 1


def get_uptime(package):
    """
    Calculate uptime in seconds for a package.

    Args:
        package: Android package identifier string.

    Returns:
        float: Uptime in seconds, or 0.0 if not running.
    """
    state = get_package_state(package)
    start = state.get("uptime_start", 0.0)
    if start == 0.0:
        return 0.0
    return time.time() - start


def format_uptime(seconds):
    """
    Format uptime seconds into human-readable string.

    Args:
        seconds: Uptime in seconds.

    Returns:
        str: Formatted string like '2h 15m' or '0m 5s'.
    """
    seconds = int(seconds)
    if seconds < 60:
        return f"0m {seconds}s"
    if seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


# ============================================================
#  PROCESS DETECTION
# ============================================================

_proc_cache     = {}
_proc_cache_ttl = 2.0


def is_package_running(package_name):
    """
    Check if a package is running by scanning /proc/*/cmdline.
    Uses a 2-second cache to avoid redundant filesystem scans.

    Args:
        package_name: Android package identifier (e.g. com.roblox.client).

    Returns:
        bool: True if the process is found running.
    """
    now = time.time()
    if package_name in _proc_cache:
        ts, cached = _proc_cache[package_name]
        if (now - ts) < _proc_cache_ttl:
            return cached

    result = _scan_proc(package_name)
    _proc_cache[package_name] = (now, result)
    return result


def _scan_proc(package_name):
    """
    Synchronous /proc filesystem scan for process detection.
    Zero-subprocess: reads /proc/[pid]/cmdline directly.

    Args:
        package_name: Process name to search for in cmdline files.

    Returns:
        bool: True if found in any /proc/[pid]/cmdline.
    """
    proc_path = Path("/proc")
    target    = package_name.encode()
    try:
        for pid_dir in proc_path.iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes()
                if target in cmdline:
                    return True
            except (OSError, PermissionError):
                continue
    except Exception:
        pass
    return False


async def async_is_package_running(package_name):
    """
    Async wrapper for /proc scan. Runs in thread pool to avoid
    blocking the event loop during filesystem iteration.

    Args:
        package_name: Android package identifier.

    Returns:
        bool: True if the process is found running.
    """
    return await asyncio.to_thread(is_package_running, package_name)


# ============================================================
#  NETWORK CHECK
# ============================================================

_net_cache     = (0.0, False)
_net_cache_ttl = 5.0


async def has_internet(timeout=2.0):
    """
    Check internet connectivity by connecting to Google DNS (8.8.8.8:53).
    Cached for 5 seconds to reduce connection overhead.

    Args:
        timeout: Max seconds to wait for TCP connection.

    Returns:
        bool: True if internet is available.
    """
    global _net_cache
    ts, cached = _net_cache
    if (time.time() - ts) < _net_cache_ttl:
        return cached

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("8.8.8.8", 53),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        _net_cache = (time.time(), True)
        return True
    except (asyncio.TimeoutError, OSError):
        _net_cache = (time.time(), False)
        return False


# ============================================================
#  SHELL COMMANDS
# ============================================================

async def run_command(cmd, timeout=10.0):
    """
    Run a shell command asynchronously with timeout.

    Args:
        cmd: List of command arguments.
        timeout: Max seconds to wait for command completion.

    Returns:
        str: stdout output (stripped), or empty string on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return ""
        return output
    except asyncio.TimeoutError:
        return ""
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


async def detect_roblox_packages():
    """
    Detect all installed Roblox packages by scanning /data/data/ directory.
    Falls back to pm list packages if /data/data/ is not readable.

    Returns:
        list: Sorted list of Roblox package names found on device.
    """
    #@ Logic: direct scan of /data/data/ is more reliable than pm
    #@ Logic: requires root access to read /data/data/
    try:
        def _scan():
            from pathlib import Path
            data_dir = Path("/data/data")
            if not data_dir.exists():
                return []
            packages = []
            for entry in data_dir.iterdir():
                if entry.is_dir() and "roblox" in entry.name.lower():
                    packages.append(entry.name)
            return sorted(packages)
        
        packages = await asyncio.to_thread(_scan)
        if packages:
            return packages
    except (PermissionError, OSError):
        pass

    #@ Logic: fallback to pm list packages if /data/data/ scan fails
    output = await run_command(["pm", "list", "packages"], timeout=10.0)
    if not output:
        return []

    packages = []
    for line in output.splitlines():
        name = line.replace("package:", "").strip()
        if "roblox" in name.lower():
            packages.append(name)
    return sorted(packages)


# ============================================================
#  DEVICE MONITOR
# ============================================================

_device_cache      = {}
_device_cache_time = 0.0
_device_cache_ttl  = 30.0
_prev_cpu_idle     = 0
_prev_cpu_total    = 0


def read_ram_info():
    """
    Read RAM usage from /proc/meminfo (zero-subprocess).

    Returns:
        tuple: (ram_percent, ram_used_mb), or (0.0, 0.0) on error.
    """
    try:
        content   = Path("/proc/meminfo").read_text()
        total     = 0
        available = 0
        for line in content.splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1])
        if total > 0:
            used    = total - available
            percent = (used / total) * 100
            used_mb = used / 1024
            return round(percent, 1), round(used_mb, 1)
    except Exception:
        pass
    return 0.0, 0.0


def read_cpu_percent():
    """
    Read CPU usage from /proc/stat using differential sampling.
    Requires two consecutive calls to compute delta between samples.

    Returns:
        float: CPU usage percentage (0-100), or 0.0 on first read.
    """
    global _prev_cpu_idle, _prev_cpu_total
    try:
        content = Path("/proc/stat").read_text()
        parts   = content.splitlines()[0].split()[1:]
        values  = [int(p) for p in parts]
        idle    = values[3]
        total   = sum(values)

        #@ Logic: first read has no previous sample, store baseline and return 0
        if _prev_cpu_total == 0:
            _prev_cpu_idle  = idle
            _prev_cpu_total = total
            return 0.0

        #@ Logic: compute delta between samples to get actual CPU usage
        idle_delta  = idle - _prev_cpu_idle
        total_delta = total - _prev_cpu_total

        _prev_cpu_idle  = idle
        _prev_cpu_total = total

        if total_delta > 0:
            busy_delta = total_delta - idle_delta
            return round((busy_delta / total_delta) * 100, 1)
    except Exception:
        pass
    return 0.0


async def get_device_stats():
    """
    Get cached device stats (CPU, RAM).
    Cache TTL: 30 seconds to minimize /proc reads over long runs.

    Returns:
        dict: Keys = cpu_percent, ram_percent, ram_used_mb.
    """
    global _device_cache, _device_cache_time
    now = time.time()
    if (now - _device_cache_time) < _device_cache_ttl and _device_cache:
        return dict(_device_cache)

    ram_pct, ram_mb = await asyncio.to_thread(read_ram_info)
    cpu_pct         = await asyncio.to_thread(read_cpu_percent)

    _device_cache = {
        "cpu_percent": cpu_pct,
        "ram_percent": ram_pct,
        "ram_used_mb": ram_mb,
    }
    _device_cache_time = now
    return dict(_device_cache)


# ============================================================
#  DISPLAY (ANSI TABLE)
# ============================================================

def clear_screen():
    """Clear terminal screen using ANSI escape code."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def move_cursor_home():
    """Move cursor to top-left corner without clearing."""
    sys.stdout.write("\033[H")
    sys.stdout.flush()


def _color_status(status):
    """
    Return ANSI-colored status string.

    Args:
        status: Status string (RUNNING, RECOVERING, STOPPED, etc).

    Returns:
        str: Colored string with ANSI escape codes.
    """
    colors = {
        "RUNNING":    "\033[32m",
        "RECOVERING": "\033[33m",
        "STOPPED":    "\033[31m",
    }
    color = colors.get(status, "")
    reset = "\033[0m" if color else ""
    return f"{color}{status}{reset}"


def render_status_table(package_states, device_stats):
    """
    Render a live status table to terminal using ANSI escape codes.
    Overwrites previous output in-place (no scrolling).

    Args:
        package_states: Dict of package_name -> state dict.
        device_stats: Dict with cpu_percent, ram_percent keys.

    Returns:
        None.
    """
    move_cursor_home()

    lines = []
    lines.append("=" * 60)
    lines.append(f"  ANDROID ROBLOX MANAGER v{VERSION}")
    lines.append("=" * 60)
    lines.append("")

    #@ Logic: header row with fixed column widths for alignment
    header = f"  {'PACKAGE':<25} {'STATUS':<12} {'UPTIME':<10} {'CRASHES':<8}"
    lines.append(header)
    lines.append("  " + "-" * 55)

    for pkg, state in package_states.items():
        status_val = state.get("status", ArmState.IDLE)
        status_str = status_val.value if isinstance(status_val, ArmState) else str(status_val)
        uptime_str = format_uptime(get_uptime(pkg))
        crashes    = state.get("crash_count", 0)

        #@ Logic: separate plain text from ANSI codes for clean alignment
        colored_status = _color_status(status_str)
        #@ Logic: pad to 21 chars because ANSI codes add 9 invisible characters
        line = f"  {pkg:<25} {colored_status:<21} {uptime_str:<10} {crashes:<8}"
        lines.append(line)

    lines.append("")
    cpu = device_stats.get("cpu_percent", 0)
    ram = device_stats.get("ram_percent", 0)
    lines.append(f"  RAM: {ram}%  CPU: {cpu}%  | Press Ctrl+C to stop")
    lines.append("")

    #@ Logic: pad with blank lines to overwrite any previous longer output
    output = "\n".join(lines) + "\n" * 5
    sys.stdout.write(output)
    sys.stdout.flush()


# ============================================================
#  WATCHDOG (CRASH DETECTION)
# ============================================================

async def watchdog_loop(config):
    """
    Monitor all configured packages for crashes using adaptive polling.
    Poll every 30s when stable, every 3s during recovery.

    Args:
        config: Config dict with packages and interval settings.

    Returns:
        None (runs until cancelled).
    """
    grace_seconds = config.get("startup_grace_seconds", 25)

    while True:
        for package in config.get("packages", []):
            try:
                await check_package(package, grace_seconds, config)
            except Exception:
                pass

        interval = get_adaptive_interval(config)
        await asyncio.sleep(interval)


def get_adaptive_interval(config):
    """
    Determine polling interval based on whether any package is recovering.
    Returns fast interval (3s) if any package is in RECOVERING state,
    otherwise returns normal interval (30s).

    Args:
        config: Config dict with interval settings.

    Returns:
        float: Seconds to sleep before next check.
    """
    for state in _package_states.values():
        if state.get("status") == ArmState.RECOVERING:
            return float(config.get("check_interval_recovery", 3))
    return float(config.get("check_interval_running", 30))


async def check_package(package, grace_seconds, config):
    """
    Check a single package: detect if running or crashed.
    Applies grace period before declaring crash to avoid false positives
    during normal app restarts.

    Args:
        package: Package name string.
        grace_seconds: Seconds to wait before declaring crash.
        config: Config dict.

    Returns:
        None.
    """
    state      = get_package_state(package)
    is_running = await async_is_package_running(package)

    if is_running:
        state["last_seen"] = time.time()
        if not state["was_running"]:
            #@ Logic: package just appeared, transition to RUNNING
            state["was_running"]  = True
            state["status"]       = ArmState.RUNNING
            state["uptime_start"] = time.time()
            state["retry_count"]  = 0
    else:
        if state["was_running"]:
            #@ Logic: grace period prevents false positive during app restart
            elapsed = time.time() - state["last_seen"]
            if elapsed > grace_seconds:
                state["was_running"] = False
                record_crash(package)
                state["status"] = ArmState.RECOVERING
                #@ Logic: capture screenshot on crash for debugging and webhook
                asyncio.create_task(capture_and_send_screenshot(package, config))
                #@ Logic: spawn recovery as separate task to not block watchdog
                asyncio.create_task(recover_package(package, config))


# ============================================================
#  RECOVERY (AUTO-REJOIN)
# ============================================================

async def recover_package(package, config):
    """
    Execute recovery sequence for a crashed package.
    Retries with exponential backoff (2^n seconds), max 5 attempts.

    Args:
        package: Package name to recover.
        config: Config dict with max_retries and server config.

    Returns:
        None.
    """
    max_retries = config.get("max_retries", 5)
    state       = get_package_state(package)

    for attempt in range(1, max_retries + 1):
        state["retry_count"] = attempt

        #@ Logic: check network before attempting relaunch
        net_ok = await has_internet()
        if not net_ok:
            await asyncio.sleep(10)
            continue

        #@ Logic: force-stop to clear stale app state before relaunch
        await run_command(["am", "force-stop", package], timeout=5.0)
        await asyncio.sleep(2)

        #@ Logic: launch the app via Android activity intent
        await run_command(
            ["am", "start", "-n", f"{package}/.MainActivity"],
            timeout=10.0,
        )

        #@ Logic: wait for app to fully initialize before sending rejoin
        await asyncio.sleep(15)
        await rejoin_server(package, config)

        record_reconnect(package)
        state["status"]       = ArmState.RUNNING
        state["uptime_start"] = time.time()
        state["was_running"]  = True
        state["retry_count"]  = 0

        #@ Logic: notify webhook about successful recovery
        await queue_webhook(
            f"[ARM] {package} recovered after {attempt} attempt(s)\n"
            f"Total crashes: {state['crash_count']}"
        )
        return

    #@ Logic: max retries exhausted, stop monitoring this package
    state["status"] = ArmState.STOPPED
    await queue_webhook(
        f"[ARM] {package} recovery failed after {max_retries} attempts, stopped"
    )


async def rejoin_server(package, config):
    """
    Send deep link intent to rejoin configured server.
    Priority order: private_server_code > place_id > skip.

    Args:
        package: Target package name for the intent.
        config: Config dict with place_id, server_id, private_server_code.

    Returns:
        None.
    """
    private_code = config.get("private_server_code", "")
    place_id     = config.get("place_id", 0)
    server_id    = config.get("server_id", "")

    if private_code:
        url = f"https://www.roblox.com/games/start?privateServerLinkCode={private_code}"
    elif place_id:
        if server_id:
            url = f"roblox://placeID={place_id}&gameInstanceId={server_id}"
        else:
            url = f"roblox://placeID={place_id}"
    else:
        return

    #@ Logic: -p flag targets specific package for multi-instance support
    await run_command(
        ["am", "start", "-a", "android.intent.action.VIEW",
         "-d", url, "-p", package],
        timeout=10.0,
    )


# ============================================================
#  WEBHOOK (DISCORD)
# ============================================================

_webhook_queue = None


async def init_webhook():
    """Initialize the webhook send queue. Call before starting webhook tasks."""
    global _webhook_queue
    _webhook_queue = asyncio.Queue()


async def queue_webhook(message):
    """
    Add a message to the webhook send queue.
    Silently drops message if webhook is not initialized.

    Args:
        message: String content to send to Discord.

    Returns:
        None.
    """
    if _webhook_queue is not None:
        await _webhook_queue.put({"content": message})


async def webhook_sender_loop(config):
    """
    Process webhook queue and send to Discord with retry.
    Lazily imports aiohttp to allow script to run without it.

    Args:
        config: Config dict with webhook_url and webhook_enabled.

    Returns:
        None (runs until cancelled).
    """
    if not config.get("webhook_enabled", False):
        #@ Logic: keep task alive but idle when webhook is disabled
        while True:
            await asyncio.sleep(60)

    try:
        import aiohttp
    except ImportError:
        while True:
            await asyncio.sleep(60)

    url = config.get("webhook_url", "")
    if not url:
        while True:
            await asyncio.sleep(60)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    ) as session:
        while True:
            try:
                msg = await asyncio.wait_for(
                    _webhook_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            await send_webhook_with_retry(session, url, msg)


async def send_webhook_with_retry(session, url, payload, max_retries=3):
    """
    Send webhook payload with exponential backoff retry.

    Args:
        session: aiohttp.ClientSession instance.
        url: Discord webhook URL.
        payload: Dict with 'content' key.
        max_retries: Maximum send attempts before discarding.

    Returns:
        None.
    """
    for attempt in range(1, max_retries + 1):
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 204):
                    return
                if resp.status == 429:
                    #@ Logic: respect Discord rate limit Retry-After header
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    await asyncio.sleep(retry_after)
                    continue
        except asyncio.CancelledError:
            return
        except Exception:
            pass

        #@ Logic: exponential backoff between retries
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)


async def webhook_status_loop(config):
    """
    Periodically send status reports to Discord webhook.

    Args:
        config: Config dict with webhook_interval setting.

    Returns:
        None (runs until cancelled).
    """
    if not config.get("webhook_enabled", False):
        while True:
            await asyncio.sleep(60)

    interval = config.get("webhook_interval", 300)

    while True:
        await asyncio.sleep(interval)
        stats = await get_device_stats()
        #@ Logic: aggregate state from all packages for overall status
        active_states = [s["status"].value for s in _package_states.values()]
        overall = "RUNNING" if "RUNNING" in active_states else "IDLE"
        lines = [
            "[ARM] Status Report",
            f"State: {overall}",
            f"CPU: {stats.get('cpu_percent', 0)}%",
            f"RAM: {stats.get('ram_percent', 0)}%",
        ]
        for pkg, st in _package_states.items():
            #@ Logic: fetch username once and cache it for future webhook reports
            if not st.get("username"):
                cookie = await get_cookie_from_package(pkg)
                if cookie:
                    info = await get_account_info(cookie)
                    if info and info.get("username"):
                        st["username"] = info["username"]
            
            username_display = f"({st['username']})" if st.get("username") else ""
            
            status_val = st.get("status", ArmState.IDLE)
            status_str = status_val.value if isinstance(status_val, ArmState) else str(status_val)
            lines.append(
                f"  {pkg} {username_display}: {status_str} | "
                f"Crashes: {st['crash_count']} | "
                f"Uptime: {format_uptime(get_uptime(pkg))}"
            )
        await queue_webhook("\n".join(lines))


async def webhook_screenshot_loop(config):
    """
    Periodically capture and send screenshot to Discord webhook.
    Requires root for screencap command.

    Args:
        config: Config dict with screenshot_interval and webhook_url.

    Returns:
        None (runs until cancelled).
    """
    if not config.get("webhook_enabled", False):
        while True:
            await asyncio.sleep(60)

    try:
        import aiohttp
    except ImportError:
        while True:
            await asyncio.sleep(60)

    url = config.get("webhook_url", "")
    if not url:
        while True:
            await asyncio.sleep(60)

    interval        = config.get("screenshot_interval", 3600)
    arm_dir         = setup_environment()
    screenshot_path = Path(arm_dir) / "screenshot.png"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        while True:
            await asyncio.sleep(interval)
            await run_command(
                ["screencap", "-p", str(screenshot_path)],
                timeout=10.0,
            )
            if screenshot_path.exists():
                try:
                    #@ Logic: use with-statement to prevent file handle leak
                    with open(str(screenshot_path), "rb") as f:
                        data = aiohttp.FormData()
                        data.add_field("file", f, filename="screenshot.png")
                        data.add_field("content", "[ARM] Screenshot")
                        async with session.post(url, data=data) as resp:
                            pass
                except Exception:
                    pass


# ============================================================
#  SCREENSHOT ON CRASH
# ============================================================

async def capture_and_send_screenshot(package, config):
    """
    Capture screenshot immediately on crash and send to Discord webhook.
    This provides visual context for debugging crash events.
    Requires root for screencap command.

    Args:
        package: Package that crashed (used in webhook message).
        config: Config dict with webhook_url and webhook_enabled.

    Returns:
        None.
    """
    if not config.get("webhook_enabled", False):
        return

    try:
        import aiohttp
    except ImportError:
        return

    url = config.get("webhook_url", "")
    if not url:
        return

    arm_dir         = setup_environment()
    screenshot_path = Path(arm_dir) / f"crash_{package}.png"

    #@ Logic: capture screen immediately after crash detection
    await run_command(
        ["screencap", "-p", str(screenshot_path)],
        timeout=10.0,
    )

    if not screenshot_path.exists():
        #@ Logic: if screencap failed, send text-only crash notification
        await queue_webhook(
            f"[ARM] Crash detected: {package}\n"
            f"Screenshot capture failed"
        )
        return

    state      = get_package_state(package)
    crash_msg  = (
        f"[ARM] Crash detected: {package}\n"
        f"Total crashes: {state['crash_count']}\n"
        f"Uptime before crash: {format_uptime(get_uptime(package))}"
    )

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            #@ Logic: use with-statement to prevent file handle leak
            with open(str(screenshot_path), "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=f"crash_{package}.png")
                data.add_field("content", crash_msg)
                async with session.post(url, data=data) as resp:
                    pass
    except Exception:
        #@ Logic: fallback to text-only if screenshot upload fails
        await queue_webhook(crash_msg)
    finally:
        #@ Logic: clean up screenshot file to save storage
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
#  ACCOUNT MANAGER (COOKIE)
# ============================================================

COOKIE_PREFS_PATHS = [
    "/data/data/{pkg}/shared_prefs/RobloxCookiesV2.xml",
    "/data/data/{pkg}/shared_prefs/RobloxCookies.xml",
]


async def login_with_cookie(package, cookie):
    """
    Write .ROBLOSECURITY cookie to Roblox shared preferences.
    Tries multiple known prefs paths for compatibility.

    Args:
        package: Target Roblox package name.
        cookie: .ROBLOSECURITY cookie string.

    Returns:
        bool: True if cookie was written successfully.
    """
    xml_content = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes" ?>\n'
        '<map>\n'
        f'    <string name=".ROBLOSECURITY">{cookie}</string>\n'
        '</map>\n'
    )
    prefs_path = COOKIE_PREFS_PATHS[0].format(pkg=package)
    try:
        result = await run_command(
            ["sh", "-c", f"echo '{xml_content}' > {prefs_path}"],
            timeout=5.0,
        )
        return True
    except Exception:
        return False


async def get_cookie_from_package(package):
    """
    Read .ROBLOSECURITY cookie from a Roblox instance.
    Checks multiple known shared_prefs paths.

    Args:
        package: Roblox package name.

    Returns:
        str: Cookie string, or empty string if not found.
    """
    for path_template in COOKIE_PREFS_PATHS:
        prefs_path = path_template.format(pkg=package)
        result = await run_command(["cat", prefs_path], timeout=5.0)
        if not result:
            continue
        #@ Logic: extract cookie value from XML <string> tag
        for line in result.splitlines():
            if ".ROBLOSECURITY" in line:
                start = line.find(">") + 1
                end   = line.rfind("<")
                if start > 0 and end > start:
                    return line[start:end]
    return ""


async def get_account_info(cookie):
    """
    Fetch Roblox account info (username, robux) using cookie.

    Args:
        cookie: .ROBLOSECURITY cookie string.

    Returns:
        dict: Keys = username, user_id, robux. Empty dict on failure.
    """
    try:
        import aiohttp
    except ImportError:
        return {}

    headers = {"Cookie": f".ROBLOSECURITY={cookie}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://users.roblox.com/v1/users/authenticated",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {}
                data     = await resp.json()
                user_id  = data.get("id", 0)
                username = data.get("name", "Unknown")

            #@ Logic: fetch Robux balance from separate economy API
            robux = 0
            async with session.get(
                f"https://economy.roblox.com/v1/users/{user_id}/currency",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    currency = await resp.json()
                    robux    = currency.get("robux", 0)

            return {
                "username": username,
                "user_id":  user_id,
                "robux":    robux,
            }
    except Exception:
        return {}


# ============================================================
#  CLI MENU
# ============================================================

def print_menu():
    """Print the main ARM menu to stdout."""
    print("=" * 42)
    print("  ANDROID ROBLOX MANAGER v" + VERSION)
    print("=" * 42)
    print("  1. Start ARM (listener + rejoin + webhook)")
    print("  2. Package Manager")
    print("  3. Setup Place ID / Private Server")
    print("  4. Setup Webhook")
    print("  5. Account Manager (Cookie)")
    print("  6. Configuration")
    print("  0. Exit")
    print("=" * 42)


async def menu_start_arm(config, config_path):
    """
    Start all monitoring tasks and display live status table.
    This is the main runtime loop of ARM v3.

    Args:
        config: Config dict.
        config_path: Path to config.json (unused here, kept for interface).

    Returns:
        None (runs until Ctrl+C).
    """
    packages = config.get("packages", [])
    if not packages:
        print("No packages configured. Run Package Manager first (option 2).")
        return

    #@ Logic: initialize per-package state for all selected packages
    for pkg in packages:
        init_package_state(pkg)

    await init_webhook()
    clear_screen()

    #@ Logic: create all concurrent background tasks
    tasks = [
        asyncio.create_task(watchdog_loop(config)),
        asyncio.create_task(display_loop(config)),
        asyncio.create_task(webhook_sender_loop(config)),
        asyncio.create_task(webhook_status_loop(config)),
        asyncio.create_task(webhook_screenshot_loop(config)),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        #@ Logic: wait for all tasks to finish cancellation
        await asyncio.gather(*tasks, return_exceptions=True)


async def display_loop(config):
    """
    Refresh the status table every 2 seconds.

    Args:
        config: Config dict (unused, kept for interface consistency).

    Returns:
        None (runs until cancelled).
    """
    while True:
        try:
            stats = await get_device_stats()
            render_status_table(_package_states, stats)
        except Exception:
            pass
        await asyncio.sleep(2)


async def menu_package_manager(config, config_path):
    """
    Interactive package selection menu.
    Detects all installed Roblox packages and allows multi-select.

    Args:
        config: Config dict.
        config_path: Path to config.json for saving.

    Returns:
        None.
    """
    print("\nDetecting installed packages")
    all_packages = await detect_roblox_packages()

    if not all_packages:
        print("No Roblox packages found on this device.")
        return

    current = config.get("packages", [])
    print("\nSelect Roblox packages to monitor:")
    for i, pkg in enumerate(all_packages, 1):
        marker = "[x]" if pkg in current else "[ ]"
        print(f"  {i}. {marker} {pkg}")

    selection = input("\nEnter numbers (comma separated): ").strip()
    if not selection:
        return

    selected = []
    for num in selection.split(","):
        num = num.strip()
        if num.isdigit():
            idx = int(num) - 1
            if 0 <= idx < len(all_packages):
                selected.append(all_packages[idx])

    config["packages"] = selected
    save_config(config_path, config)

    for pkg in selected:
        print(f"  [OK] {pkg} selected")
    print(f"\n{len(selected)} package(s) configured")


async def menu_setup_join(config, config_path):
    """
    Configure Place ID, Private Server Code, and Server ID.

    Args:
        config: Config dict.
        config_path: Path to config.json.

    Returns:
        None.
    """
    print("\nSetup Join Target:")
    print("  1. Set Place ID")
    print("  2. Set Private Server Code")
    print("  3. Set Server ID (optional)")
    print("  4. View current config")
    choice = input("Select: ").strip()

    if choice == "1":
        pid = input("Enter Place ID: ").strip()
        if pid.isdigit():
            config["place_id"] = int(pid)
            save_config(config_path, config)
            print(f"Place ID set to {pid}")
        else:
            print("Invalid Place ID (must be a number)")
    elif choice == "2":
        code = input("Enter Private Server Link Code: ").strip()
        config["private_server_code"] = code
        save_config(config_path, config)
        print("Private Server Code saved")
    elif choice == "3":
        sid = input("Enter Server ID (or leave empty): ").strip()
        config["server_id"] = sid
        save_config(config_path, config)
        print("Server ID saved")
    elif choice == "4":
        print(f"  Place ID:             {config.get('place_id', 0)}")
        print(f"  Private Server Code:  {config.get('private_server_code', '') or '(not set)'}")
        print(f"  Server ID:            {config.get('server_id', '') or '(not set)'}")


async def menu_setup_webhook(config, config_path):
    """
    Configure Discord webhook settings and test connection.

    Args:
        config: Config dict.
        config_path: Path to config.json.

    Returns:
        None.
    """
    enabled = config.get("webhook_enabled", False)
    print("\nWebhook Setup:")
    print(f"  1. Set Webhook URL")
    print(f"  2. Report interval ({config.get('webhook_interval', 300)}s)")
    print(f"  3. Screenshot interval ({config.get('screenshot_interval', 3600)}s)")
    print(f"  4. Enable/Disable (currently {'ON' if enabled else 'OFF'})")
    print(f"  5. Test webhook")
    choice = input("Select: ").strip()

    if choice == "1":
        url = input("Enter Discord Webhook URL: ").strip()
        config["webhook_url"] = url
        save_config(config_path, config)
        print("Webhook URL saved")
    elif choice == "2":
        val = input("Enter report interval (seconds): ").strip()
        if val.isdigit() and int(val) > 0:
            config["webhook_interval"] = int(val)
            save_config(config_path, config)
            print(f"Report interval set to {val}s")
        else:
            print("Invalid value (must be a positive number)")
    elif choice == "3":
        val = input("Enter screenshot interval (seconds): ").strip()
        if val.isdigit() and int(val) > 0:
            config["screenshot_interval"] = int(val)
            save_config(config_path, config)
            print(f"Screenshot interval set to {val}s")
        else:
            print("Invalid value (must be a positive number)")
    elif choice == "4":
        config["webhook_enabled"] = not enabled
        save_config(config_path, config)
        status = "ON" if config["webhook_enabled"] else "OFF"
        print(f"Webhook is now {status}")
    elif choice == "5":
        url = config.get("webhook_url", "")
        if not url:
            print("No webhook URL configured. Set it first (option 1).")
            return
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                payload = {"content": f"[ARM] Test message from ARM v{VERSION}"}
                async with session.post(url, json=payload) as resp:
                    if resp.status in (200, 204):
                        print("Test message sent successfully")
                    else:
                        print(f"Webhook returned status {resp.status}")
        except ImportError:
            print("aiohttp not installed. Run: pip install aiohttp")
        except Exception as exc:
            print(f"Webhook test failed: {exc}")


async def menu_account_manager(config, config_path):
    """
    Manage Roblox cookies and view account information.

    Args:
        config: Config dict.
        config_path: Path to config.json.

    Returns:
        None.
    """
    print("\nAccount Manager:")
    print("  1. Login via Cookie")
    print("  2. Get Cookie from Roblox")
    print("  3. Logout (clear cookie)")
    print("  4. Account Stats")
    choice = input("Select: ").strip()

    packages = config.get("packages", [])
    if not packages:
        print("No packages configured. Run Package Manager first (option 2).")
        return

    if choice == "1":
        print("\nSelect package:")
        for i, pkg in enumerate(packages, 1):
            print(f"  {i}. {pkg}")
        sel = input("Select: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(packages):
            pkg    = packages[int(sel) - 1]
            cookie = input("Enter .ROBLOSECURITY cookie: ").strip()
            if cookie:
                ok = await login_with_cookie(pkg, cookie)
                print("Cookie saved" if ok else "Failed to save cookie")
            else:
                print("Empty cookie, skipping")
        else:
            print("Invalid selection")

    elif choice == "2":
        for pkg in packages:
            cookie = await get_cookie_from_package(pkg)
            if cookie:
                #@ Logic: truncate cookie display for security
                print(f"  {pkg}: {cookie[:30]}...")
            else:
                print(f"  {pkg}: No cookie found")

    elif choice == "3":
        print("\nSelect package to logout:")
        for i, pkg in enumerate(packages, 1):
            print(f"  {i}. {pkg}")
        sel = input("Select: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(packages):
            pkg = packages[int(sel) - 1]
            for path_template in COOKIE_PREFS_PATHS:
                prefs_path = path_template.format(pkg=pkg)
                await run_command(["rm", "-f", prefs_path], timeout=5.0)
            print(f"Cookie cleared for {pkg}")
        else:
            print("Invalid selection")

    elif choice == "4":
        for pkg in packages:
            cookie = await get_cookie_from_package(pkg)
            if cookie:
                info = await get_account_info(cookie)
                if info:
                    print(f"  {pkg}:")
                    print(f"    Username: {info.get('username', 'Unknown')}")
                    print(f"    User ID:  {info.get('user_id', 0)}")
                    print(f"    Robux:    {info.get('robux', 0)}")
                else:
                    print(f"  {pkg}: Failed to fetch account info")
            else:
                print(f"  {pkg}: Not logged in")


async def menu_configuration(config, config_path):
    """
    Edit ARM runtime configuration settings.

    Args:
        config: Config dict.
        config_path: Path to config.json.

    Returns:
        None.
    """
    print("\nConfiguration:")
    print(f"  1. Check interval running ({config.get('check_interval_running', 30)}s)")
    print(f"  2. Check interval recovery ({config.get('check_interval_recovery', 3)}s)")
    print(f"  3. Max retries ({config.get('max_retries', 5)})")
    print(f"  4. Grace period ({config.get('startup_grace_seconds', 25)}s)")
    print(f"  5. Auto-execute path ({config.get('auto_execute', '') or '(not set)'})")
    print(f"  6. Reset to defaults")
    choice = input("Select: ").strip()

    field_map = {
        "1": ("check_interval_running", int),
        "2": ("check_interval_recovery", int),
        "3": ("max_retries", int),
        "4": ("startup_grace_seconds", int),
    }

    if choice in field_map:
        field, cast = field_map[choice]
        val = input(f"Enter new value for {field}: ").strip()
        try:
            config[field] = cast(val)
            save_config(config_path, config)
            print(f"{field} set to {val}")
        except ValueError:
            print("Invalid value")
    elif choice == "5":
        path = input("Enter executor script path (or empty to clear): ").strip()
        config["auto_execute"] = path
        save_config(config_path, config)
        print("Auto-execute path saved" if path else "Auto-execute cleared")
    elif choice == "6":
        config.clear()
        config.update(validate_config({}))
        save_config(config_path, config)
        print("Configuration reset to defaults")


# ============================================================
#  MAIN ENTRY POINT
# ============================================================

async def main_menu():
    """
    Main menu loop. Loads config and dispatches to menu handlers.
    Handles KeyboardInterrupt gracefully to return to menu.

    Returns:
        None.
    """
    arm_dir     = setup_environment()
    config_path = get_config_path(arm_dir)
    config      = load_config(config_path)

    menu_handlers = {
        "1": menu_start_arm,
        "2": menu_package_manager,
        "3": menu_setup_join,
        "4": menu_setup_webhook,
        "5": menu_account_manager,
        "6": menu_configuration,
    }

    while True:
        print_menu()
        choice = input("Select: ").strip()

        if choice == "0":
            print("Exiting ARM")
            break

        handler = menu_handlers.get(choice)
        if handler:
            try:
                await handler(config, config_path)
            except KeyboardInterrupt:
                print("\nReturning to menu")
            except Exception as exc:
                print(f"Error: {exc}")
                try:
                    input("\nPress Enter to continue")
                except (KeyboardInterrupt, EOFError):
                    pass
        else:
            print("Invalid selection")


if __name__ == "__main__":
    try:
        asyncio.run(main_menu())
    except KeyboardInterrupt:
        print("\nARM stopped")
        sys.exit(0)
