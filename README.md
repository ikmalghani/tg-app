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

## UI Options

The upload and download sections include several checkboxes that are enabled by default because of the current file-handling logic.

### Download options

- `Combine`
  Combines only files that belong to the same downloaded split set, such as `file.bin.part00`, `file.bin.part01`, and `file.bin.part02`.
  It does not combine unrelated files in the folder.
  It does not touch files that already end as plain `.bin`.
  Leave this enabled for normal restores after downloading split uploads.
  Turn it off if you want to keep the `.part00`, `.part01`, `.part02` files as separate pieces.

- `Decrypt`
  Decrypts downloaded `.bin` files after the download step finishes.
  Leave this enabled if the uploaded files were encrypted with this app.
  Turn it off if you want to keep the encrypted `.bin` files, or if the files were not encrypted in the first place.

### Upload options

- `Delete on Done`
  Deletes the local source files after a successful upload.
  If encryption or splitting creates temporary/generated files, those are also cleaned up based on the current workflow.
  Leave this enabled if this app is part of a move/archive workflow.
  Turn it off if you want to keep your local originals after upload.

- `Split Files`
  Only files larger than Telegram's 2GB per-file upload limit are split.
  Files smaller than or equal to 2GB are uploaded as-is.
  If you upload multiple files together, only the ones above 2GB are split; smaller files remain untouched.
  Leave this enabled for normal uploads so oversized files are handled automatically.
  Turn it off only if you do not want the app to prepare oversized files for Telegram.

- `Encrypt`
  Encrypts files before upload and produces `.bin` output for the upload step.
  Leave this enabled if you want uploaded files stored in encrypted form.
  Turn it off if you want Telegram to receive the original unencrypted files directly.
