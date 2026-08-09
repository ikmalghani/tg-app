import os
from functools import lru_cache


def _load_env_file(env_path: str) -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _load_channels():
    channels = []
    no_of_channel_raw = os.getenv("NOOFCHANNEL", "").strip()
    if no_of_channel_raw:
        try:
            count = int(no_of_channel_raw)
        except ValueError:
            count = 0
        for i in range(1, count + 1):
            name = os.getenv(f"CHANNELNAME{i}", "").strip()
            chat_id = os.getenv(f"CHANNELID{i}", "").strip()
            if name and chat_id:
                channels.append({"name": name, "id": chat_id})

    if not channels:
        channels_raw = os.getenv("CHANNELS", "").strip()
        if channels_raw:
            for item in channels_raw.split(";"):
                item = item.strip()
                if "|" not in item:
                    continue
                name, chat_id = item.split("|", 1)
                name, chat_id = name.strip(), chat_id.strip()
                if name and chat_id:
                    channels.append({"name": name, "id": chat_id})

    if not channels:
        channels = [
            {"name": "Our Lady of The Sea", "id": "-1001783837645"},
            {"name": "Sun God Nika", "id": "-1001958464364"},
        ]
    return channels


@lru_cache(maxsize=1)
def get_settings():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env_file(os.path.join(base_dir, ".env"))

    data_dir = os.getenv("DATA_DIR", os.path.join(base_dir, "data"))
    os.makedirs(data_dir, exist_ok=True)
    for sub in ("uploads", "downloads", "jobs"):
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)

    crypt_path = os.getenv("CRYPT_CONFIG", os.path.join(base_dir, "crypt.conf"))
    tg_upload_dir = os.getenv(
        "TG_UPLOAD_DIR",
        os.path.join(os.path.dirname(base_dir), "tg-upload"),
    )
    if not os.path.isdir(tg_upload_dir):
        # Docker image layout
        alt = os.path.join(base_dir, "tg-upload")
        if os.path.isdir(alt):
            tg_upload_dir = alt

    try:
        disk_reserve = int(os.getenv("DISK_RESERVE_BYTES", str(2 * 1024**3)))
    except ValueError:
        disk_reserve = 2 * 1024**3

    return {
        "base_dir": base_dir,
        "data_dir": data_dir,
        "uploads_dir": os.path.join(data_dir, "uploads"),
        "downloads_dir": os.path.join(data_dir, "downloads"),
        "jobs_dir": os.path.join(data_dir, "jobs"),
        "crypt_config": crypt_path,
        "tg_upload_dir": tg_upload_dir,
        "api_id": os.getenv("API_ID", "").strip(),
        "api_hash": os.getenv("API_HASH", "").strip(),
        "bot_token": os.getenv("BOT_TOKEN", "").strip(),
        "disk_reserve_bytes": disk_reserve,
        "channels": _load_channels(),
        "channel_map": {c["name"]: c["id"] for c in _load_channels()},
    }
