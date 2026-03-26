# tg-app

Simple Tkinter UI for uploading/downloading Telegram files using the `tg-upload` backend.

## Prerequisites
1. Clone the backend tool:
```bash
git clone https://github.com/TheCaduceus/tg-upload
```

2. Install its dependencies:
```bash
pip install -r requirements.txt
```

## Setup
1. Copy `.env.example` to `.env`.
2. Fill in your credentials in `.env`.
3. Configure channels in `.env`:
```ini
NOOFCHANNEL = 2
CHANNELNAME1 = "Channel One"
CHANNELID1 = "-1000000000000"
CHANNELNAME2 = "Channel Two"
CHANNELID2 = "-1000000000001"
```
4. After filling `API_ID`, `API_HASH`, and `BOT_TOKEN`, run the app and click `Authorize` once before using upload/download.

## Run
```bash
python tg-app.py
```

## Notes
- The app reads `.env` on startup.
- "Custom Channel" is always available in the UI.
- `tg-app.py` assumes the `tg-upload` repo lives in a subfolder named `tg-upload` next to `tg-app.py`.
```text
project-folder/
|_ tg-upload/
|_ tg-app.py
```
