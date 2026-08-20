import os
import queue
import re
import json
import select
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
    except ImportError as exc:
        raise RuntimeError(f"pyrogram is not installed: {exc}") from exc
    return pyrogram.Client


USER_SESSION_NAME = "user"
_user_auth_lock = threading.Lock()
_user_auth_state: dict = {
    "phone": "",
    "phone_code_hash": "",
    "pending": False,
}
# Keep-alive login subprocess (same connected Client for send_code + sign_in).
_login_proc: Optional[subprocess.Popen] = None


def normalize_chat_id(chat_id):
    text = str(chat_id).strip()
    if not text:
        raise ValueError("chat_id is empty")
    try:
        return int(text)
    except ValueError:
        return text


def chat_msg_to_link(chat_id, msg_id) -> str:
    cid = str(chat_id).strip()
    mid = int(msg_id)
    if cid.startswith("-100") and cid[4:].isdigit():
        return f"https://t.me/c/{cid[4:]}/{mid}"
    if cid.lstrip("-").isdigit():
        return f"https://t.me/c/{cid.lstrip('-')}/{mid}"
    return f"https://t.me/{cid.lstrip('@')}/{mid}"


def links_from_msg_ids(chat_id, msg_ids: list[int]) -> list[str]:
    return [chat_msg_to_link(chat_id, mid) for mid in msg_ids]


def user_session_paths() -> list[str]:
    settings = get_settings()
    return [
        os.path.join(settings["tg_upload_dir"], f"{USER_SESSION_NAME}.session"),
        os.path.join(settings["data_dir"], f"{USER_SESSION_NAME}.session"),
    ]


def user_session_present() -> bool:
    return any(os.path.isfile(p) for p in user_session_paths())


def _auth_lock_path() -> str:
    """Marker so entrypoint won't copy user.session while login is in progress."""
    return os.path.join(get_settings()["data_dir"], "user_auth.lock")


def _set_auth_lock(active: bool) -> None:
    path = _auth_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if active:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
                f.write("\n")
        except OSError as exc:
            _log(f"WARN: could not write auth lock: {exc}")
    else:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _delete_user_session_files() -> None:
    """Remove incomplete user sessions so send_code starts clean."""
    settings = get_settings()
    for folder in (settings["tg_upload_dir"], settings["data_dir"]):
        for name in (f"{USER_SESSION_NAME}.session", f"{USER_SESSION_NAME}.session-journal"):
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                _log(f"WARN: could not remove {path}: {exc}")


def _persist_user_session() -> None:
    settings = get_settings()
    src_dir = settings["tg_upload_dir"]
    data_dir = settings["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    for name in (f"{USER_SESSION_NAME}.session", f"{USER_SESSION_NAME}.session-journal"):
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(data_dir, name))
            except OSError as exc:
                _log(f"WARN: could not persist {name}: {exc}")


def _restore_user_session(*, only_if_missing: bool = True) -> None:
    """Copy user.session from /data into tg-upload workdir."""
    settings = get_settings()
    src_dir = settings["data_dir"]
    dest_dir = settings["tg_upload_dir"]
    os.makedirs(dest_dir, exist_ok=True)
    for name in (f"{USER_SESSION_NAME}.session", f"{USER_SESSION_NAME}.session-journal"):
        src = os.path.join(src_dir, name)
        dest = os.path.join(dest_dir, name)
        if not os.path.isfile(src):
            continue
        if only_if_missing and os.path.isfile(dest):
            continue
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            _log(f"WARN: could not restore {name}: {exc}")


def _stop_login_proc() -> None:
    global _login_proc
    proc = _login_proc
    _login_proc = None
    if not proc:
        return
    try:
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    except Exception as exc:
        _log(f"WARN: login process cleanup: {exc}")
    _set_auth_lock(False)


def _read_login_json(proc: subprocess.Popen, *, timeout: float) -> dict:
    """Read one JSON line from the login subprocess stdout (with real timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.stdout is None:
            break
        remaining = max(0.0, deadline - time.time())
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            _log(f"WARN: login non-JSON line: {text[:200]}")
            continue
        if isinstance(payload, dict):
            return payload

    raise RuntimeError(f"Login process produced no response (exit={proc.poll()})")


def user_auth_send_code(phone: str) -> dict:
    """Step 1: start keep-alive login process and send the Telegram code."""
    global _login_proc
    phone = (phone or "").strip().replace(" ", "")
    if not phone:
        raise ValueError("Phone number is required (E.164, e.g. +60123456789)")
    if not phone.startswith("+"):
        raise ValueError("Phone must include country code, e.g. +60123456789")

    settings = get_settings()
    if not settings["api_id"] or not settings["api_hash"]:
        raise RuntimeError("API_ID and API_HASH are required for user login")

    with _user_auth_lock:
        _stop_login_proc()
        # Stale sessions + mid-copy races were causing false PhoneCodeExpired.
        _delete_user_session_files()
        _set_auth_lock(True)

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_auth_cli.py")
        workdir = settings["tg_upload_dir"]
        cmd = [
            get_tg_upload_python(),
            "-u",
            script,
            "login",
            "--phone",
            phone,
            "--workdir",
            workdir,
            "--api-id",
            str(settings["api_id"]),
            "--api-hash",
            settings["api_hash"],
            "--session",
            USER_SESSION_NAME,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            _set_auth_lock(False)
            raise RuntimeError(f"Failed to start login process: {exc}") from exc

        _login_proc = proc
        try:
            payload = _read_login_json(proc, timeout=60)
        except Exception:
            _stop_login_proc()
            raise

        if not payload.get("ok"):
            _stop_login_proc()
            raise RuntimeError(payload.get("error") or "Failed to send login code")

        phone_out = payload.get("phone") or phone
        phone_code_hash = payload.get("phone_code_hash") or ""
        if not phone_code_hash:
            _stop_login_proc()
            raise RuntimeError("Telegram did not return phone_code_hash")

        _user_auth_state["phone"] = phone_out
        _user_auth_state["phone_code_hash"] = phone_code_hash
        _user_auth_state["pending"] = True
        return {
            "ok": True,
            "phone": phone_out,
            "pending": True,
            "message": payload.get("message")
            or "Login code sent — paste the newest code, then Confirm once (do not Send again)",
        }


def user_auth_confirm(code: str, password: str = "") -> dict:
    """Step 2: send code/2FA to the keep-alive login process."""
    global _login_proc
    code = (code or "").strip().replace(" ", "")
    # Telegram login codes are digits; strip dashes/odd paste chars.
    code = "".join(ch for ch in code if ch.isdigit())
    password = (password or "").strip()
    if not code and not password:
        raise ValueError("Enter the login code (and 2FA password if prompted)")

    with _user_auth_lock:
        proc = _login_proc
        if not proc or proc.poll() is not None:
            _login_proc = None
            _user_auth_state["pending"] = False
            _set_auth_lock(False)
            raise RuntimeError("No active login — click Send code once, then Confirm")

        if not proc.stdin:
            _stop_login_proc()
            raise RuntimeError("Login process stdin closed — send a new code")

        try:
            proc.stdin.write(json.dumps({"code": code, "password": password}) + "\n")
            proc.stdin.flush()
        except Exception as exc:
            _stop_login_proc()
            raise RuntimeError(f"Failed to send confirm to login process: {exc}") from exc

        try:
            payload = _read_login_json(proc, timeout=60)
        except Exception:
            _stop_login_proc()
            raise

        if payload.get("need_2fa"):
            return {
                "ok": False,
                "need_2fa": True,
                "pending": True,
                "message": payload.get("message")
                or "Two-step verification enabled — enter your cloud password",
            }

        if payload.get("pending") and not payload.get("ok"):
            # Invalid code — process still waiting for another attempt.
            raise RuntimeError(payload.get("error") or "Login failed")

        # Process should exit after success or hard failure.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

        _login_proc = None
        _set_auth_lock(False)

        if not payload.get("ok"):
            _user_auth_state["pending"] = False
            _user_auth_state["phone_code_hash"] = ""
            raise RuntimeError(payload.get("error") or "Failed to confirm login")

        _user_auth_state["pending"] = False
        _user_auth_state["phone_code_hash"] = ""
        _persist_user_session()
        return {
            "ok": True,
            "pending": False,
            "user_authorized": True,
            "user": payload.get("user") or "",
            "message": payload.get("message") or "User authorized",
        }


def user_auth_status() -> dict:
    settings = get_settings()
    return {
        "user_authorized": user_session_present(),
        "pending": bool(_user_auth_state.get("pending")),
        "phone_hint": _user_auth_state.get("phone") or settings.get("user_phone") or "",
        "default_phone": settings.get("user_phone") or "",
    }


def list_channel_media(
    chat_id,
    query: str = "",
    offset: int = 0,
    limit: int = 30,
) -> dict:
    """List/search channel media with the user session (supports messages.Search)."""
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    if offset < 0:
        offset = 0

    settings = get_settings()
    if not settings["api_id"] or not settings["api_hash"]:
        raise RuntimeError("API_ID and API_HASH are required")
    # Prefer the persisted authorized session from /data (survives rebuilds).
    _restore_user_session(only_if_missing=False)
    if not user_session_present():
        raise RuntimeError(
            "NEED_USER_AUTH: Authorize a Telegram user account first (Browse needs a human session)."
        )

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse_channel.py")
    workdir = settings["tg_upload_dir"]
    resolved = normalize_chat_id(chat_id)

    cmd = [
        get_tg_upload_python(),
        "-u",
        script,
        "--chat-id",
        str(resolved),
        "--query",
        query or "",
        "--offset",
        str(offset),
        "--limit",
        str(limit),
        "--workdir",
        workdir,
        "--api-id",
        str(settings["api_id"]),
        "--api-hash",
        settings["api_hash"],
        "--session",
        USER_SESSION_NAME,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=40,
            cwd=workdir,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Channel browse timed out talking to Telegram") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        detail = stderr[-500:] if stderr else f"exit {proc.returncode}"
        raise RuntimeError(f"Browse produced no output: {detail}")

    payload = None
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict):
        raise RuntimeError(f"Browse returned invalid JSON: {stdout[-300:]}")

    if not payload.get("ok"):
        err = payload.get("error") or "Browse failed"
        if payload.get("need_user_auth"):
            raise RuntimeError(f"NEED_USER_AUTH: {err}")
        raise RuntimeError(err)

    return {
        "items": payload.get("items") or [],
        "mode": payload.get("mode") or ("search" if query else "history"),
        "query": query or "",
        "offset": offset,
        "next_offset": int(payload.get("next_offset") or offset),
        "has_more": bool(payload.get("has_more")),
        "chat_id": str(payload.get("chat_id") or resolved),
    }


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
