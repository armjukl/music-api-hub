from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FFMPEG_PATH = PROJECT_ROOT / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
load_dotenv(PROJECT_ROOT / ".env")


def env_text(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value or default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SPOTIFY_CLIENT_ID = env_text("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = env_text("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_USE_OFFICIAL_API = env_bool("SPOTIFY_USE_OFFICIAL_API", False)
SPOTIFY_OUTPUT_DIR = Path(
    env_text("SPOTIFY_OUTPUT_DIR", str(PROJECT_ROOT / "downloads"))
).expanduser().resolve()
SPOTIFY_FFMPEG_PATH = Path(
    env_text("SPOTIFY_FFMPEG_PATH", str(DEFAULT_FFMPEG_PATH))
).expanduser().resolve()
SPOTIFY_FORMAT = env_text("SPOTIFY_FORMAT", "mp3")
REQUEST_TIMEOUT_SECONDS = float(env_text("REQUEST_TIMEOUT_SECONDS", "30"))
BILIBILI_CACHE_DIR = Path(
    env_text("BILIBILI_CACHE_DIR", str(PROJECT_ROOT / "downloads" / "bilibili"))
).expanduser().resolve()
BILIBILI_MP3_BITRATE = int(env_text("BILIBILI_MP3_BITRATE", "320"))
if not 32 <= BILIBILI_MP3_BITRATE <= 320:
    raise ValueError("BILIBILI_MP3_BITRATE must be between 32 and 320")
