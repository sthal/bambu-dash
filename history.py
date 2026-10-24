"""Print history, statistics and cost: one SQLite file in state/, never committed."""
import datetime
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL,
    job_key TEXT NOT NULL,
    task TEXT,
    plate INTEGER,
    started REAL NOT NULL,
    start_estimated INTEGER DEFAULT 0,
    ended REAL,
    result TEXT,              -- FINISH / FAILED / CANCELLED; NULL while running
    pct INTEGER DEFAULT 0,
    layers INTEGER,
    prediction_s REAL,
    weight_g REAL,
    filaments TEXT,           -- json [{type, color, g, m}]
    thumb BLOB,
    snapshot BLOB,
    energy_kwh REAL,
    cost_filament REAL,
    cost_energy REAL,
    currency TEXT
);
CREATE INDEX IF NOT EXISTS jobs_started ON jobs(started);
"""

DEFAULT_COSTS = {
    "currency": "€",
    "filament": {},          # {"PLA": 20.0, ...} price per kg; missing types use default_kg
    "default_kg": None,
    "power_w": 180,          # approximate continuous draw while printing (Bambu H2S average)
    "day_price": None,       # per kWh
    "night_price": None,
    "night_from": "22:00",
    "night_to": "06:00",
}


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def split_day_night(start, end, night_from, night_to):
    """Seconds of [start, end) that fall into the day and the night tariff windows (local time)."""
    nf, nt = _minutes(night_from), _minutes(night_to)
    day = night = 0.0
    t = start
    while t < end:
        lt = datetime.datetime.fromtimestamp(t)
        minute = lt.hour * 60 + lt.minute
        is_night = (nf <= minute < nt) if nf < nt else (minute >= nf or minute < nt)
        # step to the next minute boundary (or the end), so a long print costs a few thousand steps at most
        nxt = min(end, t + 60 - lt.second - lt.microsecond / 1e6)
        if is_night:
            night += nxt - t
        else:
            day += nxt - t
        t = nxt
    return day, night


def job_costs(costs, filaments, frac, start, end):
    """(filament cost, energy kWh, energy cost); a value is None when its prices are not set."""
    fil = None
    prices, default = costs.get("filament") or {}, costs.get("default_kg")
    if filaments:
        total, known = 0.0, True
        for f in filaments:
            price = prices.get((f.get("type") or "").upper(), default)
            if price is None:
                known = False
                break
            total += (f.get("g") or 0) / 1000 * price * frac
        fil = round(total, 2) if known else None
    kwh = cost_e = None
    if costs.get("power_w") and start and end and end > start:
        day_s, night_s = split_day_night(start, end, costs.get("night_from") or "22:00", costs.get("night_to") or "06:00")
        kw = costs["power_w"] / 1000
        kwh = round(kw * (day_s + night_s) / 3600, 3)
        dp, np_ = costs.get("day_price"), costs.get("night_price")
        if dp is not None:
            cost_e = round(kw * day_s / 3600 * dp + kw * night_s / 3600 * (np_ if np_ is not None else dp), 2)
    return fil, kwh, cost_e


class History:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def open_job(self, serial, job_key):
        with self.lock:
            r = self.db.execute("SELECT id FROM jobs WHERE serial=? AND job_key=? AND ended IS NULL ORDER BY id DESC LIMIT 1",
                                (serial, job_key)).fetchone()
        return r["id"] if r else None

    def start(self, serial, job_key, task, plate, started, estimated):
        with self.lock, self.db:
            # A job left open by a crash or restart for a different print is closed as cancelled.
            self.db.execute("UPDATE jobs SET ended=?, result='CANCELLED' WHERE serial=? AND ended IS NULL AND job_key!=?",
                            (time.time(), serial, job_key))
            cur = self.db.execute("INSERT INTO jobs(serial, job_key, task, plate, started, start_estimated) VALUES(?,?,?,?,?,?)",
                                  (serial, job_key, task, plate, started, int(estimated)))
            return cur.lastrowid

    def update(self, job_id, **fields):
        if not fields:
            return
        if "filaments" in fields and not isinstance(fields["filaments"], str):
            fields["filaments"] = json.dumps(fields["filaments"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self.db:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def get(self, job_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def delete(self, job_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def blob(self, job_id, kind):
        assert kind in ("thumb", "snapshot")
        with self.lock:
            r = self.db.execute(f"SELECT {kind} FROM jobs WHERE id=?", (job_id,)).fetchone()
        return r[0] if r else None

    def recent(self, limit=50, serial=None):
        q = ("SELECT id, serial, task, plate, started, start_estimated, ended, result, pct, layers, prediction_s, weight_g,"
             " filaments, energy_kwh, cost_filament, cost_energy, currency, thumb IS NOT NULL AS has_thumb,"
             " snapshot IS NOT NULL AS has_snapshot FROM jobs")
        args = []
        if serial:
            q += " WHERE serial=?"
            args.append(serial)
        q += " ORDER BY started DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q, args)]
        for r in rows:
            r["filaments"] = json.loads(r["filaments"] or "[]")
        return rows

    def stats(self, days=None, serial=None):
        since = time.time() - days * 86400 if days else 0
        q, args = "SELECT * FROM jobs WHERE started>=? AND ended IS NOT NULL", [since]
        if serial:
            q += " AND serial=?"
            args.append(serial)
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q, args)]
        out = {"jobs": len(rows), "finished": 0, "failed": 0, "cancelled": 0, "hours": 0.0, "grams": 0.0,
               "kwh": 0.0, "cost": 0.0, "cost_known": True, "by_material": {}, "daily": {}}
        for r in rows:
            out[{"FINISH": "finished", "FAILED": "failed"}.get(r["result"], "cancelled")] += 1
            hours = max(0.0, (r["ended"] - r["started"]) / 3600)
            out["hours"] += hours
            frac = 1.0 if r["result"] == "FINISH" else (r["pct"] or 0) / 100
            for f in json.loads(r["filaments"] or "[]"):
                g = (f.get("g") or 0) * frac
                out["grams"] += g
                key = f"{(f.get('type') or '?').upper()}|{(f.get('color') or '#888888').upper()}"
                out["by_material"][key] = out["by_material"].get(key, 0) + g
            out["kwh"] += r["energy_kwh"] or 0
            if r["cost_filament"] is None and r["cost_energy"] is None:
                out["cost_known"] = False
            out["cost"] += (r["cost_filament"] or 0) + (r["cost_energy"] or 0)
            day = datetime.date.fromtimestamp(r["started"]).isoformat()
            d = out["daily"].setdefault(day, {"hours": 0.0, "jobs": 0})
            d["hours"] += hours
            d["jobs"] += 1
        out["by_material"] = sorted(({"type": k.split("|")[0], "color": k.split("|")[1], "g": round(v, 1)}
                                     for k, v in out["by_material"].items()), key=lambda x: -x["g"])
        out["daily"] = [{"date": k, **v} for k, v in sorted(out["daily"].items())]
        for k in ("hours", "grams", "kwh", "cost"):
            out[k] = round(out[k], 2)
        return out
