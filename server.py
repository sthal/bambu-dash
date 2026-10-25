#!/usr/bin/env python3
"""Mission Control: local dashboard for one or more Bambu Lab printers.

Talks to each printer only over the LAN: MQTT (8883) for status and commands,
RTSPS (322) for the camera via ffmpeg, implicit FTPS (990) for the job's .3mf
(plate thumbnail, filament usage). Serves the pages plus SSE, fragmented MP4 and MJPEG.
"""
import collections
import concurrent.futures
import ftplib
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import struct
import sys
import subprocess
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import paho.mqtt.client as mqtt

import toolpath
from history import DEFAULT_COSTS, History, job_costs

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
DEFAULTS = {"port": 8790, "password_hash": "", "session_secret": "", "printers": []}
STREAM_SERVICES = {  # default ingest servers; the stream key is appended
    "youtube": "rtmp://a.rtmp.youtube.com/live2",
    "twitch": "rtmp://live.twitch.tv/app",
    "kick": "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp",
    "custom": "",
}
PRINTER_KEYS = ("host", "serial", "access_code", "name", "locked_cmds", "dev_mode")
ACTIVE = ("PREPARE", "RUNNING", "PAUSE")


def load_cfg():
    try:
        with open(CFG_PATH) as f:
            c = {**DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        c = dict(DEFAULTS)
    c["costs"] = {**DEFAULT_COSTS, **(c.get("costs") or {})}
    st = c.get("stream") or {}
    c["stream"] = {"overlay": st.get("overlay", True),
                   "targets": {k: {"server": (st.get("targets", {}).get(k) or {}).get("server") or v,
                                   "key": (st.get("targets", {}).get(k) or {}).get("key", "")}
                               for k, v in STREAM_SERVICES.items()}}
    # Single-printer configs (before multi-printer support) keep the printer at the top level.
    if c.get("host") and not c["printers"]:
        c["printers"] = [{"id": "p1", **{k: c.get(k) for k in PRINTER_KEYS}}]
    for k in PRINTER_KEYS:
        c.pop(k, None)
    for p in c["printers"]:
        p.setdefault("locked_cmds", [])
        p.setdefault("dev_mode", False)
        p["name"] = p.get("name") or "Bambu"
    return c


def save_cfg(c):
    # Written with 0600 from the start: the file holds the printers' access codes.
    tmp = CFG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CFG_PATH)


cfg = load_cfg()
cfg_lock = threading.Lock()
hist_db = History(os.path.join(BASE, "state", "history.sqlite"))
PORT = int(os.environ.get("PORT") or cfg.get("port") or 8790)

try:
    with open(os.path.join(BASE, "data", "hms.json"), encoding="utf-8") as _f:
        HMS_DB = json.load(_f)  # refreshed by tools/update_hms.py
except (OSError, ValueError):
    HMS_DB = {"hms": {}, "error": {}}


def hms_text(code):
    """Bambu's own description for an HMS code like 0300_0100_0001_0001, in both languages."""
    return HMS_DB["hms"].get(code.replace("_", ""))


def error_text(code_hex):
    return HMS_DB["error"].get(code_hex)


# Prefer a standalone ffmpeg dropped into ./bin (the no-Homebrew route), else whatever is on PATH.
_local_ff = os.path.join(BASE, "bin", "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
FFMPEG = _local_ff if os.path.exists(_local_ff) else "ffmpeg"
# The printer's certificate is self-signed; ffmpeg 9+ verifies TLS by default and would refuse it.
# -timeout (µs) makes ffmpeg give up when the printer silently stops sending, instead of hanging forever.
FF_IN = ["-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-tls_verify", "0", "-timeout", "10000000"]


def _has_drawtext():
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10).stdout
        return " drawtext " in out
    except (OSError, subprocess.TimeoutExpired):
        return False


HAS_DRAWTEXT = _has_drawtext()  # the stream HUD needs ffmpeg built with libfreetype
HUD_FONT = next((f for f in ("/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consola.ttf",
                             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf") if os.path.exists(f)), None)


# Every user-facing string goes out as {"ru": ..., "en": ...}; the page picks the language.
def L(ru, en):
    return {"ru": ru, "en": en}


STAGES = {
    0: L("Печать", "Printing"), 1: L("Автокалибровка стола", "Auto bed leveling"),
    2: L("Нагрев стола", "Heatbed preheating"), 3: L("Калибровка вибраций", "Vibration compensation"),
    4: L("Смена филамента", "Changing filament"), 5: L("Пауза M400", "M400 pause"),
    6: L("Пауза: кончился филамент", "Paused: filament runout"), 7: L("Нагрев сопла", "Heating hotend"),
    8: L("Калибровка экструзии", "Calibrating extrusion"), 9: L("Сканирование стола", "Scanning bed surface"),
    10: L("Проверка первого слоя", "Inspecting first layer"), 11: L("Определение пластины", "Identifying build plate"),
    12: L("Калибровка лидара", "Calibrating micro lidar"), 13: L("Парковка головы", "Homing toolhead"),
    14: L("Очистка сопла", "Cleaning nozzle tip"), 15: L("Проверка температуры экструдера", "Checking extruder temperature"),
    16: L("Пауза пользователем", "Paused by user"), 17: L("Пауза: открыта крышка", "Paused: front cover open"),
    18: L("Калибровка лидара", "Calibrating micro lidar"), 19: L("Калибровка потока", "Calibrating flow"),
    20: L("Пауза: сбой температуры сопла", "Paused: nozzle temperature fault"),
    21: L("Пауза: сбой температуры стола", "Paused: bed temperature fault"),
    22: L("Выгрузка филамента", "Unloading filament"), 23: L("Пауза: пропуск шагов", "Paused: skipped step"),
    24: L("Загрузка филамента", "Loading filament"), 25: L("Калибровка шума моторов", "Calibrating motor noise"),
    26: L("Пауза: потеряна связь с AMS", "Paused: AMS lost"),
    27: L("Пауза: вентилятор хотэнда", "Paused: heatbreak fan"),
    28: L("Пауза: ошибка температуры камеры", "Paused: chamber temperature error"),
    29: L("Охлаждение камеры", "Cooling chamber"), 30: L("Пауза из G-code", "Paused by G-code"),
    31: L("Тест шума моторов", "Motor noise showoff"), 32: L("Пауза: сопло облеплено", "Paused: nozzle clumping"),
    33: L("Пауза: ошибка резака", "Paused: cutter error"), 34: L("Пауза: ошибка первого слоя", "Paused: first layer error"),
    35: L("Пауза: засор сопла", "Paused: nozzle clog"), 255: L("Ожидание", "Idle"), -1: L("Ожидание", "Idle"),
}
STATES = {
    "IDLE": L("Ожидание", "Idle"), "PREPARE": L("Подготовка", "Preparing"), "SLICING": L("Нарезка", "Slicing"),
    "RUNNING": L("Печать", "Printing"), "PAUSE": L("Пауза", "Paused"), "FINISH": L("Готово", "Finished"),
    "FAILED": L("Сбой", "Failed"), "OFFLINE": L("Нет связи", "Offline"),
    "UNCONFIGURED": L("Не настроен", "Not configured"),
}
SPEEDS = {1: L("Тихий", "Silent"), 2: L("Стандарт", "Standard"), 3: L("Спорт", "Sport"), 4: L("Безумный", "Ludicrous")}


def stage_name(stg):
    return STAGES.get(stg, L(f"Этап {stg}", f"Stage {stg}"))


def state_name(state):
    return STATES.get(state, L(state, state))


# ---------- state merge ----------

def _merge_list(dst, src):
    if src and all(isinstance(i, dict) and "id" in i for i in src) and all(isinstance(i, dict) and "id" in i for i in dst):
        by_id = {str(i["id"]): i for i in dst}
        out = []
        for item in src:
            old = by_id.get(str(item["id"]))
            # A bare {"id": n} means the slot was emptied, so it must not inherit old fields.
            if old is not None and len(item) > 1:
                merge(old, item)
                out.append(old)
            else:
                out.append(item)
        return out
    return src


def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            dst[k] = _merge_list(dst[k], v)
        else:
            dst[k] = v


# ---------- normalization helpers ----------

def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _packed(v):
    """H2 series packs temperature as (target << 16) | current."""
    v = _num(v)
    if v is None:
        return None, None
    v = int(v)
    return float(v & 0xFFFF), float(v >> 16)


def _fan(v):
    v = _num(v)
    return None if v is None else round(v / 15 * 100)


def _real_uuid(u):
    return bool(u) and set(str(u)) != {"0"}


def _hms_code(h):
    a, c = int(h.get("attr", 0)), int(h.get("code", 0))
    return f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"


# ---------- camera plumbing (per printer) ----------

class Relay:
    """Loopback TCP relay to one printer's RTSPS port.

    macOS blocks LAN access for binaries started by launchd until the user approves them, and a
    command-line ffmpeg never shows up in Privacy & Security > Local Network to be approved. So
    ffmpeg talks to 127.0.0.1 and this process (which does have LAN access) forwards the bytes.
    TLS stays end-to-end between ffmpeg and the printer; the relay never decrypts anything.
    """

    def __init__(self, pr):
        self.pr = pr
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return  # closed: printer removed
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            up = socket.create_connection((self.pr.conf["host"], 322), timeout=10)
        except OSError:
            conn.close()
            return
        up.settimeout(None)
        threading.Thread(target=self._pipe, args=(up, conn), daemon=True).start()
        self._pipe(conn, up)

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            # Closing both ends tears the printer session down as soon as ffmpeg goes away.
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


class Camera:
    """MJPEG re-encode: works in every browser, used as the fallback and for snapshots."""

    out_args = ["-an", "-vf", "scale=1280:-2,fps=15", "-q:v", "6", "-f", "mjpeg", "pipe:1"]

    def __init__(self, pr):
        self.pr = pr
        self.cond = threading.Condition()
        self.frame, self.frame_t = None, 0.0
        self.clients, self.proc, self.idle_since, self.started_at = 0, None, time.time(), 0.0
        threading.Thread(target=self._watch, daemon=True).start()

    def restart(self):
        """Drop the stream so the watcher reopens it with the current settings."""
        self._stop(kill=True)
        self.frame = None

    def _start(self):
        if not self.pr.configured() or self.pr.stopped:
            return
        self.started_at = time.time()
        self.proc = subprocess.Popen([FFMPEG, *FF_IN, "-i", self.pr.rtsp_url(), *self.out_args],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2)
                if s < 0 or e < 0:
                    break
                with self.cond:
                    self.frame, self.frame_t = buf[s:e + 2], time.time()
                    self.cond.notify_all()
                buf = buf[e + 2:]
            if len(buf) > 4_000_000:
                buf = b""

    def _watch(self):
        while not self.pr.stopped:
            time.sleep(2)
            alive = self.proc is not None and self.proc.poll() is None
            if self.clients > 0 and not alive:
                self._start()
            elif self.clients > 0 and time.time() - max(self.frame_t, self.started_at) > 20:
                # Backstop for -timeout: a stalled ffmpeg can ignore SIGTERM, so kill it outright.
                self._stop(kill=True)
            elif self.clients == 0 and alive and time.time() - self.idle_since > 20:
                self._stop()
        self._stop(kill=True)

    def _stop(self, kill=False):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.kill() if kill else proc.terminate()

    def acquire(self):
        with self.cond:
            self.clients += 1
        if self.proc is None or self.proc.poll() is not None:
            self._start()

    def release(self):
        with self.cond:
            self.clients -= 1
            if self.clients == 0:
                self.idle_since = time.time()

    def snapshot(self):
        if self.frame and time.time() - self.frame_t < 3:
            return self.frame
        if not self.pr.configured():
            return None
        r = subprocess.run([FFMPEG, *FF_IN, "-i", self.pr.rtsp_url(), "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
                           capture_output=True, timeout=25)
        return r.stdout or None


def _read_exact(f, n):
    out = b""
    while len(out) < n:
        chunk = f.read(n - len(out))
        if not chunk:
            return None
        out += chunk
    return out


class H264Camera(Camera):
    """The printer's own H.264 (1680x1080, 30 fps, ~260 KB/s) remuxed to fragmented MP4, no re-encode.

    The camera sends a keyframe every second and frag_keyframe cuts one fragment per keyframe,
    so every fragment is a valid starting point: a new viewer gets the init segment plus the
    latest fragment and sees video within about a second.
    """

    out_args = ["-an", "-c:v", "copy", "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]

    def __init__(self, pr):
        self.init, self.codec, self.seq = None, None, 0
        super().__init__(pr)

    def restart(self):
        super().restart()
        self.init = None

    def _read(self, proc):
        init, moof = b"", None
        while True:
            hdr = _read_exact(proc.stdout, 8)
            if hdr is None:
                break
            size, kind = struct.unpack(">I4s", hdr)
            if size == 1:
                ext = _read_exact(proc.stdout, 8)
                if ext is None:
                    break
                hdr += ext
                size = struct.unpack(">Q", ext)[0]
            body = _read_exact(proc.stdout, size - len(hdr)) if size > len(hdr) else b""
            if body is None:
                break
            box = hdr + body
            if kind in (b"ftyp", b"moov"):
                init += box
                if kind == b"moov":
                    with self.cond:
                        self.init, self.codec = init, _avc_codec(box)
            elif kind == b"moof":
                moof = box
            elif kind == b"mdat" and moof is not None:
                with self.cond:
                    self.frame, self.frame_t, self.seq = moof + box, time.time(), self.seq + 1
                    self.cond.notify_all()
                moof = None

    def snapshot(self):
        raise NotImplementedError


def _avc_codec(moov):
    """RFC 6381 codec string (e.g. avc1.640029) from the avcC box, needed by Media Source Extensions."""
    i = moov.find(b"avcC")
    if i < 0 or len(moov) < i + 8:
        return "avc1.640029"
    return "avc1." + moov[i + 5:i + 8].hex()


# ---------- FTPS: plate thumbnail + slice info ----------

class ImplicitFTPS(ftplib.FTP_TLS):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, v):
        if v is not None and not isinstance(v, ssl.SSLSocket):
            v = self.context.wrap_socket(v)
        self._sock = v

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


# ---------- one printer ----------

class Printer:
    """Everything that belongs to one printer: MQTT link, state, cameras, timeline, current job."""

    def __init__(self, conf):
        self.conf = conf  # the dict inside cfg["printers"], edited in place by the settings page
        self.lock = threading.Lock()
        self.raw = {}
        self.mqtt_ok, self.last_msg, self.stopped = False, 0.0, False
        self.dirty = threading.Event()
        self.cfg_changed = threading.Event()
        self.events = collections.deque(maxlen=60)
        self.hist = collections.deque(maxlen=360)  # one sample per 5 s -> 30 min
        self.job = {"key": None, "started_at": None, "thumb": None, "meta": None, "fetched_for": None, "fetch_err_at": 0}
        self.prev = {"state": None, "stage": None, "err": None, "hms": ()}
        self.sse_clients = []
        self.seq = 0
        self.sent = collections.deque(maxlen=64)  # sequence ids of our own commands
        self.relay = Relay(self)
        self.camera = Camera(self)
        self.h264 = H264Camera(self)
        self.streamer = Streamer(self)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mission-{os.getpid()}-{conf['id']}")
        for target in (self.mqtt_loop, self.sampler, self.broadcaster):
            threading.Thread(target=target, daemon=True).start()

    @property
    def id(self):
        return self.conf["id"]

    def configured(self):
        c = self.conf
        return bool(c.get("host") and c.get("serial") and c.get("access_code"))

    def rtsp_url(self):
        return f"rtsps://bblp:{self.conf['access_code']}@127.0.0.1:{self.relay.port}/streaming/live/1"

    def log(self, kind, text):
        now = time.time()
        self.events.appendleft({"t": now, "kind": kind, "text": text})
        jid = self.job.get("db_id")
        if jid and kind in ("state", "stage", "error", "cmd", "ack", "job"):
            hist_db.add_event(jid, now, kind, text)
        self.dirty.set()

    def stop(self):
        """Printer removed in settings: drop the link, the cameras and the threads."""
        self.stopped = True
        self.cfg_changed.set()
        self.camera._stop(kill=True)
        self.h264._stop(kill=True)
        self.streamer.stop()
        self.relay.close()
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001 - never connected is fine
            pass

    # ----- view -----

    def summary(self):
        """Tiny status for the printer switcher; read without the lock (plain dict reads)."""
        p = self.raw
        online = self.mqtt_ok and time.time() - self.last_msg < 90
        state = p.get("gcode_state") or "IDLE"
        if not self.configured():
            state = "UNCONFIGURED"
        elif not online:
            state = "OFFLINE"
        return {"id": self.id, "name": self.conf["name"], "state": state, "pct": int(_num(p.get("mc_percent"), 0) or 0)}

    def build_view(self):
        p = self.raw
        dev = p.get("device") or {}
        online = self.mqtt_ok and time.time() - self.last_msg < 90
        state = p.get("gcode_state") or "IDLE"
        if not self.configured():
            state = "UNCONFIGURED"
        elif not online:
            state = "OFFLINE"
        stg = p.get("stg_cur")
        stg = int(stg) if isinstance(stg, (int, float)) or (isinstance(stg, str) and stg.lstrip("-").isdigit()) else -1
        if state == "RUNNING" and stg in (0, -1, 255):
            stage = STAGES[0]
        elif state in ACTIVE:
            stage = stage_name(stg)
        else:
            stage = state_name(state)

        noz_c, noz_t = _num(p.get("nozzle_temper")), _num(p.get("nozzle_target_temper"))
        ext = ((dev.get("extruder") or {}).get("info") or [{}])[0]
        if ext.get("temp") is not None:
            noz_c, noz_t = _packed(ext["temp"])
        bed_c, bed_t = _num(p.get("bed_temper")), _num(p.get("bed_target_temper"))
        if (dev.get("bed") or {}).get("info", {}).get("temp") is not None:
            bed_c, bed_t = _packed(dev["bed"]["info"]["temp"])
        ch = (dev.get("ctc") or {}).get("info", {}).get("temp")
        if ch is None:
            ch = p.get("chamber_temper")
        ch_c, ch_t = (_packed(ch) if _num(ch) is not None and _num(ch) > 0xFFFF else (_num(ch), None))

        ams_raw = p.get("ams") or {}
        # H2 series: extruder "snow" = (ams_id << 8) | slot, 0xFFFF = nothing loaded.
        # Older models: ams.tray_now = ams_id * 4 + slot (or 254/255 for the external spool / none).
        snow = ext.get("snow")
        if isinstance(snow, int):
            loaded = None if snow == 0xFFFF else (snow >> 8, snow & 0xFF)
        else:
            tn = int(_num(ams_raw.get("tray_now"), 255))
            loaded = (tn, 0) if tn >= 128 else (tn // 4, tn % 4)
        units = []
        for u in ams_raw.get("ams") or []:
            uid = int(u.get("id", 0))
            trays = []
            for t in u.get("tray") or []:
                tid = int(t.get("id", 0))
                empty = not t.get("tray_type")
                col = (t.get("tray_color") or "00000000")[:6]
                left = self.spool_left(t)[0] if not empty and _real_uuid(t.get("tray_uuid")) else None
                trays.append({
                    "left_g": left, "slot": tid + 1, "empty": empty, "type": t.get("tray_type") or "",
                    "brand": t.get("tray_sub_brands") or "", "color": "#" + col,
                    "remain": t.get("remain") if isinstance(t.get("remain"), int) else -1,
                    "active": loaded == (uid, tid) and not empty,
                    "tmin": t.get("nozzle_temp_min"), "tmax": t.get("nozzle_temp_max"),
                })
            units.append({
                "id": uid, "name": "AMS HT" if uid >= 128 else f"AMS {uid + 1}",
                "humidity": _num(u.get("humidity_raw")), "humidity_idx": _num(u.get("humidity")),
                "temp": _num(u.get("temp")), "dry_min": int(_num(u.get("dry_time"), 0) or 0), "trays": trays,
            })
        ext_spool = None
        for v in p.get("vir_slot") or []:
            if v.get("tray_type"):
                ext_spool = {"type": v["tray_type"], "brand": v.get("tray_sub_brands") or "",
                             "color": "#" + (v.get("tray_color") or "00000000")[:6],
                             "active": loaded is not None and loaded[0] >= 254}

        lights = {l.get("node"): l.get("mode") for l in p.get("lights_report") or []}
        xc = p.get("xcam") or {}
        rem = _num(p.get("mc_remaining_time"))
        spd = int(_num(p.get("spd_lvl"), 2) or 2)
        hms = [_hms_code(h) for h in p.get("hms") or []]
        perr = int(_num(p.get("print_error"), 0) or 0)
        wifi = p.get("wifi_signal") or ""
        job = self.job
        meta = job.get("meta") or {}

        return {
            "id": self.id, "name": self.conf["name"], "online": online, "configured": self.configured(), "ts": time.time(),
            "printers": [pr.summary() for pr in list(printers.values())],
            "state": state, "state_txt": state_name(state), "stage": stage, "stage_id": stg,
            "pct": int(_num(p.get("mc_percent"), 0) or 0), "remaining_min": rem,
            "eta": (time.time() + rem * 60) if rem and state in ACTIVE else None,
            "layer": int(_num(p.get("layer_num"), 0) or 0), "layers": int(_num(p.get("total_layer_num"), 0) or 0),
            "task": p.get("subtask_name") or "", "plate": p.get("plate_idx"),
            "print_type": p.get("print_type"),
            "temps": {"nozzle": {"cur": noz_c, "tgt": noz_t}, "bed": {"cur": bed_c, "tgt": bed_t},
                      "chamber": {"cur": ch_c, "tgt": ch_t}},
            "fans": {"part": _fan(p.get("cooling_fan_speed")), "aux": _fan(p.get("big_fan1_speed")),
                     "chamber": _fan(p.get("big_fan2_speed")), "heatbreak": _fan(p.get("heatbreak_fan_speed"))},
            "speed": {"lvl": spd, "name": SPEEDS.get(spd, L(str(spd), str(spd))), "mag": _num(p.get("spd_mag"))},
            "wifi": _num(wifi.replace("dBm", "")) if wifi else None,
            "light": lights.get("chamber_light") == "on",
            "ams": units, "ext": ext_spool,
            "nozzle": {"type": p.get("nozzle_type"), "diameter": p.get("nozzle_diameter")},
            "ai": {"spaghetti": xc.get("spaghetti_detector"), "first_layer": xc.get("first_layer_inspector"),
                   "halt": xc.get("print_halt"), "sensitivity": xc.get("halt_print_sensitivity")},
            "locked": self.conf["locked_cmds"], "dev_mode": bool(self.conf.get("dev_mode")),
            "stream": self.streamer.status() if hasattr(self, "streamer") else None,
            "errors": {"print": f"{perr:08X}" if perr else None, "print_text": error_text(f"{perr:08X}") if perr else None,
                       "hms": [{"code": c, "text": hms_text(c)} for c in hms]},
            "job": {"started_at": job.get("started_at"), "thumb": bool(job.get("thumb")),
                    "thumb_v": job.get("fetched_for") or "", "weight_g": meta.get("weight"),
                    "prediction_s": meta.get("prediction"), "filaments": meta.get("filaments") or [],
                    "cost": self.live_costs(meta) if state in ACTIVE or state == "FINISH" else None,
                    "forecast": self.spool_forecast(meta) if state in ACTIVE else []},
            "hist": list(self.hist)[-240:],
            "events": list(self.events)[:40],
        }

    # ----- job history -----

    def _job_key(self):
        return f"{self.raw.get('subtask_id') or self.raw.get('subtask_name')}#{self.raw.get('plate_idx')}"

    def _elapsed_guess(self):
        """Seconds since the print started, when we did not see it start (server launched mid-print)."""
        meta, rem = self.job.get("meta") or {}, _num(self.raw.get("mc_remaining_time"))
        if meta.get("prediction") and rem is not None:
            return max(0.0, meta["prediction"] - rem * 60)
        return 0.0

    def track_job(self):
        """Opens/closes the history record for the current print. Called under the printer lock."""
        p, state, job, serial = self.raw, self.raw.get("gcode_state"), self.job, self.conf.get("serial")
        if not p.get("subtask_name") or not serial:
            return
        key = self._job_key()
        if state in ACTIVE and job.get("db_key") != key:
            jid = hist_db.open_job(serial, key)
            if jid:  # server restarted during this print: continue the same record
                job["started_at"] = hist_db.get(jid)["started"]
            else:
                seen_start = self.prev.get("seen_start") == key
                started = job.get("started_at") if seen_start and job.get("started_at") else time.time() - self._elapsed_guess()
                jid = hist_db.start(serial, key, p.get("subtask_name"), p.get("plate_idx"), started, not seen_start)
                job["started_at"] = started
            job.update(db_id=jid, db_key=key)
            self.remember_meta()
        elif state in ("FINISH", "FAILED", "IDLE") and job.get("db_id"):
            self.finish_job(job["db_id"], {"FINISH": "FINISH", "FAILED": "FAILED"}.get(state, "CANCELLED"))
            job.update(db_id=None, db_key=None)
        elif state in ("FINISH", "FAILED") and not job.get("db_id"):
            jid = hist_db.open_job(serial, key)  # finished while the server was down
            if jid:
                self.finish_job(jid, state)

    def remember_meta(self):
        """Copy slicer info and the plate render into the open history record once both are known."""
        job = self.job
        jid, meta = job.get("db_id"), job.get("meta")
        if not jid or not meta or job.get("meta_saved") == jid:
            return
        filaments = [dict(f) for f in meta.get("filaments") or []]
        for f in filaments:  # which physical spool (RFID uuid) feeds this filament, for the spool ledger
            t = self._tray_for(f)
            if t and _real_uuid(t.get("tray_uuid")):
                f["tray_uuid"] = t["tray_uuid"]
        fields = {"prediction_s": meta.get("prediction"), "weight_g": meta.get("weight"),
                  "filaments": filaments, "layers": int(_num(self.raw.get("total_layer_num"), 0) or 0)}
        if job.get("thumb"):
            fields["thumb"] = job["thumb"]
        if job.get("toolpath"):
            hist_db.set_toolpath(jid, job["toolpath"])
        rec = hist_db.get(jid)
        if rec and rec["start_estimated"]:
            fields["started"] = time.time() - self._elapsed_guess()
            job["started_at"] = fields["started"]
        # Costs are known up front (slicer grams, predicted time, fixed tariffs): store the estimate now,
        # finish_job() replaces it with the actual figures.
        started = fields.get("started") or (rec["started"] if rec else time.time())
        fil, kwh, cost_e = job_costs(cfg["costs"], fields["filaments"], 1.0, started, started + (meta.get("prediction") or 0))
        fields.update(cost_filament=fil, cost_energy=cost_e, energy_kwh=kwh, currency=cfg["costs"].get("currency"))
        hist_db.update(jid, **fields)
        job["meta_saved"] = jid

    def finish_job(self, jid, result):
        rec = hist_db.get(jid)
        if not rec:
            return
        ended = time.time()
        pct = 100 if result == "FINISH" else int(_num(self.raw.get("mc_percent"), 0) or 0)
        filaments = json.loads(rec["filaments"] or "[]")
        fil, kwh, cost_e = job_costs(cfg["costs"], filaments, pct / 100, rec["started"], ended)
        hist_db.update(jid, ended=ended, result=result, pct=pct, energy_kwh=kwh, cost_filament=fil,
                       cost_energy=cost_e, currency=cfg["costs"].get("currency"))
        self.log("job", L(f"Записано в историю: {rec['task']}", f"Saved to history: {rec['task']}"))

        def grab():  # the final photo; ffmpeg can take a few seconds, so off the MQTT thread
            try:
                img = self.camera.snapshot()
                if img:
                    hist_db.update(jid, snapshot=img)
            except Exception:  # noqa: BLE001 - a missing photo must not break anything
                pass
        threading.Thread(target=grab, daemon=True).start()

    def live_costs(self, meta):
        """Estimated cost of the whole current print, for the payload card."""
        started = self.job.get("started_at")
        if not meta or not started:
            return None
        fil, kwh, cost_e = job_costs(cfg["costs"], meta.get("filaments") or [], 1.0,
                                     started, started + (meta.get("prediction") or 0))
        if fil is None and cost_e is None:
            return {"known": False, "currency": cfg["costs"].get("currency")}
        return {"known": True, "filament": fil, "energy": cost_e, "kwh": kwh,
                "total": round((fil or 0) + (cost_e or 0), 2), "currency": cfg["costs"].get("currency")}

    def spool_forecast(self, meta):
        """Will the loaded spools last? Per filament of the current print: grams left vs grams still needed."""
        if not meta or not meta.get("filaments"):
            return []
        done = (_num(self.raw.get("mc_percent"), 0) or 0) / 100
        out = []
        for f in meta["filaments"]:
            slot = self._slot_for(f)
            t = self._tray(slot) if slot else None
            if not t:
                continue
            need = round((f.get("g") or 0) * (1 - done), 1)
            left, printer_left = self.spool_left(t)
            status = "unknown" if left is None else "ok" if left >= need * 1.1 else "short"
            out.append({"type": f.get("type"), "color": f.get("color"), "need_g": need, "left_g": left,
                        "printer_g": printer_left, "status": status,
                        "ams": "AMS HT" if slot[0] >= 128 else f"AMS {slot[0] + 1}", "slot": slot[1] + 1})
        return out

    # ----- spools: the AMS estimate drifts (it resets on re-insertion, drying...), so keep our own ledger -----

    def _tray(self, slot):
        for u in self.raw.get("ams", {}).get("ams") or []:
            if int(u.get("id", 0)) == slot[0]:
                for t in u.get("tray") or []:
                    if int(t.get("id", 0)) == slot[1]:
                        return t
        return None

    def _slot_for(self, f):
        mapping = self.raw.get("mapping") or []
        idx = (f.get("id") or 0) - 1
        v = mapping[idx] if 0 <= idx < len(mapping) else None
        if v is None or v == 0xFFFF:
            return None
        return (v >> 8, v & 0xFF) if v >= 256 else (v // 4, v % 4)  # H2: (ams<<8)|slot; older: ams*4+slot

    def _tray_for(self, f):
        slot = self._slot_for(f)
        return self._tray(slot) if slot else None

    def current_use(self, uuid):
        """Grams the print in progress has already taken from this spool."""
        if self.raw.get("gcode_state") not in ACTIVE:
            return 0.0
        meta, done = self.job.get("meta") or {}, (_num(self.raw.get("mc_percent"), 0) or 0) / 100
        return sum((f.get("g") or 0) * done for f in meta.get("filaments") or []
                   if (self._tray_for(f) or {}).get("tray_uuid") == uuid)

    def loaded_spools(self):
        """RFID spools currently in this printer's AMS units: uuid -> (tray, unit id, slot)."""
        out = {}
        for u in self.raw.get("ams", {}).get("ams") or []:
            for t in u.get("tray") or []:
                if _real_uuid(t.get("tray_uuid")) and t.get("tray_type"):
                    out[t["tray_uuid"]] = (t, int(u.get("id", 0)), int(t.get("id", 0)))
        return out

    def note_spools(self):
        """Inventory: every original (RFID) spool that shows up in an AMS, with where and when it was seen."""
        now = time.time()
        seen = self.__dict__.setdefault("_spool_seen", {})
        for uuid, (t, uid, tid) in self.loaded_spools().items():
            remain = t.get("remain")
            if not hist_db.spool(uuid):
                # Start from the AMS estimate; with "Update remaining filament" off (remain = -1) assume a full
                # spool, the user can correct it by weighing.
                full = _num(t.get("tray_weight"), 0) or 1000
                grams = full * remain / 100 if isinstance(remain, int) and remain >= 0 else full
                hist_db.add_spool(uuid, t.get("tray_type"), (t.get("tray_color") or "")[:6], now, grams)
            loc = f"{'AMS HT' if uid >= 128 else f'AMS {uid + 1}'} · {tid + 1}"
            last = seen.get(uuid)
            if last and last[0] == loc and now - last[1] < 300:
                continue  # unchanged: refresh "last seen" every 5 minutes, not every tick
            seen[uuid] = (loc, now)
            hist_db.seen_spool(uuid, brand=t.get("tray_sub_brands") or t.get("tray_type"), code=t.get("tray_info_idx"),
                               tag_uid=t.get("tag_uid"), spool_g=_num(t.get("tray_weight"), 0) or 1000,
                               serial=self.conf.get("serial"), location=loc, last_seen=now)

    def spool_left(self, t):
        """(grams left, the printer's own estimate): the lower of our ledger and the AMS estimate."""
        remain = t.get("remain") if isinstance(t.get("remain"), int) else -1
        printer_left = round((_num(t.get("tray_weight"), 0) or 1000) * remain / 100) if remain >= 0 else None
        uuid = t.get("tray_uuid")
        sp = hist_db.spool(uuid) if _real_uuid(uuid) else None
        if not sp:
            return printer_left, printer_left
        ledger = round(hist_db.ledger_left(sp, self.current_use(uuid), exclude_job=self.job.get("db_id")))
        return (min(ledger, printer_left) if printer_left is not None else ledger), printer_left

    def track_transitions(self):
        """Called under the printer lock after each MQTT message: emits timeline events."""
        p, prev, job = self.raw, self.prev, self.job
        state = p.get("gcode_state")
        key = (p.get("subtask_id") or p.get("subtask_name"), p.get("plate_idx"))
        if state and state != prev["state"]:
            if state in ("PREPARE", "RUNNING") and prev["state"] is not None and prev["state"] not in ACTIVE:
                prev["seen_start"] = self._job_key()
            if prev["state"] is not None:
                a, b = state_name(prev["state"]), state_name(state)
                self.log("state", L(f"{a['ru']} → {b['ru']}", f"{a['en']} → {b['en']}"))
            if state in ("PREPARE", "RUNNING") and prev["state"] not in (*ACTIVE, None):
                job["started_at"] = time.time()
            prev["state"] = state
        if key != job["key"]:
            job.update(key=key, thumb=None, meta=None, toolpath=None)
            if p.get("subtask_name"):
                self.log("job", L(f"Задание: {p.get('subtask_name')}", f"Job: {p.get('subtask_name')}"))
        stg = p.get("stg_cur")
        if stg != prev["stage"]:
            if prev["stage"] is not None and isinstance(stg, int) and stg not in (0, 255, -1):
                self.log("stage", stage_name(stg))
            prev["stage"] = stg
        err = p.get("print_error") or 0
        if err != prev["err"]:
            if err:
                code = f"{int(err):08X}"
                txt = error_text(code) or L("", "")
                self.log("error", L(f"Ошибка печати {code}. {txt['ru']}".strip(" ."), f"Print error {code}. {txt['en']}".strip(" .")))
            prev["err"] = err
        hms = tuple(_hms_code(h) for h in p.get("hms") or [])
        if hms != prev["hms"]:
            for code in set(hms) - set(prev["hms"]):
                txt = hms_text(code) or L("", "")
                self.log("error", L(f"HMS {code}. {txt['ru']}".strip(" ."), f"HMS {code}. {txt['en']}".strip(" .")))
            prev["hms"] = hms
        self.track_job()

    # ----- MQTT -----

    def send(self, payload):
        self.seq += 1
        for v in payload.values():
            v["sequence_id"] = str(self.seq)
        if not any(v.get("command") == "pushall" for v in payload.values()):
            self.sent.append(str(self.seq))
        self.client.publish(f"device/{self.conf['serial']}/request", json.dumps(payload))

    def on_connect(self, c, u, flags, rc, props=None):
        self.mqtt_ok = not rc.is_failure
        if self.mqtt_ok:
            c.subscribe(f"device/{self.conf['serial']}/report")
            self.send({"pushing": {"command": "pushall"}})
            self.log("link", L("Связь с принтером установлена", "Connected to printer"))
        else:
            self.log("error", L(f"MQTT отказ: {rc}", f"MQTT refused: {rc}"))

    def on_disconnect(self, c, u, flags, rc, props=None):
        self.mqtt_ok = False
        if not self.stopped:
            self.log("link", L("Связь с принтером потеряна", "Printer connection lost"))

    def on_message(self, c, u, m):
        try:
            d = json.loads(m.payload)
        except ValueError:
            return
        self.last_msg = time.time()
        with self.lock:
            for section in ("print", "system"):
                body = d.get(section)
                if not isinstance(body, dict):
                    continue
                # The printer broadcasts every command reply to all clients (Bambu Studio, Handy...);
                # only replies to our own commands belong in our log and may lock controls.
                if body.get("result") and body.get("command") not in ("push_status", None) \
                        and str(body.get("sequence_id")) in self.sent:
                    ok = str(body.get("result")).lower() == "success"
                    why = body.get("reason") or body.get("err_code") or ""
                    cmd = body["command"]
                    # Firmware with authorization control rejects unsigned gcode/drying commands unless
                    # Developer Mode is on. Remember it so the console stops offering them (each try
                    # also raises an HMS notice on the printer screen).
                    if not ok and "verify" in str(why).lower() and cmd not in self.conf["locked_cmds"]:
                        with cfg_lock:
                            self.conf["locked_cmds"] = [*self.conf["locked_cmds"], cmd]
                            save_cfg(cfg)
                    self.log("ack" if ok else "error",
                             L(f"Команда {cmd}: {'выполнена' if ok else 'отклонена'} {why}".strip(),
                               f"Command {cmd}: {'done' if ok else 'rejected'} {why}".strip()))
                if section == "print":
                    merge(self.raw, body)
            self.track_transitions()
        self.dirty.set()

    def reset_state(self):
        with self.lock:
            self.raw.clear()
            self.hist.clear()
            self.job.update(key=None, started_at=None, thumb=None, meta=None, fetched_for=None, fetch_err_at=0,
                            db_id=None, db_key=None, meta_saved=None)
            self.prev.update(state=None, stage=None, err=None, hms=(), seen_start=None)
            self.last_msg = 0.0
        self.dirty.set()

    def reconnect(self):
        """The connection settings changed: start over with the new printer."""
        self.reset_state()
        self.camera.restart()
        self.h264.restart()
        self.log("link", L(f"Настройки сохранены: {self.conf['name']} ({self.conf['host']})",
                           f"Settings saved: {self.conf['name']} ({self.conf['host']})"))
        self.cfg_changed.set()
        self.client.disconnect()  # makes loop_forever return; mqtt_loop reconnects with the new settings

    def mqtt_loop(self):
        c = self.client
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)
        c.on_connect, c.on_disconnect, c.on_message = self.on_connect, self.on_disconnect, self.on_message
        c.reconnect_delay_set(2, 30)
        while not self.stopped:
            if not self.configured():
                self.cfg_changed.wait()
                continue
            self.cfg_changed.clear()
            host = self.conf["host"]
            c.username_pw_set("bblp", self.conf["access_code"])
            try:
                c.connect(host, 8883, 30)
                c.loop_forever(retry_first_connection=True)
            except (OSError, ssl.SSLError) as e:
                self.log("error", L(f"Нет связи с {host}: {e}", f"Cannot reach {host}: {e}"))
                self.cfg_changed.wait(10)

    def sampler(self):
        """History samples every 5 s, a full refresh every 10 min, and the job fetcher trigger."""
        n = 0
        while not self.stopped:
            time.sleep(5)
            n += 1
            with self.lock:
                try:
                    self.note_spools()
                except Exception as e:  # inventory trouble must never stop history sampling
                    print("spool tracking failed:", e, flush=True)
                v = self.build_view()
            t = v["temps"]
            self.hist.append([round(time.time()), t["nozzle"]["cur"], t["bed"]["cur"], t["chamber"]["cur"], v["pct"]])
            if n % 6 == 0 and self.job.get("db_id") and v["state"] in ACTIVE:  # every 30 s into the print report
                hist_db.add_sample(self.job["db_id"], time.time(), t["nozzle"]["cur"], t["bed"]["cur"],
                                   t["chamber"]["cur"], v["pct"], v["layer"])
            self.dirty.set()
            if n % 120 == 0 and self.mqtt_ok:
                self.send({"pushing": {"command": "pushall"}})
            if v["task"] and self.job["fetched_for"] != self._fetch_key() and time.time() - self.job["fetch_err_at"] > 120:
                threading.Thread(target=self.fetch_job, daemon=True).start()

    def broadcaster(self):
        last_push = 0.0
        while not self.stopped:
            self.dirty.wait(timeout=10)
            self.dirty.clear()
            time.sleep(max(0, 0.5 - (time.time() - last_push)))  # at most 2 pushes/s
            with self.lock:
                data = json.dumps(self.build_view(), ensure_ascii=False)
            last_push = time.time()
            for q in list(self.sse_clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # ----- FTPS -----

    def _fetch_key(self):
        return f"{self.raw.get('subtask_name')}#{self.raw.get('plate_idx')}"

    def download_3mf(self, name):
        """The job's .gcode.3mf from the printer's storage (FTPS)."""
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        f = ImplicitFTPS(context=ctx)
        f.connect(self.conf["host"], 990, timeout=20)
        f.login("bblp", self.conf["access_code"])
        f.prot_p()
        try:
            files = []
            for d in ("/cache", "/"):
                try:
                    files += [x for x in f.nlst(d) if x.endswith(".3mf")]
                except ftplib.error_perm:
                    pass
            stem = name[:-4] if name.endswith(".3mf") else name
            match = [x for x in files if os.path.basename(x).startswith(stem)] if stem else []
            if not match:
                raise FileNotFoundError(f"no .3mf for {name}")
            buf = io.BytesIO()
            f.retrbinary(f"RETR {match[0]}", buf.write)
            return buf.getvalue()
        finally:
            try:
                f.quit()
            except Exception:  # noqa: BLE001
                pass

    def backfill_toolpath(self, rec):
        """Older prints (or ones started before this feature): rebuild the 3D preview if the file is still on the printer."""
        data = self.download_3mf(rec["task"] or "")
        tp = toolpath.build(toolpath.gcode_from_3mf(data, rec["plate"] or 1) or "")
        if not tp:
            return None
        gz = gzip.compress(tp, 6)
        hist_db.set_toolpath(rec["id"], gz)
        return gz

    def fetch_job(self):
        job = self.job
        with self.lock:
            name, plate, key = self.raw.get("subtask_name") or "", int(_num(self.raw.get("plate_idx"), 1) or 1), self._fetch_key()
        if job.get("_busy"):
            return
        job["_busy"] = True
        try:
            buf = io.BytesIO(self.download_3mf(name))
            z = zipfile.ZipFile(buf)
            names = set(z.namelist())
            thumb = None
            for cand in (f"Metadata/plate_{plate}.png", "Metadata/plate_1.png"):
                if cand in names:
                    thumb = z.read(cand)
                    break
            meta = {}
            if "Metadata/slice_info.config" in names:
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                for pl in root.iter("plate"):
                    md = {m.get("key"): m.get("value") for m in pl.findall("metadata")}
                    if str(md.get("index", "1")) != str(plate):
                        continue
                    meta = {"prediction": _num(md.get("prediction")), "weight": _num(md.get("weight")),
                            "filaments": [{"id": int(_num(fl.get("id"), 0) or 0), "type": fl.get("type"),
                                           "color": fl.get("color"), "g": _num(fl.get("used_g")), "m": _num(fl.get("used_m"))}
                                          for fl in pl.findall("filament")]}
            tp = toolpath.build(toolpath.gcode_from_3mf(buf.getvalue(), plate) or "")
            with self.lock:
                job.update(thumb=thumb, meta=meta, fetched_for=key, toolpath=gzip.compress(tp, 6) if tp else None)
                self.remember_meta()
            kb = len(buf.getvalue()) // 1024
            self.log("job", L(f"Загружено превью модели ({kb} КБ .3mf)", f"Model preview loaded ({kb} KB .3mf)"))
        except Exception as e:  # noqa: BLE001 - printer FTP is flaky, never let it kill the thread
            job["fetch_err_at"] = time.time()
            self.log("error", L(f"Превью не загружено: {e}", f"Preview not loaded: {e}"))
        finally:
            job["_busy"] = False
            self.dirty.set()


class Streamer:
    """Live broadcast of one printer's camera to RTMP services (YouTube, Twitch...), optionally with a HUD.

    One ffmpeg reads the camera through the relay and pushes to every selected service at once (tee
    muxer, a failing service doesn't stop the others). It restarts itself while the broadcast is wanted.
    """

    def __init__(self, pr):
        self.pr = pr
        self.proc, self.want, self.targets, self.overlay = None, False, [], False
        self.since, self.error, self.fps, self.kbps = None, None, None, None
        self.hud_dir = os.path.join(BASE, "state", f"hud-{pr.id}")
        threading.Thread(target=self._watch, daemon=True).start()

    def status(self):
        return {"live": bool(self.want and self.proc and self.proc.poll() is None and self.fps),
                "starting": bool(self.want and not self.fps), "targets": self.targets, "since": self.since,
                "fps": self.fps, "kbps": self.kbps, "error": self.error, "overlay": self.overlay}

    def start(self, names, overlay):
        targets = cfg["stream"]["targets"]
        urls = []
        for n in names:
            t = targets.get(n) or {}
            if not t.get("server") and not t.get("key"):
                continue
            urls.append(t["server"] if n == "custom" and not t.get("key") else f"{t['server'].rstrip('/')}/{t['key']}")
        if not urls:
            return "no_targets"
        self.stop()
        self.targets, self.urls = list(names), urls
        self.overlay = bool(overlay and HAS_DRAWTEXT and HUD_FONT)
        self.want, self.error, self.since = True, None, time.time()
        self._spawn()
        self.pr.log("cmd", L(f"Трансляция запущена: {', '.join(names)}", f"Broadcast started: {', '.join(names)}"))
        return None

    def stop(self):
        was = self.want
        self.want = False
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.fps = self.kbps = None
        if was:
            self.pr.log("cmd", L("Трансляция остановлена", "Broadcast stopped"))

    def command(self, out):
        """ffmpeg arguments; `out` is the tee target list (also used by tests with a local file)."""
        args = [FFMPEG, *FF_IN, "-i", self.pr.rtsp_url(),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]  # YouTube wants an audio track
        if self.overlay:
            d, font = self.hud_dir, HUD_FONT.replace(":", "\\\\:")
            def txt(name, x, y, size, color, extra=""):
                return (f"drawtext=fontfile='{font}':textfile='{os.path.join(d, name)}':reload=1:x={x}:y={y}:"
                        f"fontsize={size}:fontcolor={color}:shadowcolor=black@0.7:shadowx=2:shadowy=2:expansion=none{extra}")
            chain = ",".join([
                "drawbox=x=0:y=ih-230:w=iw:h=230:color=0x03040c@0.55:t=fill",
                f"drawtext=fontfile='{font}':text='●':x=40:y=34:fontsize=30:fontcolor=0xff4d6a:shadowcolor=0xff4d6a@0.6:shadowx=0:shadowy=0",
                txt("tag.txt", 78, 34, 30, "white"),
                txt("temps.txt", "w-tw-40", 34, 28, "0xc9d0f0"),
                txt("pct.txt", 40, "h-180", 120, "white"),
                txt("task.txt", 330, "h-165", 44, "white"),
                txt("line.txt", 330, "h-105", 32, "0x48e0ff"),
                txt("bar.txt", 330, "h-62", 30, "0x9d7bff"),
                "pad=1920:1080:(ow-iw)/2:0:color=0x04050d",
            ])
            enc = (["-c:v", "h264_videotoolbox", "-b:v", "5000k", "-maxrate", "6000k", "-bufsize", "10000k"]
                   if sys.platform == "darwin" else ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "4500k", "-maxrate", "5000k", "-bufsize", "9000k"])
            args += ["-filter_complex", f"[0:v]{chain}[v]", "-map", "[v]", "-map", "1:a", *enc,
                     "-r", "30", "-g", "60", "-pix_fmt", "yuv420p"]
        else:
            args += ["-map", "0:v", "-map", "1:a", "-c:v", "copy"]  # the camera already sends a keyframe every second
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-progress", "pipe:2", "-nostats",
                 "-f", "tee", "|".join(f"[f=flv:onfail=ignore]{u}" for u in out)]
        return args

    def write_hud(self):
        """Text files the drawtext filters re-read every frame; replaced atomically."""
        os.makedirs(self.hud_dir, exist_ok=True)
        with self.pr.lock:
            v = self.pr.build_view()
        stage = v["stage"]["en"].upper()
        layer = f" · LAYER {v['layer']}/{v['layers']}" if v["layers"] else ""
        rem = v["remaining_min"]
        eta = f" · REMAINING {int(rem // 60)}H {int(rem % 60):02d}M" if rem and v["state"] in ACTIVE else ""
        n = 40
        filled = round(n * v["pct"] / 100)
        tt = v["temps"]
        f = lambda x: "—" if x is None else f"{x:.0f}°"
        files = {
            "tag.txt": f"LIVE · {v['name']} · MISSION CONTROL",
            "temps.txt": f"NOZZLE {f(tt['nozzle']['cur'])}   BED {f(tt['bed']['cur'])}   CHAMBER {f(tt['chamber']['cur'])}",
            "pct.txt": f"{v['pct']}%",
            "task.txt": v["task"] or v["name"],
            "line.txt": f"{stage}{layer}{eta}",
            "bar.txt": "━" * filled + "─" * (n - filled),
        }
        for name, text in files.items():
            tmp = os.path.join(self.hud_dir, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, os.path.join(self.hud_dir, name))

    def _spawn(self):
        if self.overlay:
            self.write_hud()
        self.fps = self.kbps = None
        self.proc = subprocess.Popen(self.command(self.urls), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        buf, size = b"", 0
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                line = line.strip()
                # -progress writes key=value lines; anything else on stderr is an ffmpeg error message
                if m := re.match(rb"^fps=([\d.]+)$", line):
                    self.fps = float(m.group(1)) or self.fps
                elif m := re.match(rb"^bitrate=\s*([\d.]+)kbits", line):
                    self.kbps = float(m.group(1))
                elif m := re.match(rb"^total_size=(\d+)$", line):
                    size = int(m.group(1))  # the tee muxer reports no bitrate; derive it from bytes sent
                elif (m := re.match(rb"^out_time_us=(\d+)$", line)) and int(m.group(1)) > 0 and size:
                    self.kbps = round(size * 8 / (int(m.group(1)) / 1e6) / 1000)
                elif line and b"=" not in line[:20] and self.want:
                    self.error = line.decode("utf-8", "replace")[-200:]
            self.pr.dirty.set()

    def _watch(self):
        while not self.pr.stopped:
            time.sleep(2)
            if not self.want:
                continue
            if self.overlay:
                try:
                    self.write_hud()
                except Exception:  # noqa: BLE001 - a missed HUD update is harmless
                    pass
            if self.proc is None or self.proc.poll() is not None:
                time.sleep(3)  # back off, then reconnect (network blip, service hiccup)
                if self.want:
                    self._spawn()
        self.stop()


printers = {}  # id -> Printer, in settings order


def sync_printers():
    """Start a Printer for every configured entry, stop the removed ones."""
    wanted = {p["id"]: p for p in cfg["printers"]}
    for pid in list(printers):
        if pid not in wanted:
            printers.pop(pid).stop()
    for pid, conf in wanted.items():
        if pid not in printers:
            printers[pid] = Printer(conf)
    # keep the settings order for the switcher
    order = [p["id"] for p in cfg["printers"]]
    for pid in order:
        printers[pid] = printers.pop(pid)


def inventory():
    """Every original spool ever seen, with our ledger, live location and lifetime usage."""
    live = {}
    for pr in printers.values():
        for uuid, (t, uid, tid) in pr.loaded_spools().items():
            live[uuid] = (pr, t, uid, tid)
    out = []
    for sp in hist_db.spools():
        uuid = sp["uuid"]
        here = live.get(uuid)
        cur = here[0].current_use(uuid) if here else 0.0
        left = hist_db.ledger_left(sp, cur, exclude_job=here[0].job.get("db_id") if here else None)
        printer_pct = here[1].get("remain") if here and isinstance(here[1].get("remain"), int) else None
        used, prints = hist_db.spool_usage(uuid)
        out.append({
            "uuid": uuid, "tag_uid": sp.get("tag_uid"), "type": sp.get("type"), "brand": sp.get("brand") or sp.get("type"),
            "code": sp.get("code"), "color": "#" + (sp.get("color") or "888888"), "spool_g": sp.get("spool_g") or 1000,
            "left_g": round(left), "printer_pct": printer_pct, "used_g": round(used + cur), "prints": prints,
            "first_seen": sp.get("first_seen"), "last_seen": time.time() if here else sp.get("last_seen"),
            "status": "empty" if sp.get("empty_at") else "loaded" if here else "shelf",
            "location": (f"{here[0].conf['name']} · {'AMS HT' if here[2] >= 128 else f'AMS {here[2] + 1}'} · {here[3] + 1}"
                         if here else sp.get("location")),
            "weighed_at": sp.get("adjust_at"), "note": sp.get("note") or "",
        })
    return {"spools": out}


# ---------- backup & restore ----------

BACKUP_DIR = os.path.join(BASE, "state", "backups")
DB_PATH = os.path.join(BASE, "state", "history.sqlite")
KEEP = {"auto": 7, "safety": 5}


def make_backup(path, secrets=True):
    """One zip with everything a fresh install needs: manifest, config.json, history.sqlite."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = json.loads(json.dumps(cfg))
    if not secrets:
        for p in c.get("printers", []):
            p["access_code"] = ""
        for t in c.get("stream", {}).get("targets", {}).values():
            t["key"] = ""
    with tempfile.TemporaryDirectory() as d:
        db_copy = os.path.join(d, "history.sqlite")
        hist_db.snapshot(db_copy)
        manifest = {"app": "mission-control", "format": 1, "created": time.time(), "secrets": secrets,
                    "printers": [p.get("name") for p in c.get("printers", [])], "jobs": len(hist_db.recent(100000))}
        tmp = path + ".part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # may contain access codes
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr("config.json", json.dumps(c, ensure_ascii=False, indent=2))
            z.write(db_copy, "history.sqlite")
        os.replace(tmp, path)
    return manifest


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if name.endswith(".zip"):
            st = os.stat(os.path.join(BACKUP_DIR, name))
            out.append({"name": name, "size": st.st_size, "created": st.st_mtime,
                        "kind": name.split("-", 1)[0]})
    return out


def prune_backups():
    for kind, keep in KEEP.items():
        for b in [b for b in list_backups() if b["kind"] == kind][keep:]:
            os.remove(os.path.join(BACKUP_DIR, b["name"]))


def backup_now(kind):
    name = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    make_backup(os.path.join(BACKUP_DIR, name))
    prune_backups()
    return name


def auto_backups():
    """A full backup once a day, the last 7 kept, so a bad change can always be rolled back."""
    while True:
        try:
            autos = [b for b in list_backups() if b["kind"] == "auto"]
            if not autos or time.time() - autos[0]["created"] > 86400:
                backup_now("auto")
        except Exception as e:  # noqa: BLE001 - a failed backup must never take the server down
            print("auto backup failed:", e, flush=True)
        time.sleep(3600)


def check_backup(path):
    """(config, manifest) if `path` is a valid backup, else raises ValueError with a reason code."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not_zip")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if not {"manifest.json", "config.json"} <= names:
            raise ValueError("not_backup")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("app") != "mission-control":
            raise ValueError("not_backup")
        new_cfg = json.loads(z.read("config.json"))
        if not isinstance(new_cfg, dict) or not isinstance(new_cfg.get("printers", []), list):
            raise ValueError("bad_config")
        if "history.sqlite" in names:
            with tempfile.TemporaryDirectory() as d:
                z.extract("history.sqlite", d)
                db = sqlite3.connect(os.path.join(d, "history.sqlite"))
                try:
                    ok = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
                finally:
                    db.close()
                if not ok or not has_jobs:
                    raise ValueError("bad_database")
    return new_cfg, manifest


def restore_backup(path):
    """Validate, keep a safety copy of the current state, swap config + database, restart."""
    new_cfg, manifest = check_backup(path)
    backup_now("safety")
    # A backup made without secrets must not wipe the codes/keys this machine already knows.
    known = {p.get("serial"): p.get("access_code") for p in cfg.get("printers", [])}
    for p in new_cfg.get("printers", []):
        if not p.get("access_code") and known.get(p.get("serial")):
            p["access_code"] = known[p.get("serial")]
    for k, t in (new_cfg.get("stream") or {}).get("targets", {}).items():
        cur = cfg.get("stream", {}).get("targets", {}).get(k, {})
        if not t.get("key") and cur.get("key"):
            t["key"] = cur["key"]
    new_db = None
    with zipfile.ZipFile(path) as z:
        if "history.sqlite" in z.namelist():
            new_db = DB_PATH + ".restore"
            with z.open("history.sqlite") as src, open(new_db, "wb") as dst:
                dst.write(src.read())
    save_cfg(new_cfg)
    threading.Thread(target=restart_process, args=(new_db,), daemon=True).start()
    return manifest


def restart_process(new_db=None):
    """Re-exec the server in place (same PID, so launchd and run.sh are fine) after the reply went out.
    A restored database is swapped in at the last moment, so nothing touches a closed connection."""
    time.sleep(0.8)
    for pr in list(printers.values()):
        pr.camera._stop(kill=True)
        pr.h264._stop(kill=True)
        if pr.streamer.proc is not None:
            pr.streamer.proc.kill()
    if new_db:
        hist_db.close()
        os.replace(new_db, DB_PATH)
    os.execv(sys.executable, [sys.executable, os.path.join(BASE, "server.py")])


def status_summary(pr):
    """Small status for the iPhone app, widget and Live Activity (no video, no history)."""
    with pr.lock:
        v = pr.build_view()
    return {"id": v["id"], "name": v["name"], "online": v["online"], "state": v["state"], "state_txt": v["state_txt"],
            "stage": v["stage"], "pct": v["pct"], "layer": v["layer"], "layers": v["layers"], "task": v["task"],
            "remaining_min": v["remaining_min"], "eta": v["eta"], "started_at": v["job"]["started_at"],
            "temps": {k: t["cur"] for k, t in v["temps"].items()},
            "error": bool(v["errors"]["print"] or v["errors"]["hms"])}


def any_configured():
    return any(pr.configured() for pr in printers.values())


# ---------- setup: discovery and connection test ----------

SERIAL_RE = re.compile(r"^[0-9A-Z]{8,20}$")


def printer_identity(host, timeout=3):
    """Bambu printers present a cert whose CN is the serial, issued by BBL. Returns serial or None."""
    try:
        with socket.create_connection((host, 8883), timeout=timeout) as raw_sock:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            with ctx.wrap_socket(raw_sock) as s:
                der = s.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    # The stdlib only decodes certificates from a file, so round-trip through a temp PEM.
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(ssl.DER_cert_to_PEM_cert(der))
    try:
        info = ssl._ssl._test_decode_cert(f.name)  # noqa: SLF001
    except ssl.SSLError:
        return None
    finally:
        os.unlink(f.name)
    subject = dict(x[0] for x in info.get("subject", ()))
    issuer = dict(x[0] for x in info.get("issuer", ()))
    cn = subject.get("commonName", "")
    if "BBL" not in issuer.get("organizationName", "") + issuer.get("commonName", ""):
        return None
    return cn if SERIAL_RE.match(cn) else None


def local_subnets():
    """The /24 of the interface that routes to the internet (good enough for a home LAN)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return []
    finally:
        s.close()
    return [ipaddress.ip_network(f"{ip}/24", strict=False)]


def discover():
    def port_open(ip):
        try:
            with socket.create_connection((str(ip), 8883), timeout=0.8):
                return str(ip)
        except OSError:
            return None

    hosts = [ip for net in local_subnets() for ip in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        candidates = [h for h in ex.map(port_open, hosts) if h]
        serials = list(ex.map(printer_identity, candidates))
    known = {p.get("serial"): p["id"] for p in cfg["printers"]}
    return [{"host": h, "serial": s, "added": known.get(s)} for h, s in zip(candidates, serials) if s]


def test_printer(host, code, serial=None):
    """Checks reachability, identity and the access code; returns what it learned."""
    found = printer_identity(host, timeout=5)
    if not found:
        return {"ok": False, "step": "reach"}
    serial = serial or found
    result = {"ok": False, "step": "auth", "serial": found}
    got = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mission-test-{os.getpid()}-{time.time():.0f}")
    c.username_pw_set("bblp", code)
    c.tls_set(cert_reqs=ssl.CERT_NONE)
    c.tls_insecure_set(True)

    def on_conn(cl, u, f, rc, p=None):
        if rc.is_failure:
            got.set()
            return
        result["step"] = "status"
        cl.subscribe(f"device/{serial}/report")
        cl.publish(f"device/{serial}/request", json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}))

    def on_msg(cl, u, m):
        try:
            p = json.loads(m.payload).get("print") or {}
        except ValueError:
            return
        if p.get("gcode_state"):
            result.update(ok=True, step="done", state=p.get("gcode_state"))
            got.set()

    c.on_connect, c.on_message = on_conn, on_msg
    try:
        c.connect(host, 8883, 15)
        c.loop_start()
        got.wait(10)
    except (OSError, ssl.SSLError):
        pass
    finally:
        c.loop_stop()
        c.disconnect()
    return result


# ---------- printer commands ----------

COMMANDS = {
    "pause": lambda a: {"print": {"command": "pause", "param": ""}},
    "resume": lambda a: {"print": {"command": "resume", "param": ""}},
    "stop": lambda a: {"print": {"command": "stop", "param": ""}},
    "speed": lambda a: {"print": {"command": "print_speed", "param": str(a["level"])}},
}
LIMITS = {"nozzle": (0, 300), "bed": (0, 110)}
FAN_PORTS = {"part": "P1", "aux": "P2", "chamber": "P3"}


def gcode(line):
    return {"print": {"command": "gcode_line", "param": line + "\n"}}


def extra_command(act, a):
    """Temperature, fan and AMS-drying commands with server-side limits; (payload, error)."""
    if act == "temp":
        which, v = a.get("target"), _num(a.get("value"))
        if which not in LIMITS or v is None or not LIMITS[which][0] <= v <= LIMITS[which][1]:
            return None, "bad temperature"
        return gcode(f"{'M104' if which == 'nozzle' else 'M140'} S{int(v)}"), None
    if act == "fan":
        port, pct = FAN_PORTS.get(a.get("fan")), _num(a.get("pct"))
        if not port or pct is None or not 0 <= pct <= 100:
            return None, "bad fan"
        return gcode(f"M106 {port} S{math.ceil(255 * round(pct / 10) * 10 / 100)}"), None
    if act in ("dry_start", "dry_stop"):
        ams_id = int(_num(a.get("ams_id"), -1) if a.get("ams_id") is not None else -1)
        if ams_id < 0 or ams_id > 255:
            return None, "bad ams"
        temp, hours = int(_num(a.get("temp"), 0) or 0), int(_num(a.get("hours"), 0) or 0)
        if act == "dry_start" and not (35 <= temp <= 85 and 1 <= hours <= 24):
            return None, "bad drying settings"
        start = act == "dry_start"
        return {"print": {"command": "ams_filament_drying", "ams_id": ams_id,
                          "temp": temp if start else 0, "cooling_temp": 45 if start else 0,
                          "duration": hours if start else 0, "humidity": 0, "mode": 1 if start else 0,
                          "rotate_tray": False, "filament": str(a.get("filament") or "")[:16] if start else "",
                          "close_power_conflict": False}}, None
    return None, "unknown action"


def light_cmds(on):
    return [{"system": {"command": "ledctrl", "led_node": node, "led_mode": "on" if on else "off",
                        "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
            for node in ("chamber_light", "chamber_light2")]


# ---------- optional password for controls and settings ----------

SESSION_COOKIE = "mc_session"
SESSION_DAYS = 30
login_failures = {}  # ip -> (count, first_failure_time)


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def check_password(pw, stored):
    try:
        _, salt, digest = stored.split("$")
        return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)).split("$")[2], digest)
    except ValueError:
        return False


def auth_required():
    return bool(cfg.get("password_hash"))


def make_token():
    # Stateless: expiry signed with a per-password secret, so changing the password logs everyone out.
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(cfg["session_secret"].encode(), exp.encode(), "sha256").hexdigest()
    return f"{exp}.{sig}"


def token_ok(tok):
    try:
        exp, sig = tok.split(".")
        good = hmac.new(cfg.get("session_secret", "").encode(), exp.encode(), "sha256").hexdigest()
        return int(exp) > time.time() and hmac.compare_digest(sig, good)
    except (ValueError, AttributeError):
        return False


def parse_costs(c):
    """Validate the cost settings form; empty fields mean "not set"."""
    if not isinstance(c, dict):
        return None, "costs"

    def price(v):
        if v in (None, ""):
            return None
        v = _num(v)
        if v is None or not 0 <= v <= 100000:
            raise ValueError
        return v
    try:
        out = {"currency": str(c.get("currency") or "€").strip()[:4] or "€",
               "default_kg": price(c.get("default_kg")), "power_w": price(c.get("power_w")),
               "day_price": price(c.get("day_price")), "night_price": price(c.get("night_price")),
               "filament": {}}
        for k, v in (c.get("filament") or {}).items():
            if re.match(r"^[A-Z0-9+-]{1,12}$", str(k).upper()) and price(v) is not None:
                out["filament"][str(k).upper()] = price(v)
        for k in ("night_from", "night_to"):
            v = str(c.get(k) or DEFAULT_COSTS[k])
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
                raise ValueError
            out[k] = v
    except ValueError:
        return None, "costs"
    return out, None


STATIC_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json",
                ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ----- plumbing -----

    def _send(self, code, body, ctype, headers=(), cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, headers=()):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def _page(self, name):
        with open(os.path.join(BASE, "static", name), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _static(self, name):
        path = os.path.normpath(os.path.join(BASE, "static", name))
        ext = os.path.splitext(path)[1]
        if not path.startswith(os.path.join(BASE, "static") + os.sep) or ext not in STATIC_TYPES or not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain")
        with open(path, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES[ext], cache="max-age=86400")

    def _query(self):
        return {k: v[0] for k, v in parse_qs(self.path.partition("?")[2]).items()}

    def _printer(self, pid=None):
        """The printer a request is about: ?p=<id> (or "p" in a POST body); the first one by default."""
        pid = pid or self._query().get("p")
        if pid and pid in printers:
            return printers[pid]
        return next(iter(printers.values()), None) if not pid else None

    def _authed(self):
        if not auth_required():
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE and token_ok(v):
                return True
        return False

    def _deny(self):
        self._json(401, {"ok": False, "error": "auth"})

    def _session_cookie(self, token, max_age):
        return ("Set-Cookie", f"{SESSION_COOKIE}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict")

    # ----- GET -----

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not any_configured():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._page("index.html")
        if path == "/setup":
            return self._page("setup.html")
        if path == "/inventory":
            return self._page("inventory.html")
        if path == "/api/inventory":
            return self._json(200, inventory())
        m = re.match(r"^/api/inventory/([0-9A-Fa-f]{8,64})$", path)
        if m:
            return self._json(200, {"jobs": hist_db.spool_jobs(m.group(1))})
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/manifest.webmanifest":
            return self._static("manifest.webmanifest")
        if path == "/api/status":
            return self._json(200, {"printers": [status_summary(pr) for pr in list(printers.values())]})
        if path == "/api/session":
            return self._json(200, {"required": auth_required(), "authed": self._authed()})
        if (path in ("/api/config", "/api/discover", "/api/backups", "/api/backup") or path.startswith("/api/backups/")) \
                and not self._authed():
            return self._deny()
        if path == "/api/config":
            # Never hand an access code back out, only whether one is stored.
            return self._json(200, {
                "port": cfg["port"], "running_port": PORT, "has_password": auth_required(), "costs": cfg["costs"],
                "stream": {"overlay": cfg["stream"]["overlay"], "overlay_supported": bool(HAS_DRAWTEXT and HUD_FONT),
                           "targets": {k: {"server": v["server"], "has_key": bool(v["key"])} for k, v in cfg["stream"]["targets"].items()}},
                "printers": [{"id": p["id"], "name": p["name"], "host": p.get("host"), "serial": p.get("serial"),
                              "has_code": bool(p.get("access_code")), "dev_mode": bool(p.get("dev_mode")),
                              "configured": printers[p["id"]].configured()}
                             for p in cfg["printers"] if p["id"] in printers]})
        if path == "/api/backups":
            return self._json(200, {"backups": list_backups()})
        if path == "/api/backup" or path.startswith("/api/backups/"):
            return self._download_backup(path)
        if path == "/api/discover":
            return self._json(200, {"printers": discover(), "subnets": [str(n) for n in local_subnets()]})
        if path in ("/api/history", "/api/stats"):
            q = self._query()
            serial = None if q.get("p") == "all" else (self._printer().conf.get("serial") if self._printer() else None)
            if path == "/api/history":
                return self._json(200, {"jobs": hist_db.recent(100, serial), "currency": cfg["costs"].get("currency")})
            days = int(q["days"]) if q.get("days", "").isdigit() else None
            return self._json(200, {**hist_db.stats(days, serial), "currency": cfg["costs"].get("currency")})
        m = re.match(r"^/api/history/(\d+)$", path)
        if m:
            d = hist_db.detail(int(m.group(1)))
            if not d:
                return self._send(404, b"", "text/plain")
            pr = next((x for x in printers.values() if x.conf.get("serial") == d["serial"]), None)
            d["printer"] = pr.conf["name"] if pr else d["serial"]
            return self._json(200, d)
        m = re.match(r"^/api/history/(\d+)/toolpath\.bin$", path)
        if m:
            jid = int(m.group(1))
            gz = hist_db.toolpath(jid)
            if not gz:
                rec = hist_db.get(jid)
                pr = rec and next((x for x in printers.values() if x.conf.get("serial") == rec["serial"]), None)
                try:
                    gz = pr.backfill_toolpath(rec) if pr and pr.configured() else None
                except Exception:  # noqa: BLE001 - file gone from the printer, FTP down...
                    gz = None
            if not gz:
                return self._send(404, b"no toolpath", "text/plain")
            return self._send(200, gz, "application/octet-stream", headers=[("Content-Encoding", "gzip")],
                              cache="max-age=86400")
        m = re.match(r"^/api/history/(\d+)/(snapshot\.jpg|thumb\.png)$", path)
        if m:
            data = hist_db.blob(int(m.group(1)), "snapshot" if m.group(2).startswith("snap") else "thumb")
            if not data:
                return self._send(404, b"", "text/plain")
            return self._send(200, data, "image/jpeg" if m.group(2).endswith("jpg") else "image/png", cache="max-age=86400")
        pr = self._printer()
        if pr is None:
            return self._send(404, b"no such printer", "text/plain")
        if path == "/events":
            return self._sse(pr)
        if path == "/video.mp4":
            return self._fmp4(pr)
        if path == "/stream.mjpg":
            return self._mjpeg(pr)
        if path == "/snapshot.jpg":
            img = pr.camera.snapshot()
            return self._send(200, img, "image/jpeg") if img else self._send(503, b"no frame", "text/plain")
        if path == "/thumb.png":
            t = pr.job.get("thumb")
            return self._send(200, t, "image/png") if t else self._send(404, b"", "text/plain")
        self._send(404, b"not found", "text/plain")

    # ----- POST -----

    def do_POST(self):
        if self.path == "/api/restore":
            return self._restore_upload()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            a = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self._send(400, b"bad json", "text/plain")
        if not isinstance(a, dict):
            return self._send(400, b"bad json", "text/plain")
        if self.path == "/api/login":
            return self._login(a)
        if self.path == "/api/logout":
            return self._send(200, b'{"ok":true}', "application/json", headers=[self._session_cookie("", 0)])
        if self.path in ("/api/cmd", "/api/test", "/api/config", "/api/printer/delete", "/api/history/delete") \
                and not self._authed():
            return self._deny()
        if self.path == "/api/inventory":
            if not self._authed():
                return self._deny()
            return self._inventory_edit(a)
        if self.path == "/api/stream":
            if not self._authed():
                return self._deny()
            pr = self._printer(a.get("p"))
            if pr is None or not pr.configured():
                return self._json(409, {"ok": False, "error": "not configured"})
            if a.get("action") == "stop":
                pr.streamer.stop()
                return self._json(200, {"ok": True})
            names = [n for n in (a.get("targets") or []) if n in STREAM_SERVICES]
            err = pr.streamer.start(names, a.get("overlay", cfg["stream"]["overlay"]))
            return self._json(400 if err else 200, {"ok": not err, "error": err})
        if self.path == "/api/history/delete":
            hist_db.delete(int(_num(a.get("id"), 0) or 0))
            return self._json(200, {"ok": True})
        if self.path == "/api/printer/delete":
            return self._delete_printer(a)
        m = re.match(r"^/api/backups/((?:auto|safety|manual)-[\d-]+\.zip)/restore$", self.path)
        if m:
            if not self._authed():
                return self._deny()
            path = os.path.join(BACKUP_DIR, m.group(1))
            if not os.path.isfile(path):
                return self._json(404, {"ok": False, "error": "no_backup"})
            try:
                return self._json(200, {"ok": True, "manifest": restore_backup(path)})
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
        if self.path == "/api/cmd":
            return self._cmd(a)
        if self.path == "/api/test":
            return self._test(a)
        if self.path == "/api/config":
            return self._save(a)
        self._send(404, b"", "text/plain")

    def _creds(self, a, conf):
        """Validated (host, serial, code); a stored code is reused only for the same printer at the same address."""
        host = str(a.get("host", "")).strip()
        serial = str(a.get("serial", "")).strip().upper()
        code = str(a.get("access_code", "")).strip()
        if not re.match(r"^[A-Za-z0-9.-]{1,253}$", host):
            return None, "host"
        if serial and not SERIAL_RE.match(serial):
            return None, "serial"
        if not code:
            if not (conf and conf.get("access_code") and host == conf.get("host") and serial in ("", conf.get("serial"))):
                return None, "code_required"
            code = conf["access_code"]
        elif not re.match(r"^[A-Za-z0-9]{4,32}$", code):
            return None, "code"
        return (host, serial, code), None

    def _conf_for(self, a):
        pid = a.get("p")
        return next((p for p in cfg["printers"] if p["id"] == pid), None)

    def _test(self, a):
        creds, err = self._creds(a, self._conf_for(a))
        if err:
            return self._json(400, {"ok": False, "error": err})
        host, serial, code = creds
        self._json(200, test_printer(host, code, serial or None))

    def _save(self, a):
        """Global settings (port, costs, password) plus, when "host" is present, one printer ("p": id or "new")."""
        new_global = {}
        try:
            port = int(a.get("port") or cfg["port"])
        except (TypeError, ValueError):
            port = 0
        if not 1024 <= port <= 65535:
            return self._json(400, {"ok": False, "error": "port"})
        new_global["port"] = port
        if isinstance(a.get("stream"), dict):
            st, cur = a["stream"], cfg["stream"]
            targets = {}
            for k in STREAM_SERVICES:
                t, old = st.get("targets", {}).get(k) or {}, cur["targets"][k]
                server = str(t.get("server", old["server"]) or STREAM_SERVICES[k]).strip()
                if server and not re.match(r"^rtmps?://[^\s|]+$", server):
                    return self._json(400, {"ok": False, "error": "stream_server"})
                key = old["key"] if t.get("key") in (None, "") else str(t["key"]).strip()
                if t.get("clear"):
                    key = ""
                if not re.match(r"^[^\s|]{0,200}$", key):
                    return self._json(400, {"ok": False, "error": "stream_key"})
                targets[k] = {"server": server, "key": key}
            new_global["stream"] = {"overlay": bool(st.get("overlay", cur["overlay"])), "targets": targets}
        if "costs" in a:
            costs, err = parse_costs(a["costs"])
            if err:
                return self._json(400, {"ok": False, "error": err})
            new_global["costs"] = costs
        pw = str(a.get("new_password") or "")
        if pw:
            if not 4 <= len(pw) <= 128:
                return self._json(400, {"ok": False, "error": "password"})
            new_global.update(password_hash=hash_password(pw), session_secret=secrets.token_hex(32))
        elif a.get("remove_password"):
            new_global.update(password_hash="", session_secret="")

        result = {"ok": True, "restart_needed": port != PORT}
        conf, reconnect = None, False
        if a.get("host"):
            conf = self._conf_for(a)
            creds, err = self._creds(a, conf)
            if err:
                return self._json(400, {"ok": False, "error": err})
            host, serial, code = creds
            if not serial:
                serial = printer_identity(host) or ""
                if not serial:
                    return self._json(400, {"ok": False, "error": "serial"})
            if any(p.get("serial") == serial and p is not conf for p in cfg["printers"]):
                return self._json(400, {"ok": False, "error": "duplicate"})
            name = (str(a.get("name") or "").strip() or "Bambu")[:40]
            fields = {"host": host, "serial": serial, "access_code": code, "name": name, "dev_mode": bool(a.get("dev_mode"))}
            with cfg_lock:
                if conf is None:  # a new printer
                    used = {p["id"] for p in cfg["printers"]}
                    pid = next(f"p{i}" for i in range(1, 1000) if f"p{i}" not in used)
                    conf = {"id": pid, "locked_cmds": [], **fields}
                    cfg["printers"].append(conf)
                else:
                    reconnect = any(fields[k] != conf.get(k) for k in ("host", "serial", "access_code"))
                    if reconnect or (fields["dev_mode"] and not conf.get("dev_mode")):
                        conf["locked_cmds"] = []  # a different printer may well accept them
                    conf.update(fields)
            result.update(serial=serial, p=conf["id"])
        with cfg_lock:
            cfg.update(new_global)
            save_cfg(cfg)
        sync_printers()
        if conf is not None:
            pr = printers[conf["id"]]
            if reconnect:
                pr.reconnect()
            elif not pr.mqtt_ok:
                pr.cfg_changed.set()  # a brand-new printer: let its MQTT loop start
            pr.dirty.set()
        headers = []
        if pw:  # keep the person who just set the password logged in
            headers.append(self._session_cookie(make_token(), SESSION_DAYS * 86400))
        self._json(200, result, headers)

    def _inventory_edit(self, a):
        uuid, act = str(a.get("uuid") or ""), a.get("action")
        sp = hist_db.spool(uuid)
        if not sp:
            return self._json(404, {"ok": False, "error": "no such spool"})
        if act == "weigh":
            g = _num(a.get("grams"))
            if g is None or not 0 <= g <= 5000:
                return self._json(400, {"ok": False, "error": "grams"})
            # from now on: this weight minus whatever gets printed after it
            hist_db.seen_spool(uuid, adjust_g=g, adjust_at=time.time(), empty_at=None)
        elif act == "empty":
            hist_db.seen_spool(uuid, empty_at=time.time())
        elif act == "restore":
            hist_db.seen_spool(uuid, empty_at=None)
        elif act == "note":
            hist_db.seen_spool(uuid, note=str(a.get("note") or "")[:200])
        elif act == "delete":
            hist_db.delete_spool(uuid)
        else:
            return self._json(400, {"ok": False, "error": "action"})
        for pr in printers.values():
            pr.dirty.set()
        return self._json(200, {"ok": True})

    def _download_backup(self, path):
        if path == "/api/backup":
            secrets_on = self._query().get("secrets", "1") != "0"
            with tempfile.TemporaryDirectory() as d:
                f = os.path.join(d, "b.zip")
                make_backup(f, secrets=secrets_on)
                data = open(f, "rb").read()
            name = f"mission-control-backup-{time.strftime('%Y-%m-%d')}{'' if secrets_on else '-no-secrets'}.zip"
        else:
            name = path.rsplit("/", 1)[1]
            f = os.path.join(BACKUP_DIR, name)
            if not re.match(r"^(auto|safety|manual)-[\d-]+\.zip$", name) or not os.path.isfile(f):
                return self._send(404, b"", "text/plain")
            data = open(f, "rb").read()
            name = "mission-control-" + name
        self._send(200, data, "application/zip", headers=[("Content-Disposition", f'attachment; filename="{name}"')])

    def _restore_upload(self):
        if not self._authed():
            return self._deny()
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 4 * 1024 ** 3:
            return self._json(400, {"ok": False, "error": "size"})
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "upload.zip")
            with open(f, "wb") as out:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            try:
                manifest = restore_backup(f)
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
        self._json(200, {"ok": True, "manifest": manifest})

    def _delete_printer(self, a):
        conf = self._conf_for(a)
        if not conf:
            return self._json(404, {"ok": False, "error": "no such printer"})
        with cfg_lock:
            cfg["printers"].remove(conf)
            save_cfg(cfg)
        sync_printers()
        return self._json(200, {"ok": True})

    def _login(self, a):
        ip = self.client_address[0]
        count, since = login_failures.get(ip, (0, time.time()))
        if time.time() - since > 300:
            count, since = 0, time.time()
        if count >= 5:
            return self._json(429, {"ok": False, "error": "too_many"})
        if not auth_required():
            return self._json(200, {"ok": True})
        if not check_password(str(a.get("password") or ""), cfg["password_hash"]):
            login_failures[ip] = (count + 1, since)
            time.sleep(0.7)
            return self._json(403, {"ok": False, "error": "wrong"})
        login_failures.pop(ip, None)
        self._json(200, {"ok": True}, [self._session_cookie(make_token(), SESSION_DAYS * 86400)])

    def _cmd(self, a):
        pr = self._printer(a.get("p"))
        if pr is None or not pr.configured():
            return self._send(409, b"not configured", "text/plain")
        act = a.get("action")
        if act == "light":
            for c in light_cmds(bool(a.get("on"))):
                pr.send(c)
        elif act == "speed" and a.get("level") not in (1, 2, 3, 4):
            return self._send(400, b"bad speed level", "text/plain")
        elif act == "unlock":
            with cfg_lock:
                pr.conf["locked_cmds"] = []
                save_cfg(cfg)
            pr.dirty.set()
            return self._json(200, {"ok": True})
        elif act in COMMANDS:
            pr.send(COMMANDS[act](a))
        else:
            # Temperature/fan/drying need Developer Mode on the printer; without it every attempt
            # is rejected and leaves an HMS notice on the printer screen, so don't even try.
            if not pr.conf.get("dev_mode"):
                return self._json(409, {"ok": False, "error": "dev_mode_off"})
            payload, err = extra_command(act, a)
            mqtt_cmd = (payload or {}).get("print", {}).get("command")
            if mqtt_cmd in pr.conf["locked_cmds"]:
                return self._json(409, {"ok": False, "error": "locked"})
            if err:
                return self._send(400, err.encode(), "text/plain")
            pr.send(payload)
        pr.log("cmd", L(f"Отправлено: {act}", f"Sent: {act}"))
        self._send(200, b'{"ok":true}', "application/json")

    # ----- streams -----

    def _sse(self, pr):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = queue.Queue(maxsize=4)
        with pr.lock:
            first = json.dumps(pr.build_view(), ensure_ascii=False)
        pr.sse_clients.append(q)
        try:
            self.wfile.write(f"data: {first}\n\n".encode())
            self.wfile.flush()
            while not pr.stopped:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            pr.sse_clients.remove(q)

    def _fmp4(self, pr):
        h264 = pr.h264
        h264.acquire()
        try:
            with h264.cond:
                h264.cond.wait_for(lambda: h264.init is not None and h264.frame is not None, timeout=20)
                init, codec, frag, seq = h264.init, h264.codec, h264.frame, h264.seq
            if init is None or frag is None:
                return self._send(503, b"camera not ready", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("X-Codec", codec)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(init + frag)
            while not pr.stopped:
                with h264.cond:
                    h264.cond.wait_for(lambda: h264.seq != seq or h264.init is not init, timeout=10)
                    if h264.init is not init:
                        break  # ffmpeg restarted: new init segment, let the page reconnect
                    if h264.seq == seq:
                        continue
                    frag, seq = h264.frame, h264.seq
                # A slow viewer simply skips to the newest fragment; each one starts on a keyframe.
                self.wfile.write(frag)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            h264.release()

    def _mjpeg(self, pr):
        camera = pr.camera
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        camera.acquire()
        last = None
        try:
            while not pr.stopped:
                with camera.cond:
                    camera.cond.wait(timeout=10)
                    frame = camera.frame
                if frame is None or frame is last:
                    continue
                last = frame
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            camera.release()


def shutdown(*_):
    # ffmpeg stuck on a dead stream ignores SIGTERM and would outlive us holding a printer session.
    for pr in list(printers.values()):
        pr.camera._stop(kill=True)
        pr.h264._stop(kill=True)
        if pr.streamer.proc is not None:
            pr.streamer.proc.kill()
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if any(k in json.load(open(CFG_PATH)) for k in PRINTER_KEYS) if os.path.exists(CFG_PATH) else False:
        save_cfg(cfg)  # migrate a single-printer config to the "printers" list on disk
    sync_printers()
    threading.Thread(target=auto_backups, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    names = ", ".join(f"{pr.conf['name']} ({pr.conf['host']})" for pr in printers.values()) or "not configured, open /setup"
    print(f"Mission Control: http://localhost:{PORT}  (printers: {names})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
