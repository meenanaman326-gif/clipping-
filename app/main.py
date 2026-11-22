"""ClipForge browser-based video clipping demonstration."""
from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .processor import ClipError, acquire_video, process_video, youtube_video_url
from .live import LiveError, capture_live, validate_live_url

ROOT = Path(os.getenv("WORK_DIR", "/tmp/clipforge-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "200")) * 1024 * 1024
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "3600"))
JOBS: dict[str, dict] = {}
Q: queue.Queue[str] = queue.Queue(maxsize=5)
LOCK = threading.Lock()


def expire_jobs():
    now = time.time()
    with LOCK:
        expired = [k for k, v in JOBS.items() if v["status"] in ("done", "error") and now - v["created"] > JOB_TTL]
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
                source = capture_live(job["url"], folder,
                                      lambda progress, message: update(key, progress=progress, message=message),
                                      seconds=job.get("capture_seconds", 120))
            elif job["mode"] == "youtube":
                update(key, status="processing", progress=8, message="Connecting to YouTube and checking video availability")
                source = acquire_video(job["url"], folder,
                                       lambda progress, message: update(key, progress=progress, message=message))
            else:
                update(key, status="processing", progress=14, message="Checking uploaded video")
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
            update(key, status="error", message=str(exc), progress=0)
        except Exception:
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
        active = sum(1 for v in JOBS.values() if v["status"] in ("queued", "processing"))
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
    key = new_job("youtube", url=clean, quality=validate_quality(payload.quality),
                  analysis_mode=validate_analysis_mode(payload.analysis_mode))
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
