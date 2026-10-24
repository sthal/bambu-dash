#!/bin/sh
# Start H2S Mission Control; creates the venv on first run.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip -q install paho-mqtt; }
exec .venv/bin/python server.py
