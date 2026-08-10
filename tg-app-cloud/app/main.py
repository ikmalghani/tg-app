import os
import re
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .disk import (
    check_free_space,
    disk_usage,
    estimate_download_bytes,
    estimate_upload_bytes,
    format_bytes,
)
from . import pipeline
from .worker import JobKind, JobStatus, job_queue

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _redact_command(command: str) -> str:
    text = command or ""
    text = re.sub(r"(--bot)\s+\S+", r"\1 ***", text)
    text = re.sub(r"(--api_hash)\s+\S+", r"\1 ***", text)
    return text


def _job_to_dict(job):
    return {
        "id": job.id,
        "kind": job.kind.value,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "file_name": job.file_name,
        "file_size": job.file_size,
        "error": job.error,
        "command": job.command,
        "log": job.log,
        "result": job.result,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _progress_logger(job_id: str):
    def on_progress(kind, pct, msg):
        text = (msg or "").strip()
        if kind == "command":
            redacted = _redact_command(text)
            job_queue.update(job_id, command=redacted)
            job_queue.append_log(job_id, f"$ {redacted}")
            return
        if text:
            job_queue.append_log(job_id, text)
        current = job_queue.get(job_id)
        job_queue.update(
            job_id,
            progress=pct if pct >= 0 else (current.progress if current else 0),
            message=(text[:500] if text else (current.message if current else "")),
        )

    return on_progress


def _resolve_chat_id(channel: str, custom_chat_id: str) -> str:
    settings = get_settings()
    if channel == "Custom Channel":
        chat_id = (custom_chat_id or "").strip()
        if not chat_id:
            raise HTTPException(status_code=400, detail="Custom Chat ID is required")
        return chat_id
    chat_id = settings["channel_map"].get(channel, "")
    if not chat_id:
        raise HTTPException(status_code=400, detail=f"Unknown channel: {channel}")
    return chat_id


def _crypt_status():
    settings = get_settings()
    path = settings["crypt_config"]
    exists = os.path.isfile(path)
    configured = False
    size = 0
    if exists:
        size = os.path.getsize(path)
        from .pipeline import _crypt_passwords_configured

        configured = _crypt_passwords_configured(path)
    return {
        "exists": exists,
        "configured": configured,
        "path": path,
        "size": size,
        "size_human": format_bytes(size) if exists else "—",
    }


def _save_crypt_content(content: str):
    text = (content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="crypt.conf content is empty — paste the full file first")
    if "[crypt]" not in text:
        raise HTTPException(
            status_code=400,
            detail="crypt.conf must include an rclone [crypt] remote section",
        )
    # Reject blank password templates
    has_password = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("password") and "=" in line:
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                has_password = True
                break
    if not has_password:
        raise HTTPException(
            status_code=400,
            detail="crypt.conf passwords are empty — paste your real rclone crypt passwords",
        )
    settings = get_settings()
    dest = settings["crypt_config_writable"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    get_settings.cache_clear()
    return _crypt_status()


def _persist_session_to_data() -> None:
    settings = get_settings()
    src_dir = settings["tg_upload_dir"]
    data_dir = settings["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    for name in ("profile.session", "profile.session-journal"):
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(data_dir, name))


def _session_present() -> bool:
    settings = get_settings()
    paths = [
        os.path.join(settings["tg_upload_dir"], "profile.session"),
        os.path.join(settings["data_dir"], "profile.session"),
    ]
    return any(os.path.isfile(p) for p in paths)


def _register_handlers():
    settings = get_settings()

    def handle_authorize(job):
        on_progress = _progress_logger(job.id)
        pipeline.authorize(on_progress=on_progress)
        _persist_session_to_data()
        job_queue.update(job.id, message="Authorization complete", progress=100)

    def handle_upload(job):
        path = job.payload["path"]
        chat_id = job.payload["chat_id"]
        encrypt = job.payload.get("encrypt", True)
        split = job.payload.get("split", True)
        delete_on_done = job.payload.get("delete_on_done", True)

        size = os.path.getsize(path) if os.path.exists(path) else job.file_size
        required = estimate_upload_bytes(size, encrypt=encrypt, split=split)
        check = check_free_space(settings["data_dir"], required, settings["disk_reserve_bytes"])
        if not check.ok:
            raise RuntimeError(check.message)

        on_progress = _progress_logger(job.id)
        pipeline.process_upload_file(
            file_path=path,
            chat_id=chat_id,
            encrypt=encrypt,
            split=split,
            delete_on_done=delete_on_done,
            on_progress=on_progress,
        )
        job_queue.update(job.id, message="Uploaded to Telegram", progress=100)

    def handle_download(job):
        links = job.payload["links"]
        chat_id = job.payload["chat_id"]
        combine = job.payload.get("combine", True)
        decrypt = job.payload.get("decrypt", True)

        required = estimate_download_bytes(len(links))
        check = check_free_space(settings["data_dir"], required, settings["disk_reserve_bytes"])
        if not check.ok:
            raise RuntimeError(check.message)

        on_progress = _progress_logger(job.id)
        files = pipeline.process_download(
            links=links,
            chat_id=chat_id,
            combine=combine,
            decrypt=decrypt,
            on_progress=on_progress,
        )
        relative = [os.path.relpath(f, settings["data_dir"]) for f in files]
        job.result = {"files": relative}
        job_queue.update(
            job.id,
            message=f"Ready ({len(files)} file(s))",
            progress=100,
            result={"files": relative},
        )

    job_queue.register(JobKind.AUTHORIZE, handle_authorize)
    job_queue.register(JobKind.UPLOAD, handle_upload)
    job_queue.register(JobKind.DOWNLOAD, handle_download)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _register_handlers()
    job_queue.start()
    yield
    job_queue.stop()


app = FastAPI(title="tg-app-cloud", lifespan=lifespan)


@app.get("/api/health")
def health():
    total, used, free = disk_usage(get_settings()["data_dir"])
    return {
        "ok": True,
        "disk": {
            "total": total,
            "used": used,
            "free": free,
            "free_human": format_bytes(free),
            "reserve": get_settings()["disk_reserve_bytes"],
        },
    }


@app.get("/")
def app_page():
    return FileResponse(os.path.join(STATIC_DIR, "app.html"))


@app.get("/api/config")
def api_config():
    settings = get_settings()
    total, used, free = disk_usage(settings["data_dir"])
    return {
        "channels": settings["channels"],
        "authorized": _session_present(),
        "crypt": _crypt_status(),
        "disk": {
            "total": total,
            "used": used,
            "free": free,
            "free_human": format_bytes(free),
            "reserve": settings["disk_reserve_bytes"],
            "reserve_human": format_bytes(settings["disk_reserve_bytes"]),
        },
    }


@app.get("/api/crypt-config")
def api_crypt_config_get():
    return _crypt_status()


@app.post("/api/crypt-config")
async def api_crypt_config_set(request: Request):
    body = await request.json()
    text = body.get("content", "")
    return {"ok": True, "crypt": _save_crypt_content(text)}


@app.post("/api/crypt-config/upload")
async def api_crypt_config_upload(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="crypt.conf must be UTF-8 text") from exc
    return {"ok": True, "crypt": _save_crypt_content(text)}


@app.post("/api/authorize")
def api_authorize():
    job = job_queue.enqueue(JobKind.AUTHORIZE, {})
    return _job_to_dict(job)


@app.post("/api/upload")
async def api_upload(
    files: list[UploadFile] = File(...),
    channel: str = Form(...),
    custom_chat_id: str = Form(""),
    encrypt: bool = Form(True),
    split: bool = Form(True),
    delete_on_done: bool = Form(True),
):
    settings = get_settings()
    chat_id = _resolve_chat_id(channel, custom_chat_id)
    uploads_dir = settings["uploads_dir"]
    os.makedirs(uploads_dir, exist_ok=True)

    jobs_out = []
    saved_paths = []
    running_required = 0

    def _cleanup_saved():
        for path in saved_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    try:
        for upload in files:
            safe_name = os.path.basename(upload.filename or "upload.bin")
            dest = os.path.join(uploads_dir, safe_name)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(safe_name)
                dest = os.path.join(uploads_dir, f"{stem}_{os.urandom(3).hex()}{ext}")

            size = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size == len(chunk) or size % (32 * 1024 * 1024) < len(chunk):
                        tentative = running_required + estimate_upload_bytes(
                            size, encrypt=encrypt, split=split
                        )
                        check = check_free_space(
                            settings["data_dir"], tentative, settings["disk_reserve_bytes"]
                        )
                        if not check.ok:
                            out.close()
                            os.remove(dest)
                            _cleanup_saved()
                            raise HTTPException(status_code=507, detail=check.message)
                    out.write(chunk)

            file_required = estimate_upload_bytes(size, encrypt=encrypt, split=split)
            running_required += file_required
            final_check = check_free_space(
                settings["data_dir"], running_required, settings["disk_reserve_bytes"]
            )
            if not final_check.ok:
                os.remove(dest)
                _cleanup_saved()
                raise HTTPException(status_code=507, detail=final_check.message)

            saved_paths.append(dest)

            job = job_queue.enqueue(
                JobKind.UPLOAD,
                {
                    "path": dest,
                    "chat_id": chat_id,
                    "encrypt": encrypt,
                    "split": split,
                    "delete_on_done": delete_on_done,
                },
                file_name=os.path.basename(dest),
                file_size=size,
            )
            jobs_out.append(_job_to_dict(job))

        return {
            "ok": True,
            "jobs": jobs_out,
            "disk_check": check_free_space(
                settings["data_dir"], running_required, settings["disk_reserve_bytes"]
            ).__dict__,
        }
    except HTTPException:
        raise
    except Exception as exc:
        _cleanup_saved()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/download")
async def api_download(request: Request):
    settings = get_settings()
    body = await request.json()
    links_raw = body.get("links", "")
    if isinstance(links_raw, list):
        links = [str(x).strip() for x in links_raw if str(x).strip()]
    else:
        links = [line.strip() for line in str(links_raw).splitlines() if line.strip()]
    if not links:
        raise HTTPException(status_code=400, detail="Enter at least one Telegram link")

    channel = body.get("channel", "Custom Channel")
    custom_chat_id = body.get("custom_chat_id", "")
    chat_id = _resolve_chat_id(channel, custom_chat_id)
    combine = bool(body.get("combine", True))
    decrypt = bool(body.get("decrypt", True))

    required = estimate_download_bytes(len(links))
    check = check_free_space(settings["data_dir"], required, settings["disk_reserve_bytes"])
    if not check.ok:
        raise HTTPException(status_code=507, detail=check.message)

    job = job_queue.enqueue(
        JobKind.DOWNLOAD,
        {
            "links": links,
            "chat_id": chat_id,
            "combine": combine,
            "decrypt": decrypt,
        },
        file_name=f"{len(links)} link(s)",
        file_size=0,
    )

    return {
        "ok": True,
        "job": _job_to_dict(job),
        "disk_check": check.__dict__,
    }


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": [_job_to_dict(j) for j in job_queue.list_jobs()]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = job_queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


def _downloads_root() -> str:
    return get_settings()["downloads_dir"]


def _safe_download_path(rel_path: str) -> str:
    root = os.path.realpath(_downloads_root())
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if not rel or rel in (".", "..") or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Path outside downloads")
    return full


@app.get("/api/downloads/files")
def api_downloads_list():
    settings = get_settings()
    root = settings["downloads_dir"]
    os.makedirs(root, exist_ok=True)
    files = []
    errors = []
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                try:
                    st = os.stat(full)
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
                    continue
                files.append(
                    {
                        "path": rel,
                        "name": name,
                        "size": st.st_size,
                        "size_human": format_bytes(st.st_size),
                        "mtime": st.st_mtime,
                        "job_id": rel.split("/", 1)[0] if "/" in rel else "",
                    }
                )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot read downloads dir {root}: {exc}",
        ) from exc
    files.sort(key=lambda f: f["mtime"], reverse=True)
    total, used, free = disk_usage(settings["data_dir"])
    return {
        "files": files,
        "count": len(files),
        "root": root,
        "errors": errors,
        "disk": {
            "total": total,
            "used": used,
            "free": free,
            "free_human": format_bytes(free),
            "used_human": format_bytes(used),
            "total_human": format_bytes(total),
            "reserve": settings["disk_reserve_bytes"],
            "reserve_human": format_bytes(settings["disk_reserve_bytes"]),
        },
    }


@app.get("/api/downloads/file")
def api_downloads_get(path: str):
    full = _safe_download_path(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        full,
        filename=os.path.basename(full),
        media_type="application/octet-stream",
    )


@app.delete("/api/downloads/file")
async def api_downloads_delete(request: Request):
    body = await request.json()
    rel = body.get("path", "")
    full = _safe_download_path(rel)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(full)
    # Remove empty parent job directories
    root = os.path.realpath(_downloads_root())
    parent = os.path.dirname(full)
    while parent.startswith(root + os.sep) and parent != root:
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)
    total, used, free = disk_usage(get_settings()["data_dir"])
    return {
        "ok": True,
        "disk": {
            "free": free,
            "free_human": format_bytes(free),
            "used_human": format_bytes(used),
            "total_human": format_bytes(total),
        },
    }


@app.get("/api/files/{job_id}/{file_name}")
def api_file(job_id: str, file_name: str):
    settings = get_settings()
    job = job_queue.get(job_id)
    if not job or job.kind != JobKind.DOWNLOAD:
        raise HTTPException(status_code=404, detail="Download job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=409, detail="Job not finished")

    safe_name = os.path.basename(file_name)
    path = os.path.join(settings["downloads_dir"], job_id, safe_name)
    if not os.path.isfile(path):
        for rel in job.result.get("files", []):
            candidate = os.path.join(settings["data_dir"], rel)
            if os.path.basename(candidate) == safe_name and os.path.isfile(candidate):
                path = candidate
                break
        else:
            raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path,
        filename=safe_name,
        media_type="application/octet-stream",
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
