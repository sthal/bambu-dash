#!/usr/bin/env python3
"""H2S Mission Control: local dashboard for a Bambu Lab printer.

Talks to the printer only over the LAN: MQTT (8883) for status and commands,
RTSPS (322) for the camera via ffmpeg, implicit FTPS (990) for the job's .3mf
(plate thumbnail, filament usage). Serves a single page plus SSE and MJPEG.
"""
import collections
import concurrent.futures
import ftplib
import io
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
DEFAULTS = {"host": "", "serial": "", "access_code": "", "name": "Bambu", "port": 8790}


def load_cfg():
    try:
        with open(CFG_PATH) as f:
            return {**DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        return dict(DEFAULTS)


def save_cfg(c):
    # Written with 0600 from the start: the file holds the printer's access code.
    tmp = CFG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CFG_PATH)


cfg = load_cfg()
cfg_changed = threading.Event()
PORT = int(os.environ.get("PORT") or cfg.get("port") or 8790)


def configured():
    return bool(cfg["host"] and cfg["serial"] and cfg["access_code"])


# Prefer a standalone ffmpeg dropped into ./bin (the no-Homebrew route), else whatever is on PATH.
_local_ff = os.path.join(BASE, "bin", "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
FFMPEG = _local_ff if os.path.exists(_local_ff) else "ffmpeg"
# The printer's certificate is self-signed; ffmpeg 9+ verifies TLS by default and would refuse it.
FF_IN = ["-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-tls_verify", "0"]


def rtsp_url():
    return f"rtsps://bblp:{cfg['access_code']}@{cfg['host']}:322/streaming/live/1"

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


lock = threading.Lock()
raw = {}
mqtt_ok = False
last_msg = 0.0
dirty = threading.Event()
events = collections.deque(maxlen=60)
hist = collections.deque(maxlen=360)  # one sample per 5 s -> 30 min
job = {"key": None, "started_at": None, "thumb": None, "meta": None, "fetched_for": None, "fetch_err_at": 0}
prev = {"state": None, "stage": None, "err": None, "hms": ()}


def log_event(kind, text):
    events.appendleft({"t": time.time(), "kind": kind, "text": text})
    dirty.set()


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


# ---------- normalization ----------

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


def _hms_code(h):
    a, c = int(h.get("attr", 0)), int(h.get("code", 0))
    return f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"


def build_view():
    p = raw
    dev = p.get("device") or {}
    online = mqtt_ok and time.time() - last_msg < 90
    state = p.get("gcode_state") or "IDLE"
    if not configured():
        state = "UNCONFIGURED"
    elif not online:
        state = "OFFLINE"
    stg = p.get("stg_cur")
    stg = int(stg) if isinstance(stg, (int, float)) or (isinstance(stg, str) and stg.lstrip("-").isdigit()) else -1
    if state == "RUNNING" and stg in (0, -1, 255):
        stage = STAGES[0]
    elif state in ("RUNNING", "PREPARE", "PAUSE"):
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
            trays.append({
                "slot": tid + 1, "empty": empty, "type": t.get("tray_type") or "",
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
    meta = job.get("meta") or {}

    return {
        "name": cfg["name"] or "Bambu", "online": online, "configured": configured(), "ts": time.time(),
        "state": state, "state_txt": state_name(state), "stage": stage, "stage_id": stg,
        "pct": int(_num(p.get("mc_percent"), 0) or 0), "remaining_min": rem,
        "eta": (time.time() + rem * 60) if rem and state in ("RUNNING", "PAUSE", "PREPARE") else None,
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
        "errors": {"print": f"{perr:08X}" if perr else None, "hms": hms},
        "job": {"started_at": job.get("started_at"), "thumb": bool(job.get("thumb")),
                "thumb_v": job.get("fetched_for") or "", "weight_g": meta.get("weight"),
                "prediction_s": meta.get("prediction"), "filaments": meta.get("filaments") or []},
        "hist": list(hist)[-240:],
        "events": list(events)[:40],
    }


def track_transitions():
    """Called under lock after each MQTT message: emits timeline events."""
    p = raw
    state = p.get("gcode_state")
    key = (p.get("subtask_id") or p.get("subtask_name"), p.get("plate_idx"))
    if state and state != prev["state"]:
        if prev["state"] is not None:
            a, b = state_name(prev["state"]), state_name(state)
            log_event("state", L(f"{a['ru']} → {b['ru']}", f"{a['en']} → {b['en']}"))
        if state in ("PREPARE", "RUNNING") and prev["state"] not in ("PREPARE", "RUNNING", "PAUSE", None):
            job["started_at"] = time.time()
        prev["state"] = state
    if key != job["key"]:
        job.update(key=key, thumb=None, meta=None)
        if p.get("subtask_name"):
            log_event("job", L(f"Задание: {p.get('subtask_name')}", f"Job: {p.get('subtask_name')}"))
    stg = p.get("stg_cur")
    if stg != prev["stage"]:
        if prev["stage"] is not None and isinstance(stg, int) and stg not in (0, 255, -1):
            log_event("stage", stage_name(stg))
        prev["stage"] = stg
    err = p.get("print_error") or 0
    if err != prev["err"]:
        if err:
            log_event("error", L(f"Ошибка печати {int(err):08X}", f"Print error {int(err):08X}"))
        prev["err"] = err
    hms = tuple(_hms_code(h) for h in p.get("hms") or [])
    if hms != prev["hms"]:
        for code in set(hms) - set(prev["hms"]):
            log_event("error", L(f"HMS {code}", f"HMS {code}"))
        prev["hms"] = hms


# ---------- MQTT ----------

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mission-{os.getpid()}")
seq = [0]


def send(payload):
    seq[0] += 1
    for v in payload.values():
        v["sequence_id"] = str(seq[0])
    client.publish(f"device/{cfg['serial']}/request", json.dumps(payload))


def on_connect(c, u, flags, rc, props=None):
    global mqtt_ok
    mqtt_ok = not rc.is_failure
    if mqtt_ok:
        c.subscribe(f"device/{cfg['serial']}/report")
        send({"pushing": {"command": "pushall"}})
        log_event("link", L("Связь с принтером установлена", "Connected to printer"))
    else:
        log_event("error", L(f"MQTT отказ: {rc}", f"MQTT refused: {rc}"))


def on_disconnect(c, u, flags, rc, props=None):
    global mqtt_ok
    mqtt_ok = False
    log_event("link", L("Связь с принтером потеряна", "Printer connection lost"))


def on_message(c, u, m):
    global last_msg
    try:
        d = json.loads(m.payload)
    except ValueError:
        return
    last_msg = time.time()
    with lock:
        for section in ("print", "system"):
            body = d.get(section)
            if not isinstance(body, dict):
                continue
            if body.get("result") and body.get("command") not in ("push_status", None):
                ok = str(body.get("result")).lower() == "success"
                why = body.get("reason") or body.get("err_code") or ""
                c = body["command"]
                log_event("ack" if ok else "error",
                          L(f"Команда {c}: {'выполнена' if ok else 'отклонена'} {why}".strip(),
                            f"Command {c}: {'done' if ok else 'rejected'} {why}".strip()))
            if section == "print":
                merge(raw, body)
        track_transitions()
    dirty.set()


def reset_state():
    global last_msg
    with lock:
        raw.clear()
        hist.clear()
        job.update(key=None, started_at=None, thumb=None, meta=None, fetched_for=None, fetch_err_at=0)
        prev.update(state=None, stage=None, err=None, hms=())
        last_msg = 0.0
    dirty.set()


def apply_cfg(new):
    """Save settings and reconnect everything to the (possibly different) printer."""
    cfg.update(new)
    save_cfg(cfg)
    reset_state()
    camera.restart()
    log_event("link", L(f"Настройки сохранены: {cfg['name']} ({cfg['host']})",
                        f"Settings saved: {cfg['name']} ({cfg['host']})"))
    cfg_changed.set()
    client.disconnect()  # makes loop_forever return, mqtt_loop reconnects with the new settings


def mqtt_loop():
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(2, 30)
    while True:
        if not configured():
            cfg_changed.wait()
        cfg_changed.clear()
        host = cfg["host"]
        client.username_pw_set("bblp", cfg["access_code"])
        try:
            client.connect(host, 8883, 30)
            client.loop_forever(retry_first_connection=True)
        except (OSError, ssl.SSLError) as e:
            log_event("error", L(f"Нет связи с {host}: {e}", f"Cannot reach {host}: {e}"))
            cfg_changed.wait(10)


def sampler():
    """History samples every 5 s, a full refresh every 10 min, and the job fetcher trigger."""
    n = 0
    while True:
        time.sleep(5)
        n += 1
        with lock:
            v = build_view()
        t = v["temps"]
        hist.append([round(time.time()), t["nozzle"]["cur"], t["bed"]["cur"], t["chamber"]["cur"], v["pct"]])
        dirty.set()
        if n % 120 == 0 and mqtt_ok:
            send({"pushing": {"command": "pushall"}})
        if v["task"] and job["fetched_for"] != _job_key_str() and time.time() - job["fetch_err_at"] > 120:
            threading.Thread(target=fetch_job, daemon=True).start()


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


def _job_key_str():
    return f"{raw.get('subtask_name')}#{raw.get('plate_idx')}"


def fetch_job():
    with lock:
        name, plate, key = raw.get("subtask_name") or "", int(_num(raw.get("plate_idx"), 1) or 1), _job_key_str()
    if job.get("_busy"):
        return
    job["_busy"] = True
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        f = ImplicitFTPS(context=ctx)
        f.connect(cfg["host"], 990, timeout=20)
        f.login("bblp", cfg["access_code"])
        f.prot_p()
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
        f.quit()
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
                        "filaments": [{"type": fl.get("type"), "color": fl.get("color"),
                                       "g": _num(fl.get("used_g")), "m": _num(fl.get("used_m"))}
                                      for fl in pl.findall("filament")]}
        with lock:
            job.update(thumb=thumb, meta=meta, fetched_for=key)
        kb = len(buf.getvalue()) // 1024
        log_event("job", L(f"Загружено превью модели ({kb} КБ .3mf)", f"Model preview loaded ({kb} KB .3mf)"))
    except Exception as e:  # noqa: BLE001 - printer FTP is flaky, never let it kill the thread
        job["fetch_err_at"] = time.time()
        log_event("error", L(f"Превью не загружено: {e}", f"Preview not loaded: {e}"))
    finally:
        job["_busy"] = False
        dirty.set()


# ---------- camera: one shared ffmpeg, fan-out MJPEG ----------

class Camera:
    def __init__(self):
        self.cond = threading.Condition()
        self.frame, self.frame_t = None, 0.0
        self.clients, self.proc, self.idle_since = 0, None, time.time()
        threading.Thread(target=self._watch, daemon=True).start()

    def restart(self):
        """Drop the stream so the watcher reopens it with the current settings."""
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self.frame = None

    def _start(self):
        if not configured():
            return
        self.proc = subprocess.Popen(
            [FFMPEG, *FF_IN, "-i", rtsp_url(),
             "-an", "-vf", "scale=1280:-2,fps=15", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
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
        while True:
            time.sleep(2)
            alive = self.proc is not None and self.proc.poll() is None
            if self.clients > 0 and not alive:
                self._start()
            elif self.clients == 0 and alive and time.time() - self.idle_since > 20:
                self.proc.terminate()
                self.proc = None

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
        if not configured():
            return None
        r = subprocess.run([FFMPEG, *FF_IN, "-i", rtsp_url(), "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
                           capture_output=True, timeout=25)
        return r.stdout or None


camera = Camera()

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
    return [{"host": h, "serial": s} for h, s in zip(candidates, serials) if s]


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

# ---------- HTTP ----------

sse_clients = []


def broadcaster():
    last_push = 0.0
    while True:
        dirty.wait(timeout=10)
        dirty.clear()
        time.sleep(max(0, 0.5 - (time.time() - last_push)))  # at most 2 pushes/s
        with lock:
            data = json.dumps(build_view(), ensure_ascii=False)
        last_push = time.time()
        for q in list(sse_clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


COMMANDS = {
    "pause": lambda a: {"print": {"command": "pause", "param": ""}},
    "resume": lambda a: {"print": {"command": "resume", "param": ""}},
    "stop": lambda a: {"print": {"command": "stop", "param": ""}},
    "speed": lambda a: {"print": {"command": "print_speed", "param": str(int(a.get("level", 2)))}},
}


def light_cmds(on):
    return [{"system": {"command": "ledctrl", "led_node": node, "led_mode": "on" if on else "off",
                        "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
            for node in ("chamber_light", "chamber_light2")]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _page(self, name):
        with open(os.path.join(BASE, "static", name), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not configured():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._page("index.html")
        if path == "/setup":
            return self._page("setup.html")
        if path == "/api/config":
            # Never hand the access code back out, only whether one is stored.
            return self._json(200, {"host": cfg["host"], "serial": cfg["serial"], "name": cfg["name"],
                                    "port": cfg["port"], "running_port": PORT,
                                    "has_code": bool(cfg["access_code"]), "configured": configured()})
        if path == "/api/discover":
            return self._json(200, {"printers": discover(), "subnets": [str(n) for n in local_subnets()]})
        if path == "/events":
            return self._sse()
        if path == "/stream.mjpg":
            return self._mjpeg()
        if path == "/snapshot.jpg":
            img = camera.snapshot()
            return self._send(200, img, "image/jpeg") if img else self._send(503, b"no frame", "text/plain")
        if path == "/thumb.png":
            t = job.get("thumb")
            return self._send(200, t, "image/png") if t else self._send(404, b"", "text/plain")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            a = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self._send(400, b"bad json", "text/plain")
        if not isinstance(a, dict):
            return self._send(400, b"bad json", "text/plain")
        if self.path == "/api/cmd":
            return self._cmd(a)
        if self.path == "/api/test":
            return self._test(a)
        if self.path == "/api/config":
            return self._save(a)
        self._send(404, b"", "text/plain")

    def _creds(self, a):
        """Validated (host, serial, code) from a form; the stored code is reused only for the same printer."""
        host = str(a.get("host", "")).strip()
        serial = str(a.get("serial", "")).strip().upper()
        code = str(a.get("access_code", "")).strip()
        if not re.match(r"^[A-Za-z0-9.-]{1,253}$", host):
            return None, "host"
        if serial and not SERIAL_RE.match(serial):
            return None, "serial"
        if not code:
            if not (cfg["access_code"] and host == cfg["host"] and serial in ("", cfg["serial"])):
                return None, "code_required"
            code = cfg["access_code"]
        elif not re.match(r"^[A-Za-z0-9]{4,32}$", code):
            return None, "code"
        return (host, serial, code), None

    def _test(self, a):
        creds, err = self._creds(a)
        if err:
            return self._json(400, {"ok": False, "error": err})
        host, serial, code = creds
        self._json(200, test_printer(host, code, serial or None))

    def _save(self, a):
        creds, err = self._creds(a)
        if err:
            return self._json(400, {"ok": False, "error": err})
        host, serial, code = creds
        if not serial:
            serial = printer_identity(host) or ""
            if not serial:
                return self._json(400, {"ok": False, "error": "serial"})
        try:
            port = int(a.get("port") or cfg["port"])
        except (TypeError, ValueError):
            port = 0
        if not 1024 <= port <= 65535:
            return self._json(400, {"ok": False, "error": "port"})
        name = (str(a.get("name") or "").strip() or "Bambu")[:40]
        apply_cfg({"host": host, "serial": serial, "access_code": code, "name": name, "port": port})
        self._json(200, {"ok": True, "serial": serial, "restart_needed": port != PORT})

    def _cmd(self, a):
        if not configured():
            return self._send(409, b"not configured", "text/plain")
        act = a.get("action")
        if act == "light":
            for c in light_cmds(bool(a.get("on"))):
                send(c)
        elif act in COMMANDS:
            send(COMMANDS[act](a))
        else:
            return self._send(400, b"unknown action", "text/plain")
        log_event("cmd", L(f"Отправлено: {act}", f"Sent: {act}"))
        self._send(200, b'{"ok":true}', "application/json")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = queue.Queue(maxsize=4)
        with lock:
            first = json.dumps(build_view(), ensure_ascii=False)
        sse_clients.append(q)
        try:
            self.wfile.write(f"data: {first}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            sse_clients.remove(q)

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        camera.acquire()
        last = None
        try:
            while True:
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


def main():
    for target in (mqtt_loop, sampler, broadcaster):
        threading.Thread(target=target, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    where = cfg["host"] if configured() else "not configured, open /setup"
    print(f"Mission Control: http://localhost:{PORT}  (printer: {where})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
