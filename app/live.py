"""Short capture of accessible, non-DRM live streams from supported providers.

Streamlink captures only a bounded live interval. A live broadcast is not a VOD:
segments prior to starting the capture are not guaranteed to be available.
"""
from pathlib import Path
from urllib.parse import urlparse
import re
import subprocess
import time


class LiveError(Exception):
    pass


def validate_live_url(raw: str) -> str:
    url = urlparse(raw.strip())
    if url.scheme != "https" or url.username or url.password or url.port is not None:
        raise LiveError("Use an HTTPS Twitch, Kick or YouTube live-stream URL.")
    host = (url.hostname or "").lower()
    path = url.path.rstrip("/")
    if host in {"www.twitch.tv", "twitch.tv"}:
        if re.fullmatch(r"/[A-Za-z0-9_]{3,25}", path):
            return "https://www.twitch.tv" + path
    if host in {"kick.com", "www.kick.com"}:
        if re.fullmatch(r"/[A-Za-z0-9_-]{2,40}", path):
            return "https://kick.com" + path
    if host in {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        from .processor import youtube_video_url, ClipError
        try:
            return youtube_video_url(raw.strip())
        except ClipError:
            pass
    raise LiveError("Unsupported live link. Use a Twitch or Kick channel, or a YouTube Live video URL.")


def capture_live(raw: str, folder: Path, on_progress=None, seconds=120) -> Path:
    """Save upcoming live content with Streamlink; stop after a strict wallclock limit."""
    url = validate_live_url(raw)
    seconds = min(180, max(60, int(seconds)))
    target = folder / "source_live.ts"
    cmd = ["streamlink", "--no-plugin-sideloading", "--no-plugin-cache",
           "--loglevel", "error", "--progress", "no", "--force",
           "--stream-segmented-duration", str(seconds),
           "--stream-timeout", "15", "--stream-segment-timeout", "12",
           "--output", str(target), url, "best"]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise LiveError("Live-stream recorder is unavailable on this server.") from exc
    max_bytes = 200 * 1024 * 1024
    try:
        while proc.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > seconds + 55:
                proc.kill()
                raise LiveError("Live recording exceeded the time limit. Try again.")
            if target.exists() and target.stat().st_size > max_bytes:
                proc.kill()
                raise LiveError("The stream exceeded the size limit. Try a shorter capture.")
            if on_progress:
                pct = min(29, 8 + int(elapsed / (seconds + 20) * 21))
                on_progress(pct, f"Recording live stream: {int(elapsed)}s elapsed (up to {seconds}s)")
            time.sleep(2)
        err = (proc.stderr.read(1800) if proc.stderr else b"").decode("utf-8", errors="replace")
        if proc.returncode != 0 or not target.exists() or target.stat().st_size < 100_000:
            raise LiveError("Live capture failed. The stream may be offline, restricted or unsupported by this server.")
        from .processor import probe, ClipError
        try:
            probe(target)
        except ClipError as exc:
            raise LiveError("The recorded stream could not be decoded. Try another live link.") from exc
        if on_progress:
            on_progress(30, "Live segment captured. Finding highlights.")
        return target
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=4)
        if proc.stderr:
            proc.stderr.close()
