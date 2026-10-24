#!/bin/sh
# H2S Mission Control (macOS / Linux): checks prerequisites, prepares .venv, starts the server.
cd "$(dirname "$0")" || exit 1
# A standalone ffmpeg can live in ./bin (no Homebrew needed); server.py looks there too.
PATH="$PWD/bin:$PATH"

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
            pkgs=""
            case "$missing" in *python*) pkgs="$pkgs python" ;; esac
            case "$missing" in *ffmpeg*) pkgs="$pkgs ffmpeg" ;; esac
            # Apple Silicon installs to /opt/homebrew, Intel to /usr/local; neither is always on PATH.
            prefix=/usr/local
            [ "$(uname -m)" = arm64 ] && prefix=/opt/homebrew
            shellenv="echo 'eval \"\$($prefix/bin/brew shellenv)\"' >> ~/.zprofile && eval \"\$($prefix/bin/brew shellenv)\""
            if command -v brew >/dev/null 2>&1; then
                cmd "brew install$pkgs"
            elif [ -x "$prefix/bin/brew" ]; then
                echo "Homebrew установлен, но не добавлен в PATH / Homebrew is installed but not on PATH:"
                cmd "$shellenv"
                cmd "brew install$pkgs"
            else
                echo "1. Homebrew (спросит пароль от Mac / asks for your Mac password):"
                cmd '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
                echo "2. Добавить brew в PATH / Put brew on PATH:"
                cmd "$shellenv"
                echo "3. Затем / Then:"
                cmd "brew install$pkgs"
                echo
                echo "Без Homebrew / Without Homebrew:"
                case "$missing" in *python*) echo "  python: https://www.python.org/downloads/macos/  (или / or: xcode-select --install)" ;; esac
                case "$missing" in *ffmpeg*)
                    arch=amd64; [ "$(uname -m)" = arm64 ] && arch=arm64
                    echo "  ffmpeg в папку проекта / into the project folder:"
                    cmd "mkdir -p bin && curl -L https://ffmpeg.martin-riedl.de/redirect/latest/macos/$arch/release/ffmpeg.zip -o /tmp/ffmpeg.zip && unzip -o /tmp/ffmpeg.zip -d bin && rm /tmp/ffmpeg.zip" ;;
                esac
            fi
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
