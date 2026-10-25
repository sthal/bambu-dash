# Mission Control for Bambu Lab

A local dashboard for Bambu Lab 3D printers, styled like a mission control center. Live camera in native quality, progress, temperatures, AMS, print history and its cost, all on one screen, for one or several printers. Runs on your home network only; the Bambu cloud is not needed. Built and tested on an H2S with AMS 2 Pro and AMS HT. The interface is in English and Russian.

![](https://img.shields.io/badge/python-3.9+-blue) ![](https://img.shields.io/badge/deps-paho--mqtt%20%2B%20ffmpeg-violet) ![](https://img.shields.io/badge/cloud-not%20needed-3dffaa)

<p align="center"><img src="docs/hero.png" alt="The dashboard during a print on a computer and a phone" width="100%"></p>

## Features

<table>
<tr>
<td width="290" valign="top"><img src="docs/phone.png" alt="The dashboard on a phone" width="280"></td>
<td valign="top">

**Camera**
- **Native quality**: 1680×1080, 30 fps, no re-encoding, about 250 KB/s. The real resolution and FPS are shown in the corner of the video.
- **Zoom and pan**: mouse wheel or two-finger pinch, drag, double-click. Handy for inspecting the first layer.
- **Model over the camera**: the model's silhouette fills in as it prints, a line marks the current layer.
- **Broadcast to YouTube, Twitch, Kick, Facebook** or any RTMP server, several at once. Either with the dashboard overlay on the video (percent, job, layer, time left, temperatures; 1920×1080 frame, hardware encoding on a Mac) or as clean video without re-encoding. Button under the video, keys in the settings.
- **Snapshot in the dashboard style**: a full-resolution frame with borders, percent, job name, layer, progress bar and model silhouette. Plus **full screen**. If the browser can't play the newer video format, a fallback stream turns on by itself.

**Printing**
- **Mission**: progress ring, layer scale, time left, finish time, speed.
- **Payload**: model preview and filament use by color from the job file.
- **Temperatures** of nozzle, bed and chamber: current and target, 30-minute charts.
- **AMS**: spools in their real color with remaining amount, the active one highlighted; humidity and temperature of each unit.
- **Console**: speed, light, pause and resume, stop (hold for 2 seconds), fans, AI print monitoring status.
- **Temperatures, fans and AMS drying** from the console, with limits enforced on the server. Turned on in the settings if Developer Mode is on at the printer (see "Limitations").

**History and money**
- **Print log**: every print with a final snapshot, duration, grams and cost split into filament, electricity and total. Survives a server restart in the middle of a print.
- **Print report** (click a log row): 3D view of the real G-code toolpaths with a "printed up to layer N" slider and colors by line type, the final photo in the dashboard style, plate preview, temperature and progress charts for the whole print, event feed, cost. Download the photo and the 3D frame.
- **Statistics** for a week, month, year or all time: prints, hours, filament, kWh, cost, success rate; charts by day and by material.
- **Cost**: filament prices per material and day/night electricity rates in the settings, 180 W average power by default. Live estimate for the current print.
- **Spool forecast**: whether the filament in the AMS slot will last until the end of the print, with an early warning.
- **Spool inventory** (`/inventory`): every original Bambu spool that has ever been in the AMS, with an RFID number column, remaining amount, location (AMS slot or "on shelf"), amount printed and the print history of each. The remainder is tracked by RFID with our own accounting, not only by the AMS estimate (which resets when a spool is reinserted). You can enter the exact weight after weighing, mark a spool "used up", add a note; filters and search.

**Errors and events**
- **Errors in plain text**: HMS and print error codes explained from Bambu's official database, in English and Russian.
- **Sound**: a tune when a print finishes, signals on pause and error.
- **Browser notifications** with a camera snapshot.
- **Logbook**: state and stage changes, errors, commands sent.

**Convenience**
- **Phone**: the page adapts to a narrow screen and installs to the home screen as an app with its own icon.
- **Kiosk mode** for a wall tablet or a TV: full-screen camera, large numbers, the screen stays on.
- **Console password** (optional): anyone can watch, only signed-in users can control.
- **Autostart** at login, restarts itself after a crash.
- **Setup in the browser**: finds printers on the network, tests the connection step by step.
- **Several printers**: a switcher in the header with each printer's state; each has its own camera and log, history and statistics are shared.
- **Home screen** in one tap: the icon to the left of the printer name. The browser's and the mouse's Back button also always return to the home screen.
- **EN / RU** switch in the header. English is the default.

</td>
</tr>
</table>

## Installation

**macOS / Linux** (Terminal):

```sh
git clone https://github.com/sthal/bambu-dash.git && cd bambu-dash
./run.sh
```

**Windows** (PowerShell or cmd):

```bat
git clone https://github.com/sthal/bambu-dash.git && cd bambu-dash
run.bat
```

`git clone` by itself installs and runs nothing: Git does not allow that. `run.sh` / `run.bat` does everything:

- **Checks for Python 3.9+ and ffmpeg.** If something is missing, it prints the exact install commands for your system and stops:

  | System | Commands |
  |---|---|
  | macOS | `brew install python ffmpeg`. If Homebrew is missing, the script shows how to install it and add it to PATH, and also a way without Homebrew: download ffmpeg with one command into the project's `bin/` folder |
  | Windows | `winget install -e --id Python.Python.3.12`<br>`winget install -e --id Gyan.FFmpeg` |
  | Debian / Ubuntu | `sudo apt install -y python3 python3-venv ffmpeg` |

  After installing, run the script again. On Windows, open a **new** terminal window first so the PATH is picked up.
- **On the first run** it creates `.venv` and installs the dependencies from `requirements.txt` (only `paho-mqtt`).
- **Starts the server.** Then open `http://localhost:8790`.

If `git` is not installed: macOS `xcode-select --install`, Windows `winget install -e --id Git.Git`. You can also do without it: download the ZIP from the repository page.

> Tested on macOS. The Windows scripts are written but have not yet been run on a real Windows machine. If something is wrong, please open an issue.

### First setup

On the first run the **setup page** (`/setup`) opens:

1. **"Scan network"** finds Bambu Lab printers on your subnet and fills in the IP and serial number.
2. Enter the **Access Code**: printer screen → Settings → Network (WLAN).
3. **"Test connection"** shows step by step what is wrong: the printer doesn't answer, the code is wrong, or no status arrives.
4. **"Save and connect"** writes `config.json` (permissions `600`) and connects without a restart.

More printers are added in the same place: "My printers" → "Add printer". An old single-printer `config.json` is converted to the new format automatically.

For video, the **LAN Mode Liveview** option must be on at the printer. After that, the settings open from the gear in the header. You can also set a console password there. The port is changed in the settings or with the variable `PORT=8800 ./run.sh`.

### Broadcasting

Keys are entered in the settings, "Broadcasting" section; they are stored only in `config.json`. The **overlay on the broadcast** needs ffmpeg with the `drawtext` filter. The Homebrew build doesn't include it, so put a full build into the project folder (the server picks it up automatically):

```sh
mkdir -p bin && curl -L https://ffmpeg.martin-riedl.de/redirect/latest/macos/$( [ "$(uname -m)" = arm64 ] && echo arm64 || echo amd64 )/release/ffmpeg.zip -o /tmp/ffmpeg.zip && unzip -o /tmp/ffmpeg.zip -d bin && rm /tmp/ffmpeg.zip
```

Without it, the broadcast goes out as clean video. YouTube and other services require an audio track, so silence is added to the stream.

### Backup and moving

Settings → "Backup":

- **"Download backup"**: a single `.zip` with printers, prices, password, broadcasts, print history (with 3D and charts) and the spool inventory. By default it includes access codes and broadcast keys (keep the file like a password); the checkbox can be cleared.
- **"Restore from file"**: checks the file and the database integrity, saves a safety copy of the current state and restarts the server with the restored data. If the backup has no codes but this computer already has them, they are kept.
- **Automatic copies** once a day (last 7) and before every restore (last 5) are kept in `state/backups/`; any of them can be downloaded or restored with a button.

**Moving to a new computer:** install as described above, open `http://localhost:8790` and press "Restore from file" right on the setup page.

### iPhone app

The `ios/` folder holds the native "Mission" app: a full-screen dashboard (video inside, the server address is remembered), **Live Activity on the lock screen and in the Dynamic Island** with a countdown to the end of the print, **widgets** for the home and lock screens (progress, finish time, camera snapshot), and "done / failed / paused / error" notifications.

Building and installing with a free Apple ID (you need Xcode from the App Store and an iPhone with "Developer Mode" on):

```sh
cd ios && ./build.sh          # generate the project (XcodeGen) and open it in Xcode
cd ios && ./build.sh install  # build and install on an iPhone connected by cable
```

With a free signature the app works for 7 days; then install it again with the same command. Limits of a free signature: no push notifications from the server, so the exact percent in the Live Activity updates when the app is open or iOS runs it in the background (the countdown and the bar to the finish run on their own); the widget keeps its own dashboard address (long-press → "Edit Widget"). The app follows the iPhone's language: Russian on a Russian iPhone, English otherwise.

### Autostart

- **macOS:** `./autostart.sh` (remove: `./autostart.sh --remove`). The log goes to `server.log`. On the first run macOS may ask to allow Python to access the local network; allow it.
- **Windows:** `powershell -ExecutionPolicy Bypass -File autostart.ps1` (remove: add `-Remove`). Not yet tested on a real Windows machine.

### Phone, tablet, TV

- **Phone**: open `http://<computer IP>:8790`. On iPhone in Safari: "Share" → "Add to Home Screen".
- **Kiosk**: the monitor button in the header or the address `http://<computer IP>:8790/?kiosk=1`. Exit: Esc or the same button.
- **Browser notifications** work only on `localhost` or over HTTPS (a browser restriction). They won't work from a phone at a home network address.
- **The newer video format** on iPhone needs iOS 17.1 or later; otherwise the fallback stream is used (1280 px, 15 fps).

## How it works

`server.py` is a single Python process:

| Channel | Printer port | Purpose |
|---|---|---|
| MQTT over TLS | 8883 | real-time status and commands |
| RTSPS | 322 | camera: the native H.264 is packed into fragmented MP4 without re-encoding, one shared `ffmpeg` for all viewers, with a watchdog against stalls. Fallback stream: MJPEG |
| FTPS (implicit) | 990 | the current job's `.3mf`: plate preview and `slice_info` |

The browser gets the state over SSE (`/events`) and the video over `/video.mp4` or `/stream.mjpg`. The server sends every text in both languages at once.

On macOS, programs started by autostart can't reach the local network without permission, and a console ffmpeg can't be given it. So ffmpeg connects to the server itself on `127.0.0.1`, and the server relays the bytes to the printer. Encryption stays end to end: the server decrypts nothing.

The error text database can be updated: `python3 tools/update_hms.py`.

The 3D view is built from the G-code inside the `.gcode.3mf` (`toolpath.py`): extrusion paths are packed into ~1 MB and kept in the history. The 3D model itself (STL) is not in a Bambu sliced file. For prints started before history recording, the 3D is recovered while the file is still on the printer. The 3D view loads three.js from a CDN, so it needs internet access.

## Security

- **The Access Code is stored only in `config.json` on your computer.** The file is in `.gitignore` and never gets into the repository.
- **The API never returns the code**, only a "code saved" flag.
- **A saved code is never sent to a different address.** If the IP or serial number changes in the settings, the code has to be entered again; otherwise anyone on the network could point the server at their own address and get the code.
- **Only a device with a certificate from BBL Technologies is treated as a printer**; the serial number is taken from that certificate.
- **The console password** is stored only as a hash (PBKDF2); a sign-in lasts 30 days in a cookie. After 5 wrong attempts, sign-in is blocked for 5 minutes. Changing the password signs everyone out. Forgot the password: delete the `password_hash` and `session_secret` lines from `config.json` and restart the server.

## Limitations

- **Without a password, the console and settings are open to everyone on the local network.** The code doesn't leak, but anyone can pause a print. Set a password in the settings.
- **Temperatures, fans and AMS drying need Developer Mode** on recent firmware (tested on H2S): without it the printer answers "mqtt message verify failed" and shows HMS notice 0500_0500_0001_0007. So these controls are hidden by default: they appear only if "Developer Mode is on at the printer" is checked in the printer's settings. If the printer still refuses, the dashboard shows a hint and stops sending these commands until you press "Try again". Developer Mode is turned on at the printer (Settings → Network → LAN Only + Developer Mode), but then the Bambu cloud and Handy stop working.
- **Light, pause, resume, stop and speed** are accepted without Developer Mode.
- **180 W** is an average estimate for the H2S. With a metering smart plug the numbers will be more accurate.
