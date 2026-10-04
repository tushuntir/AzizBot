import base64
import os
import tempfile
from pathlib import Path

import yt_dlp


def _ensure_cookies_from_env() -> None:
    """PaaS hosts (Railway/Render/Fly/...) have no volume mounts, and
    .dockerignore keeps cookies.txt out of the image — so the file never
    arrives. Workaround: paste base64(cookies.txt) into a COOKIES_B64
    env var and materialize it here at import time."""
    if os.getenv("COOKIES_B64"):
        dest = os.getenv("COOKIES_FILE") or "/app/cookies.txt"
        try:
            p = Path(dest)
            if not p.exists() or p.stat().st_size == 0:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(base64.b64decode(os.environ["COOKIES_B64"]))
        except Exception:
            pass  # _cookies_path() returning None keeps old behavior


_ensure_cookies_from_env()


def diagnostics() -> dict:
    cp = _cookies_path()
    return {
        "yt_dlp": yt_dlp.version.__version__,
        "cookies_file": str(cp) if cp else None,
        "player_clients": _player_clients(),
    }

def _cookies_path() -> Path | None:
    val = os.getenv("COOKIES_FILE")
    if val:
        p = Path(val)
        if p.exists():
            return p
    # Fall back to well-known filenames (browsers save re-downloads as
    # "cookies (1).txt", and compose mounts ./cookies.txt by default).
    for cand in ("cookies.txt", "cookies (1).txt",
                 "/app/cookies.txt", "/app/cookies (1).txt"):
        p = Path(cand)
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _player_clients() -> list[str]:
    env = os.getenv("YT_PLAYER_CLIENT")
    if env:
        clients = [c.strip() for c in env.split(",") if c.strip()]
        if clients:
            return clients
    # Don't pair cookies with tv: it authenticates differently and tends
    # to invalidate the session. No cookies: tv first (least scrutinised).
    if _cookies_path() is not None:
        return ["web_safari", "web_embedded", "mweb"]
    return ["tv", "web_safari"]
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
        # ---- YouTube "Sign in to confirm you're not a bot" mitigations ----
        # Try alternative player clients in order; web client without PO token
        # is what triggers the challenge most often on datacenter IPs.
        "extractor_args": {
            "youtube": {"player_client": _player_clients()},
            "youtubepot-bgutilhttp": {"base_url": os.getenv("POT_BASE_URL", "http://127.0.0.1:4416").rstrip("/")},
        },
        # Force IPv4: datacenter IPv6 ranges are blocked hardest by YouTube.
        "source_address": "0.0.0.0",
        # Be less bot-like: small sleeps between requests.
        "sleep_interval_requests": 1,
        "sleep_interval": 1,
        "max_sleep_interval": 5,
    }
    cp = _cookies_path()
    if cp is not None:
        opts["cookiefile"] = str(cp)
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
    # YouTube serves separate video/audio streams; merge them into one mp4.
    # No-op for single-file sources like Instagram.
    opts["merge_output_format"] = "mp4"
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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
        "extractor_args": {
            "youtube": {"player_client": _player_clients()},
            "youtubepot-bgutilhttp": {"base_url": os.getenv("POT_BASE_URL", "http://127.0.0.1:4416").rstrip("/")},
        },
        "source_address": "0.0.0.0",
    }
    cp = _cookies_path()
    if cp is not None:
        opts["cookiefile"] = str(cp)
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
