import importlib
import os
import shutil
import tempfile
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox

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

# Split function from split.py
def split_file(file_path, split_size=1500 * 1024 * 1024):
    file_size = os.path.getsize(file_path)
    num_parts = -(-file_size // split_size)  # Ceiling division

    part_files = []
    with open(file_path, 'rb') as f:
        for i in range(num_parts):
            part_file_name = f"{file_path}.part{i:02d}"
            with open(part_file_name, 'wb') as part_file:
                part_file.write(f.read(split_size))
            part_files.append(part_file_name)

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
    # Get captions from all links
    captions = []
    if links_list:
        for link in links_list:
            caption = get_caption_from_link(link)
            if caption:
                captions.append(caption)
    
    for root, dirs, files in os.walk(directory):
        part_prefixes = set()
        part_prefix_to_files = {}
        
        for file_name in files:
            if ".part" in file_name:
                # Extract base filename by removing .partXX
                part_prefix = file_name.split(".part")[0]
                part_prefixes.add(part_prefix)
                if part_prefix not in part_prefix_to_files:
                    part_prefix_to_files[part_prefix] = []
                part_prefix_to_files[part_prefix].append(file_name)

        # Try to match part prefixes to captions
        used_captions = set()
        for part_prefix in part_prefixes:
            part_files = part_prefix_to_files[part_prefix]
            
            # Sort by the part number to ensure correct order
            def get_part_number(fname):
                try:
                    part_str = fname.split(".part")[1]
                    return int(part_str)
                except (ValueError, IndexError):
                    return 0
            part_files = sorted(part_files, key=get_part_number)
            
            if len(part_files) > 1:  # Ensure there are multiple parts
                # Try to find a matching caption
                final_filename = None
                
                # Normalize part_prefix for comparison
                # Remove .bin from part_prefix for comparison since captions might have it
                part_prefix_for_match = part_prefix
                if part_prefix.endswith('.bin'):
                    part_prefix_for_match = part_prefix[:-4]  # Remove .bin
                
                part_prefix_normalized = part_prefix_for_match.lower().replace('_', '.').replace('-', '.')
                
                # Try to match captions to part files
                for caption in captions:
                    if caption in used_captions:
                        continue
                    
                    # Remove .bin from caption for comparison
                    caption_for_match = caption
                    if caption.endswith('.bin'):
                        caption_for_match = caption[:-4]  # Remove .bin
                    
                    caption_base = caption_for_match.lower().replace('_', '.').replace('-', '.')
                    
                    # Check various matching strategies
                    # 1. Exact match (normalized)
                    if part_prefix_normalized == caption_base:
                        final_filename = caption  # Use caption as-is (it already has .bin)
                        used_captions.add(caption)
                        break
                    
                    # 2. Part prefix is contained in caption or vice versa
                    if part_prefix_normalized in caption_base or caption_base in part_prefix_normalized:
                        # Make sure it's a significant match (at least 10 characters)
                        min_len = min(len(part_prefix_normalized), len(caption_base))
                        if min_len >= 10:
                            final_filename = caption  # Use caption as-is (it already has .bin)
                            used_captions.add(caption)
                            break
                    
                    # 3. Check if they share a significant common substring
                    if len(part_prefix_normalized) >= 10 and len(caption_base) >= 10:
                        # Check if first 10 chars match or last 10 chars match
                        if (part_prefix_normalized[:10] in caption_base or 
                            caption_base[:10] in part_prefix_normalized or
                            part_prefix_normalized[-10:] in caption_base or
                            caption_base[-10:] in part_prefix_normalized):
                            final_filename = caption  # Use caption as-is (it already has .bin)
                            used_captions.add(caption)
                            break
                
                # If still no match, use the first unused caption (for cases where order matters)
                if not final_filename and captions:
                    for caption in captions:
                        if caption not in used_captions:
                            final_filename = caption
                            used_captions.add(caption)
                            break
                
                # Fallback to part_prefix with .bin extension
                if not final_filename:
                    if not part_prefix.endswith('.bin'):
                        final_filename = part_prefix + '.bin'
                    else:
                        final_filename = part_prefix
                
                combined_file = os.path.join(root, final_filename)
                
                with open(combined_file, "wb") as combined:
                    for part_file in part_files:
                        part_file_path = os.path.join(root, part_file)
                        with open(part_file_path, "rb") as pf:
                            shutil.copyfileobj(pf, combined)
                        os.remove(part_file_path)  # Optionally delete the part file

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
    target_directory = UPLOAD_DIR
    if os.getcwd() != target_directory:
        os.chdir(target_directory)
    
    command = f'python tg-upload.py --profile profile --api_id {API_ID} --api_hash {API_HASH} --bot {BOT_TOKEN} --login_only'
    os.system(command)
    messagebox.showinfo("Info", "Authorization complete.")

# Removed browse_txt_file function - using text field instead

def browse_upload():
    source_type = var_source_type_upload.get()
    if source_type == "File":
        file_path = filedialog.askopenfilename(filetypes=[("Binary files", "*.bin"), ("Part files", "*.part*")])
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
    target_directory = UPLOAD_DIR
    if os.getcwd() != target_directory:
        os.chdir(target_directory)

    channel = var_channel.get()
    if channel == "Custom Channel":
        chat_id = entry_custom_chat_id.get()
    else:
        chat_id = CHANNEL_MAP.get(channel, "")

    directory = entry_download_dir.get()
    combine = var_combine.get()

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

    command = f'python tg-upload.py --profile profile --chat_id {chat_id} --dl_dir "{directory}"'

    # Auto-detect: 1 link = Single, multiple links = Batch
    if len(links_list) == 1:
        # Single mode
        command += f' --dl --links "{links_list[0]}"'
    else:
        # Batch mode - create temporary file with links
        temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', prefix='tg_links_')
        temp_file.write('\n'.join(links_list))
        temp_file.close()
        command += f' --dl --txt_file "{temp_file.name}"'

    os.system(command)
    
    # Combine files after download if requested
    if combine and directory:
        combine_files(directory, links_list)
    else:
        # If not combining, rename files using captions
        if directory and links_list:
            rename_files_with_captions(directory, links_list)
    
    messagebox.showinfo("Info", "Download complete.")

def browse_upload_directory():
    upload_directory = filedialog.askdirectory()
    entry_upload_path.delete(0, tk.END)
    entry_upload_path.insert(0, upload_directory)

def upload():
    target_directory = UPLOAD_DIR
    if os.getcwd() != target_directory:
        os.chdir(target_directory)

    channel = var_channel_upload.get()
    if channel == "Custom Channel":
        chat_id = entry_custom_chat_id_upload.get()
    else:
        chat_id = CHANNEL_MAP.get(channel, "")

    source_type = var_source_type_upload.get()
    delete_on_done = var_delete_on_done.get()
    split_files = var_split.get()

    files = []
    if source_type == "File":
        file_path = entry_upload_path.get()
        if file_path and is_allowed_file(os.path.basename(file_path)):
            files = [file_path]
    else:
        folder_path = entry_upload_path.get()
        if folder_path:
            files = [os.path.join(folder_path, f) for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f)) and is_allowed_file(f)]

    # Split files before upload if needed
    files_to_upload = []
    min_split_size = 2 * 1024 * 1024 * 1024  # 2GB in bytes
    for file_path in files:
        # Skip if already a part file
        if ".part" in os.path.basename(file_path):
            files_to_upload.append(file_path)
        elif split_files:
            # Check file size - only split if 2GB or larger
            file_size = os.path.getsize(file_path)
            if file_size >= min_split_size:
                # Split the file
                part_files = split_file(file_path, split_size=1500 * 1024 * 1024)
                files_to_upload.extend(part_files)
                # Delete original file if delete_on_done is enabled
                if delete_on_done:
                    os.remove(file_path)
            else:
                # File is smaller than 2GB, upload as-is
                files_to_upload.append(file_path)
        else:
            files_to_upload.append(file_path)

    # Upload all files (including split parts)
    for file_path in files_to_upload:
        filename = os.path.basename(file_path)
        command = f'python tg-upload.py --profile profile --path "{file_path}" --chat_id {chat_id} --caption "{filename}"'
        if delete_on_done:
            command += ' --delete_on_done'
        os.system(command)
    messagebox.showinfo("Info", "Upload complete.")

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

var_combine = tk.BooleanVar(value=False)
check_combine = tk.Checkbutton(frame_download, text="Combine", variable=var_combine)
check_combine.grid(row=3, column=0, padx=5, pady=5)

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

var_split = tk.BooleanVar(value=False)
check_split = tk.Checkbutton(frame_upload, text="Split Files (1.5GB)", variable=var_split)
check_split.grid(row=3, column=1, padx=5, pady=5)

button_upload = tk.Button(frame_upload, text="Upload", command=upload)
button_upload.grid(row=7, column=1, padx=5, pady=5)

root.mainloop()
