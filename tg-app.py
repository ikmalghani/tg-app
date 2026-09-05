import contextlib
import importlib
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import tkinter.font as tkfont
import platform

def _load_env_file(env_path):
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "tg-upload")
CRYPT_CONFIG_NAME = "crypt.conf"

_load_env_file(os.path.join(BASE_DIR, ".env"))

API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

def get_browse_start_path(env_key):
    """Return configured browse start path if it exists, otherwise the app directory."""
    configured = os.getenv(env_key, "").strip().strip('"').strip("'")
    if configured and os.path.isdir(configured):
        return os.path.abspath(os.path.normpath(configured))
    return BASE_DIR

def _load_channels_from_env():
    """
    Supported .env formats:
    1) Indexed:
       NOOFCHANNEL = 2
       CHANNELNAME1 = "Name One"
       CHANNELID1 = "-100123"
       CHANNELNAME2 = "Name Two"
       CHANNELID2 = "-100456"
    2) Single line:
       CHANNELS = "Name One|chat_id;Name Two|chat_id"
    """
    channels = []

    # Preferred: indexed format
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
                channels.append((name, chat_id))

    # Fallback: single-line format
    if not channels:
        channels_raw = os.getenv("CHANNELS", "").strip()
        if channels_raw:
            for item in channels_raw.split(";"):
                item = item.strip()
                if not item:
                    continue
                if "|" not in item:
                    continue
                name, chat_id = item.split("|", 1)
                name = name.strip()
                chat_id = chat_id.strip()
                if name and chat_id:
                    channels.append((name, chat_id))

    return channels

CHANNELS = _load_channels_from_env()
if not CHANNELS:
    CHANNELS = [
        ("Our Lady of The Sea", "-1001783837645"),
        ("Sun God Nika", "-1001958464364"),
    ]
CHANNEL_MAP = {name: chat_id for name, chat_id in CHANNELS}

if not API_ID or not API_HASH or not BOT_TOKEN:
    messagebox.showwarning(
        "Missing Credentials",
        "API_ID, API_HASH, or BOT_TOKEN is missing. Add them to .env in the app folder.",
    )

def get_pyrogram_client():
    """Lazy import for pyrogram Client so the app can start without the package."""
    try:
        pyrogram = importlib.import_module("pyrogram")
    except Exception:
        messagebox.showerror(
            "Missing Dependency",
            "The 'pyrogram' package is not installed. Install it to use Telegram features.",
        )
        return None
    return pyrogram.Client

def get_resource_path(filename):
    """Get the correct path to a resource file, works for both dev and PyInstaller."""
    if getattr(sys, "frozen", False):
        base_path = sys._MEIPASS
    else:
        base_path = BASE_DIR
    return os.path.join(base_path, filename)

def get_crypt_config_path():
    return get_resource_path(CRYPT_CONFIG_NAME)

_original_stdout = sys.stdout
_current_log_channel = "upload"
_active_children = []
_busy_operation = None
_instance_lock_fd = None

LOG_CHANNELS = ("upload", "download", "authorize")

class LogStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []
        self.is_progress = []
        self.synced_count = 0

    def append(self, text, is_progress=False):
        with self.lock:
            if is_progress:
                if self.entries and self.is_progress[-1]:
                    self.entries[-1] = text
                    return
                self.entries.append(text)
                self.is_progress.append(True)
            else:
                self.entries.append(text)
                self.is_progress.append(False)

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.is_progress.clear()
        self.synced_count = 0

    def snapshot(self):
        with self.lock:
            return list(self.entries), list(self.is_progress)

_log_stores = {channel: LogStore() for channel in LOG_CHANNELS}

def append_log_entry(text, is_progress=False):
    store = _log_stores.get(_current_log_channel, _log_stores["upload"])
    store.append(text, is_progress)

class LogTee:
    def __init__(self, real):
        self._real = real
        self._buffer = ""
        self._is_progress_tail = False

    def write(self, data):
        if self._real is not None:
            try:
                self._real.write(data)
            except Exception:
                pass
        self._consume(data)

    def flush(self):
        if self._buffer:
            line = self._buffer
            self._buffer = ""
            if line.strip():
                append_log_entry(line, is_progress=self._is_progress_tail)
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass

    def _consume(self, data):
        self._buffer += data
        idx = 0
        while idx < len(self._buffer):
            ch = self._buffer[idx]
            if ch in ("\r", "\n"):
                line = self._buffer[:idx]
                self._buffer = self._buffer[idx + 1:]
                self._is_progress_tail = (ch == "\r")
                if line:
                    append_log_entry(line, is_progress=(ch == "\r"))
                idx = 0
            else:
                idx += 1

sys.stdout = LogTee(_original_stdout)

def log_message(message):
    text = str(message)
    if _original_stdout is not None:
        try:
            _original_stdout.write(text + "\n")
            _original_stdout.flush()
        except Exception:
            pass
    append_log_entry(text, is_progress=False)

def run_subprocess(command_args, working_directory=None):
    process = subprocess.Popen(
        command_args,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    _active_children.append(process)

    output_queue = queue.Queue()
    collected_output = []

    def enqueue_output():
        buffer = bytearray()
        try:
            while True:
                char = process.stdout.read(1)
                if char == b"":
                    break
                if char in (b"\r", b"\n"):
                    if buffer:
                        output_queue.put((buffer.decode("utf-8", errors="replace"), char.decode("ascii", errors="ignore")))
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

    root_widget = globals().get("root")
    output_done = False
    progress_prefixes = ("UP:", "DL:", "SPLIT:", "COMBINE:", "Calculating ")
    progress_line_active = False
    progress_line_length = 0
    while not output_done or process.poll() is None:
        try:
            item = output_queue.get(timeout=0.1)
            if item is None:
                output_done = True
            else:
                line, separator = item
                if line:
                    collected_output.append(line)
                    is_progress_update = separator == "\r" and line.startswith(progress_prefixes)
                    if is_progress_update:
                        padded_line = line.ljust(progress_line_length)
                        sys.stdout.write(f"\r{padded_line}")
                        sys.stdout.flush()
                        progress_line_active = True
                        progress_line_length = max(progress_line_length, len(line))
                    else:
                        if progress_line_active:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            progress_line_active = False
                            progress_line_length = 0
                        log_message(line)
        except queue.Empty:
            pass

        if root_widget is not None and root_widget.winfo_exists():
            try:
                root_widget.update_idletasks()
                root_widget.update()
            except tk.TclError:
                root_widget = None

    if progress_line_active:
        sys.stdout.write("\n")
        sys.stdout.flush()

    try:
        returncode = process.wait()
    finally:
        if process in _active_children:
            _active_children.remove(process)
    stdout_text = "\n".join(collected_output)
    return subprocess.CompletedProcess(
        command_args,
        returncode,
        stdout=stdout_text,
        stderr="",
    )

def run_subprocess_capture(command_args, working_directory=None):
    kwargs = {
        "cwd": working_directory,
        "capture_output": True,
        "text": True,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(command_args, **kwargs)

def run_subprocess_passthrough(command_args, working_directory=None):
    process = subprocess.Popen(
        command_args,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    output_queue = queue.Queue()

    def enqueue_output():
        try:
            while True:
                char = process.stdout.read(1)
                if char == b"":
                    break
                output_queue.put(char.decode("utf-8", errors="replace"))
        finally:
            process.stdout.close()
            output_queue.put(None)

    reader_thread = threading.Thread(target=enqueue_output, daemon=True)
    reader_thread.start()

    root_widget = globals().get("root")
    buffer = []
    output_done = False
    while not output_done or process.poll() is None:
        try:
            item = output_queue.get(timeout=0.05)
            if item is None:
                output_done = True
            else:
                buffer.append(item)
                if item in ("\n", "\r"):
                    line = "".join(buffer).rstrip("\r\n")
                    buffer = []
                    sys.stdout.write(line + item)
                    sys.stdout.flush()
        except queue.Empty:
            pass

        if root_widget is not None and root_widget.winfo_exists():
            try:
                root_widget.update_idletasks()
                root_widget.update()
            except tk.TclError:
                root_widget = None

    if buffer:
        sys.stdout.write("".join(buffer))
        sys.stdout.flush()

    returncode = process.wait()
    return subprocess.CompletedProcess(
        command_args,
        returncode,
        stdout="",
        stderr="",
    )

def get_tg_upload_python():
    if platform.system() == "Windows":
        venv_python = os.path.join(UPLOAD_DIR, "venv", "Scripts", "python.exe")
    else:
        venv_python = os.path.join(UPLOAD_DIR, "venv", "bin", "python")
    if os.path.exists(venv_python):
        return venv_python
    return sys.executable

def _pump_ui(wait_s=0.1):
    root_widget = globals().get("root")
    if root_widget is not None:
        try:
            if root_widget.winfo_exists():
                root_widget.update_idletasks()
                root_widget.update()
        except tk.TclError:
            pass
    time.sleep(wait_s)

def _acquire_file_lock(lock_path, exclusive=True, nonblocking=False):
    fd = open(lock_path, "a+")
    try:
        if platform.system() == "Windows":
            import msvcrt
            fd.seek(0)
            mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
            msvcrt.locking(fd.fileno(), mode, 1)
        else:
            import fcntl
            flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if nonblocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(fd, flags)
    except (OSError, BlockingIOError, PermissionError):
        fd.close()
        return None
    return fd

def _release_file_lock(fd):
    if fd is None:
        return
    try:
        if platform.system() == "Windows":
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fd.close()
    except Exception:
        pass

def ensure_single_instance():
    lock_path = os.path.join(BASE_DIR, ".tg-app.lock")
    fd = _acquire_file_lock(lock_path, exclusive=True, nonblocking=True)
    if fd is None:
        return None
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except Exception:
        pass
    return fd

@contextlib.contextmanager
def telegram_session_lock(wait_message="Waiting for another Telegram task to finish..."):
    lock_path = os.path.join(UPLOAD_DIR, "profile.session.lock")
    fd = _acquire_file_lock(lock_path, exclusive=True, nonblocking=True)
    logged_wait = False
    while fd is None:
        if not logged_wait:
            log_message(wait_message)
            logged_wait = True
        _pump_ui(0.2)
        fd = _acquire_file_lock(lock_path, exclusive=True, nonblocking=True)
    try:
        yield
    finally:
        _release_file_lock(fd)

def tg_upload_bot_args():
    """Login with the bot token in memory so uploads do not lock profile.session."""
    args = ["--profile", "profile", "--no_update"]
    if API_ID and API_HASH and BOT_TOKEN:
        args.extend([
            "--api_id", str(API_ID),
            "--api_hash", API_HASH,
            "--bot", BOT_TOKEN,
            "--tmp_session",
        ])
    return args

def leftover_tg_upload_pids():
    leftover = []
    my_pid = os.getpid()
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return leftover
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == my_pid:
            continue
        try:
            with open(os.path.join(proc_dir, entry, "cmdline"), "rb") as cmd_file:
                cmdline = cmd_file.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            if "tg-upload.py" not in cmdline:
                continue
            with open(os.path.join(proc_dir, entry, "stat"), "r", encoding="utf-8") as stat_file:
                stat_text = stat_file.read()
            ppid = int(stat_text.split(")")[-1].split()[0])
            if ppid != my_pid:
                leftover.append(pid)
        except (OSError, IndexError, ValueError):
            continue
    return leftover

def stop_leftover_tg_uploads():
    pids = leftover_tg_upload_pids()
    if not pids:
        return
    log_message(
        "Processing: Telegram - Stopping leftover tg-upload process(es) that still hold the session: "
        + ", ".join(str(pid) for pid in pids)
    )
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            continue
    deadline = time.time() + 5
    while time.time() < deadline:
        remaining = leftover_tg_upload_pids()
        if not remaining:
            break
        _pump_ui(0.2)
    for pid in leftover_tg_upload_pids():
        try:
            os.kill(pid, 9)
        except OSError:
            pass

def run_tg_upload(arguments, lock_session=True):
    command_args = [get_tg_upload_python(), "-u", "tg-upload.py", *arguments]
    log_message(f"Executing command: {' '.join(command_args)}")
    if lock_session:
        with telegram_session_lock(
            "Waiting for another Telegram upload/download to release the session..."
        ):
            result = run_subprocess(command_args, working_directory=UPLOAD_DIR)
    else:
        result = run_subprocess(command_args, working_directory=UPLOAD_DIR)
    if result.returncode != 0:
        stderr = result.stdout.strip() if result.stdout else "tg-upload command failed."
        raise RuntimeError(stderr)
    return result

def show_copyable_error(title, message):
    dialog = tk.Toplevel(root)
    dialog.title(title)
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("760x320")

    text = tk.Text(dialog, wrap=tk.WORD)
    text.insert("1.0", message)
    text.configure(state=tk.NORMAL)
    text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    button_frame = tk.Frame(dialog)
    button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

    def copy_all():
        dialog.clipboard_clear()
        dialog.clipboard_append(text.get("1.0", tk.END).rstrip())
        dialog.update()

    tk.Button(button_frame, text="Copy", command=copy_all).pack(side=tk.LEFT)
    tk.Button(button_frame, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)

    text.focus_set()

def encrypt_decrypt(is_encrypt, file_or_folders, config_file_path):
    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"Config file not found at {config_file_path}")

    log_message(f"Using config file: {config_file_path}")

    for file_or_folder in file_or_folders:
        if not file_or_folder:
            continue

        normalized_path = os.path.abspath(file_or_folder)
        file_directory = os.path.dirname(normalized_path) or BASE_DIR
        basename = os.path.basename(normalized_path)

        if os.path.isfile(normalized_path):
            if is_encrypt:
                if basename.lower().endswith(".bin"):
                    log_message(f"SKIPPED: {basename} - File already encrypted (.bin extension detected)")
                    continue
                log_message(f"Processing: {basename} - File will be encrypted")
                command_args = [
                    "rclone",
                    "--config",
                    config_file_path,
                    "move",
                    normalized_path,
                    "crypt:.",
                    "--progress",
                    "--stats-one-line",
                ]
            else:
                remote_name = basename[:-4] if basename.lower().endswith(".bin") else basename
                command_args = [
                    "rclone",
                    "--config",
                    config_file_path,
                    "move",
                    f"crypt:{remote_name}",
                    ".",
                    "--progress",
                    "--stats-one-line",
                ]
        elif os.path.isdir(normalized_path):
            if is_encrypt:
                command_args = [
                    "rclone",
                    "--config",
                    config_file_path,
                    "move",
                    normalized_path,
                    "crypt:.",
                    "--transfers",
                    "1",
                    "--progress",
                    "--stats-one-line",
                ]
            else:
                command_args = [
                    "rclone",
                    "--config",
                    config_file_path,
                    "move",
                    "crypt:",
                    normalized_path,
                    "--transfers",
                    "1",
                    "--progress",
                    "--stats-one-line",
                ]
        else:
            raise FileNotFoundError(f"Invalid file or folder path: {file_or_folder}")

        log_message(f"Executing command: {' '.join(command_args)}")
        result = run_subprocess_passthrough(command_args, working_directory=file_directory)
        if result.returncode != 0:
            action = "encrypt" if is_encrypt else "decrypt"
            raise RuntimeError(f"Failed to {action} {basename or normalized_path}")
        time.sleep(1)

def get_encrypted_output_path(file_path):
    return f"{file_path}.bin"

def encrypt_file_for_upload(file_path, config_file_path):
    encrypt_decrypt(True, [file_path], config_file_path)
    encrypted_path = get_encrypted_output_path(file_path)
    if not os.path.exists(encrypted_path):
        raise FileNotFoundError(f"Encrypted file not found after encryption: {encrypted_path}")
    return encrypted_path

def decrypt_files_in_directory(directory, config_file_path):
    decrypted_any = False
    for entry in sorted(os.listdir(directory)):
        file_path = os.path.join(directory, entry)
        if os.path.isfile(file_path) and entry.lower().endswith(".bin"):
            encrypt_decrypt(False, [file_path], config_file_path)
            decrypted_any = True
    return decrypted_any

# Split function from split.py
def split_file(file_path, split_size=1500 * 1024 * 1024):
    file_size = os.path.getsize(file_path)
    num_parts = -(-file_size // split_size)  # Ceiling division
    log_message(
        f"Processing: {os.path.basename(file_path)} - Splitting file into {num_parts} part(s) "
        f"with chunk size {split_size} bytes"
    )

    part_files = []
    with open(file_path, 'rb') as f:
        for i in range(num_parts):
            part_file_name = f"{file_path}.part{i:02d}"
            log_message(f"Processing: {os.path.basename(file_path)} - Creating split part {os.path.basename(part_file_name)}")
            with open(part_file_name, 'wb') as part_file:
                part_file.write(f.read(split_size))
            part_files.append(part_file_name)

    log_message(f"Completed: {os.path.basename(file_path)} - Created {len(part_files)} split part(s)")
    return part_files

# Helper function to parse Telegram link and extract chat_id and message_id
def parse_telegram_link(link):
    """Parse a Telegram link to extract chat_id and message_id"""
    link = link.strip().replace(" ", "")
    if not link.startswith(("https://", "http://")):
        return None, None
    
    parts = link.split('/')
    try:
        if 't.me' in parts or 'telegram.me' in parts:
            # Find the index of t.me or telegram.me
            domain_idx = parts.index('t.me') if 't.me' in parts else parts.index('telegram.me')
            # Check if it's a channel (c/username) or direct (username)
            if domain_idx + 1 < len(parts) and parts[domain_idx + 1] == 'c':
                # Channel format: https://t.me/c/1234567890/123
                chat_id = int(f"-100{parts[domain_idx + 2]}")
                msg_id = int(parts[domain_idx + 3])
            else:
                # Direct format: https://t.me/username/123
                username = parts[domain_idx + 1]
                msg_id = int(parts[domain_idx + 2])
                chat_id = username  # Will need to resolve to ID
            return chat_id, msg_id
    except (ValueError, IndexError):
        return None, None
    return None, None

# Helper function to get caption from Telegram message
def get_caption_from_link(link):
    """Fetch the caption from a Telegram message using the link"""
    chat_id, msg_id = parse_telegram_link(link)
    if not chat_id or not msg_id:
        return None
    
    try:
        target_directory = UPLOAD_DIR
        original_dir = os.getcwd()
        session_file = os.path.join(target_directory, "profile.session")
        
        # Check if session file exists
        if not os.path.exists(session_file):
            return None
        
        if os.getcwd() != target_directory:
            os.chdir(target_directory)
        
        Client = get_pyrogram_client()
        if Client is None:
            return None
        client = Client(
            "profile",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True,
        )
        with client:
            message = client.get_messages(chat_id, msg_id)
            if message and message.caption:
                caption = message.caption.strip()
                if '.part' in caption:
                    caption = caption.split('.part')[0]
                if not caption.endswith('.bin'):
                    caption = caption + '.bin'
                return caption
            if message and message.document and message.document.file_name:
                filename = message.document.file_name
                if '.part' in filename:
                    filename = filename.split('.part')[0]
                if not filename.endswith('.bin'):
                    filename = filename + '.bin'
                return filename
    except Exception as e:
        print(f"Error fetching caption: {e}")
        return None
    finally:
        if 'original_dir' in locals():
            os.chdir(original_dir)
    return None

# Combine function from combine.py
def combine_files(directory, links_list=None):
    if not directory or not os.path.exists(directory) or not links_list:
        log_message("SKIPPED: Combine - Missing download directory, directory does not exist, or no links were provided")
        return

    downloaded_names = []
    seen_names = set()
    for link in links_list:
        caption = get_caption_from_link(link)
        if not caption:
            log_message(f"SKIPPED: Combine - Could not resolve caption from link: {link}")
            continue
        if not caption.endswith(".bin"):
            caption = caption + ".bin"
        if caption not in seen_names:
            downloaded_names.append(caption)
            seen_names.add(caption)

    if not downloaded_names:
        log_message("SKIPPED: Combine - No downloadable captions were resolved from the provided links")
        return

    file_names = [
        name for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
    ]

    for downloaded_name in downloaded_names:
        # Combine only real split-part siblings like "name.bin.part00", "name.bin.part01", etc.
        part_pattern = re.compile(rf"^{re.escape(downloaded_name)}\.part(\d+)$")
        part_files = []
        for file_name in file_names:
            match = part_pattern.fullmatch(file_name)
            if match:
                part_files.append((int(match.group(1)), file_name))

        # Leave standalone ".bin" files untouched.
        if len(part_files) <= 1:
            if len(part_files) == 1:
                log_message(f"SKIPPED: {downloaded_name} - Only one split part found, leaving file as-is")
            else:
                log_message(f"SKIPPED: {downloaded_name} - No matching split parts found to combine")
            continue

        part_files.sort(key=lambda item: item[0])
        combined_file = os.path.join(directory, downloaded_name)
        log_message(f"Processing: {downloaded_name} - Combining {len(part_files)} part(s) into {combined_file}")

        with open(combined_file, "wb") as combined:
            for _part_number, part_file in part_files:
                part_file_path = os.path.join(directory, part_file)
                log_message(f"Processing: {downloaded_name} - Appending {part_file}")
                with open(part_file_path, "rb") as pf:
                    shutil.copyfileobj(pf, combined)
                os.remove(part_file_path)
                log_message(f"Completed: {downloaded_name} - Removed source part {part_file}")
        log_message(f"Completed: {downloaded_name} - Combine finished")

# Function to rename downloaded files using captions
def rename_files_with_captions(directory, links_list):
    """Rename downloaded files to use their captions from Telegram messages"""
    if not links_list or not directory:
        return
    
    # Get captions from all links
    link_to_caption = {}
    for link in links_list:
        caption = get_caption_from_link(link)
        if caption:
            link_to_caption[link] = caption
    
    if not link_to_caption:
        return
    
    # Get all files in the directory
    if not os.path.exists(directory):
        return
    
    files = [f for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
    
    # Try to match files to links and rename them
    used_captions = set()
    for file_name in files:
        # Skip part files (they should be handled by combine)
        if ".part" in file_name:
            continue
        
        file_path = os.path.join(directory, file_name)
        file_name_lower = file_name.lower()
        
        # Try to match this file to a caption
        for link, caption in link_to_caption.items():
            if caption in used_captions:
                continue
            
            # Normalize for comparison
            caption_base = caption.replace('.bin', '').lower().replace('_', '.').replace('-', '.')
            file_base = file_name.replace('.bin', '').lower().replace('_', '.').replace('-', '.')
            
            # Check if file matches caption (various matching strategies)
            matched = False
            
            # 1. Exact match (normalized)
            if file_base == caption_base:
                matched = True
            # 2. File name is contained in caption or vice versa
            elif file_base in caption_base or caption_base in file_base:
                min_len = min(len(file_base), len(caption_base))
                if min_len >= 10:
                    matched = True
            # 3. Share significant common substring
            elif len(file_base) >= 10 and len(caption_base) >= 10:
                if (file_base[:10] in caption_base or caption_base[:10] in file_base or
                    file_base[-10:] in caption_base or caption_base[-10:] in file_base):
                    matched = True
            
            if matched:
                # Rename file to use caption
                new_file_path = os.path.join(directory, caption)
                if file_path != new_file_path and not os.path.exists(new_file_path):
                    try:
                        os.rename(file_path, new_file_path)
                        used_captions.add(caption)
                    except Exception as e:
                        print(f"Error renaming file {file_name} to {caption}: {e}")
                break

# Helper function to check if file has .bin or .part* extension
def is_allowed_file(filename):
    filename_lower = filename.lower()
    if filename_lower.endswith('.bin'):
        return True
    # Check if extension starts with .part
    if '.' in filename_lower:
        ext = filename_lower.rsplit('.', 1)[1]
        if ext.startswith('part'):
            return True
    return False

# Function to authorize
def authorize():
    global _current_log_channel, _busy_operation
    _current_log_channel = "authorize"
    _busy_operation = "authorize"
    try:
        stop_leftover_tg_uploads()
        run_tg_upload([
            "--profile", "profile",
            "--api_id", API_ID,
            "--api_hash", API_HASH,
            "--bot", BOT_TOKEN,
            "--login_only",
        ])
    except Exception as exc:
        show_copyable_error("Authorize Error", str(exc))
        return
    finally:
        _busy_operation = None
    messagebox.showinfo("Info", "Authorization complete.")

# Removed browse_txt_file function - using text field instead

def browse_upload():
    initial_dir = get_browse_start_path("BROWSE_UPLOAD_PATH")
    source_type = var_source_type_upload.get()
    if source_type == "File":
        file_path = filedialog.askopenfilename(initialdir=initial_dir)
        if file_path:
            entry_upload_path.delete(0, tk.END)
            entry_upload_path.insert(0, file_path)
    else:
        folder_path = filedialog.askdirectory(initialdir=initial_dir)
        if folder_path:
            entry_upload_path.delete(0, tk.END)
            entry_upload_path.insert(0, folder_path)

def browse_download_directory():
    initial_dir = get_browse_start_path("BROWSE_DOWNLOAD_PATH")
    download_directory = filedialog.askdirectory(initialdir=initial_dir)
    if download_directory:
        entry_download_dir.delete(0, tk.END)
        entry_download_dir.insert(0, download_directory)

def download():
    global _current_log_channel, _busy_operation
    _current_log_channel = "download"
    _busy_operation = "download"
    set_download_button_busy(True)
    try:
        channel = var_channel.get()
        if channel == "Custom Channel":
            chat_id = entry_custom_chat_id.get()
        else:
            chat_id = CHANNEL_MAP.get(channel, "")

        directory = entry_download_dir.get()
        combine = var_combine.get()
        decrypt_after_download = var_decrypt_download.get()

        # Get links from text widget (one per line)
        links_text = text_tg_links.get("1.0", tk.END).strip()
        if not links_text:
            messagebox.showerror("Error", "Please enter Telegram links in the text field.")
            return

        # Split by lines and filter out empty lines
        links_list = [line.strip() for line in links_text.split('\n') if line.strip()]
        
        if not links_list:
            messagebox.showerror("Error", "Please enter at least one Telegram link.")
            return

        command_args = [*tg_upload_bot_args(), "--chat_id", str(chat_id), "--dl_dir", directory]

        # Auto-detect: 1 link = Single, multiple links = Batch
        if len(links_list) == 1:
            # Single mode
            log_message(f"Processing: Download - Single link mode selected for 1 link into {directory}")
            command_args.extend(["--dl", "--links", links_list[0]])
            temp_file = None
        else:
            # Batch mode - create temporary file with links
            log_message(f"Processing: Download - Batch mode selected for {len(links_list)} links into {directory}")
            temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', prefix='tg_links_')
            temp_file.write('\n'.join(links_list))
            temp_file.close()
            log_message(f"Processing: Download - Created temporary link list file {temp_file.name}")
            command_args.extend(["--dl", "--txt_file", temp_file.name])

        try:
            stop_leftover_tg_uploads()
            run_tg_upload(command_args, lock_session=False)
        except Exception as exc:
            show_copyable_error("Download Error", str(exc))
            return
        finally:
            if temp_file and os.path.exists(temp_file.name):
                os.remove(temp_file.name)
        
        # Combine files after download if requested
        if combine and directory:
            log_message("Processing: Download - Combine option enabled, checking for split files to restore")
            combine_files(directory, links_list)
        else:
            if combine:
                log_message("SKIPPED: Combine - Download directory is empty or unavailable")
            else:
                log_message("SKIPPED: Combine - Option disabled by user")
            # If not combining, rename files using captions
            if directory and links_list:
                log_message("Processing: Download - Renaming downloaded files using captions because combine was not run")
                rename_files_with_captions(directory, links_list)

        if decrypt_after_download and directory:
            config_file_path = get_crypt_config_path()
            log_message("Processing: Download - Decrypt option enabled, checking for .bin files to decrypt")
            decrypt_files_in_directory(directory, config_file_path)
        elif decrypt_after_download:
            log_message("SKIPPED: Decrypt - Download directory is empty or unavailable")
        else:
            log_message("SKIPPED: Decrypt - Option disabled by user")
        
        messagebox.showinfo("Info", "Download complete.")
    finally:
        _busy_operation = None
        set_download_button_busy(False)

def browse_upload_directory():
    initial_dir = get_browse_start_path("BROWSE_UPLOAD_PATH")
    upload_directory = filedialog.askdirectory(initialdir=initial_dir)
    if upload_directory:
        entry_upload_path.delete(0, tk.END)
        entry_upload_path.insert(0, upload_directory)

def normalize_input_path(raw_path):
    if not raw_path:
        return ""
    cleaned_path = raw_path.strip().strip('"').strip("'")
    if not cleaned_path:
        return ""
    return os.path.abspath(os.path.normpath(cleaned_path))

def iter_upload_source_files(source_type, selected_path):
    selected_path = normalize_input_path(selected_path)

    if source_type == "File":
        if selected_path and os.path.isfile(selected_path):
            yield os.path.abspath(selected_path)
        return

    if not selected_path or not os.path.isdir(selected_path):
        return

    pending_directories = [selected_path]
    while pending_directories:
        current_directory = pending_directories.pop()
        try:
            with os.scandir(current_directory) as entries:
                sorted_entries = sorted(entries, key=lambda entry: entry.name.lower())
        except OSError as exc:
            log_message(f"SKIPPED: Upload - Failed to scan folder: {current_directory} ({exc})")
            continue

        child_directories = []
        for entry in sorted_entries:
            try:
                if entry.is_file(follow_symlinks=True):
                    yield os.path.abspath(entry.path)
                elif entry.is_dir(follow_symlinks=True):
                    child_directories.append(entry.path)
            except OSError as exc:
                log_message(f"SKIPPED: Upload - Failed to inspect path: {entry.path} ({exc})")

        for child_directory in reversed(child_directories):
            pending_directories.append(child_directory)

def delete_empty_directories(start_directory, root_directory):
    current_directory = os.path.abspath(start_directory)
    root_directory = os.path.abspath(root_directory)

    while current_directory.startswith(root_directory):
        if current_directory == root_directory:
            break

        if not os.path.isdir(current_directory):
            break

        try:
            if os.listdir(current_directory):
                break
            log_message(f"Processing: Upload - Deleting empty folder: {current_directory}")
            os.rmdir(current_directory)
            log_message(f"Completed: Upload - Deleted empty folder: {current_directory}")
        except OSError:
            log_message(f"SKIPPED: Delete on Done - Failed to delete empty folder: {current_directory}")
            break

        parent_directory = os.path.dirname(current_directory)
        if parent_directory == current_directory:
            break
        current_directory = parent_directory

def set_upload_button_busy(is_busy):
    if "button_upload" not in globals():
        return
    if is_busy:
        button_upload.configure(state=tk.DISABLED, text="Uploading...")
    else:
        button_upload.configure(state=tk.NORMAL, text="Upload")
    if "root" in globals() and root.winfo_exists():
        root.update_idletasks()

def set_download_button_busy(is_busy):
    if "button_download" not in globals():
        return
    if is_busy:
        button_download.configure(state=tk.DISABLED, text="Downloading...")
    else:
        button_download.configure(state=tk.NORMAL, text="Download")
    if "root" in globals() and root.winfo_exists():
        root.update_idletasks()

def upload():
    global _current_log_channel, _busy_operation
    _current_log_channel = "upload"
    _busy_operation = "upload"
    set_upload_button_busy(True)
    try:
        channel = var_channel_upload.get()
        if channel == "Custom Channel":
            chat_id = entry_custom_chat_id_upload.get()
        else:
            chat_id = CHANNEL_MAP.get(channel, "")

        source_type = var_source_type_upload.get()
        delete_on_done = var_delete_on_done.get()
        split_files = var_split.get()
        encrypt_before_upload = var_encrypt_upload.get()
        config_file_path = get_crypt_config_path()
        selected_path = normalize_input_path(entry_upload_path.get())

        if not selected_path:
            messagebox.showerror("Error", "Please choose a valid upload path.")
            return

        if source_type == "Folder":
            log_message(f"Processing: Upload - Walking folder root: {selected_path}")
            if not os.path.isdir(selected_path):
                messagebox.showerror("Error", f"Folder does not exist or is not accessible: {selected_path}")
                return
        elif not os.path.isfile(selected_path):
            messagebox.showerror("Error", f"File does not exist or is not accessible: {selected_path}")
            return

        if encrypt_before_upload and not os.path.exists(config_file_path):
            messagebox.showerror("Error", f"Config file not found: {config_file_path}")
            return

        stop_leftover_tg_uploads()

        min_split_size = int(1.9 * 1024 * 1024 * 1024)  # Split before Telegram's 2GB upload limit
        if delete_on_done:
            log_message("Processing: Upload - Delete on Done is enabled, each source file will be removed after its upload finishes")
        else:
            log_message("SKIPPED: Delete on Done - Option disabled by user")

        found_any_file = False
        for original_file in iter_upload_source_files(source_type, selected_path):
            found_any_file = True
            staging_dir = tempfile.mkdtemp(prefix="tg-staging-")
            staged_filename = os.path.basename(original_file)
            staged_file = os.path.join(staging_dir, staged_filename)
            log_message(f"Processing: Upload - Created staging folder: {staging_dir}")
            log_message(f"Processing: Upload - Copying original file to staging: {original_file}")

            staging_keep = False
            try:
                shutil.copy2(original_file, staged_file)
                log_message(f"Completed: Upload - Copied to staging: {staged_file}")
                current_file = staged_file
                files_to_upload = []

                try:
                    if encrypt_before_upload:
                        if staged_filename.lower().endswith(".bin"):
                            log_message(f"SKIPPED: {staged_filename} - Encryption not needed because file already ends with .bin")
                        else:
                            current_file = encrypt_file_for_upload(current_file, config_file_path)
                    else:
                        log_message(f"SKIPPED: {staged_filename} - Encrypt option disabled by user")
                except Exception as exc:
                    messagebox.showerror("Encryption Error", str(exc))
                    return

                if ".part" in os.path.basename(current_file):
                    log_message(f"SKIPPED: {os.path.basename(current_file)} - File is already a split part and will be uploaded as-is")
                    files_to_upload.append(current_file)
                elif split_files:
                    file_size = os.path.getsize(current_file)
                    if file_size > min_split_size:
                        part_files = split_file(current_file, split_size=1500 * 1024 * 1024)
                        files_to_upload.extend(part_files)
                    else:
                        log_message(
                            f"SKIPPED: {os.path.basename(current_file)} - Split not needed because file size "
                            f"({file_size} bytes) is within the 1.9GB split threshold"
                        )
                        files_to_upload.append(current_file)
                else:
                    log_message(f"SKIPPED: {os.path.basename(current_file)} - Split option disabled by user")
                    files_to_upload.append(current_file)

                for file_path in files_to_upload:
                    filename = os.path.basename(file_path)
                    log_message(f"Processing: Upload - Uploading file now: {filename}")
                    try:
                        run_tg_upload(
                            [
                                *tg_upload_bot_args(),
                                "--path", file_path,
                                "--chat_id", str(chat_id),
                                "--caption", filename,
                            ],
                            lock_session=False,
                        )
                    except Exception as exc:
                        staging_keep = True
                        show_copyable_error("Upload Error", str(exc))
                        return

                if delete_on_done:
                    if os.path.exists(original_file):
                        try:
                            log_message(f"Processing: Upload - Deleting original source file: {original_file}")
                            os.remove(original_file)
                            log_message(f"Completed: Upload - Deleted original source file: {original_file}")
                        except OSError:
                            log_message(f"SKIPPED: Delete on Done - Failed to delete original source file: {original_file}")
                    if source_type == "Folder":
                        delete_empty_directories(os.path.dirname(original_file), selected_path)

            finally:
                still_running = any(child.poll() is None for child in list(_active_children))
                if still_running or staging_keep:
                    log_message(
                        f"SKIPPED: Upload - Leaving staging folder in place: {staging_dir}"
                    )
                else:
                    log_message(f"Processing: Upload - Cleaning up staging folder: {staging_dir}")
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    log_message(f"Completed: Upload - Cleaned up staging folder: {staging_dir}")

        if not found_any_file:
            messagebox.showerror("Error", f"No files were found under: {selected_path}")
            return

        messagebox.showinfo("Info", "Upload complete.")
    finally:
        _busy_operation = None
        set_upload_button_busy(False)

def _insert_log_segment(widget, text):
    if not text:
        return
    try:
        if widget.index("end-1c") != "1.0":
            widget.insert(tk.END, "\n")
        widget.insert(tk.END, text)
    except tk.TclError:
        pass

_log_widgets = {}
_log_progress_positions = {}

def _build_log_panel(parent, channel):
    store = _log_stores[channel]
    frame = ttk.Frame(parent, padding=(0, 6, 0, 0))
    header = ttk.Frame(frame)
    header.pack(fill=tk.X)

    auto_var = tk.BooleanVar(value=True)

    def clear_log():
        store.clear()
        text.delete("1.0", tk.END)

    def copy_log():
        content = text.get("1.0", tk.END).rstrip("\n")
        win = globals().get("root")
        if not content:
            return
        if win is not None and win.winfo_exists():
            win.clipboard_clear()
            win.clipboard_append(content)
            win.update()
        copy_button.config(text="Copied!")
        if win is not None and win.winfo_exists():
            win.after(1200, lambda: copy_button.config(text="Copy Log"))

    ttk.Checkbutton(header, text="Auto-scroll", variable=auto_var).pack(side=tk.LEFT, padx=(0, 8))
    copy_button = ttk.Button(header, text="Copy Log", command=copy_log)
    copy_button.pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(header, text="Clear Log", command=clear_log).pack(side=tk.LEFT)

    body = tk.Frame(frame)
    body.pack(fill=tk.BOTH, expand=True)

    text = tk.Text(
        body,
        wrap=tk.CHAR,
        bg="#0d1117",
        fg="#e6edf3",
        insertbackground="#e6edf3",
        relief=tk.FLAT,
        borderwidth=0,
        padx=8,
        pady=6,
        font=tkfont.nametofont("TkFixedFont"),
        selectbackground="#264f78",
        state=tk.NORMAL,
    )
    vsb = tk.Scrollbar(body, orient=tk.VERTICAL, command=text.yview)
    text.config(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    _log_widgets[channel] = (text, auto_var)
    return frame

def _sync_store_to_widget(store, widget, auto_var):
    try:
        entries, is_prog = store.snapshot()
        count = len(entries)

        if count < store.synced_count:
            widget.delete("1.0", tk.END)
            store.synced_count = 0
            _log_progress_positions.pop(widget, None)

        if count > store.synced_count:
            _insert_log_segment(widget, "\n".join(entries[store.synced_count:]))
            store.synced_count = count
            if is_prog[-1]:
                _log_progress_positions[widget] = widget.index("end-1c linestart")
        elif entries and is_prog[-1] and count == store.synced_count:
            pos = _log_progress_positions.get(widget)
            if pos is None:
                pos = widget.index("end-1c linestart")
            current_last = widget.get(pos, f"{pos} lineend")
            if current_last != entries[-1]:
                widget.delete(pos, f"{pos} lineend")
                widget.insert(pos, entries[-1])
                _log_progress_positions[widget] = pos

        if auto_var.get():
            widget.see(tk.END)
    except tk.TclError:
        pass

def _update_log_view():
    for channel, (widget, auto_var) in list(_log_widgets.items()):
        _sync_store_to_widget(_log_stores[channel], widget, auto_var)

    root_widget = globals().get("root")
    if root_widget is not None and root_widget.winfo_exists():
        try:
            root_widget.after(120, _update_log_view)
        except tk.TclError:
            pass

# Create the main window
_instance_lock_fd = ensure_single_instance()
if _instance_lock_fd is None:
    _prompt = tk.Tk()
    _prompt.withdraw()
    messagebox.showerror(
        "Already running",
        "Telegram Upload/Download is already running.\n\n"
        "Use the existing window. Starting a second copy can lock the Telegram session.",
    )
    _prompt.destroy()
    sys.exit(0)

root = tk.Tk()
root.title("Telegram Upload/Download")
root.geometry("1000x780")
root.minsize(860, 600)

def _on_app_close():
    if _busy_operation:
        messagebox.showwarning(
            "Busy",
            f"A {_busy_operation} is still running.\n\n"
            "Wait for it to finish before closing the window, otherwise the Telegram session can stay locked.",
        )
        return
    root.destroy()

root.protocol("WM_DELETE_WINDOW", _on_app_close)

notebook = ttk.Notebook(root)
notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

default_channel = CHANNELS[0][0] if CHANNELS else "Custom Channel"

# ============================================================ Upload tab
frame_upload = ttk.Frame(notebook, padding=12)
notebook.add(frame_upload, text="Upload")

grp_upload_channel = ttk.LabelFrame(frame_upload, text="Channel")
grp_upload_channel.pack(fill=tk.X, pady=(0, 8))

var_channel_upload = tk.StringVar(value=default_channel)
label_channel_upload = ttk.Label(grp_upload_channel, text="Destination:")
label_channel_upload.grid(row=0, column=0, padx=(10, 8), pady=8, sticky="w")
upload_channel_col = 1
for name, _chat_id in CHANNELS:
    ttk.Radiobutton(grp_upload_channel, text=name, variable=var_channel_upload, value=name).grid(
        row=0, column=upload_channel_col, padx=(4, 12), pady=8, sticky="w"
    )
    upload_channel_col += 1
ttk.Radiobutton(grp_upload_channel, text="Custom", variable=var_channel_upload, value="Custom Channel").grid(
    row=0, column=upload_channel_col, padx=(4, 12), pady=8, sticky="w"
)

ttk.Label(grp_upload_channel, text="Custom Chat ID:").grid(row=1, column=0, padx=(10, 8), pady=(0, 8), sticky="w")
entry_custom_chat_id_upload = ttk.Entry(grp_upload_channel, width=28)
entry_custom_chat_id_upload.grid(row=1, column=1, columnspan=3, padx=(4, 12), pady=(0, 8), sticky="ew")

grp_upload_source = ttk.LabelFrame(frame_upload, text="Source")
grp_upload_source.pack(fill=tk.X, pady=(0, 8))

ttk.Label(grp_upload_source, text="Source Type:").grid(row=0, column=0, padx=(10, 8), pady=8, sticky="w")
var_source_type_upload = tk.StringVar(value="File")
ttk.Radiobutton(grp_upload_source, text="File", variable=var_source_type_upload, value="File").grid(
    row=0, column=1, padx=(4, 12), pady=8, sticky="w"
)
ttk.Radiobutton(grp_upload_source, text="Folder", variable=var_source_type_upload, value="Folder").grid(
    row=0, column=2, padx=(4, 12), pady=8, sticky="w"
)

ttk.Label(grp_upload_source, text="Upload Path:").grid(row=1, column=0, padx=(10, 8), pady=(0, 10), sticky="w")
entry_upload_path = ttk.Entry(grp_upload_source, width=50)
entry_upload_path.grid(row=1, column=1, columnspan=3, padx=(4, 8), pady=(0, 10), sticky="ew")
grp_upload_source.columnconfigure(3, weight=1)
button_browse_upload = ttk.Button(grp_upload_source, text="Browse", command=browse_upload)
button_browse_upload.grid(row=1, column=4, padx=(0, 10), pady=(0, 10))

grp_upload_options = ttk.LabelFrame(frame_upload, text="Options")
grp_upload_options.pack(fill=tk.X, pady=(0, 8))

var_delete_on_done = tk.BooleanVar(value=True)
var_split = tk.BooleanVar(value=True)
var_encrypt_upload = tk.BooleanVar(value=True)
ttk.Checkbutton(grp_upload_options, text="Delete on Done", variable=var_delete_on_done).pack(side=tk.LEFT, padx=12, pady=8)
ttk.Checkbutton(grp_upload_options, text="Split Files", variable=var_split).pack(side=tk.LEFT, padx=12, pady=8)
ttk.Checkbutton(grp_upload_options, text="Encrypt", variable=var_encrypt_upload).pack(side=tk.LEFT, padx=12, pady=8)
button_upload = ttk.Button(grp_upload_options, text="Upload", command=upload)
button_upload.pack(side=tk.RIGHT, padx=12, pady=8)

_build_log_panel(frame_upload, "upload").pack(fill=tk.BOTH, expand=True)

# ============================================================ Download tab
frame_download = ttk.Frame(notebook, padding=12)
notebook.add(frame_download, text="Download")

grp_download_channel = ttk.LabelFrame(frame_download, text="Channel")
grp_download_channel.pack(fill=tk.X, pady=(0, 8))

var_channel = tk.StringVar(value=default_channel)
ttk.Label(grp_download_channel, text="Source:").grid(row=0, column=0, padx=(10, 8), pady=8, sticky="w")
download_channel_col = 1
for name, _chat_id in CHANNELS:
    ttk.Radiobutton(grp_download_channel, text=name, variable=var_channel, value=name).grid(
        row=0, column=download_channel_col, padx=(4, 12), pady=8, sticky="w"
    )
    download_channel_col += 1
ttk.Radiobutton(grp_download_channel, text="Custom", variable=var_channel, value="Custom Channel").grid(
    row=0, column=download_channel_col, padx=(4, 12), pady=8, sticky="w"
)

ttk.Label(grp_download_channel, text="Custom Chat ID:").grid(row=1, column=0, padx=(10, 8), pady=(0, 8), sticky="w")
entry_custom_chat_id = ttk.Entry(grp_download_channel, width=28)
entry_custom_chat_id.grid(row=1, column=1, columnspan=3, padx=(4, 12), pady=(0, 8), sticky="ew")

grp_download_files = ttk.LabelFrame(frame_download, text="Links & Destination")
grp_download_files.pack(fill=tk.X, pady=(0, 8))

ttk.Label(grp_download_files, text="Save to...").grid(row=0, column=0, padx=(10, 8), pady=(8, 4), sticky="w")
entry_download_dir = ttk.Entry(grp_download_files, width=36)
entry_download_dir.grid(row=0, column=1, columnspan=3, padx=(4, 8), pady=(8, 4), sticky="ew")
grp_download_files.columnconfigure(3, weight=1)
button_browse_download_dir = ttk.Button(grp_download_files, text="Browse", command=browse_download_directory)
button_browse_download_dir.grid(row=0, column=4, padx=(0, 10), pady=(8, 4))

ttk.Label(grp_download_files, text="TG Links:").grid(row=1, column=0, padx=(10, 8), pady=(4, 10), sticky="nw")
text_tg_links = tk.Text(grp_download_files, width=50, height=6, wrap=tk.WORD)
text_tg_links.grid(row=1, column=1, columnspan=3, padx=(4, 8), pady=(4, 10), sticky="ew")
scrollbar_tg_links = tk.Scrollbar(grp_download_files, orient=tk.VERTICAL, command=text_tg_links.yview)
scrollbar_tg_links.grid(row=1, column=4, sticky="ns", padx=(0, 8), pady=(4, 10))
text_tg_links.config(yscrollcommand=scrollbar_tg_links.set)

grp_download_options = ttk.LabelFrame(frame_download, text="Options")
grp_download_options.pack(fill=tk.X, pady=(0, 8))

var_combine = tk.BooleanVar(value=True)
var_decrypt_download = tk.BooleanVar(value=True)
ttk.Checkbutton(grp_download_options, text="Combine", variable=var_combine).pack(side=tk.LEFT, padx=12, pady=8)
ttk.Checkbutton(grp_download_options, text="Decrypt", variable=var_decrypt_download).pack(side=tk.LEFT, padx=12, pady=8)
button_download = ttk.Button(grp_download_options, text="Download", command=download)
button_download.pack(side=tk.RIGHT, padx=12, pady=8)

_build_log_panel(frame_download, "download").pack(fill=tk.BOTH, expand=True)

# ============================================================ Authorize tab
frame_authorize = ttk.Frame(notebook, padding=12)
notebook.add(frame_authorize, text="Authorize")

auth_header = ttk.Frame(frame_authorize)
auth_header.pack(fill=tk.X, pady=(0, 8))

button_authorize = ttk.Button(auth_header, text="Authorize Telegram Account", command=authorize)
button_authorize.pack(side=tk.LEFT, padx=(0, 10), pady=6)
ttk.Label(auth_header, text="Logs in using the bot token from the .env file.").pack(side=tk.LEFT, pady=6)

_build_log_panel(frame_authorize, "authorize").pack(fill=tk.BOTH, expand=True)

_update_log_view()
root.mainloop()
