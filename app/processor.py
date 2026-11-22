"""Independent video highlight processing: not copied from HypeCut."""
import json
import math
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
CLIP_COUNT = int(os.getenv("CLIP_COUNT", "5"))


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


def highlight_windows(video, duration, mode='fast'):
    """Fast, CPU-only heuristic: energetic changes and varied sound, not semantic understanding."""
    length = min(CLIP_LENGTH, max(8, int(duration)))
    target_clips = min(max(1, CLIP_COUNT), max(1, math.ceil(duration / 95)))
    if duration < length + 4:
        return [(0., length, 0.)]
    # NeonClipping MIT-licensed multimodal activity ranker.
    # Fast = audio jump + 2-4 kHz voice activity; Deep = also optical flow and hard cuts.
    try:
        from .neon_highlights import rank
        neon = rank(str(video), duration, count=target_clips, length=length,
                    motion_on=(mode == "deep"))
        chosen = [
            (float(clip["start"]), float(clip["end"]) - float(clip["start"]),
             float(clip.get("score", 0)))
            for clip in neon if float(clip["end"]) > float(clip["start"])
        ]
        if chosen:
            return chosen
    except Exception as exc:
        # Keep a functioning fallback for unusual source codecs and missing audio.
        print("Neon highlight ranker unavailable; using fallback:", repr(exc), flush=True)
    sample_rate = 2000
    candidates = []
    try:
        cp = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
             "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate),
             "-f", "s16le", "pipe:1"], capture_output=True,
            timeout=max(35, min(180, int(duration * 0.5) + 20)))
        samples = np.frombuffer(cp.stdout, dtype=np.int16)
        seconds = len(samples) // sample_rate
        if cp.returncode != 0 or seconds < length:
            raise ValueError("No usable audio track")
        # Compute one amplitude measurement per second; decoding is just 2 kHz mono.
        frames = samples[:seconds * sample_rate].reshape(seconds, sample_rate)
        signal = frames.astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(signal * signal, axis=1))
        loudness = np.log1p(rms * 40.0)
        local_mean = np.convolve(loudness, np.ones(9) / 9, mode="same")
        # Prefer changes, reactions and bursts over consistently loud music.
        bursts = np.maximum(0.0, loudness - local_mean)
        changes = np.abs(np.diff(loudness, prepend=loudness[0]))
        activity = 0.15 * loudness + 1.8 * bursts + 1.15 * changes
        activity = np.convolve(activity, np.ones(3) / 3, mode="same")
        count = max(1, seconds - length + 1)
        # Rolling mean is efficient and helps choose a full, usable video section.
        scores = np.convolve(activity, np.ones(length) / length, mode="valid")[:count]
        candidates = [(float(i), float(scores[i])) for i in np.argsort(scores)[::-1]]
    except (ValueError, OSError, subprocess.SubprocessError):
        candidates = [(max(0., duration * (j + 1) / (CLIP_COUNT + 1) - length / 2), 0.)
                      for j in range(target_clips)]

    selected = []
    separation = max(12, length * 0.85)
    for start, score in candidates:
        start = min(start, max(0., duration - length))
        if all(abs(start - earlier[0]) >= separation for earlier in selected):
            selected.append((start, length, score))
        if len(selected) >= target_clips:
            break
    if not selected:
        selected = [(0., length, 0.)]
    # Rank strongest highlights first so the best candidate can download first.
    return selected



def render_clip(video, start, seconds, target, quality="balanced"):
    """Render with user-selectable output quality while preserving aspect ratio."""
    presets = {
        "fast":     (480, 854, "ultrafast", 25, "96k", "fast_bilinear", 24),
        "balanced": (720, 1280, "veryfast", 20, "160k", "bicubic", 30),
        "high":     (1080, 1920, "veryfast", 18, "192k", "lanczos", 30),
    }
    if quality not in presets:
        raise ClipError("Unsupported output quality.")
    width, height, preset, crf, audio_rate, scale_algorithm, fps = presets[quality]
    filters = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"flags={scale_algorithm},"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x171b29,"
        f"fps={fps},format=yuv420p"
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.2f}", "-i", str(video), "-t", f"{seconds:.2f}",
           "-map", "0:v:0", "-map", "0:a?", "-vf", filters,
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-threads", "2", "-c:a", "aac", "-b:a", audio_rate,
           "-movflags", "+faststart", str(target)]
    try:
        timeout = 360 if quality == "high" else 240
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise ClipError("Rendering timed out. Try Balanced quality or a shorter video.") from exc
    if cp.returncode or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise ClipError("Video rendering failed. Try an MP4 with H.264 video.")


def process_video(path, folder, progress, on_clip=None, on_plan=None, quality="balanced", mode="fast"):
    progress(35, "Scanning audio and scene activity" if mode == "deep" else "Scanning audio peaks and vocal reactions")
    duration = probe(path)
    windows = highlight_windows(path, duration, mode=mode)
    if on_plan:
        on_plan(len(windows))
    clips = []
    failed = 0
    for n, (start, seconds, score) in enumerate(windows, 1):
        progress(35 + int(60 * (n - 1) / len(windows)), f"Rendering clip {n} of {len(windows)}")
        name = f"clip_{n:02d}.mp4"
        try:
            render_clip(path, start, seconds, folder / name, quality=quality)
        except ClipError:
            failed += 1
            progress(35 + int(60 * n / len(windows)), f"Could not render candidate {n}; continuing")
            continue
        clip = {"name": name, "start": round(start, 1),
                "duration": seconds, "score": round(score, 2)}
        clips.append(clip)
        if on_clip:
            on_clip(clip)
        progress(35 + int(60 * n / len(windows)),
                 f"Clip {n} ready — {len(windows) - n} more to render")
    if not clips:
        raise ClipError("No clips could be rendered. Try another MP4 video.")
    progress(98, "Preparing download ZIP")
    with zipfile.ZipFile(folder / "all_clips.zip", "w", zipfile.ZIP_STORED) as archive:
        for clip in clips:
            archive.write(folder / clip["name"], clip["name"])
    return clips
