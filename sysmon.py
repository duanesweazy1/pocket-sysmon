#!/usr/bin/env python3
"""SysMon - pocket system monitor for the M5Stack CardputerZero.

Draws straight to the Linux framebuffer (320x170) with Pillow.

Pages
  1 Overview   CPU total/cores/history, RAM, temperature, battery
  2 Processes  top 5 by CPU or memory, end a process from the keyboard
  3 Network    interfaces, Wi-Fi signal, live download/upload speed graph
  4 Power      temperature history, battery time estimate, disk, uptime
  5 Level      bubble level from the BMI270 motion sensor
  6 Sound      microphone loudness meter (ES8389 codec)

Keys
  Left/Right or Tab   previous/next page        1-6   jump to page
  ESC or Q            quit (or cancel a prompt)
  Processes: Up/Down select, Enter or K end process, S switch CPU/MEM sort
  Level:     Enter or Z set current angle as zero, X clear zero
  Sound:     Enter calibrate 'quiet' (saved), X clear calibration

Run with  --preview out.png PAGE [--mock]  to render one frame to a PNG.
"""

import fcntl
import glob
import math
import mmap
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

VERSION = "1.1.2"

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
W, H = 320, 170
FB_DEVICE = os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
CPU_HISTORY = 60      # seconds
NET_HISTORY = 60      # seconds
TEMP_HISTORY = 240    # seconds
SOUND_HISTORY = 100   # samples at ~10 Hz
BUBBLE_RANGE_DEG = 18.0  # same full-scale tilt as the official Compass app

PAGES = ["Overview", "Processes", "Network", "Power", "Level", "Sound"]
P_OVERVIEW, P_PROCS, P_NET, P_POWER, P_LEVEL, P_SOUND = range(6)
FRAME_INTERVAL = {P_LEVEL: 1.0 / 15, P_SOUND: 1.0 / 10}  # others: 1 s

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
SIOCGIFADDR = 0x8915

# Linux input-event key codes (linux/input-event-codes.h)
EV_KEY = 0x01
KEY_ESC, KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6 = 1, 2, 3, 4, 5, 6, 7
KEY_TAB, KEY_Q, KEY_Y, KEY_ENTER = 15, 16, 21, 28
KEY_S, KEY_K, KEY_SEMICOLON = 31, 37, 39
KEY_Z, KEY_X, KEY_N, KEY_COMMA, KEY_DOT, KEY_SLASH = 44, 45, 49, 51, 52, 53
KEY_SPACE = 57
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
NUM_KEYS = {KEY_1: 0, KEY_2: 1, KEY_3: 2, KEY_4: 3, KEY_5: 4, KEY_6: 5}

# Colours (dark theme)
BG = (10, 12, 16)
PANEL = (24, 28, 36)
GRID = (40, 46, 58)
SEL = (34, 52, 74)
TEXT = (230, 234, 240)
DIM = (130, 138, 152)
GREEN = (60, 220, 130)
GREEN_FILL = (20, 70, 50)
CYAN = (60, 190, 255)
YELLOW = (255, 205, 60)
ORANGE = (255, 140, 50)
ORANGE_FILL = (80, 45, 20)
RED = (255, 75, 75)
RED_FILL = (80, 28, 28)
PURPLE = (175, 125, 255)

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

_running = True


def _stop(_sig, _frm):
    global _running
    _running = False


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def load_font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in (os.path.join(FONT_DIR, name),
                 "/usr/share/fonts/TTF/" + name,
                 "/Library/Fonts/" + name):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            pass
    return ImageFont.load_default()


def read_file(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError):
        return default


def read_int(path):
    try:
        return int(read_file(path))
    except (TypeError, ValueError):
        return None


def level_color(pct, warn=60, crit=85):
    if pct >= crit:
        return RED
    if pct >= warn:
        return YELLOW
    return GREEN


def fmt_mb(mb):
    return "{:.1f}G".format(mb / 1024.0) if mb >= 1024 else "{}M".format(int(mb))


def fmt_rate(bps):
    """Bytes/second -> short human string."""
    if bps >= 1024 * 1024:
        return "{:.1f} MB/s".format(bps / 1048576.0)
    if bps >= 1024:
        return "{:.0f} KB/s".format(bps / 1024.0)
    return "{:.0f} B/s".format(bps)


def fmt_duration(sec, short=False):
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "{}d {}h".format(d, h) if short else "{}d {}h {}m".format(d, h, m)
    if h:
        return "{}h{:02d}m".format(h, m) if short else "{}h {}m".format(h, m)
    return "{}m".format(max(m, 1) if sec else 0)


# --------------------------------------------------------------------------
# System statistics
# --------------------------------------------------------------------------
class CpuStats:
    """Total and per-core CPU usage from /proc/stat deltas."""

    def __init__(self):
        self.prev = {}
        self.history = deque([0.0] * CPU_HISTORY, maxlen=CPU_HISTORY)
        self.total = 0.0
        self.cores = []
        self.sample()

    def sample(self):
        cores = []
        try:
            with open("/proc/stat") as f:
                lines = [l for l in f if l.startswith("cpu")]
        except OSError:
            return
        for line in lines:
            parts = line.split()
            name, vals = parts[0], [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals[:8])
            p_idle, p_total = self.prev.get(name, (idle, total))
            self.prev[name] = (idle, total)
            dt = total - p_total
            pct = 100.0 * (dt - (idle - p_idle)) / dt if dt > 0 else 0.0
            pct = max(0.0, min(100.0, pct))
            if name == "cpu":
                self.total = pct
            else:
                cores.append(pct)
        self.cores = cores
        self.history.append(self.total)


def read_mem():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return 0.0, 0, 1, 0, 0
    total = info.get("MemTotal", 1)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    swap_t = info.get("SwapTotal", 0)
    swap_u = swap_t - info.get("SwapFree", 0)
    return 100.0 * used / total, used / 1024.0, total / 1024.0, swap_u / 1024.0, swap_t / 1024.0


def read_temp():
    best = None
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        v = read_int(zone)
        if v is None:
            continue
        t = v / 1000.0
        if best is None or t > best:
            best = t
    return best


def read_freq_mhz():
    v = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return v // 1000 if v else None


def read_throttled():
    """Raspberry Pi firmware throttling flags, if vcgencmd is around."""
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=1)
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def read_disk(path="/"):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize / 1048576.0
    free = st.f_bavail * st.f_frsize / 1048576.0
    used = total - free
    return (100.0 * used / total if total else 0.0), used, total


def read_uptime_load():
    up = (read_file("/proc/uptime", "0") or "0").split()[0]
    load = (read_file("/proc/loadavg", "0 0 0") or "0 0 0").split()[:3]
    try:
        up = float(up)
    except ValueError:
        up = 0.0
    return up, load


# -- battery ---------------------------------------------------------------
class Battery:
    """BQ27220 fuel gauge via /sys/class/power_supply, with a time estimate."""

    def __init__(self):
        self.path = None
        for d in sorted(glob.glob("/sys/class/power_supply/*")):
            if (read_file(os.path.join(d, "type"), "") or "").lower() == "battery":
                self.path = d
                break
        self.currents = deque(maxlen=30)   # mA, for a smoothed estimate
        self.caps = deque(maxlen=600)      # (time, capacity) for slope fallback

    def _get(self, name):
        return read_int(os.path.join(self.path, name)) if self.path else None

    def read(self):
        if not self.path:
            return None
        cap = self._get("capacity")
        status = read_file(os.path.join(self.path, "status"), "Unknown") or "Unknown"
        volt = self._get("voltage_now")
        curr = self._get("current_now")
        b = {"cap": cap, "status": status,
             "volt": volt / 1e6 if volt else None,
             "curr": curr / 1e3 if curr is not None else None,
             "eta": None, "eta_kind": None}
        if b["curr"] is not None:
            self.currents.append(abs(b["curr"]))
        if cap is not None:
            self.caps.append((time.monotonic(), cap))

        charging = status == "Charging"
        discharging = status == "Discharging"
        eta = None
        # 1) The gauge's own estimate (seconds in mainline bq27xxx driver)
        if discharging:
            for n in ("time_to_empty_avg", "time_to_empty_now"):
                v = self._get(n)
                if v and 0 < v < 7 * 86400:
                    eta = v
                    break
        elif charging:
            for n in ("time_to_full_avg", "time_to_full_now"):
                v = self._get(n)
                if v and 0 < v < 7 * 86400:
                    eta = v
                    break
        # 2) Remaining charge / average current
        if eta is None and (charging or discharging) and self.currents:
            avg_ma = sum(self.currents) / len(self.currents)
            now_mah = self._get("charge_now")
            full_mah = self._get("charge_full")
            if avg_ma > 5 and now_mah:
                now_mah /= 1000.0
                if discharging:
                    eta = now_mah / avg_ma * 3600
                elif full_mah:
                    eta = max(0.0, full_mah / 1000.0 - now_mah) / avg_ma * 3600
        # 3) Slope of capacity % over the last minutes
        if eta is None and (charging or discharging) and len(self.caps) > 60 and cap is not None:
            t0, c0 = self.caps[0]
            t1, c1 = self.caps[-1]
            rate = (c1 - c0) / max(1.0, t1 - t0)  # % per second
            if discharging and rate < 0:
                eta = cap / -rate
            elif charging and rate > 0:
                eta = (100 - cap) / rate
        if eta is not None:
            b["eta"] = eta
            b["eta_kind"] = "left" if discharging else "full"
        return b


# -- network ---------------------------------------------------------------
def iface_ip(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name[:15].encode())
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


SKIP_IFACES = ("lo", "docker", "veth", "br-", "virbr", "ifb", "tun", "sit", "dummy", "p2p")


def read_interfaces():
    out = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name.startswith(SKIP_IFACES):
            continue
        state = read_file(os.path.join(path, "operstate"), "unknown")
        wireless = os.path.isdir(os.path.join(path, "wireless"))
        out.append({"name": name, "ip": iface_ip(name), "up": state == "up",
                    "wifi": wireless})
    out.sort(key=lambda i: (i["ip"] is None, not i["wifi"], i["name"]))
    return out


class NetStats:
    """Per-interface and total byte rates from /proc/net/dev."""

    def __init__(self):
        self.prev = None
        self.prev_t = None
        self.rx_hist = deque([0.0] * NET_HISTORY, maxlen=NET_HISTORY)
        self.tx_hist = deque([0.0] * NET_HISTORY, maxlen=NET_HISTORY)
        self.rates = {}   # iface -> (rx B/s, tx B/s)
        self.rx = self.tx = 0.0
        self.sample()

    @staticmethod
    def _read():
        data = {}
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()[2:]
        except OSError:
            return data
        for line in lines:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name.startswith(SKIP_IFACES):
                continue
            f = rest.split()
            if len(f) >= 9:
                data[name] = (int(f[0]), int(f[8]))
        return data

    def sample(self):
        now = time.monotonic()
        cur = self._read()
        if self.prev is not None:
            dt = max(0.001, now - self.prev_t)
            rates = {}
            for name, (rx, tx) in cur.items():
                prx, ptx = self.prev.get(name, (rx, tx))
                rates[name] = (max(0, rx - prx) / dt, max(0, tx - ptx) / dt)
            self.rates = rates
            self.rx = sum(r[0] for r in rates.values())
            self.tx = sum(r[1] for r in rates.values())
            self.rx_hist.append(self.rx)
            self.tx_hist.append(self.tx)
        self.prev, self.prev_t = cur, now


def read_wifi_signal():
    """Return (iface, dBm) from /proc/net/wireless, or None."""
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            dbm = float(parts[3].rstrip("."))
        except ValueError:
            continue
        if dbm > 0:
            dbm -= 256
        return parts[0].rstrip(":"), dbm
    return None


_ssid_cache = {"t": -99.0, "v": None}


def read_ssid():
    now = time.monotonic()
    if now - _ssid_cache["t"] < 10:
        return _ssid_cache["v"]
    _ssid_cache["t"] = now
    val = None
    for cmd in (["iwgetid", "-r"],
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL,
                                          timeout=1.5).strip()
        except Exception:
            continue
        if cmd[0] == "nmcli":
            for line in out.splitlines():
                if line.startswith("yes:"):
                    val = line[4:]
                    break
        else:
            val = out or None
        if val:
            break
    _ssid_cache["v"] = val
    return val


# -- processes -------------------------------------------------------------
class ProcStats:
    """Top processes by CPU or memory, from /proc/<pid>/stat and statm."""

    def __init__(self):
        self.prev = {}
        self.prev_t = None
        self.rows = []        # list of dicts sorted by current key
        self.sort_by = "cpu"
        self.ready = False

    def reset(self):
        self.prev, self.prev_t, self.ready = {}, None, False

    def sample(self, mem_total_mb):
        now = time.monotonic()
        cur = {}
        rows = []
        dt = (now - self.prev_t) if self.prev_t else None
        for pid_dir in glob.glob("/proc/[0-9]*"):
            pid = int(os.path.basename(pid_dir))
            try:
                with open(pid_dir + "/stat") as f:
                    stat = f.read()
                with open(pid_dir + "/statm") as f:
                    rss_pages = int(f.read().split()[1])
            except (OSError, ValueError, IndexError):
                continue
            lp, rp = stat.find("("), stat.rfind(")")
            if lp < 0 or rp < 0:
                continue
            name = stat[lp + 1:rp]
            rest = stat[rp + 2:].split()
            try:
                ticks = int(rest[11]) + int(rest[12])
            except (IndexError, ValueError):
                continue
            cur[pid] = ticks
            rss_mb = rss_pages * PAGE_SIZE / 1048576.0
            if rss_pages == 0:
                continue  # kernel threads
            cpu = 0.0
            if dt and pid in self.prev:
                cpu = 100.0 * (ticks - self.prev[pid]) / (dt * CLK_TCK)
            rows.append({"pid": pid, "name": name, "cpu": max(0.0, cpu), "rss": rss_mb,
                         "mem": 100.0 * rss_mb / max(1.0, mem_total_mb)})
        self.ready = dt is not None
        self.prev, self.prev_t = cur, now
        self.rows = rows
        self.sort()

    def sort(self):
        key = "cpu" if self.sort_by == "cpu" else "rss"
        self.rows.sort(key=lambda r: (r[key], r["rss"]), reverse=True)

    def top(self, n=5):
        return self.rows[:n]


# -- motion sensor ---------------------------------------------------------
class Imu:
    """BMI270 accelerometer through the kernel IIO driver."""

    def __init__(self):
        self.path = None
        for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
            name = (read_file(os.path.join(d, "name"), "") or "").lower()
            if "bmi270" in name or "bmi260" in name:
                if os.path.exists(os.path.join(d, "in_accel_x_raw")):
                    self.path = d
                    break
        self.scale = 1.0
        if self.path:
            try:
                self.scale = float(read_file(os.path.join(self.path, "in_accel_scale"), "1"))
            except ValueError:
                self.scale = 1.0
        self.smooth = None
        self.zero = (0.0, 0.0)

    @property
    def available(self):
        return self.path is not None

    def read(self):
        """Return dict with pitch/roll (deg, zero applied), g and bubble x/y."""
        if not self.path:
            return None
        try:
            raw = [int(read_file(os.path.join(self.path, "in_accel_%s_raw" % a)))
                   for a in "xyz"]
        except (TypeError, ValueError):
            return None
        sx, sy, sz = [v * self.scale for v in raw]
        # Same sensor -> screen mapping as the official Compass app
        ax, ay, az = sy, -sx, sz
        if self.smooth is None:
            self.smooth = [ax, ay, az]
        else:
            k = 0.35
            self.smooth = [s + k * (v - s) for s, v in zip(self.smooth, (ax, ay, az))]
        ax, ay, az = self.smooth
        pitch = math.degrees(math.atan2(ay, math.hypot(ax, az)))
        roll = math.degrees(math.atan2(ax, math.hypot(ay, az)))
        g = math.sqrt(ax * ax + ay * ay + az * az) / 9.80665
        return self._result(pitch, roll, g)

    def _result(self, pitch, roll, g):
        self.last_raw = (pitch, roll)
        pitch -= self.zero[0]
        roll -= self.zero[1]
        clamp = lambda v: max(-1.0, min(1.0, v))
        return {"pitch": pitch, "roll": roll, "g": g,
                "bx": clamp(roll / BUBBLE_RANGE_DEG), "by": clamp(-pitch / BUBBLE_RANGE_DEG),
                "zeroed": self.zero != (0.0, 0.0)}

    def set_zero(self):
        if getattr(self, "last_raw", None):
            self.zero = self.last_raw

    def clear_zero(self):
        self.zero = (0.0, 0.0)


# -- microphone ------------------------------------------------------------
def _die_with_parent():
    """Run in the child before exec: get SIGTERM if SysMon dies, however it dies."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def which(cmd):
    for d in (os.environ.get("PATH") or "/usr/bin:/bin").split(os.pathsep):
        f = os.path.join(d, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


def mic_commands(rate):
    """Capture commands to try, best first. All emit raw S16LE stereo.

    On CardputerZero the clean microphone signal comes through PipeWire (the
    route the official Recorder app uses). Opening the codec directly with
    ALSA gives a signal swamped by electrical interference, so it is only a
    last resort.
    """
    cmds = []
    if which("pw-record"):
        cmds.append(("pipewire", ["pw-record", "--rate", str(rate), "--channels", "2",
                                  "--format", "s16", "-"]))
    if which("parecord"):
        cmds.append(("pulse", ["parecord", "--raw", "--format=s16le", "--rate=%d" % rate,
                               "--channels=2"]))
    if which("arecord"):
        cards = read_file("/proc/asound/cards", "") or ""
        for line in cards.splitlines():
            line = line.strip()
            if line[:1].isdigit() and ("es8389" in line.lower() or "es8388" in line.lower()):
                cmds.append(("alsa-direct", ["arecord", "-q", "-D", "plughw:%s,0" % line.split()[0],
                                             "-f", "S16_LE", "-r", str(rate), "-c", "2", "-t", "raw"]))
    return cmds


CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                          os.path.join(os.path.expanduser("~"), ".config"), "pocket-sysmon")
SOUND_CONF = os.path.join(CONFIG_DIR, "sound.conf")
HPF_HZ = 150.0   # ignore hum / rumble / DC below this


class Mic:
    """Microphone loudness meter.

    Captures 48 kHz stereo S16 through PipeWire (fallbacks: PulseAudio,
    direct ALSA), automatically keeps whichever channel actually carries the
    mic, removes everything below HPF_HZ and reports dBFS. A per-device
    'quiet' baseline can be captured; it is saved per capture route.
    """

    RATE = 48000
    WINDOW = 4800      # frames = 100 ms
    FRAME = 4          # bytes per stereo S16 frame

    def __init__(self):
        self.proc = None
        self.buf = b""
        self.error = None
        self.db = None
        self.peak = -90.0
        self.peak_t = 0.0
        self.hist = deque([-90.0] * SOUND_HISTORY, maxlen=SOUND_HISTORY)
        self.device = None          # capture route name
        self.channel = None         # 0 = left, 1 = right, None = deciding
        self.ch_levels = [[], []]
        self.recent = deque(maxlen=30)
        self.baselines = self._load_baselines()
        if HAS_NUMPY:
            self.win = np.hanning(self.WINDOW).astype(np.float32)
            self.win_pow = float(np.mean(self.win * self.win))
            freqs = np.fft.rfftfreq(self.WINDOW, 1.0 / self.RATE)
            self.keep = freqs >= HPF_HZ

    # -- baseline persistence (one value per capture route) --
    @staticmethod
    def _load_baselines():
        out = {}
        txt = read_file(SOUND_CONF) or ""
        for line in txt.splitlines():
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("quiet_dbfs."):   # old un-suffixed key is ignored on purpose
                try:
                    out[k.split(".", 1)[1]] = float(v)
                except ValueError:
                    pass
        return out

    def _save_baselines(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(SOUND_CONF, "w") as f:
                for route, v in sorted(self.baselines.items()):
                    f.write("quiet_dbfs.{}={:.2f}\n".format(route, v))
        except OSError:
            pass

    @property
    def baseline(self):
        return self.baselines.get(self.device) if self.device else None

    def calibrate(self):
        if len(self.recent) < 5 or not self.device:
            return False
        vals = sorted(self.recent)
        self.baselines[self.device] = vals[len(vals) // 2]
        self._save_baselines()
        return True

    def clear_calibration(self):
        if self.device:
            self.baselines.pop(self.device, None)
            self._save_baselines()

    @property
    def rel(self):
        b = self.baseline
        if self.db is None or b is None:
            return None
        return max(0.0, self.db - b)

    # -- capture --
    def start(self):
        if self.proc:
            return
        self.error = None
        import tempfile
        cmds = mic_commands(self.RATE)
        if not cmds:
            self.error = "no audio capture tool found (pw-record / arecord)"
            return
        for route, cmd in cmds:
            errf = tempfile.TemporaryFile()
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                     stdin=subprocess.DEVNULL, preexec_fn=_die_with_parent)
            except OSError as e:
                errf.close()
                self.error = str(e)
                continue
            time.sleep(0.3)
            if p.poll() is None:
                errf.close()   # unlinked temp file: stderr can never block the helper
                self.proc, self.device = p, route
                os.set_blocking(p.stdout.fileno(), False)
                self.db, self.buf = None, b""
                self.channel, self.ch_levels = None, [[], []]
                self.recent.clear()
                return
            errf.seek(0)
            err = errf.read().decode(errors="replace").strip()
            errf.close()
            self.error = "{}: {}".format(route, err.splitlines()[-1] if err else "failed to start")
        self.proc = None

    def stop(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
                p.wait(timeout=1)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=1)
                except Exception:
                    pass
        self.buf = b""

    def measure(self, x):
        """dBFS of one channel (sequence of ints/floats), hum below HPF_HZ removed."""
        n = len(x)
        if HAS_NUMPY and n == self.WINDOW:
            a = np.asarray(x, dtype=np.float32)
            a = a - a.mean()
            spec = np.fft.rfft(a * self.win)
            mag2 = (spec.real ** 2 + spec.imag ** 2)[self.keep]
            energy = 2.0 * float(mag2.sum()) / (n * n)
            rms = math.sqrt(max(energy, 0.0) / self.win_pow)
        else:
            vals = list(x)
            mean = sum(vals) / float(max(1, n))
            sig = [v - mean for v in vals]
            rc = 1.0 / (2 * math.pi * HPF_HZ)
            alpha = rc / (rc + 1.0 / self.RATE)
            for _ in range(3):
                out, y_prev, x_prev = [], 0.0, sig[0] if sig else 0.0
                for v in sig:
                    y_prev = alpha * (y_prev + v - x_prev)
                    x_prev = v
                    out.append(y_prev)
                sig = out
            tail = sig[n // 4:]
            rms = math.sqrt(sum(y * y for y in tail) / max(1, len(tail)))
        return max(-90.0, 20 * math.log10(max(rms, 0.5) / 32768.0))

    def _split(self, frames_bytes):
        if HAS_NUMPY:
            a = np.frombuffer(frames_bytes, dtype="<i2").reshape(-1, 2)
            return a[:, 0], a[:, 1]
        v = struct.unpack("<%dh" % (len(frames_bytes) // 2), frames_bytes)
        return v[0::2], v[1::2]

    def update(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.error = "microphone stopped"
            self.proc = None
            return
        try:
            chunk = os.read(self.proc.stdout.fileno(), 262144)
        except (BlockingIOError, OSError):
            chunk = b""
        if not chunk:
            return
        buf = self.buf + chunk
        need = self.WINDOW * self.FRAME
        if len(buf) > need + self.FRAME:
            # drop whole frames from the front so frame alignment is preserved
            drop = (len(buf) - need) // self.FRAME * self.FRAME
            buf = buf[drop:]
        self.buf = buf
        usable = len(buf) // self.FRAME * self.FRAME
        if usable < need:
            return
        left, right = self._split(buf[usable - need:usable])

        if self.channel is None:
            # pick the channel that actually carries the microphone
            self.ch_levels[0].append(self.measure(left))
            self.ch_levels[1].append(self.measure(right))
            if len(self.ch_levels[0]) < 6:
                return
            l = sum(self.ch_levels[0]) / len(self.ch_levels[0])
            r = sum(self.ch_levels[1]) / len(self.ch_levels[1])
            self.channel = 0 if l >= r else 1
        db = self.measure(left if self.channel == 0 else right)
        self.db = db if self.db is None else self.db + 0.5 * (db - self.db)
        self.recent.append(db)
        now = time.monotonic()
        if db > self.peak or now - self.peak_t > 2.0:
            self.peak, self.peak_t = db, now
        self.hist.append(self.db)


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
class Renderer:
    def __init__(self):
        self.f_title = load_font(13, bold=True)
        self.f_big = load_font(22, bold=True)
        self.f_med = load_font(13, bold=True)
        self.f_reg = load_font(12)
        self.f_sm = load_font(10)
        self.f_smb = load_font(10, bold=True)

    # -- primitives --
    def bar(self, d, x, y, w, h, pct, color):
        w = max(h, int(w))
        d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=GRID)
        fill_w = int(w * max(0.0, min(100.0, pct)) / 100.0)
        if fill_w >= h:
            d.rounded_rectangle((x, y, x + fill_w, y + h), radius=h // 2, fill=color)
        elif fill_w > 0:
            d.ellipse((x, y, x + h, y + h), fill=color)

    def panel(self, d, box):
        d.rounded_rectangle(box, radius=6, fill=PANEL)

    def text_right(self, d, xr, y, s, font, fill):
        d.text((xr - d.textlength(s, font=font), y), s, font=font, fill=fill)

    def text_center(self, d, xc, y, s, font, fill):
        d.text((xc - d.textlength(s, font=font) / 2, y), s, font=font, fill=fill)

    def graph(self, d, box, values, color, fill=None, vmax=None, vmin=0.0, width=2):
        x0, y0, x1, y1 = box
        vals = list(values)
        if len(vals) < 2:
            return
        hi = vmax if vmax is not None else max(vals)
        lo = vmin
        if hi - lo < 1e-9:
            hi = lo + 1.0
        step = (x1 - x0) / float(len(vals) - 1)
        pts = [(x0 + i * step, y1 - (y1 - y0) * (min(max(v, lo), hi) - lo) / (hi - lo))
               for i, v in enumerate(vals)]
        if fill:
            d.polygon([(x0, y1)] + pts + [(x1, y1)], fill=fill)
        d.line(pts, fill=color, width=width)

    def grid_lines(self, d, box, n=3):
        x0, y0, x1, y1 = box
        for i in range(1, n + 1):
            gy = y1 - int((y1 - y0) * i / (n + 1))
            d.line((x0, gy, x1, gy), fill=GRID)

    def battery_icon(self, d, x, y, batt):
        d.rounded_rectangle((x, y, x + 20, y + 10), radius=2, outline=DIM, width=1)
        d.rectangle((x + 21, y + 3, x + 22, y + 7), fill=DIM)
        if batt and batt["cap"] is not None:
            cap = batt["cap"]
            col = RED if cap < 15 else (YELLOW if cap < 35 else GREEN)
            d.rectangle((x + 2, y + 2, x + 2 + int(16 * cap / 100), y + 8), fill=col)
            if batt["status"] == "Charging":
                d.polygon([(x + 11, y + 1), (x + 7, y + 6), (x + 10, y + 6),
                           (x + 9, y + 10), (x + 13, y + 4), (x + 10, y + 4)], fill=TEXT)

    def header(self, d, page, batt):
        d.rectangle((0, 0, W, 18), fill=PANEL)
        d.text((6, 2), "SysMon", font=self.f_title, fill=CYAN)
        for i in range(len(PAGES)):
            cx = 66 + i * 8
            d.ellipse((cx, 7, cx + 4, 11), fill=CYAN if i == page else GRID)
        d.text((118, 4), PAGES[page], font=self.f_smb, fill=TEXT)
        clock = datetime.now().strftime("%H:%M:%S")
        self.text_right(d, W - 34, 3, clock, self.f_reg, TEXT)
        if batt and batt["cap"] is not None:
            clock_w = d.textlength(clock, font=self.f_reg)
            self.text_right(d, W - 34 - clock_w - 6, 3, "{}%".format(batt["cap"]),
                            self.f_reg, DIM)
        self.battery_icon(d, W - 28, 4, batt)

    def footer(self, d, hint, right=None, right_color=CYAN):
        d.text((6, H - 13), hint, font=self.f_sm, fill=DIM)
        if right:
            self.text_right(d, 314, H - 13, right, self.f_sm, right_color)

    # -- page 1: overview --
    def page_overview(self, d, s):
        cpu = s["cpu"]
        self.panel(d, (4, 22, 214, 100))
        col = level_color(cpu.total)
        d.text((10, 24), "CPU", font=self.f_med, fill=DIM)
        d.text((10, 38), "{:.0f}%".format(cpu.total), font=self.f_big, fill=col)
        if s["freq"]:
            d.text((10, 64), "{} MHz".format(s["freq"]), font=self.f_sm, fill=DIM)
        d.text((10, 77), "load {}".format(s["load"][0]), font=self.f_sm, fill=DIM)
        gbox = (72, 28, 208, 94)
        self.grid_lines(d, gbox)
        self.graph(d, gbox, cpu.history, GREEN, GREEN_FILL, vmax=100)

        self.panel(d, (218, 22, 316, 100))
        d.text((224, 24), "CORES", font=self.f_sm, fill=DIM)
        cores = cpu.cores[:4] or [0.0]
        n, bw = len(cores), 16
        gap = (316 - 218 - 12 - n * bw) // max(1, n - 1) if n > 1 else 0
        for i, v in enumerate(cores):
            bx, by0, by1 = 224 + i * (bw + gap), 40, 86
            d.rounded_rectangle((bx, by0, bx + bw, by1), radius=3, fill=GRID)
            h = int((by1 - by0) * v / 100.0)
            if h > 2:
                d.rounded_rectangle((bx, by1 - h, bx + bw, by1), radius=3, fill=level_color(v))
            self.text_center(d, bx + bw / 2, 87, str(i), self.f_sm, DIM)

        mem_pct, mem_used, mem_total, _, _ = s["mem"]
        self.panel(d, (4, 104, 316, 126))
        d.text((10, 108), "RAM", font=self.f_med, fill=DIM)
        label = "{} / {}  {:.0f}%".format(fmt_mb(mem_used), fmt_mb(mem_total), mem_pct)
        lw = d.textlength(label, font=self.f_reg)
        self.bar(d, 48, 111, 310 - lw - 10 - 48, 8, mem_pct, level_color(mem_pct, 70, 90))
        self.text_right(d, 310, 108, label, self.f_reg, TEXT)

        self.panel(d, (4, 130, 148, 152))
        temp = s["temp"]
        d.text((10, 134), "TEMP", font=self.f_med, fill=DIM)
        if temp is None:
            d.text((58, 134), "--", font=self.f_med, fill=DIM)
        else:
            d.text((58, 134), "{:.1f}°C".format(temp), font=self.f_med,
                   fill=level_color(temp, 65, 78))
            if s["throttled"]:
                self.text_right(d, 143, 135, "THR", self.f_sm, RED)

        self.panel(d, (152, 130, 316, 152))
        batt = s["batt"]
        d.text((158, 134), "BATT", font=self.f_med, fill=DIM)
        if batt and batt["cap"] is not None:
            cap = batt["cap"]
            col = RED if cap < 15 else (YELLOW if cap < 35 else GREEN)
            d.text((202, 134), "{}%".format(cap), font=self.f_med, fill=col)
            if batt["eta"] is not None:
                txt = fmt_duration(batt["eta"], short=True)
                txt = ("↑" if batt["eta_kind"] == "full" else "") + txt
                self.text_right(d, 310, 135, txt, self.f_sm, CYAN)
            elif batt["status"] in ("Charging", "Full"):
                self.text_right(d, 310, 135, batt["status"].lower(), self.f_sm, CYAN)
        else:
            d.text((202, 134), "n/a", font=self.f_med, fill=DIM)

        ip = next((i["ip"] for i in s["ifaces"] if i["ip"]), None)
        self.footer(d, "←→ page   1-6 jump   ESC quit", ip)

    # -- page 2: processes --
    def page_procs(self, d, s, ui):
        procs = s["procs"]
        self.panel(d, (4, 22, 316, 152))
        sort_cpu = procs.sort_by == "cpu"
        d.text((10, 25), "PROCESS", font=self.f_smb, fill=DIM)
        d.text((160, 25), "PID", font=self.f_smb, fill=DIM)
        self.text_right(d, 252, 25, "CPU" + (" ▼" if sort_cpu else ""), self.f_smb,
                        CYAN if sort_cpu else DIM)
        self.text_right(d, 310, 25, "MEM" + ("" if sort_cpu else " ▼"), self.f_smb,
                        DIM if sort_cpu else CYAN)
        rows = procs.top(5)
        y = 39
        for i, r in enumerate(rows):
            selected = i == ui["sel"]
            if selected:
                d.rounded_rectangle((7, y - 1, 313, y + 20), radius=4, fill=SEL)
            name = r["name"] if len(r["name"]) <= 17 else r["name"][:16] + "…"
            d.text((12, y + 3), name, font=self.f_reg, fill=TEXT)
            d.text((160, y + 4), str(r["pid"]), font=self.f_sm, fill=DIM)
            cpu_txt = "{:.0f}%".format(r["cpu"]) if procs.ready else "…"
            self.text_right(d, 252, y + 3, cpu_txt, self.f_reg,
                            level_color(r["cpu"], 50, 90) if procs.ready else DIM)
            self.text_right(d, 310, y + 3, fmt_mb(r["rss"]), self.f_reg, TEXT)
            y += 22
        if not rows:
            d.text((12, 60), "reading processes…", font=self.f_reg, fill=DIM)
        self.footer(d, "↑↓ select  Enter end  S sort  ←→ page")
        if ui.get("confirm"):
            r = ui["confirm"]
            box = (40, 52, 280, 122)
            d.rounded_rectangle(box, radius=8, fill=(14, 16, 22), outline=RED, width=2)
            self.text_center(d, 160, 60, "End this process?", self.f_med, TEXT)
            nm = r["name"] if len(r["name"]) <= 22 else r["name"][:21] + "…"
            self.text_center(d, 160, 80, "{}  (PID {})".format(nm, r["pid"]), self.f_reg, YELLOW)
            self.text_center(d, 160, 100, "Y = end     N / ESC = cancel", self.f_sm, DIM)

    # -- page 3: network --
    def page_net(self, d, s):
        net = s["net"]
        self.panel(d, (4, 22, 316, 70))
        sig, ssid = s["wifi_sig"], s["ssid"]
        y, shown = 25, 0
        for itf in s["ifaces"]:
            if shown >= 3:
                break
            name = itf["name"]
            d.ellipse((10, y + 4, 16, y + 10), fill=GREEN if itf["ip"] else DIM)
            d.text((22, y), name, font=self.f_med, fill=TEXT)
            d.text((80, y + 1), itf["ip"] or ("up" if itf["up"] else "down"),
                   font=self.f_reg, fill=TEXT if itf["ip"] else DIM)
            if itf["wifi"] and sig and sig[0] == name:
                dbm = sig[1]
                q = max(0, min(100, 2 * (dbm + 100)))
                bars = 4 if q > 75 else 3 if q > 50 else 2 if q > 25 else 1
                for b in range(4):
                    bx, bh = 298 + b * 4, 3 + b * 3
                    d.rectangle((bx, y + 12 - bh, bx + 2, y + 12), fill=CYAN if b < bars else GRID)
                self.text_right(d, 292, y + 2, "{:.0f}dBm".format(dbm), self.f_sm, DIM)
                if ssid:
                    label = ssid if len(ssid) <= 11 else ssid[:10] + "…"
                    self.text_right(d, 248, y + 2, label, self.f_sm, CYAN)
            elif itf["ip"]:
                rx, tx = net.rates.get(name, (0.0, 0.0))
                self.text_right(d, 310, y + 2, "↓{}  ↑{}".format(
                    fmt_rate(rx), fmt_rate(tx)), self.f_sm, DIM)
            y += 15
            shown += 1
        if shown == 0:
            d.text((10, 38), "no network interfaces", font=self.f_reg, fill=DIM)

        self.panel(d, (4, 74, 316, 152))
        d.text((10, 77), "↓", font=self.f_med, fill=GREEN)
        d.text((24, 77), fmt_rate(net.rx), font=self.f_med, fill=GREEN)
        d.text((160, 77), "↑", font=self.f_med, fill=ORANGE)
        d.text((174, 77), fmt_rate(net.tx), font=self.f_med, fill=ORANGE)
        peak = max(max(net.rx_hist), max(net.tx_hist), 1024.0)
        gbox = (10, 96, 310, 148)
        self.grid_lines(d, gbox)
        self.graph(d, gbox, net.rx_hist, GREEN, GREEN_FILL, vmax=peak)
        self.graph(d, gbox, net.tx_hist, ORANGE, None, vmax=peak)
        self.text_right(d, 310, 94, "max " + fmt_rate(peak), self.f_sm, DIM)
        self.footer(d, "last 60 s   ←→ page   ESC quit")

    # -- page 4: power & thermal --
    def page_power(self, d, s):
        temps = s["temp_hist"]
        temp = s["temp"]
        self.panel(d, (4, 22, 316, 92))
        d.text((10, 25), "TEMP", font=self.f_med, fill=DIM)
        valid = [t for t in temps if t is not None]
        if temp is not None:
            d.text((10, 41), "{:.1f}°C".format(temp), font=self.f_big,
                   fill=level_color(temp, 65, 78))
        else:
            d.text((10, 41), "--", font=self.f_big, fill=DIM)
        if valid:
            d.text((10, 68), "min {:.0f}  max {:.0f}".format(min(valid), max(valid)),
                   font=self.f_sm, fill=DIM)
        if s["freq"]:
            d.text((10, 79), "{} MHz".format(s["freq"]), font=self.f_sm, fill=DIM)
        if s["throttled"]:
            self.text_right(d, 100, 27, "THROTTLED", self.f_smb, RED)
        gbox = (106, 28, 310, 86)
        if valid:
            lo = min(30.0, math.floor(min(valid) / 5.0) * 5)
            hi = max(85.0, math.ceil(max(valid) / 5.0) * 5)
            # warning line at 75 C
            wy = gbox[3] - (gbox[3] - gbox[1]) * (75 - lo) / (hi - lo)
            d.line((gbox[0], wy, gbox[2], wy), fill=RED_FILL, width=1)
            self.grid_lines(d, gbox, 2)
            series = [t if t is not None else valid[0] for t in temps]
            self.graph(d, gbox, series, ORANGE, ORANGE_FILL, vmax=hi, vmin=lo)
            self.text_right(d, 310, 86 - 10, "4 min", self.f_sm, DIM)

        batt = s["batt"]
        self.panel(d, (4, 96, 316, 126))
        d.text((10, 99), "BATT", font=self.f_med, fill=DIM)
        if batt and batt["cap"] is not None:
            cap = batt["cap"]
            col = RED if cap < 15 else (YELLOW if cap < 35 else GREEN)
            d.text((54, 99), "{}%".format(cap), font=self.f_med, fill=col)
            self.bar(d, 96, 103, 70, 7, cap, col)
            if batt["eta"] is not None:
                if batt["eta_kind"] == "full":
                    eta_txt = "full in {}".format(fmt_duration(batt["eta"]))
                else:
                    eta_txt = "about {} left".format(fmt_duration(batt["eta"]))
                self.text_right(d, 310, 99, eta_txt, self.f_reg, CYAN)
            else:
                self.text_right(d, 310, 99, batt["status"].lower(), self.f_reg, DIM)
            parts = []
            if batt["volt"]:
                parts.append("{:.2f} V".format(batt["volt"]))
            if batt["curr"] is not None:
                parts.append("{:+.0f} mA".format(batt["curr"]))
            if parts:
                d.text((10, 113), "   ".join(parts), font=self.f_sm, fill=PURPLE)
        else:
            d.text((54, 99), "no battery data", font=self.f_reg, fill=DIM)

        self.panel(d, (4, 130, 316, 152))
        disk = s["disk"]
        d.text((10, 134), "DISK", font=self.f_med, fill=DIM)
        if disk:
            pct, used, total = disk
            self.bar(d, 50, 137, 60, 7, pct, level_color(pct, 75, 90))
            d.text((116, 135), "{}/{}".format(fmt_mb(used), fmt_mb(total)),
                   font=self.f_sm, fill=TEXT)
        self.text_right(d, 310, 135, "up " + fmt_duration(s["uptime"]), self.f_sm, TEXT)
        self.footer(d, "←→ page   ESC quit", s["host"][:18], DIM)

    # -- page 5: bubble level --
    def page_level(self, d, s):
        imu = s["imu"]
        cx, cy, r = 84, 92, 64
        if imu is None:
            self.panel(d, (4, 22, 316, 152))
            self.text_center(d, 160, 62, "No motion sensor found", self.f_med, TEXT)
            self.text_center(d, 160, 84, "The Lite model has no IMU, or the", self.f_reg, DIM)
            self.text_center(d, 160, 100, "bmi270 driver is not loaded.", self.f_reg, DIM)
            self.footer(d, "←→ page   ESC quit")
            return
        level = abs(imu["pitch"]) < 1.0 and abs(imu["roll"]) < 1.0
        ring = GREEN if level else DIM
        # vial
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(18, 30, 24), outline=ring, width=2)
        for rr in (r * 2 // 3, r // 3):
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=GRID, width=1)
        d.line((cx - r + 4, cy, cx + r - 4, cy), fill=GRID)
        d.line((cx, cy - r + 4, cx, cy + r - 4), fill=GRID)
        # bubble
        br = 11
        bx = cx + imu["bx"] * (r - br - 3)
        by = cy + imu["by"] * (r - br - 3)
        bcol = GREEN if level else (YELLOW if max(abs(imu["pitch"]), abs(imu["roll"])) < 5 else ORANGE)
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=bcol)
        d.ellipse((bx - br + 4, by - br + 3, bx - br + 9, by - br + 7), fill=(255, 255, 255))
        # centre target
        d.ellipse((cx - br - 2, cy - br - 2, cx + br + 2, cy + br + 2), outline=TEXT, width=1)

        self.panel(d, (164, 22, 316, 152))
        d.text((172, 27), "PITCH", font=self.f_sm, fill=DIM)
        d.text((172, 39), "{:+.1f}°".format(imu["pitch"]), font=self.f_big, fill=TEXT)
        d.text((172, 67), "ROLL", font=self.f_sm, fill=DIM)
        d.text((172, 79), "{:+.1f}°".format(imu["roll"]), font=self.f_big, fill=TEXT)
        if level:
            d.text((172, 109), "LEVEL", font=self.f_med, fill=GREEN)
        else:
            tilt = math.degrees(math.acos(max(-1.0, min(1.0,
                   math.cos(math.radians(imu["pitch"])) * math.cos(math.radians(imu["roll"]))))))
            d.text((172, 109), "tilt {:.1f}°".format(tilt), font=self.f_reg, fill=YELLOW)
        d.text((172, 128), "{:.2f} g{}".format(imu["g"], "   zeroed" if imu["zeroed"] else ""),
               font=self.f_sm, fill=PURPLE if imu["zeroed"] else DIM)
        self.footer(d, "Enter: set zero   X: clear   ←→ page")

    # -- page 6: sound meter --
    def page_sound(self, d, s):
        mic = s["mic"]
        self.panel(d, (4, 22, 316, 152))
        if mic is None or (mic.proc is None and mic.error):
            self.text_center(d, 160, 62, "Microphone unavailable", self.f_med, TEXT)
            import textwrap
            msg = (mic.error if mic else "") or ""
            for i, line in enumerate(textwrap.wrap(msg, 46)[:3]):
                self.text_center(d, 160, 86 + i * 14, line, self.f_sm, DIM)
            self.footer(d, "←→ page   ESC quit")
            return
        db = mic.db if mic.db is not None else -90.0
        rel = mic.rel
        calibrated = rel is not None
        if db > -3:
            label, col = "Clipping!", RED
        elif calibrated:
            if rel < 6:
                label, col = "Quiet", GREEN
            elif rel < 25:
                label, col = "Moderate", GREEN
            elif rel < 40:
                label, col = "Loud", YELLOW
            elif rel < 50:
                label, col = "Very loud", ORANGE
            else:
                label, col = "Extremely loud", RED
        else:
            if db < -70:
                label, col = "Quiet", GREEN
            elif db < -48:
                label, col = "Moderate", GREEN
            elif db < -30:
                label, col = "Loud", YELLOW
            else:
                label, col = "Very loud", ORANGE

        if calibrated:
            d.text((10, 26), "ABOVE QUIET", font=self.f_sm, fill=DIM)
            big = "+{:.0f} dB".format(rel)
        else:
            d.text((10, 26), "LEVEL (dBFS)", font=self.f_sm, fill=DIM)
            big = "{:.0f} dB".format(db)
        d.text((10, 38), big if mic.db is not None else "\u2026", font=self.f_big, fill=col)
        self.text_right(d, 310, 28, label, self.f_med, col)
        if calibrated:
            sub = "{:.0f} dBFS  quiet ref {:.0f}".format(db, mic.baseline)
        else:
            sub = "peak {:.0f} dBFS".format(mic.peak)
        self.text_right(d, 310, 47, sub, self.f_sm, DIM)

        # meter: 0..60 dB above quiet when calibrated, else -60..0 dBFS
        mx0, mx1, my = 10, 310, 66
        span = mx1 - mx0
        if calibrated:
            lo, hi, ticks = 0.0, 60.0, (0, 20, 40, 60)
            zones = ((0, 40, GREEN), (40, 50, YELLOW), (50, 60, RED))
            val, peak = rel, max(0.0, mic.peak - mic.baseline)
            series = [max(0.0, v - mic.baseline) for v in mic.hist]
            tick_fmt = lambda v: "+%d" % v if v else "quiet"
        else:
            lo, hi, ticks = -90.0, 0.0, (-90, -60, -30, 0)
            zones = ((-90, -30, GREEN), (-30, -12, YELLOW), (-12, 0, RED))
            val, peak = db, mic.peak
            series = list(mic.hist)
            tick_fmt = str
        to_x = lambda v: mx0 + span * (max(lo, min(hi, v)) - lo) / (hi - lo)
        d.rounded_rectangle((mx0, my, mx1, my + 14), radius=4, fill=GRID)
        fill_x = to_x(val)
        for z0, z1, zc in zones:
            a, b = to_x(z0), min(to_x(z1), fill_x)
            if b > a:
                d.rectangle((a, my + 2, b, my + 12), fill=zc)
        px = to_x(peak)
        d.line((px, my, px, my + 14), fill=TEXT, width=2)
        for v in ticks:
            lbl = tick_fmt(v)
            lw = d.textlength(lbl, font=self.f_sm)
            lx = min(max(to_x(v) - lw / 2, mx0), mx1 - lw)
            d.text((lx, my + 16), lbl, font=self.f_sm, fill=DIM)
        gbox = (10, 98, 310, 148)
        self.grid_lines(d, gbox, 2)
        if calibrated:
            self.graph(d, gbox, series, CYAN, (20, 50, 70), vmax=60.0, vmin=0.0)
            self.footer(d, "Enter: re-calibrate   X: clear   \u2190\u2192 page")
        else:
            self.graph(d, gbox, series, CYAN, (20, 50, 70), vmax=-20.0, vmin=-90.0)
            d.rounded_rectangle((60, 112, 260, 134), radius=6, fill=(14, 16, 22), outline=CYAN)
            self.text_center(d, 160, 116, "In a quiet room, press Enter", self.f_reg, CYAN)
            self.footer(d, "Enter: calibrate quiet   \u2190\u2192 page   ESC quit")

    def render(self, page, s, ui):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        self.header(d, page, s["batt"])
        if page == P_OVERVIEW:
            self.page_overview(d, s)
        elif page == P_PROCS:
            self.page_procs(d, s, ui)
        elif page == P_NET:
            self.page_net(d, s)
        elif page == P_POWER:
            self.page_power(d, s)
        elif page == P_LEVEL:
            self.page_level(d, s)
        else:
            self.page_sound(d, s)
        if ui.get("toast") and time.monotonic() < ui.get("toast_until", 0):
            msg, col = ui["toast"]
            w = d.textlength(msg, font=self.f_reg) + 20
            d.rounded_rectangle((160 - w / 2, 128, 160 + w / 2, 150), radius=6,
                                fill=(14, 16, 22), outline=col, width=1)
            self.text_center(d, 160, 132, msg, self.f_reg, col)
        return img


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
class Collector:
    def __init__(self):
        self.cpu = CpuStats()
        self.net = NetStats()
        self.procs = ProcStats()
        self.battery = Battery()
        self.imu = Imu()
        self.mic = Mic()
        self.temp_hist = deque([None] * TEMP_HISTORY, maxlen=TEMP_HISTORY)
        self.slow_t = -99.0
        self.s = {}

    def tick(self, page):
        """Once-per-second sampling."""
        self.cpu.sample()
        self.net.sample()
        mem = read_mem()
        temp = read_temp()
        self.temp_hist.append(temp)
        if page == P_PROCS:
            self.procs.sample(mem[2])
        now = time.monotonic()
        s = self.s
        if now - self.slow_t > 5:
            self.slow_t = now
            s["disk"] = read_disk("/")
            s["ifaces"] = read_interfaces()
            s["throttled"] = read_throttled()
            s["host"] = socket.gethostname()
        up, load = read_uptime_load()
        sig = read_wifi_signal()
        s.update({
            "cpu": self.cpu, "net": self.net, "procs": self.procs, "mem": mem,
            "temp": temp, "temp_hist": self.temp_hist, "freq": read_freq_mhz(),
            "batt": self.battery.read(), "uptime": up, "load": load,
            "wifi_sig": sig, "ssid": read_ssid() if sig else None,
            "mic": self.mic,
        })
        s.setdefault("imu", None)
        return s

    def fast(self, page):
        """Per-frame sensor updates for the Level and Sound pages."""
        if page == P_LEVEL:
            self.s["imu"] = self.imu.read() if self.imu.available else None
        elif page == P_SOUND:
            self.mic.update()


# --------------------------------------------------------------------------
# Framebuffer output
# --------------------------------------------------------------------------
class Framebuffer:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)
        vbuf = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, vbuf)
        self.xres, self.yres = struct.unpack_from("2I", vbuf, 0)
        self.bpp = struct.unpack_from("I", vbuf, 24)[0]
        fix_fmt = "16sL" + "I" * 6 + "HHHI"
        fbuf = bytearray(struct.calcsize(fix_fmt))
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fbuf)
        fields = struct.unpack(fix_fmt, fbuf)
        self.smem_len, self.line_length = fields[2], fields[8]
        if self.bpp not in (16, 32):
            os.close(self.fd)
            raise RuntimeError("unsupported framebuffer depth: {} bpp".format(self.bpp))
        self.mm = mmap.mmap(self.fd, self.smem_len, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

    def _convert(self, img):
        if self.bpp == 32:
            return img.convert("RGBA").tobytes("raw", "BGRA")
        if HAS_NUMPY:
            a = np.asarray(img, dtype=np.uint16)
            v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
            return v.astype("<u2").tobytes()
        out = bytearray()
        for r, g, b in img.getdata():
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out += bytes((v & 0xFF, v >> 8))
        return bytes(out)

    def show(self, img):
        if (self.xres, self.yres) != img.size:
            if (self.yres, self.xres) == img.size:
                img = img.rotate(90, expand=True)
            else:
                img = img.resize((self.xres, self.yres))
        data = self._convert(img)
        row = self.xres * self.bpp // 8
        if row == self.line_length:
            self.mm.seek(0)
            self.mm.write(data)
        else:
            for y in range(self.yres):
                self.mm.seek(y * self.line_length)
                self.mm.write(data[y * row:(y + 1) * row])

    def clear(self):
        self.mm.seek(0)
        self.mm.write(b"\x00" * min(self.smem_len, self.line_length * self.yres))

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)


# --------------------------------------------------------------------------
# Keyboard (raw evdev, no python3-evdev dependency)
# --------------------------------------------------------------------------
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)


class Keys:
    def __init__(self):
        self.fds = []
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                self.fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def poll(self, timeout):
        """Return key codes pressed (or auto-repeated) within timeout seconds."""
        if not self.fds:
            time.sleep(timeout)
            return []
        try:
            r, _, _ = select.select(self.fds, [], [], timeout)
        except (OSError, ValueError):
            return []
        pressed = []
        for fd in r:
            try:
                data = os.read(fd, EVENT_SIZE * 32)
            except (BlockingIOError, OSError):
                continue
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                if etype == EV_KEY and value in (1, 2):
                    pressed.append((code, value))
        return pressed

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------
# App controller
# --------------------------------------------------------------------------
class App:
    def __init__(self, collector):
        self.c = collector
        self.page = P_OVERVIEW
        self.ui = {"sel": 0, "confirm": None, "toast": None, "toast_until": 0}

    def toast(self, msg, color=CYAN, secs=2.5):
        self.ui["toast"] = (msg, color)
        self.ui["toast_until"] = time.monotonic() + secs

    def set_page(self, page):
        page %= len(PAGES)
        if page == self.page:
            return
        if self.page == P_SOUND:
            self.c.mic.stop()
        self.page = page
        self.ui["confirm"] = None
        if page == P_PROCS:
            self.c.procs.reset()
            self.c.procs.sample(self.c.s.get("mem", (0, 0, 1))[2])
            self.ui["sel"] = 0
        if page == P_SOUND:
            self.c.mic.start()

    def end_process(self, row):
        pid = row["pid"]
        if pid in (1, os.getpid()):
            self.toast("Can't end that process", RED)
            return
        try:
            os.kill(pid, signal.SIGTERM)
            self.toast("Sent stop to {}".format(row["name"][:20]), GREEN)
        except PermissionError:
            self.toast("Permission denied (owned by another user)", RED, 3.5)
        except ProcessLookupError:
            self.toast("Process already ended", DIM)

    def handle(self, code, repeat):
        """Return False to quit."""
        ui = self.ui
        if ui["confirm"]:
            if code == KEY_Y and not repeat:
                self.end_process(ui["confirm"])
                ui["confirm"] = None
            elif code in (KEY_N, KEY_ESC, KEY_Q):
                ui["confirm"] = None
            return True
        if repeat and code not in (KEY_UP, KEY_DOWN, KEY_SEMICOLON, KEY_DOT):
            return True
        if code in (KEY_ESC, KEY_Q):
            return False
        if code in (KEY_RIGHT, KEY_TAB, KEY_SLASH):
            self.set_page(self.page + 1)
        elif code in (KEY_LEFT, KEY_COMMA):
            self.set_page(self.page - 1)
        elif code in NUM_KEYS:
            self.set_page(NUM_KEYS[code])
        elif self.page == P_PROCS:
            n = len(self.c.procs.top(5))
            if code in (KEY_UP, KEY_SEMICOLON):
                ui["sel"] = max(0, ui["sel"] - 1)
            elif code in (KEY_DOWN, KEY_DOT):
                ui["sel"] = min(max(0, n - 1), ui["sel"] + 1)
            elif code == KEY_S:
                p = self.c.procs
                p.sort_by = "mem" if p.sort_by == "cpu" else "cpu"
                p.sort()
                ui["sel"] = 0
            elif code in (KEY_ENTER, KEY_K) and n:
                ui["confirm"] = dict(self.c.procs.top(5)[min(ui["sel"], n - 1)])
        elif self.page == P_SOUND:
            if code in (KEY_ENTER, KEY_Z, KEY_SPACE):
                if self.c.mic.calibrate():
                    self.toast("Quiet level saved: {:.0f} dBFS".format(self.c.mic.baseline), GREEN)
                else:
                    self.toast("Listening\u2026 try again in a second", YELLOW)
            elif code == KEY_X:
                self.c.mic.clear_calibration()
                self.toast("Calibration cleared")
        elif self.page == P_LEVEL:
            if code in (KEY_ENTER, KEY_Z, KEY_SPACE):
                self.c.imu.set_zero()
                self.toast("Zero set to current angle")
            elif code == KEY_X:
                self.c.imu.clear_zero()
                self.toast("Zero cleared")
        return True


# --------------------------------------------------------------------------
# Preview / mock data
# --------------------------------------------------------------------------
def mock_collector():
    c = Collector()
    c.cpu.cores = [34.0, 12.0, 71.0, 8.0]
    c.cpu.total = sum(c.cpu.cores) / 4
    c.cpu.history = deque([25 + 18 * math.sin(i / 6.0) + (i % 7) * 3 for i in range(CPU_HISTORY)],
                          maxlen=CPU_HISTORY)
    c.cpu.history[-1] = c.cpu.total
    c.net.rx_hist = deque([max(0.0, 400e3 * math.sin(i / 9.0) ** 2 + (i % 5) * 20e3)
                           for i in range(NET_HISTORY)], maxlen=NET_HISTORY)
    c.net.tx_hist = deque([60e3 + 40e3 * math.sin(i / 4.0) for i in range(NET_HISTORY)],
                          maxlen=NET_HISTORY)
    c.net.rx, c.net.tx = c.net.rx_hist[-1], c.net.tx_hist[-1]
    c.procs.rows = [
        {"pid": 812, "name": "python3", "cpu": 38.0, "rss": 31.2, "mem": 7.3},
        {"pid": 455, "name": "APPLaunch", "cpu": 12.0, "rss": 22.8, "mem": 5.3},
        {"pid": 1290, "name": "chromium-headless", "cpu": 9.0, "rss": 96.4, "mem": 22.6},
        {"pid": 377, "name": "NetworkManager", "cpu": 2.0, "rss": 9.1, "mem": 2.1},
        {"pid": 301, "name": "systemd-journald", "cpu": 1.0, "rss": 6.4, "mem": 1.5},
    ]
    c.procs.ready = True
    temps = deque([48 + 6 * math.sin(i / 30.0) + (4 if 150 < i < 190 else 0)
                   for i in range(TEMP_HISTORY)], maxlen=TEMP_HISTORY)
    c.mic.db, c.mic.peak = -31.0, -22.0
    c.mic.proc = object()
    c.mic.hist = deque([-50 + 15 * abs(math.sin(i / 7.0)) + (i % 3) * 2 for i in range(SOUND_HISTORY)],
                       maxlen=SOUND_HISTORY)
    c.s = {
        "cpu": c.cpu, "net": c.net, "procs": c.procs, "mic": c.mic,
        "mem": (49.0, 210.0, 427.0, 12.0, 512.0), "temp": temps[-1], "temp_hist": temps,
        "freq": 1000, "batt": {"cap": 78, "status": "Discharging", "volt": 3.94,
                               "curr": -412.0, "eta": 2 * 3600 + 40 * 60, "eta_kind": "left"},
        "uptime": 3 * 3600 + 27 * 60, "load": ["0.42", "0.35", "0.30"],
        "wifi_sig": ("wlan0", -54.0), "ssid": "HomeNet",
        "disk": (31.0, 9100.0, 29300.0),
        "ifaces": [{"name": "wlan0", "ip": "192.168.1.42", "up": True, "wifi": True},
                   {"name": "eth0", "ip": "10.0.0.17", "up": True, "wifi": False}],
        "throttled": 0, "host": "cardputerzero",
    }
    c.net.rates = {"eth0": (12e3, 3e3)}
    c.imu.path = "mock"
    c.s["imu"] = c.imu._result(3.2, -6.5, 1.01)
    return c


def preview(path, page, mock=False, confirm=False):
    if mock:
        c = mock_collector()
    else:
        c = Collector()
        time.sleep(0.5)
        c.tick(page)
        c.procs.sample(c.s["mem"][2])
        time.sleep(0.3)
        c.tick(page)
        c.procs.sample(c.s["mem"][2])
        c.fast(page)
        if page == P_SOUND and not c.mic.proc:
            c.mic.start()
            c.fast(page)
    app = App(c)
    app.page = page
    if confirm and c.procs.top(5):
        app.ui["sel"] = 2
        app.ui["confirm"] = dict(c.procs.top(5)[2])
    img = Renderer().render(page, c.s, app.ui)
    img.save(path)
    c.mic.stop() if not mock else None
    print("wrote", path)
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if args and args[0] == "--preview":
        out = args[1] if len(args) > 1 else "preview.png"
        page = int(args[2]) if len(args) > 2 and args[2].isdigit() else 0
        return preview(out, page, mock="--mock" in args, confirm="--confirm" in args)
    if args and args[0] in ("--version", "-V"):
        print("SysMon", VERSION)
        return 0

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        fb = Framebuffer(FB_DEVICE)
    except Exception as e:
        print("SysMon: cannot open framebuffer {}: {}".format(FB_DEVICE, e), file=sys.stderr)
        print("Hint: add your user to the 'video' group, or run with sudo.", file=sys.stderr)
        return 3

    keys = Keys()
    renderer = Renderer()
    col = Collector()
    app = App(col)
    col.tick(app.page)
    try:
        next_tick = time.monotonic() + 1.0
        next_frame = 0.0
        while _running:
            now = time.monotonic()
            if now >= next_tick:
                col.tick(app.page)
                next_tick = now + 1.0
                next_frame = 0.0
            if now >= next_frame:
                col.fast(app.page)
                fb.show(renderer.render(app.page, col.s, app.ui))
                next_frame = now + FRAME_INTERVAL.get(app.page, 1.0)
            wait = max(0.01, min(next_tick, next_frame) - time.monotonic())
            for code, value in keys.poll(wait):
                if not app.handle(code, value == 2):
                    return 0
                next_frame = 0.0
    finally:
        keys.close()
        col.mic.stop()
        try:
            fb.clear()
        except Exception:
            pass
        fb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
