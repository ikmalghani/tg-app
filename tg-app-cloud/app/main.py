import os
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
        "result": job.result,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


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


def _register_handlers():
    settings = get_settings()

    def handle_authorize(job):
        def on_progress(_kind, pct, msg):
            job_queue.update(
                job.id,
                progress=pct if pct >= 0 else job.progress,
                message=msg[:500],
            )

        pipeline.authorize(on_progress=on_progress)
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

        def on_progress(_kind, pct, msg):
            job_queue.update(
                job.id,
                progress=pct if pct >= 0 else job.progress,
                message=msg[:500],
            )

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
        job_dir = job.payload["job_dir"]

        required = estimate_download_bytes(len(links))
        check = check_free_space(settings["data_dir"], required, settings["disk_reserve_bytes"])
        if not check.ok:
            raise RuntimeError(check.message)

        def on_progress(_kind, pct, msg):
            job_queue.update(
                job.id,
                progress=pct if pct >= 0 else job.progress,
                message=msg[:500],
            )

        files = pipeline.process_download(
            links=links,
            chat_id=chat_id,
            combine=combine,
            decrypt=decrypt,
            job_dir=job_dir,
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
    session_path = os.path.join(settings["tg_upload_dir"], "profile.session")
    return {
        "channels": settings["channels"],
        "authorized": os.path.exists(session_path),
        "disk": {
            "total": total,
            "used": used,
            "free": free,
            "free_human": format_bytes(free),
            "reserve": settings["disk_reserve_bytes"],
            "reserve_human": format_bytes(settings["disk_reserve_bytes"]),
        },
    }


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

    batch_id = os.urandom(4).hex()
    batch_dir = os.path.join(settings["uploads_dir"], batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    jobs_out = []
    saved_paths = []
    running_required = 0
    try:
        for upload in files:
            safe_name = os.path.basename(upload.filename or "upload.bin")
            dest = os.path.join(batch_dir, safe_name)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(safe_name)
                dest = os.path.join(batch_dir, f"{stem}_{os.urandom(3).hex()}{ext}")

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
                            for path in saved_paths:
                                if os.path.exists(path):
                                    os.remove(path)
                            shutil.rmtree(batch_dir, ignore_errors=True)
                            raise HTTPException(status_code=507, detail=check.message)
                    out.write(chunk)

            file_required = estimate_upload_bytes(size, encrypt=encrypt, split=split)
            running_required += file_required
            final_check = check_free_space(
                settings["data_dir"], running_required, settings["disk_reserve_bytes"]
            )
            if not final_check.ok:
                os.remove(dest)
                for path in saved_paths:
                    if os.path.exists(path):
                        os.remove(path)
                shutil.rmtree(batch_dir, ignore_errors=True)
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
            "batch_id": batch_id,
            "jobs": jobs_out,
            "disk_check": check_free_space(
                settings["data_dir"], running_required, settings["disk_reserve_bytes"]
            ).__dict__,
        }
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(batch_dir, ignore_errors=True)
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

    job_id_preview = os.urandom(6).hex()
    job_dir = os.path.join(settings["downloads_dir"], job_id_preview)
    os.makedirs(job_dir, exist_ok=True)

    job = job_queue.enqueue(
        JobKind.DOWNLOAD,
        {
            "links": links,
            "chat_id": chat_id,
            "combine": combine,
            "decrypt": decrypt,
            "job_dir": job_dir,
        },
        file_name=f"{len(links)} link(s)",
        file_size=0,
    )
    new_dir = os.path.join(settings["downloads_dir"], job.id)
    if job_dir != new_dir:
        os.rename(job_dir, new_dir)
        job.payload["job_dir"] = new_dir

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
