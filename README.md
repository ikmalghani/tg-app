# tg-app

Tkinter desktop UI for uploading and downloading Telegram files with an included `tg-upload` backend.

## Credit

This project bundles and builds on the original `tg-upload` work by TheCaduceus:

- Original project: `https://github.com/TheCaduceus/tg-upload`

That backend repository is archived and no longer actively maintained, so this repo includes the backend source directly and applies a small compatibility update needed for current environments.

## Backend Update

The bundled [tg-upload.py](/home/malnitro5/Documents/Tools/tg-app/tg-upload/tg-upload.py) has been updated from the archived upstream version so it works without relying on the deprecated `pkg_resources` API from `setuptools`.

In practice, this means:

- you do not need to clone the backend separately
- you do not need to pin legacy `setuptools` just to run the app
- the backend can be installed cleanly in a fresh virtual environment

## Project Layout

```text
tg-app/
|_ tg-app.py
|_ run.sh
|_ run.bat
|_ crypt.conf.example (rename to crypt.conf)
|_ .env.example (rename to .env)
|_ tg-upload/
   |_ tg-upload.py
   |_ requirements.txt
   |_ README.md
   |_ caption.json
   |_ profile.session (appears after you click Authorize successfully)
   |_ proxy-sample.json
   |_ venv/
```

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in `API_ID`, `API_HASH`, and `BOT_TOKEN`.
3. Configure your channels in `.env`.
4. Copy `crypt.conf.example` to `crypt.conf` and update it for your rclone crypt remote if you want encrypt/decrypt support.
5. Create the backend virtual environment and install dependencies:

```bash
cd /path/to/tg-app/tg-upload
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

On Windows:

```bat
cd C:\Users\username\path\to\tg-app\tg-upload
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
```

Example channel config:

```ini
NOOFCHANNEL = 2
CHANNELNAME1 = "Channel One"
CHANNELID1 = "-1000000000000"
CHANNELNAME2 = "Channel Two"
CHANNELID2 = "-1000000000001"
```

## Run

Start the app with the provided launcher so the backend virtual environment is activated first.

Linux/macOS:

```bash
cd /home/malnitro5/Documents/Tools/tg-app
./run.sh
```

Windows:

```bat
cd /d C:\Users\malnitro5\Documents\Tools\tg-app
run.bat
```

## Notes

- The app reads `.env` on startup.
- "Custom Channel" is always available in the UI.
- `Authorize`, `Upload`, and `Download` use the Python interpreter inside `tg-upload/venv` when it exists.
- The app supports optional encrypt/decrypt using `crypt.conf` and `rclone`.
