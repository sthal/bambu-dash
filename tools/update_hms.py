#!/usr/bin/env python3
"""Refresh data/hms.json from Bambu's public error-message database (Russian + English).

Usage: python3 tools/update_hms.py
"""
import json
import os
import urllib.request

URL = "https://e.bambulab.com/query.php?lang={}"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "hms.json")


def fetch(lang):
    # The endpoint answers 403 to urllib's default User-Agent.
    req = urllib.request.Request(URL.format(lang), headers={"User-Agent": "Mozilla/5.0 bambu-dash"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)["data"]
    return {kind: {e["ecode"].upper(): e["intro"].strip() for e in data[kind][lang] if e.get("intro", "").strip()}
            for kind in ("device_hms", "device_error")}


def main():
    ru, en = fetch("ru"), fetch("en")
    out = {}
    for kind, key in (("device_hms", "hms"), ("device_error", "error")):
        codes = sorted(set(ru[kind]) | set(en[kind]))
        out[key] = {c: {"ru": ru[kind].get(c, en[kind].get(c)), "en": en[kind].get(c, ru[kind].get(c))} for c in codes}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{OUT}: {len(out['hms'])} HMS, {len(out['error'])} error codes")


if __name__ == "__main__":
    main()
