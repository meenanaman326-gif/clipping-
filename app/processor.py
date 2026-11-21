"""Independent video highlight processing: not copied from HypeCut."""
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

MAX_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "1500"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "250"))
CLIP_LENGTH = int(os.getenv("CLIP_LENGTH_SECONDS", "30"))
CLIP_COUNT = int(os.getenv("CLIP_COUNT", "3"))


class ClipError(Exception):
    pass


def youtube_video_url(raw):
    url = urlparse(raw.strip())
    host = (url.hostname or "").lower()
    video_id = None
    if url.scheme != "https" or url.username or url.password or url.port:
        raise ClipError("Please enter a valid HTTPS YouTube link.")
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", url.path)
            if match:
                video_id = match.group(1)
    elif host in ("youtu.be", "www.youtu.be"):
        match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", url.path)
        if match:
            video_id = match.group(1)
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ClipError("Unsupported YouTube video URL.")
    return "https://www.youtube.com/watch?v=" + video_id


def probe(path):
    cp = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "json", str(path)], capture_output=True, text=True, timeout=25)
    try:
        seconds = float(json.loads(cp.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ClipError("Could not read video duration.") from exc
    if cp.returncode or seconds < 8:
        raise ClipError("Video should be at least 8 seconds long.")
    if seconds > MAX_SECONDS:
        raise ClipError("Video is too long for this demo (25-minute maximum).")
    return seconds


def acquire_video(url, folder):
    import yt_dlp
    canonical = youtube_video_url(url)
    limit = MAX_DOWNLOAD_MB * 1024 * 1024

    def filter_video(info, *, incomplete=False):
        if incomplete:
            return None
        if info.get("is_live"):
            return "Live videos are not supported."
        if info.get("duration") and info["duration"] > MAX_SECONDS:
            return "Video is too long."
        if (info.get("filesize") or info.get("filesize_approx") or 0) > limit:
            return "Video exceeds download size limit."
        return None

    def progress(data):
        if data.get("status") == "downloading" and data.get("downloaded_bytes", 0) > limit:
            raise ClipError("Video exceeds download limit.")

    options = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": str(folder / "source.%(ext)s"),
        "noplaylist": True, "match_filter": filter_video,
        "progress_hooks": [progress], "quiet": True, "no_warnings": True,
        "socket_timeout": 20, "retries": 1, "fragment_retries": 1,
        "max_filesize": limit
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([canonical])
    except Exception as exc:
        raise ClipError("YouTube import failed. Try uploading an authorized video file.") from exc
    files = [f for f in folder.glob("source.*") if f.is_file() and not f.name.endswith((".part", ".ytdl"))]
    if not files:
        raise ClipError("No video could be imported.")
    path = max(files, key=lambda p: p.stat().st_size)
    probe(path)
    return path


def highlight_windows(video, duration):
    length = min(CLIP_LENGTH, max(8, int(duration)))
    if duration < length + 5:
        return [(0., length, 0.)]
    sample_rate = 4000
    try:
        cp = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "s16le", "pipe:1"], capture_output=True,
            timeout=max(45, min(240, int(duration * 0.8))))
        samples = np.frombuffer(cp.stdout, dtype=np.int16).astype(np.float32)
        whole = len(samples) // sample_rate
        if cp.returncode or whole < 5:
            raise ValueError("No audio")
        rms = np.sqrt(np.mean(samples[:whole * sample_rate].reshape(-1, sample_rate) ** 2, axis=1))
        loud = np.log1p(rms)
        variation = np.abs(np.diff(loud, prepend=loud[0]))
        energy = loud * 0.45 + variation * 1.8
        scores = np.convolve(energy, np.ones(length) / length, mode="valid")
        candidates = [(float(i), float(scores[i])) for i in np.argsort(scores)[::-1]]
    except (ValueError, OSError, subprocess.SubprocessError):
        candidates = [(max(0., duration * (j + 1) / (CLIP_COUNT + 1) - length / 2), 0.)
                      for j in range(CLIP_COUNT)]
    result = []
    for start, score in candidates:
        start = min(start, max(0., duration - length))
        if all(abs(start - prior[0]) >= max(length + 8, 35) for prior in result):
            result.append((start, length, score))
        if len(result) >= CLIP_COUNT:
            break
    return sorted(result or [(0., length, 0.)])


def render_clip(video, start, seconds, target):
    filters = ("[0:v]split=2[b][f];"
               "[b]scale=720:1280:force_original_aspect_ratio=increase,"
               "crop=720:1280,boxblur=20:2[bg];"
               "[f]scale=720:1280:force_original_aspect_ratio=decrease[fg];"
               "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
           "-i", str(video), "-t", f"{seconds:.2f}", "-filter_complex", filters,
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "27", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", str(target)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if cp.returncode or not target.is_file() or target.stat().st_size == 0:
        raise ClipError("Video rendering failed. Try another MP4.")


def process_video(path, folder, progress):
    progress(35, "Selecting energetic video moments")
    duration = probe(path)
    windows = highlight_windows(path, duration)
    clips = []
    for n, (start, seconds, score) in enumerate(windows, 1):
        progress(35 + int(60 * (n - 1) / len(windows)), f"Rendering clip {n} of {len(windows)}")
        name = f"clip_{n:02d}.mp4"
        render_clip(path, start, seconds, folder / name)
        clips.append({"name": name, "start": round(start, 1),
                      "duration": seconds, "score": round(score, 2)})
    progress(98, "Preparing download ZIP")
    with zipfile.ZipFile(folder / "all_clips.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for clip in clips:
            archive.write(folder / clip["name"], clip["name"])
    return clips
