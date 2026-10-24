#!/bin/sh
# H2S Mission Control (macOS / Linux): checks prerequisites, prepares .venv, starts the server.
cd "$(dirname "$0")" || exit 1

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
cmd() { printf '    \033[36m%s\033[0m\n' "$1"; }

missing=""
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        PY="$c"; break
    fi
done
[ -n "$PY" ] || missing="$missing python"
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"

if [ -n "$missing" ]; then
    bold "Не хватает / Missing:$missing"
    echo
    case "$(uname -s)" in
        Darwin)
            if ! command -v brew >/dev/null 2>&1; then
                echo "1. Homebrew:"
                cmd '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
                echo "2. Затем / Then:"
            fi
            pkgs=""
            case "$missing" in *python*) pkgs="$pkgs python" ;; esac
            case "$missing" in *ffmpeg*) pkgs="$pkgs ffmpeg" ;; esac
            cmd "brew install$pkgs"
            ;;
        Linux)
            echo "Debian / Ubuntu:"
            cmd "sudo apt update && sudo apt install -y python3 python3-venv ffmpeg"
            echo "Fedora:"
            cmd "sudo dnf install -y python3 ffmpeg"
            ;;
        *)
            echo "Установите Python 3.9+ и ffmpeg / Install Python 3.9+ and ffmpeg."
            ;;
    esac
    echo
    bold "Потом снова / Then run again:"
    cmd "./run.sh"
    exit 1
fi

if ! .venv/bin/python -c 'import paho.mqtt' 2>/dev/null; then
    echo "Первый запуск: ставлю зависимости / First run: installing dependencies…"
    "$PY" -m venv .venv || {
        echo "Не удалось создать .venv / Could not create .venv. Debian/Ubuntu:"
        cmd "sudo apt install -y python3-venv"
        exit 1
    }
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
fi

exec .venv/bin/python server.py
