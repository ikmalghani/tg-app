import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

from .config import get_settings

ProgressCallback = Callable[[str, float, str], None]


def _log(message: str) -> None:
    print(message, flush=True)


def run_subprocess(command_args, working_directory=None, on_progress: Optional[ProgressCallback] = None):
    process = subprocess.Popen(
        command_args,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    output_queue: queue.Queue = queue.Queue()
    collected_output = []
    progress_prefixes = ("UP:", "DL:", "SPLIT:", "COMBINE:", "Calculating ")

    def enqueue_output():
        buffer = bytearray()
        try:
            while True:
                char = process.stdout.read(1)
                if char == b"":
                    break
                if char in (b"\r", b"\n"):
                    if buffer:
                        output_queue.put(
                            (
                                buffer.decode("utf-8", errors="replace"),
                                char.decode("ascii", errors="ignore"),
                            )
                        )
                        buffer.clear()
                else:
                    buffer.extend(char)
            if buffer:
                output_queue.put((buffer.decode("utf-8", errors="replace"), "\n"))
        finally:
            process.stdout.close()
            output_queue.put(None)

    reader_thread = threading.Thread(target=enqueue_output, daemon=True)
    reader_thread.start()

    output_done = False
    while not output_done or process.poll() is None:
        try:
            item = output_queue.get(timeout=0.1)
            if item is None:
                output_done = True
            else:
                line, separator = item
                if not line:
                    continue
                collected_output.append(line)
                if separator == "\r" and line.startswith(progress_prefixes):
                    if on_progress:
                        pct = _parse_percent(line)
                        on_progress("transfer", pct, line)
                    sys.stdout.write(f"\r{line}")
                    sys.stdout.flush()
                else:
                    _log(line)
                    if on_progress:
                        on_progress("log", -1, line)
        except queue.Empty:
            pass

    returncode = process.wait()
    return subprocess.CompletedProcess(
        command_args,
        returncode,
        stdout="\n".join(collected_output),
        stderr="",
    )


def _parse_percent(line: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
    if match:
        try:
            return max(0.0, min(100.0, float(match.group(1))))
        except ValueError:
            return -1
    return -1


def run_subprocess_passthrough(command_args, working_directory=None):
    process = subprocess.Popen(command_args, cwd=working_directory)
    process.wait()
    return subprocess.CompletedProcess(command_args, process.returncode, stdout="", stderr="")


def get_tg_upload_python():
    settings = get_settings()
    upload_dir = settings["tg_upload_dir"]
    venv_python = os.path.join(upload_dir, "venv", "bin", "python")
    if os.path.exists(venv_python):
        return venv_python
    return sys.executable


def run_tg_upload(arguments, on_progress: Optional[ProgressCallback] = None):
    settings = get_settings()
    command_args = [get_tg_upload_python(), "-u", "tg-upload.py", *arguments]
    cmd_str = " ".join(command_args)
    _log(f"Executing command: {cmd_str}")
    if on_progress:
        on_progress("command", -1, cmd_str)
    result = run_subprocess(
        command_args,
        working_directory=settings["tg_upload_dir"],
        on_progress=on_progress,
    )
    if result.returncode != 0:
        stderr = result.stdout.strip() if result.stdout else "tg-upload command failed."
        raise RuntimeError(stderr)
    return result


def encrypt_decrypt(is_encrypt, file_or_folders, config_file_path, on_progress: Optional[ProgressCallback] = None):
    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"Config file not found at {config_file_path}")
    if not _crypt_passwords_configured(config_file_path):
        raise RuntimeError(
            "crypt.conf has empty passwords — paste/upload a real rclone crypt config in Crypt Config"
        )

    settings = get_settings()
    for file_or_folder in file_or_folders:
        if not file_or_folder:
            continue
        normalized_path = os.path.abspath(file_or_folder)
        file_directory = os.path.dirname(normalized_path) or settings["data_dir"]
        basename = os.path.basename(normalized_path)

        if os.path.isfile(normalized_path):
            if is_encrypt:
                if basename.lower().endswith(".bin"):
                    _log(f"SKIPPED: {basename} - already encrypted")
                    continue
                command_args = [
                    "rclone", "--config", config_file_path,
                    "move", normalized_path, "crypt:.",
                    "--progress", "--stats-one-line",
                ]
            else:
                remote_name = basename[:-4] if basename.lower().endswith(".bin") else basename
                command_args = [
                    "rclone", "--config", config_file_path,
                    "move", f"crypt:{remote_name}", ".",
                    "--progress", "--stats-one-line",
                ]
        else:
            raise FileNotFoundError(f"Invalid file path: {file_or_folder}")

        result = run_subprocess(
            command_args,
            working_directory=file_directory,
            on_progress=on_progress,
        )
        if result.returncode != 0:
            action = "encrypt" if is_encrypt else "decrypt"
            detail = (result.stdout or "").strip() or "rclone failed"
            raise RuntimeError(f"Failed to {action} {basename}: {detail[-800:]}")
        time.sleep(0.5)


def _crypt_passwords_configured(config_file_path: str) -> bool:
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    if "[crypt]" not in text:
        return False
    values = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in ("password", "password2"):
            values.append(value.strip().strip('"').strip("'"))
    return len(values) >= 2 and all(values)


def encrypt_file_for_upload(file_path, config_file_path):
    encrypt_decrypt(True, [file_path], config_file_path)
    encrypted_path = f"{file_path}.bin"
    if not os.path.exists(encrypted_path):
        raise FileNotFoundError(f"Encrypted file not found: {encrypted_path}")
    return encrypted_path


def decrypt_files_in_directory(directory, config_file_path, on_progress: Optional[ProgressCallback] = None):
    for entry in sorted(os.listdir(directory)):
        file_path = os.path.join(directory, entry)
        if os.path.isfile(file_path) and entry.lower().endswith(".bin"):
            encrypt_decrypt(False, [file_path], config_file_path, on_progress=on_progress)


def split_file(file_path, split_size=1500 * 1024 * 1024):
    file_size = os.path.getsize(file_path)
    num_parts = -(-file_size // split_size)
    part_files = []
    with open(file_path, "rb") as f:
        for i in range(num_parts):
            part_file_name = f"{file_path}.part{i:02d}"
            with open(part_file_name, "wb") as part_file:
                part_file.write(f.read(split_size))
            part_files.append(part_file_name)
    return part_files


def parse_telegram_link(link):
    link = link.strip().replace(" ", "")
    if not link.startswith(("https://", "http://")):
        return None, None
    parts = link.split("/")
    try:
        if "t.me" in parts or "telegram.me" in parts:
            domain_idx = parts.index("t.me") if "t.me" in parts else parts.index("telegram.me")
            if domain_idx + 1 < len(parts) and parts[domain_idx + 1] == "c":
                chat_id = int(f"-100{parts[domain_idx + 2]}")
                msg_id = int(parts[domain_idx + 3])
            else:
                chat_id = parts[domain_idx + 1]
                msg_id = int(parts[domain_idx + 2])
            return chat_id, msg_id
    except (ValueError, IndexError):
        return None, None
    return None, None


def get_pyrogram_client():
    try:
        import pyrogram
    except Exception as exc:
        raise RuntimeError("pyrogram is not installed") from exc
    return pyrogram.Client


# Separate from tg-upload's profile.session so browse does not lock download jobs.
_browser_lock = threading.Lock()


def normalize_chat_id(chat_id):
    text = str(chat_id).strip()
    if not text:
        raise ValueError("chat_id is empty")
    try:
        return int(text)
    except ValueError:
        return text


def chat_msg_to_link(chat_id, msg_id) -> str:
    """Build a private-channel style t.me link from chat_id + message id."""
    cid = str(chat_id).strip()
    mid = int(msg_id)
    if cid.startswith("-100") and cid[4:].isdigit():
        return f"https://t.me/c/{cid[4:]}/{mid}"
    if cid.lstrip("-").isdigit():
        # Already a bare numeric id without -100 prefix
        bare = cid.lstrip("-")
        return f"https://t.me/c/{bare}/{mid}"
    # Public username
    return f"https://t.me/{cid.lstrip('@')}/{mid}"


def links_from_msg_ids(chat_id, msg_ids: list[int]) -> list[str]:
    return [chat_msg_to_link(chat_id, mid) for mid in msg_ids]


def _media_from_message(message):
    """Return (size, telegram_file_name) for downloadable media, or None."""
    if not message or getattr(message, "empty", False):
        return None
    for attr in ("document", "video", "audio", "animation", "voice", "video_note"):
        media = getattr(message, attr, None)
        if media is not None:
            size = int(getattr(media, "file_size", 0) or 0)
            name = (getattr(media, "file_name", None) or "").strip()
            return size, name
    return None


def _format_message_item(message, chat_id) -> Optional[dict]:
    media = _media_from_message(message)
    if media is None:
        return None
    size, file_name = media
    caption = (message.caption or "").strip()
    # Caption is the true name for this app; filename is fallback only.
    display = caption or file_name or f"message_{message.id}"
    date_ts = 0.0
    if message.date is not None:
        try:
            date_ts = float(message.date.timestamp())
        except Exception:
            date_ts = 0.0
    return {
        "msg_id": int(message.id),
        "caption": caption,
        "name": display,
        "file_name": file_name,
        "size": size,
        "date": date_ts,
        "link": chat_msg_to_link(chat_id, message.id),
    }


def _with_browser_client(fn):
    """
    Run fn(client) with a short-lived bot client on a dedicated session name.
    Bot token login does not need interactive Authorize.
    """
    settings = get_settings()
    if not settings["api_id"] or not settings["api_hash"] or not settings["bot_token"]:
        raise RuntimeError("API_ID, API_HASH, and BOT_TOKEN are required")

    target_directory = settings["tg_upload_dir"]
    os.makedirs(target_directory, exist_ok=True)
    Client = get_pyrogram_client()

    with _browser_lock:
        original_dir = os.getcwd()
        try:
            os.chdir(target_directory)
            client = Client(
                "browser",
                api_id=int(settings["api_id"]),
                api_hash=settings["api_hash"],
                bot_token=settings["bot_token"],
            )
            with client:
                return fn(client)
        finally:
            os.chdir(original_dir)


def list_channel_media(
    chat_id,
    query: str = "",
    offset: int = 0,
    limit: int = 40,
) -> dict:
    """
    Live-list channel media (no local index).

    - Empty query: walk chat history (newest first), media only.
      `offset` is the last seen message id (0 = start from newest).
    - Non-empty query: Telegram caption/text search.
      `offset` is how many search hits to skip.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0

    resolved_chat = normalize_chat_id(chat_id)
    q = (query or "").strip()

    def _run(client):
        items = []
        next_offset = offset
        has_more = False

        if q:
            # Fetch a wider batch so we can skip non-media search hits.
            fetch_n = min(200, max(limit * 4, limit))
            batch = list(
                client.search_messages(
                    resolved_chat,
                    query=q,
                    limit=fetch_n,
                    offset=offset,
                )
            )
            scanned = 0
            for msg in batch:
                scanned += 1
                info = _format_message_item(msg, resolved_chat)
                if info:
                    items.append(info)
                    if len(items) >= limit:
                        break
            next_offset = offset + scanned
            has_more = scanned >= fetch_n or len(items) >= limit
            mode = "search"
        else:
            last_id = offset
            scanned = 0
            max_scan = limit * 15
            while len(items) < limit and scanned < max_scan:
                chunk_limit = min(100, max(30, (limit - len(items)) * 5))
                chunk = list(
                    client.get_chat_history(
                        resolved_chat,
                        limit=chunk_limit,
                        offset_id=last_id or 0,
                    )
                )
                if not chunk:
                    break
                for msg in chunk:
                    last_id = int(msg.id)
                    scanned += 1
                    info = _format_message_item(msg, resolved_chat)
                    if info:
                        items.append(info)
                        if len(items) >= limit:
                            break
                if len(chunk) < chunk_limit:
                    break
            next_offset = last_id
            has_more = len(items) >= limit
            mode = "history"

        return {
            "items": items,
            "mode": mode,
            "query": q,
            "offset": offset,
            "next_offset": next_offset,
            "has_more": has_more,
            "chat_id": str(resolved_chat),
        }

    try:
        return _with_browser_client(_run)
    except Exception as exc:
        raise RuntimeError(f"Failed to list channel media: {exc}") from exc


def get_caption_from_link(link):
    settings = get_settings()
    chat_id, msg_id = parse_telegram_link(link)
    if not chat_id or not msg_id:
        return None

    target_directory = settings["tg_upload_dir"]
    session_file = os.path.join(target_directory, "profile.session")
    if not os.path.exists(session_file):
        return None

    original_dir = os.getcwd()
    try:
        os.chdir(target_directory)
        Client = get_pyrogram_client()
        client = Client(
            "profile",
            api_id=settings["api_id"],
            api_hash=settings["api_hash"],
            bot_token=settings["bot_token"],
        )
        with client:
            message = client.get_messages(chat_id, msg_id)
            if message and message.caption:
                caption = message.caption.strip()
                if ".part" in caption:
                    caption = caption.split(".part")[0]
                if not caption.endswith(".bin"):
                    caption = caption + ".bin"
                return caption
            if message and message.document and message.document.file_name:
                filename = message.document.file_name
                if ".part" in filename:
                    filename = filename.split(".part")[0]
                if not filename.endswith(".bin"):
                    filename = filename + ".bin"
                return filename
    except Exception as exc:
        _log(f"Error fetching caption: {exc}")
        return None
    finally:
        os.chdir(original_dir)
    return None


def combine_files(directory, links_list):
    if not directory or not os.path.exists(directory) or not links_list:
        return

    downloaded_names = []
    seen = set()
    for link in links_list:
        caption = get_caption_from_link(link)
        if not caption:
            continue
        if not caption.endswith(".bin"):
            caption += ".bin"
        if caption not in seen:
            downloaded_names.append(caption)
            seen.add(caption)

    file_names = [
        name for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
    ]

    for downloaded_name in downloaded_names:
        part_pattern = re.compile(rf"^{re.escape(downloaded_name)}\.part(\d+)$")
        part_files = []
        for file_name in file_names:
            match = part_pattern.fullmatch(file_name)
            if match:
                part_files.append((int(match.group(1)), file_name))
        if len(part_files) <= 1:
            continue
        part_files.sort(key=lambda item: item[0])
        combined_file = os.path.join(directory, downloaded_name)
        with open(combined_file, "wb") as combined:
            for _n, part_file in part_files:
                part_path = os.path.join(directory, part_file)
                with open(part_path, "rb") as pf:
                    shutil.copyfileobj(pf, combined)
                os.remove(part_path)


def authorize(on_progress: Optional[ProgressCallback] = None):
    settings = get_settings()
    run_tg_upload(
        [
            "--profile", "profile",
            "--api_id", settings["api_id"],
            "--api_hash", settings["api_hash"],
            "--bot", settings["bot_token"],
            "--login_only",
        ],
        on_progress=on_progress,
    )


def process_upload_file(
    file_path: str,
    chat_id: str,
    encrypt: bool,
    split: bool,
    delete_on_done: bool,
    on_progress: Optional[ProgressCallback] = None,
):
    settings = get_settings()
    config_file_path = settings["crypt_config"]
    min_split_size = int(1.9 * 1024 * 1024 * 1024)
    current_file = file_path
    files_to_upload = []
    files_to_delete = set()

    if delete_on_done:
        files_to_delete.add(file_path)

    if on_progress:
        on_progress("stage", 5, "Preparing…")

    if encrypt:
        if not os.path.exists(config_file_path):
            raise FileNotFoundError(f"crypt.conf not found: {config_file_path}")
        if not os.path.basename(current_file).lower().endswith(".bin"):
            if on_progress:
                on_progress("stage", 15, "Encrypting…")
            current_file = encrypt_file_for_upload(current_file, config_file_path)
            if delete_on_done:
                files_to_delete.add(current_file)

    if ".part" in os.path.basename(current_file):
        files_to_upload.append(current_file)
    elif split:
        file_size = os.path.getsize(current_file)
        if file_size > min_split_size:
            if on_progress:
                on_progress("stage", 35, "Splitting…")
            part_files = split_file(current_file)
            files_to_upload.extend(part_files)
            if delete_on_done:
                files_to_delete.update(part_files)
                files_to_delete.add(current_file)
        else:
            files_to_upload.append(current_file)
            if delete_on_done:
                files_to_delete.add(current_file)
    else:
        files_to_upload.append(current_file)
        if delete_on_done:
            files_to_delete.add(current_file)

    total_parts = max(len(files_to_upload), 1)
    for idx, upload_path in enumerate(files_to_upload):
        filename = os.path.basename(upload_path)
        base_pct = 40 + int(50 * idx / total_parts)

        def _cb(kind, pct, msg, _base=base_pct, _idx=idx):
            if on_progress:
                mapped = _base
                if pct >= 0:
                    mapped = _base + int((pct / 100.0) * (50 / total_parts))
                on_progress(kind, mapped, f"[{_idx + 1}/{total_parts}] {msg}")

        if on_progress:
            on_progress("stage", base_pct, f"Uploading {filename}…")
        run_tg_upload(
            [
                "--profile", "profile",
                "--path", upload_path,
                "--chat_id", str(chat_id),
                "--caption", filename,
            ],
            on_progress=_cb,
        )

    if delete_on_done:
        for path in sorted(files_to_delete):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    if on_progress:
        on_progress("stage", 100, "Uploaded to Telegram")


def rename_files_with_captions(directory, links_list):
    """Rename downloaded files to use their captions from Telegram messages."""
    if not links_list or not directory or not os.path.exists(directory):
        return

    link_to_caption = {}
    for link in links_list:
        caption = get_caption_from_link(link)
        if caption:
            link_to_caption[link] = caption
    if not link_to_caption:
        _log("SKIPPED: Caption rename - could not resolve captions from links")
        return

    files = [
        name for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
    ]
    used_captions = set()
    for file_name in files:
        if ".part" in file_name:
            continue
        file_path = os.path.join(directory, file_name)
        for _link, caption in link_to_caption.items():
            if caption in used_captions:
                continue
            caption_base = caption.replace(".bin", "").lower().replace("_", ".").replace("-", ".")
            file_base = file_name.replace(".bin", "").lower().replace("_", ".").replace("-", ".")
            matched = False
            if file_base == caption_base:
                matched = True
            elif file_base in caption_base or caption_base in file_base:
                if min(len(file_base), len(caption_base)) >= 10:
                    matched = True
            elif len(file_base) >= 10 and len(caption_base) >= 10:
                if (
                    file_base[:10] in caption_base
                    or caption_base[:10] in file_base
                    or file_base[-10:] in caption_base
                    or caption_base[-10:] in file_base
                ):
                    matched = True
            if matched:
                new_file_path = os.path.join(directory, caption)
                if file_path != new_file_path and not os.path.exists(new_file_path):
                    try:
                        os.rename(file_path, new_file_path)
                        used_captions.add(caption)
                        _log(f"Renamed: {file_name} -> {caption}")
                    except OSError as exc:
                        _log(f"Error renaming {file_name} to {caption}: {exc}")
                elif file_path == new_file_path:
                    used_captions.add(caption)
                break


def _unique_dest(directory: str, filename: str) -> str:
    dest = os.path.join(directory, filename)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(filename)
    for i in range(1, 1000):
        candidate = os.path.join(directory, f"{stem}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, f"{stem}_{os.urandom(3).hex()}{ext}")


def process_download(
    links: list[str],
    chat_id: str,
    combine: bool,
    decrypt: bool,
    on_progress: Optional[ProgressCallback] = None,
):
    """
    Download into a temp work dir, post-process, then move finished files
    flat into /data/downloads (e.g. /data/downloads/video.mkv).
    """
    settings = get_settings()
    downloads_dir = settings["downloads_dir"]
    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(settings["jobs_dir"], exist_ok=True)

    work_dir = tempfile.mkdtemp(prefix="dl_", dir=settings["jobs_dir"])
    temp_file = None

    if on_progress:
        on_progress("stage", 5, "Starting Telegram download…")

    command_args = [
        "--profile", "profile",
        "--chat_id", str(chat_id),
        "--dl_dir", work_dir,
    ]
    if len(links) == 1:
        command_args.extend(["--dl", "--links", links[0]])
    else:
        temp_file = tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt", prefix="tg_links_", dir=settings["jobs_dir"]
        )
        temp_file.write("\n".join(links))
        temp_file.close()
        command_args.extend(["--dl", "--txt_file", temp_file.name])

    try:
        try:
            run_tg_upload(command_args, on_progress=on_progress)
        finally:
            if temp_file and os.path.exists(temp_file.name):
                os.remove(temp_file.name)

        if on_progress:
            on_progress("stage", 70, "Post-processing…")

        if combine:
            combine_files(work_dir, links)

        # Always rename by caption (covers single-file downloads when combine finds no parts)
        rename_files_with_captions(work_dir, links)

        if decrypt:
            config_file_path = settings["crypt_config"]
            if not os.path.exists(config_file_path):
                raise FileNotFoundError(f"crypt.conf not found: {config_file_path}")
            if not _crypt_passwords_configured(config_file_path):
                raise RuntimeError(
                    "crypt.conf has empty passwords — paste/upload a real rclone crypt config first"
                )
            if on_progress:
                on_progress("stage", 85, "Decrypting…")
            decrypt_files_in_directory(work_dir, config_file_path, on_progress=on_progress)

        result_files = []
        for name in sorted(os.listdir(work_dir)):
            src = os.path.join(work_dir, name)
            if not os.path.isfile(src):
                continue
            dest = _unique_dest(downloads_dir, name)
            shutil.move(src, dest)
            result_files.append(dest)
            _log(f"Saved: {dest}")

        if on_progress:
            on_progress("stage", 100, f"Ready ({len(result_files)} file(s))")
        return result_files
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
