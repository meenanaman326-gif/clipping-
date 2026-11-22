"""ClipForge browser-based video clipping demonstration."""
from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .processor import ClipError, acquire_video, process_video, youtube_video_url
from .live import LiveError, capture_live, validate_live_url

ROOT = Path(os.getenv("WORK_DIR", "/tmp/clipforge-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "3600"))
JOBS: dict[str, dict] = {}
Q: queue.Queue[str] = queue.Queue(maxsize=5)
LOCK = threading.Lock()


def expire_jobs():
    now = time.time()
    with LOCK:
        expired = [k for k, v in JOBS.items()
                   if (v["status"] in ("done", "error") and now - v["created"] > JOB_TTL)
                   or (v["status"] == "uploading" and now - v.get("last_activity", v["created"]) > 3600)]
        for k in expired:
            JOBS.pop(k, None)
    for key in expired:
        shutil.rmtree(ROOT / key, ignore_errors=True)


def update(key: str, **fields):
    with LOCK:
        if key in JOBS:
            JOBS[key].update(fields)


def append_clip(key: str, clip: dict):
    with LOCK:
        if key in JOBS:
            JOBS[key]["clips"].append(clip)


def worker():
    while True:
        key = Q.get()
        try:
            with LOCK:
                job = dict(JOBS[key])
            folder = ROOT / key
            if job["mode"] == "live":
                update(key, status="processing", progress=8, message="Connecting to live stream")
                try:
                    source = capture_live(
                        job["url"], folder,
                        lambda progress, message: update(key, progress=progress, message=message),
                        seconds=job.get("capture_seconds", 120)
                    )
                except LiveError as stream_error:
                    if not job.get("try_replay_on_live_failure"):
                        raise
                    # A YouTube /live/ URL may refer to an ended broadcast.
                    # If live capture fails, try its accessible VOD replay.
                    logging.warning("Live capture unavailable for job %s: %s", key, stream_error)
                    update(key, progress=9, message="Live capture unavailable; checking for a video replay")
                    try:
                        source = acquire_video(
                            job["url"], folder,
                            lambda progress, message: update(key, progress=progress, message=message)
                        )
                    except ClipError as replay_error:
                        raise ClipError(
                            "This YouTube stream could not be accessed for live capture or replay. "
                            "It may be offline, restricted, or unavailable from this server. "
                            "Try a supported live feed or upload an MP4 you can process."
                        ) from replay_error
            elif job["mode"] == "youtube":
                update(key, status="processing", progress=8, message="Connecting to YouTube and checking video availability")
                source = acquire_video(job["url"], folder,
                                       lambda progress, message: update(key, progress=progress, message=message))
            else:
                update(key, status="processing", progress=30, message="Checking uploaded video")
                source = folder / job["source_name"]
            update(key, progress=30, message="Video ready for highlight analysis")
            clips = process_video(source, folder, lambda progress, message: update(key, progress=progress, message=message),
                                  lambda clip: append_clip(key, clip),
                                  lambda count: update(key, planned_clips=count),
                                  quality=job.get("quality", "balanced"),
                                  mode=job.get("analysis_mode", "fast"))
            update(key, status="done", progress=100, message="Clips ready!", clips=clips)
            source.unlink(missing_ok=True)
        except (ClipError, LiveError) as exc:
            logging.warning("ClipForge job %s failed: %s", key, exc, exc_info=True)
            update(key, status="error", message=str(exc), progress=0)
        except Exception:
            logging.exception("Unexpected ClipForge failure for job %s", key)
            update(key, status="error", message="Processing failed. Try a shorter MP4 video.", progress=0)
        finally:
            Q.task_done()
            expire_jobs()


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=worker, name="clip-worker", daemon=True).start()
    yield


app = FastAPI(title="ClipForge — Smart Clip Maker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class URLJob(BaseModel):
    url: str
    quality: str = "balanced"
    analysis_mode: str = "fast"


def validate_analysis_mode(mode: str) -> str:
    if mode not in ("fast", "deep"):
        raise HTTPException(status_code=400, detail="Invalid highlight mode.")
    return mode


def validate_quality(quality: str) -> str:
    if quality not in ("fast", "balanced", "high"):
        raise HTTPException(status_code=400, detail="Invalid output quality.")
    return quality


def new_job(mode: str, **extra):
    expire_jobs()
    with LOCK:
        active = sum(1 for v in JOBS.values() if v["status"] in ("queued", "processing", "uploading"))
    if active >= 4 or Q.full():
        raise HTTPException(status_code=429, detail="All demo processing slots are busy. Try again later.")
    key = uuid.uuid4().hex
    (ROOT / key).mkdir(parents=True)
    with LOCK:
        JOBS[key] = {"id": key, "mode": mode, "created": time.time(), "status": "queued",
                     "progress": 0, "message": "Waiting in queue", "clips": [], "planned_clips": 0, **extra}
    return key


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/jobs/youtube")
def youtube_job(payload: URLJob):
    try:
        clean = youtube_video_url(payload.url)
    except ClipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # /live/<video-id> is an active stream or its later replay, not just a VOD.
    # Direct it to the live recorder first, then try replay if recording fails.
    path = urlparse(payload.url.strip()).path.rstrip("/")
    live_link = path.startswith("/live/") and path.count("/") == 2
    key = new_job(
        "live" if live_link else "youtube",
        url=clean,
        capture_seconds=120,
        try_replay_on_live_failure=live_link,
        quality=validate_quality(payload.quality),
        analysis_mode=validate_analysis_mode(payload.analysis_mode)
    )
    Q.put_nowait(key)
    return {"job_id": key}


class LiveJob(BaseModel):
    url: str
    capture_seconds: int = 120
    quality: str = "balanced"
    analysis_mode: str = "fast"


@app.post("/api/jobs/live")
def live_job(payload: LiveJob):
    try:
        clean = validate_live_url(payload.url)
    except LiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.capture_seconds not in (60, 120, 180):
        raise HTTPException(status_code=400, detail="Choose 60, 120 or 180 seconds.")
    key = new_job("live", url=clean, capture_seconds=payload.capture_seconds,
                  quality=validate_quality(payload.quality),
                  analysis_mode=validate_analysis_mode(payload.analysis_mode))
    Q.put_nowait(key)
    return {"job_id": key}


class UploadStart(BaseModel):
    filename: str
    size: int
    quality: str = "balanced"
    analysis_mode: str = "fast"


@app.post("/api/jobs/upload/start")
def start_chunked_upload(payload: UploadStart):
    suffix = Path(payload.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(status_code=400, detail="Use MP4, MOV, MKV, or WebM.")
    if payload.size <= 0 or payload.size > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail=f"Maximum upload size is {MAX_UPLOAD // (1024 * 1024)} MB.")
    if shutil.disk_usage(ROOT).free < payload.size + 128 * 1024 * 1024:
        raise HTTPException(status_code=507, detail="Not enough temporary server storage. Try a smaller video.")
    key = new_job(
        "upload", status="uploading", last_activity=time.time(),
        source_name="source" + suffix, filesize=payload.size,
        received=0, uploading_chunk=False,
        quality=validate_quality(payload.quality),
        analysis_mode=validate_analysis_mode(payload.analysis_mode)
    )
    return {"job_id": key, "chunk_size": CHUNK_BYTES}


@app.put("/api/jobs/upload/{job_id}/chunk")
async def upload_chunk(job_id: str, index: int, request: Request):
    if index < 0:
        raise HTTPException(status_code=400, detail="Invalid chunk index.")
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("mode") != "upload" or job["status"] != "uploading":
            raise HTTPException(status_code=404, detail="Upload session unavailable. Start again.")
        old_size = job["received"]
        expected_index = (old_size + CHUNK_BYTES - 1) // CHUNK_BYTES
        if index < expected_index:
            # A mobile network may drop a response after the server stores the chunk.
            return {"received": old_size, "total": job["filesize"]}
        if index != expected_index or job.get("uploading_chunk"):
            raise HTTPException(status_code=409, detail="Chunks must arrive sequentially.")
        chunk_limit = min(CHUNK_BYTES, job["filesize"] - old_size)
        if chunk_limit <= 0:
            raise HTTPException(status_code=409, detail="Upload is already complete.")
        job["uploading_chunk"] = True
        name = job["source_name"]
        total = job["filesize"]
    target = ROOT / job_id / name
    written = 0
    try:
        with target.open("ab") as output:
            async for block in request.stream():
                written += len(block)
                if written > chunk_limit:
                    raise HTTPException(status_code=413, detail="Chunk exceeds the allowed size.")
                output.write(block)
        if written != chunk_limit:
            raise HTTPException(status_code=400, detail="Incomplete chunk; please retry.")
        with LOCK:
            job = JOBS.get(job_id)
            if not job or job["status"] != "uploading":
                raise HTTPException(status_code=409, detail="Upload expired.")
            job["received"] = old_size + written
            job["last_activity"] = time.time()
        return {"received": old_size + written, "total": total}
    except Exception:
        if target.exists():
            with target.open("r+b") as output:
                output.truncate(old_size)
        raise
    finally:
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id]["uploading_chunk"] = False


@app.post("/api/jobs/upload/{job_id}/finish")
def finish_chunked_upload(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("mode") != "upload":
            raise HTTPException(status_code=404, detail="Upload session unavailable.")
        if job["status"] != "uploading" or job.get("uploading_chunk"):
            raise HTTPException(status_code=409, detail="Upload is not ready to finish.")
        if job["received"] != job["filesize"]:
            raise HTTPException(status_code=400, detail="Upload incomplete; send the remaining chunks.")
        job["status"] = "queued"
        job["message"] = "Upload complete. Waiting for clipping worker."
    try:
        Q.put_nowait(job_id)
    except queue.Full as exc:
        update(job_id, status="uploading")
        raise HTTPException(status_code=429, detail="Processing queue full; try finishing again.") from exc
    return {"job_id": job_id}


@app.post("/api/jobs/upload")
async def upload_job(file: UploadFile = File(...), quality: str = Form("balanced"), analysis_mode: str = Form("fast")):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(status_code=400, detail="Upload MP4, MOV, MKV, or WebM video.")
    key = new_job("upload", source_name="source" + suffix, quality=validate_quality(quality),
                  analysis_mode=validate_analysis_mode(analysis_mode))
    target = ROOT / key / ("source" + suffix)
    size = 0
    try:
        with target.open("wb") as out:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD:
                    raise HTTPException(status_code=413, detail="File exceeds the demo upload limit.")
                out.write(block)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        Q.put_nowait(key)
        return {"job_id": key}
    except Exception:
        with LOCK:
            JOBS.pop(key, None)
        shutil.rmtree(ROOT / key, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with LOCK:
        job = dict(JOBS.get(job_id, {}))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return {k: v for k, v in job.items() if k in {"id", "status", "progress", "message", "clips", "planned_clips"}}


@app.get("/api/jobs/{job_id}/download/{filename}")
def download(job_id: str, filename: str):
    with LOCK:
        job = JOBS.get(job_id)
        ready = bool(job and job["status"] == "done")
    if not job or (filename == "all_clips.zip" and not ready) or (filename != "all_clips.zip" and filename not in {clip["name"] for clip in job["clips"]}):
        raise HTTPException(status_code=404, detail="Clip not available.")
    target = ROOT / job_id / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Clip expired.")
    return FileResponse(target, filename=filename, media_type="application/zip" if filename.endswith(".zip") else "video/mp4")
