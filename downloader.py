import os
import tempfile
from pathlib import Path

import yt_dlp

COOKIES_FILE = os.getenv("COOKIES_FILE") or None
MAX_BYTES = 50 * 1024 * 1024  # Telegram cloud Bot API upload limit


class TooBig(Exception):
    pass


def _base_opts(outdir: str) -> dict:
    opts = {
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
    }
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _pick_file(outdir: str, suffix: str | None = None) -> Path:
    files = [
        p for p in Path(outdir).iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
        and (suffix is None or p.suffix == suffix)
    ]
    if not files:
        raise FileNotFoundError("download produced no file")
    return max(files, key=lambda p: p.stat().st_size)


def download_video(url: str):
    """Returns (tmpdir, file_path, info). Caller must delete tmpdir."""
    tmp = tempfile.mkdtemp(prefix="reel_")
    opts = _base_opts(tmp)
    opts["format"] = "best[ext=mp4][filesize<48M]/best[ext=mp4]/best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = _pick_file(tmp)
    if path.stat().st_size > MAX_BYTES:
        raise TooBig()
    return tmp, path, info


def download_audio(url: str):
    """Extracts MP3. Returns (tmpdir, file_path, info)."""
    tmp = tempfile.mkdtemp(prefix="aud_")
    opts = _base_opts(tmp)
    opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = _pick_file(tmp, ".mp3")
    if path.stat().st_size > MAX_BYTES:
        raise TooBig()
    return tmp, path, info


def search_music(query: str, limit: int = 10) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    results = []
    for e in (data or {}).get("entries", []):
        if not e or not e.get("id"):
            continue
        dur = e.get("duration") or 0
        if dur and dur > 15 * 60:  # skip long mixes/podcasts
            continue
        results.append({
            "id": e["id"],
            "title": e.get("title") or "Unknown",
            "duration": int(dur),
            "channel": e.get("channel") or e.get("uploader") or "",
        })
    return results
