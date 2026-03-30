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

def log_message(message):
    print(message)

def run_subprocess(command_args, working_directory=None):
    process = subprocess.Popen(
        command_args,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

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

    returncode = process.wait()
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
    kwargs = {
        "cwd": working_directory,
    }
    process = subprocess.Popen(command_args, **kwargs)

    root_widget = globals().get("root")
    while process.poll() is None:
        if root_widget is not None and root_widget.winfo_exists():
            try:
                root_widget.update_idletasks()
                root_widget.update()
            except tk.TclError:
                root_widget = None
        time.sleep(0.05)

    return subprocess.CompletedProcess(
        command_args,
        process.returncode,
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

def run_tg_upload(arguments):
    command_args = [get_tg_upload_python(), "-u", "tg-upload.py", *arguments]
    log_message(f"Executing command: {' '.join(command_args)}")
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
        client = Client("profile", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
        with client:
            message = client.get_messages(chat_id, msg_id)
            if message and message.caption:
                # Use caption as filename, ensure it ends with .bin
                caption = message.caption.strip()
                # Remove any .partXX that might be in the caption
                if '.part' in caption:
                    caption = caption.split('.part')[0]
                if not caption.endswith('.bin'):
                    caption = caption + '.bin'
                return caption
            elif message and message.document and message.document.file_name:
                # Fallback to document filename if no caption
                filename = message.document.file_name
                # Remove .partXX if present
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
    try:
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
    messagebox.showinfo("Info", "Authorization complete.")

# Removed browse_txt_file function - using text field instead

def browse_upload():
    source_type = var_source_type_upload.get()
    if source_type == "File":
        file_path = filedialog.askopenfilename()
        if file_path:
            entry_upload_path.delete(0, tk.END)
            entry_upload_path.insert(0, file_path)
    else:
        folder_path = filedialog.askdirectory()
        if folder_path:
            entry_upload_path.delete(0, tk.END)
            entry_upload_path.insert(0, folder_path)

def browse_download_directory():
    download_directory = filedialog.askdirectory()
    entry_download_dir.delete(0, tk.END)
    entry_download_dir.insert(0, download_directory)

def download():
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

        command_args = ["--profile", "profile", "--chat_id", str(chat_id), "--dl_dir", directory]

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
            run_tg_upload(command_args)
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
        set_download_button_busy(False)

def browse_upload_directory():
    upload_directory = filedialog.askdirectory()
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

        min_split_size = 2 * 1024 * 1024 * 1024  # Telegram per-file upload limit: 2GB
        if delete_on_done:
            log_message("Processing: Upload - Delete on Done is enabled, each source file will be removed after its upload finishes")
        else:
            log_message("SKIPPED: Delete on Done - Option disabled by user")

        found_any_file = False
        for original_file in iter_upload_source_files(source_type, selected_path):
            found_any_file = True
            current_file = original_file
            files_to_upload = []
            files_to_delete = set()

            if delete_on_done:
                files_to_delete.add(original_file)
                log_message(f"Processing: Upload - Marking original source for deletion after upload: {original_file}")

            try:
                if encrypt_before_upload:
                    if os.path.basename(current_file).lower().endswith(".bin"):
                        log_message(f"SKIPPED: {os.path.basename(current_file)} - Encryption not needed because file already ends with .bin")
                    else:
                        current_file = encrypt_file_for_upload(current_file, config_file_path)
                        if delete_on_done:
                            files_to_delete.add(current_file)
                else:
                    log_message(f"SKIPPED: {os.path.basename(current_file)} - Encrypt option disabled by user")
            except Exception as exc:
                messagebox.showerror("Encryption Error", str(exc))
                return

            if ".part" in os.path.basename(current_file):
                log_message(f"SKIPPED: {os.path.basename(current_file)} - File is already a split part and will be uploaded as-is")
                files_to_upload.append(current_file)
                if delete_on_done:
                    files_to_delete.add(current_file)
            elif split_files:
                file_size = os.path.getsize(current_file)
                if file_size > min_split_size:
                    part_files = split_file(current_file, split_size=1500 * 1024 * 1024)
                    files_to_upload.extend(part_files)
                    if delete_on_done:
                        files_to_delete.update(part_files)
                        files_to_delete.add(current_file)
                        log_message(f"Processing: Upload - Marking generated split parts for deletion: {', '.join(part_files)}")
                        log_message(f"Processing: Upload - Marking intermediate file for deletion after split upload: {current_file}")
                else:
                    log_message(
                        f"SKIPPED: {os.path.basename(current_file)} - Split not needed because file size "
                        f"({file_size} bytes) is within the 2GB upload limit"
                    )
                    files_to_upload.append(current_file)
                    if delete_on_done:
                        files_to_delete.add(current_file)
            else:
                log_message(f"SKIPPED: {os.path.basename(current_file)} - Split option disabled by user")
                files_to_upload.append(current_file)
                if delete_on_done:
                    files_to_delete.add(current_file)

            for file_path in files_to_upload:
                filename = os.path.basename(file_path)
                log_message(f"Processing: Upload - Uploading file now: {filename}")
                try:
                    run_tg_upload([
                        "--profile", "profile",
                        "--path", file_path,
                        "--chat_id", str(chat_id),
                        "--caption", filename,
                    ])
                except Exception as exc:
                    show_copyable_error("Upload Error", str(exc))
                    return

            if delete_on_done:
                for file_path in sorted(files_to_delete):
                    if os.path.exists(file_path):
                        try:
                            log_message(f"Processing: Upload - Deleting file: {file_path}")
                            os.remove(file_path)
                            log_message(f"Completed: Upload - Deleted file: {file_path}")
                        except OSError:
                            log_message(f"SKIPPED: Delete on Done - Failed to delete file: {file_path}")
                    else:
                        log_message(f"SKIPPED: Delete on Done - File already missing, nothing to delete: {file_path}")

                if source_type == "Folder":
                    delete_empty_directories(os.path.dirname(original_file), selected_path)

        if not found_any_file:
            messagebox.showerror("Error", f"No files were found under: {selected_path}")
            return
        messagebox.showinfo("Info", "Upload complete.")
    finally:
        set_upload_button_busy(False)

# Create the main window
root = tk.Tk()
root.title("Telegram Upload/Download")

# Authorization Section
frame_authorize = tk.Frame(root, padx=10, pady=10)
frame_authorize.pack()

button_authorize = tk.Button(frame_authorize, text="Authorize", command=authorize, bg="lightblue")
button_authorize.pack(pady=5)

# Download section
frame_download = tk.Frame(root, padx=10, pady=10)
frame_download.pack()

label_channel = tk.Label(frame_download, text="Channel:")
label_channel.grid(row=0, column=0, padx=5, pady=5)

default_channel = CHANNELS[0][0] if CHANNELS else "Custom Channel"
var_channel = tk.StringVar(value=default_channel)
download_channel_col = 1
for name, _chat_id in CHANNELS:
    tk.Radiobutton(frame_download, text=name, variable=var_channel, value=name).grid(
        row=0, column=download_channel_col, padx=5, pady=5
    )
    download_channel_col += 1
tk.Radiobutton(frame_download, text="Custom Channel", variable=var_channel, value="Custom Channel").grid(
    row=0, column=download_channel_col, padx=5, pady=5
)

label_custom_chat_id = tk.Label(frame_download, text="Custom Chat ID:")
label_custom_chat_id.grid(row=1, column=0, padx=5, pady=5)

entry_custom_chat_id = tk.Entry(frame_download, width=30)
entry_custom_chat_id.grid(row=1, column=1, columnspan=3, padx=5, pady=5)

label_tg_links = tk.Label(frame_download, text="TG Links:")
label_tg_links.grid(row=2, column=0, padx=5, pady=5, sticky="nw")

text_tg_links = tk.Text(frame_download, width=50, height=10, wrap=tk.WORD)
text_tg_links.grid(row=2, column=1, columnspan=2, padx=5, pady=5, sticky="ew")

# Add scrollbar for the text widget
scrollbar_tg_links = tk.Scrollbar(frame_download, orient=tk.VERTICAL, command=text_tg_links.yview)
scrollbar_tg_links.grid(row=2, column=3, sticky="ns", padx=(0, 5), pady=5)
text_tg_links.config(yscrollcommand=scrollbar_tg_links.set)

var_combine = tk.BooleanVar(value=True)
check_combine = tk.Checkbutton(frame_download, text="Combine", variable=var_combine)
check_combine.grid(row=3, column=0, padx=5, pady=5)

var_decrypt_download = tk.BooleanVar(value=True)
check_decrypt_download = tk.Checkbutton(frame_download, text="Decrypt", variable=var_decrypt_download)
check_decrypt_download.grid(row=3, column=1, padx=5, pady=5)

label_download_dir = tk.Label(frame_download, text="Save to...")
label_download_dir.grid(row=4, column=0, padx=5, pady=5)

entry_download_dir = tk.Entry(frame_download, width=50)
entry_download_dir.grid(row=4, column=1, padx=5, pady=5)

button_browse_download_dir = tk.Button(frame_download, text="Browse", command=browse_download_directory)
button_browse_download_dir.grid(row=4, column=2, padx=5, pady=5)

button_download = tk.Button(frame_download, text="Download", command=download)
button_download.grid(row=5, column=1, padx=5, pady=5)

# Upload section
frame_upload = tk.Frame(root, padx=10, pady=10)
frame_upload.pack()

label_channel_upload = tk.Label(frame_upload, text="Channel:")
label_channel_upload.grid(row=0, column=0, padx=5, pady=5)

var_channel_upload = tk.StringVar(value=default_channel)
upload_channel_col = 1
for name, _chat_id in CHANNELS:
    tk.Radiobutton(frame_upload, text=name, variable=var_channel_upload, value=name).grid(
        row=0, column=upload_channel_col, padx=5, pady=5
    )
    upload_channel_col += 1
tk.Radiobutton(frame_upload, text="Custom Channel", variable=var_channel_upload, value="Custom Channel").grid(
    row=0, column=upload_channel_col, padx=5, pady=5
)

label_custom_chat_id_upload = tk.Label(frame_upload, text="Custom Chat ID:")
label_custom_chat_id_upload.grid(row=1, column=0, padx=5, pady=5)

entry_custom_chat_id_upload = tk.Entry(frame_upload, width=30)
entry_custom_chat_id_upload.grid(row=1, column=1, columnspan=3, padx=5, pady=5)

label_source_type_upload = tk.Label(frame_upload, text="Source Type:")
label_source_type_upload.grid(row=5, column=0, padx=5, pady=5)
var_source_type_upload = tk.StringVar(value="File")
radio_file_upload = tk.Radiobutton(frame_upload, text="File", variable=var_source_type_upload, value="File")
radio_file_upload.grid(row=5, column=1, padx=5, pady=5)
radio_folder_upload = tk.Radiobutton(frame_upload, text="Folder", variable=var_source_type_upload, value="Folder")
radio_folder_upload.grid(row=5, column=2, padx=5, pady=5)

label_upload_path = tk.Label(frame_upload, text="Upload Path:")
label_upload_path.grid(row=6, column=0, padx=5, pady=5)
entry_upload_path = tk.Entry(frame_upload, width=50)
entry_upload_path.grid(row=6, column=1, padx=5, pady=5)
button_browse_upload = tk.Button(frame_upload, text="Browse", command=browse_upload)
button_browse_upload.grid(row=6, column=2, padx=5, pady=5)

var_delete_on_done = tk.BooleanVar(value=True)
check_delete_on_done = tk.Checkbutton(frame_upload, text="Delete on Done", variable=var_delete_on_done)
check_delete_on_done.grid(row=3, column=0, padx=5, pady=5)

var_split = tk.BooleanVar(value=True)
check_split = tk.Checkbutton(frame_upload, text="Split Files", variable=var_split)
check_split.grid(row=3, column=1, padx=5, pady=5)

var_encrypt_upload = tk.BooleanVar(value=True)
check_encrypt_upload = tk.Checkbutton(frame_upload, text="Encrypt", variable=var_encrypt_upload)
check_encrypt_upload.grid(row=3, column=2, padx=5, pady=5)

button_upload = tk.Button(frame_upload, text="Upload", command=upload)
button_upload.grid(row=7, column=1, padx=5, pady=5)

root.mainloop()
