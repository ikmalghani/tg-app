import shutil
from dataclasses import dataclass


@dataclass
class DiskCheckResult:
    ok: bool
    free_bytes: int
    total_bytes: int
    used_bytes: int
    required_bytes: int
    reserve_bytes: int
    message: str


def disk_usage(path: str):
    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free


def estimate_upload_bytes(file_size: int, encrypt: bool, split: bool) -> int:
    """
    Worst-case staging space while a job is in flight.

    - Incoming upload already needs `file_size`.
    - Encrypt uses rclone move → roughly same footprint (plaintext replaced by .bin).
    - Split keeps the source while writing parts → up to another `file_size`.
    - Small overhead buffer for captions/temp lists.
    """
    if file_size < 0:
        file_size = 0
    needed = file_size
    if split:
        needed += file_size
    # Encrypt alone does not double space (move), but leave a small cushion.
    if encrypt:
        needed += max(16 * 1024 * 1024, int(file_size * 0.02))
    else:
        needed += 8 * 1024 * 1024
    return needed


def estimate_download_bytes(link_count: int) -> int:
    """Unknown remote sizes — require reserve + a soft per-link cushion."""
    cushion = max(link_count, 1) * 512 * 1024 * 1024  # 512 MiB per link soft estimate
    return cushion


def check_free_space(path: str, required_bytes: int, reserve_bytes: int) -> DiskCheckResult:
    total, used, free = disk_usage(path)
    available_for_job = max(0, free - reserve_bytes)
    ok = available_for_job >= required_bytes
    if ok:
        message = (
            f"OK: need {format_bytes(required_bytes)}, "
            f"available {format_bytes(available_for_job)} "
            f"(free {format_bytes(free)}, reserve {format_bytes(reserve_bytes)})"
        )
    else:
        message = (
            f"Not enough disk: need {format_bytes(required_bytes)}, "
            f"available {format_bytes(available_for_job)} "
            f"(free {format_bytes(free)}, reserve {format_bytes(reserve_bytes)}). "
            f"Free space or disable Split / process fewer files."
        )
    return DiskCheckResult(
        ok=ok,
        free_bytes=free,
        total_bytes=total,
        used_bytes=used,
        required_bytes=required_bytes,
        reserve_bytes=reserve_bytes,
        message=message,
    )


def format_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"
