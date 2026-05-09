"""
ChapterForge TTS
Turn manuscript drafts into chapter audio.
"""

import os
import re
import uuid
import io
import json
import sqlite3
import hashlib
import threading
import time
import subprocess
import tempfile
import logging
import zipfile
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from html import escape as html_escape
from typing import Optional
from urllib.parse import quote

import requests
import aiofiles
from mutagen.id3 import ID3, TIT2, TRCK, TALB, TPE1, ID3NoHeaderError
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_DIR = Path("/app/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)

_file_handler = RotatingFileHandler(
    _LOG_DIR / "chapterforge.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=5,
)
_file_handler.setFormatter(_formatter)

_error_handler = RotatingFileHandler(
    _LOG_DIR / "chapterforge.errors.log",
    maxBytes=5 * 1024 * 1024,  # 5 MB per file
    backupCount=5,
)
_error_handler.setFormatter(_formatter)
_error_handler.setLevel(logging.WARNING)

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler, _error_handler])
logger = logging.getLogger("chapterforge")

# Suppress noisy poll requests from uvicorn's access log
class _SuppressJobPolls(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/api/jobs/" not in msg

for _uvicorn_logger_name in ("uvicorn.access", "uvicorn"):
    logging.getLogger(_uvicorn_logger_name).addFilter(_SuppressJobPolls())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KOKORO_ENDPOINT = os.environ.get(
    "KOKORO_ENDPOINT", "http://tts.throne.middl.earth/v1/audio/speech"
)
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "af_bella")
DEFAULT_SPEED = float(os.environ.get("DEFAULT_SPEED", "0.85"))
DEFAULT_MAX_CHARS = int(os.environ.get("DEFAULT_MAX_CHARS", "1400"))
CHAPTER_TRAIL_SILENCE = float(os.environ.get("CHAPTER_TRAIL_SILENCE", "3.0"))
KOKORO_RETRIES = int(os.environ.get("KOKORO_RETRIES", "3"))  # attempts per chunk
KOKORO_RETRY_DELAY = float(os.environ.get("KOKORO_RETRY_DELAY", "10.0"))  # seconds between retries
KOKORO_TIMEOUT = float(os.environ.get("KOKORO_TIMEOUT", "300.0"))  # seconds per request
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # optional POST on job complete/error
APP_VERSION = os.environ.get("APP_VERSION", "dev")

BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/app/books"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
PRONUNCIATIONS_FILE = Path(os.environ.get("PRONUNCIATIONS_FILE", "/app/books/pronunciations.json"))
VOICES_FILE = Path(os.environ.get("VOICES_FILE", "/app/books/voices.json"))

BOOKS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_MANUSCRIPT_SUFFIXES = {".md", ".txt"}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ChapterForge TTS")

@app.on_event("startup")
def on_startup():
    _init_app()

# ---------------------------------------------------------------------------
# SQLite job store
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("DB_PATH", "/app/output/chapterforge.db"))
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                filename TEXT,
                voice TEXT,
                speed REAL,
                max_chars INTEGER,
                status TEXT,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                current_chapter TEXT,
                current_chapter_index INTEGER DEFAULT 0,
                total_chapters INTEGER DEFAULT 0,
                current_chunk INTEGER DEFAULT 0,
                total_chunks INTEGER DEFAULT 0,
                build_id TEXT,
                error TEXT,
                stop_requested INTEGER DEFAULT 0,
                rechapter_index INTEGER
            )
        """)
        conn.commit()


def _job_to_dict(row) -> dict:
    d = dict(row)
    d["stop_requested"] = bool(d["stop_requested"])
    return d


def job_get(job_id: str) -> dict | None:
    with _db_lock, _get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _job_to_dict(row) if row else None


def job_set(job_id: str, **kwargs) -> None:
    """Update one or more fields on a job row."""
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    with _db_lock, _get_db() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", vals)
        conn.commit()


def job_insert(data: dict) -> None:
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    with _db_lock, _get_db() as conn:
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", list(data.values()))
        conn.commit()


def jobs_list_recent(limit: int = 100) -> list[dict]:
    with _db_lock, _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_job_to_dict(r) for r in rows]


def validate_manuscript_filename(filename: str) -> str:
    """Return a safe manuscript filename relative to BOOKS_DIR."""
    if not filename:
        raise HTTPException(status_code=400, detail="Missing manuscript filename.")
    path = Path(filename)
    if path.name != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid manuscript filename.")
    if path.suffix.lower() not in ALLOWED_MANUSCRIPT_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only .md and .txt files are supported.")
    return filename


def manuscript_path_for(filename: str) -> Path:
    filename = validate_manuscript_filename(filename)
    path = BOOKS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Manuscript not found.")
    return path


# In-memory stop_requested flags (not persisted — only meaningful while process runs)
_stop_flags: dict[str, bool] = {}


def _init_app() -> None:
    _init_db()
    # Mark any jobs that were running/queued when the process last died as error
    with _db_lock, _get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'interrupted', error = 'Process restarted'"
            " WHERE status IN ('running', 'queued', 'stopping')"
        )
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# Text processing
# ---------------------------------------------------------------------------

CHAPTER_HEADING_RE = re.compile(
    r"^#{1,2}\s+("
    r"chapter\s+[\divxlc]+"       # Chapter 1, Chapter IV
    r"|ch\.?\s*[\divxlc]+"        # Ch 1, Ch. IV
    r"|prologue"
    r"|epilogue"
    r"|part\s+[\divxlc]+"         # Part 1, Part II  (top-level only — ## or #)
    r"|act\s+[\divxlc]+"          # Act 1, Act II
    r"|interlude(?:\s+[\divxlc]+|\s+\w+)?"  # Interlude, Interlude 1, Interlude: The Rift
    r"|journal(?:\s+[\divxlc]+|\s+\w+)?"    # Journal, Journal 1, Journal: Anya
    r"|entry(?:\s+[\divxlc]+)?"   # Entry 1, Entry IV
    r"|scene\s+[\divxlc]+"        # Scene 1
    r"|coda"
    r"|afterword"
    r"|foreword"
    r"|preface"
    r"|introduction"
    r").*",
    re.IGNORECASE | re.MULTILINE,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1).lstrip()


def clean_markdown(text: str) -> str:
    """Remove Markdown syntax that Kokoro should not read aloud."""
    # Strip chapter metadata lines: italic lines containing | separators
    # e.g. *~25 Frostis, AS 2846 | POV: Rotating | ~5,400 words*
    text = re.sub(r"^\*[^*\n]*\|[^*\n]*\*\s*$", "", text, flags=re.MULTILINE)
    # Strip any remaining bare metadata lines (after italic stripping) with | separators
    # e.g. ~25 Frostis, AS 2846 | POV: Rotating | ~5,400 words
    text = re.sub(r"^~[^\n]*\|[^\n]*$", "", text, flags=re.MULTILINE)
    # Replace scene break dividers (---) with a paragraph break pause
    # A blank line is enough for a natural breath; no narrated text needed
    text = re.sub(r"^[-*_]{3,}\s*$", "\n", text, flags=re.MULTILINE)
    # Bold/italic
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Links [text](url)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Remaining heading hashes
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Strip backslash escapes e.g. \! \" \[ \* → just the character
    text = re.sub(r"\\(.)", r"\1", text)
    # Collapse excess blank lines left by stripping
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_chapters(text: str) -> list[dict]:
    """Split manuscript text into chapters by heading."""
    text = strip_frontmatter(text)
    parts = CHAPTER_HEADING_RE.split(text)

    # re.split with a capturing group interleaves captured text into parts:
    # [preamble, cap0, body0, cap1, body1, ...]
    # Use finditer to get full heading titles (e.g. "Chapter 1: A Price Beyond Gold")
    # instead of just the captured keyword fragment.
    headings = [
        re.sub(r"^#{1,3}\s+", "", m.group(0)).strip()
        for m in CHAPTER_HEADING_RE.finditer(text)
    ]

    chapters = []

    # Text before the first heading becomes a preamble chapter if non-empty
    if parts[0].strip():
        chapters.append({"title": "Preamble", "body": parts[0].strip()})

    for i, heading in enumerate(headings):
        # Body sits at index (i+1)*2 because captured groups occupy odd indices
        body_index = (i + 1) * 2
        body = parts[body_index].strip() if body_index < len(parts) else ""
        chapters.append({"title": heading, "body": body})

    # If no headings were found, treat the whole text as a single chapter
    if not chapters:
        chapters.append({"title": "Chapter 1", "body": text.strip()})

    return [c for c in chapters if c["body"]]


def load_pronunciations() -> dict[str, str]:
    """Load word substitutions from pronunciations.json if it exists.
    Lines beginning with # (optionally preceded by whitespace) are stripped
    so users can comment out entries without breaking JSON parsing.
    """
    if PRONUNCIATIONS_FILE.exists():
        try:
            raw = PRONUNCIATIONS_FILE.read_text(encoding="utf-8")
            stripped = "\n".join(
                line for line in raw.splitlines()
                if not line.lstrip().startswith("#")
            )
            return json.loads(stripped)
        except Exception as exc:
            logger.warning("Could not load pronunciations file: %s", exc)
    return {}


def apply_pronunciations(text: str) -> str:
    """Replace words/phrases with their phonetic equivalents.

    Substitutions are whole-word matches (case-insensitive) unless the key
    contains spaces, in which case the phrase is matched literally.
    """
    subs = load_pronunciations()
    for word, replacement in subs.items():
        if " " in word:
            # Phrase replacement — literal, case-insensitive
            text = re.sub(re.escape(word), replacement, text, flags=re.IGNORECASE)
        else:
            # Single word — whole-word boundary match, preserves surrounding space
            text = re.sub(rf"\b{re.escape(word)}\b", replacement, text, flags=re.IGNORECASE)
    return text


SPEAKER_TAG_RE = re.compile(r"^::([\w][\w\s]*)::[ \t]*", re.MULTILINE)
VOICE_BLEND_PART_RE = re.compile(r"^([^\s+()]+)\s*(?:\(\s*(\d+(?:\.\d+)?|\.\d+)\s*\))?$")


def load_voices() -> dict:
    """Load character voice profiles from voices.json. Returns empty dict if not found."""
    if VOICES_FILE.exists():
        try:
            return json.loads(VOICES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def normalize_voice_blend(voice: str) -> str:
    """Normalize and lightly validate a Kokoro voice or weighted voice blend."""
    voice = (voice or DEFAULT_VOICE).strip()
    parts = [p.strip() for p in voice.split("+") if p.strip()]
    if not parts:
        return DEFAULT_VOICE

    normalized: list[str] = []
    for part in parts:
        match = VOICE_BLEND_PART_RE.match(part)
        if not match:
            raise ValueError(f"Invalid voice blend part: {part!r}")
        voice_name, weight = match.groups()
        if weight is None:
            normalized.append(voice_name)
            continue
        weight_value = float(weight)
        if weight_value <= 0:
            raise ValueError(f"Voice blend weights must be greater than zero: {part!r}")
        normalized.append(f"{voice_name}({weight_value:g})")
    return "+".join(normalized)


def normalize_voice_profiles(voices: dict) -> dict:
    """Normalize persisted character voice profiles before saving."""
    normalized: dict[str, dict] = {}
    for name, profile in voices.items():
        if not isinstance(profile, dict):
            raise ValueError(f"Voice profile for {name!r} must be an object.")
        key = name.strip().lower()
        if not key:
            continue
        normalized[key] = {
            "voice": normalize_voice_blend(str(profile.get("voice", DEFAULT_VOICE))),
            "speed": float(profile.get("speed", DEFAULT_SPEED)),
            "pitch_ratio": float(profile.get("pitch_ratio", 1.0)),
        }
    return normalized


def parse_segments(text: str) -> list[tuple[str, str]]:
    """Split text into (speaker_key, text) segments based on ::speaker:: paragraph tags.

    A paragraph beginning with ::name:: is attributed to that character.
    Untagged paragraphs revert to 'narrator'.
    Consecutive same-speaker paragraphs are merged.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    segments: list[tuple[str, str]] = []
    current_speaker = "narrator"
    current_parts: list[str] = []

    for para in paragraphs:
        m = SPEAKER_TAG_RE.match(para)
        if m:
            speaker = m.group(1).strip().lower()
            para_text = para[m.end():].strip()
            if speaker != current_speaker:
                if current_parts:
                    segments.append((current_speaker, "\n\n".join(current_parts)))
                    current_parts = []
                current_speaker = speaker
            if para_text:
                current_parts.append(para_text)
        else:
            if current_speaker != "narrator":
                if current_parts:
                    segments.append((current_speaker, "\n\n".join(current_parts)))
                    current_parts = []
                current_speaker = "narrator"
            current_parts.append(para)

    if current_parts:
        segments.append((current_speaker, "\n\n".join(current_parts)))

    return segments if segments else [("narrator", text.strip())]


def apply_pitch(wav_bytes: bytes, pitch_ratio: float) -> bytes:
    """Shift pitch of WAV audio using ffmpeg asetrate trick, preserving duration."""
    if abs(pitch_ratio - 1.0) < 0.001:
        return wav_bytes
    sample_rate = 24000
    new_rate = int(sample_rate * pitch_ratio)
    tempo = max(0.5, min(2.0, 1.0 / pitch_ratio))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        in_path = tmp / "in.wav"
        out_path = tmp / "out.wav"
        in_path.write_bytes(wav_bytes)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(in_path),
                "-filter:a", f"asetrate={new_rate},aresample={sample_rate},atempo={tempo:.6f}",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_bytes()


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Split chapter text into chunks small enough for Kokoro."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # If a single paragraph is too long, split by sentence
        if len(para) > max_chars:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sentence in sentences:
                if len(current) + len(sentence) + 1 <= max_chars:
                    current = (current + " " + sentence).strip()
                else:
                    if current:
                        chunks.append(current)
                    # A single sentence longer than max_chars goes as-is
                    current = sentence
        else:
            if len(current) + len(para) + 2 <= max_chars:
                current = (current + "\n\n" + para).strip()
            else:
                if current:
                    chunks.append(current)
                current = para

    if current:
        chunks.append(current)

    return chunks


def tag_mp3(path: Path, title: str, track: int, album: str, artist: str = "ChapterForge TTS") -> None:
    """Embed ID3 tags into an MP3 file."""
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TRCK"] = TRCK(encoding=3, text=str(track))
    tags["TALB"] = TALB(encoding=3, text=album)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags.save(str(path))


def get_audio_duration_ms(path: Path) -> int:
    """Return audio duration in milliseconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return int(float(result.stdout.strip()) * 1000)


def build_m4b(build_dir: Path, chapters: list[dict], book: str) -> Path:
    """Concatenate all chapter MP3s into a single M4B with chapter markers."""
    mp3s = [build_dir / ch["output_mp3"] for ch in chapters]

    # Build ffmetadata with chapter markers
    meta_lines = [";", "[CHAPTER]"[:0]]  # just start with the header
    meta_lines = [";"]  # comment
    meta_lines.append("[STREAM]"[:0])  # placeholder, not needed
    # Build proper ffmetadata
    metadata = ";FFMETADATA1\n"
    metadata += f"title={book}\n"
    metadata += f"artist=ChapterForge TTS\n\n"

    offset_ms = 0
    for ch in chapters:
        mp3_path = build_dir / ch["output_mp3"]
        duration_ms = get_audio_duration_ms(mp3_path)
        start_ms = offset_ms
        end_ms = offset_ms + duration_ms
        metadata += "[CHAPTER]\n"
        metadata += "TIMEBASE=1/1000\n"
        metadata += f"START={start_ms}\n"
        metadata += f"END={end_ms}\n"
        metadata += f"title={ch['title']}\n\n"
        offset_ms = end_ms

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        meta_path = tmp / "metadata.txt"
        meta_path.write_text(metadata, encoding="utf-8")

        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{mp3}'" for mp3 in mp3s),
            encoding="utf-8",
        )

        m4b_path = build_dir / f"{_safe_slug(book)}.m4b"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-i", str(meta_path),
                "-map_metadata", "1",
                "-codec:a", "aac", "-b:a", "64k",
                "-movflags", "+faststart",
                str(m4b_path),
            ],
            check=True,
            capture_output=True,
        )
    return m4b_path


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def call_kokoro(text: str, voice: str, speed: float) -> bytes:
    """Send a chunk to Kokoro and return raw audio bytes (WAV).

    Retries up to KOKORO_RETRIES times on transient errors (timeouts, 5xx).
    Raises on the final failure.
    """
    voice = normalize_voice_blend(voice)

    payload = {
        "model": "kokoro",
        "voice": voice,
        "speed": speed,
        "input": text,
    }
    logger.info("Kokoro payload: voice=%r speed=%s text_len=%d", voice, speed, len(text))
    last_exc: Exception | None = None
    for attempt in range(1, KOKORO_RETRIES + 1):
        try:
            resp = requests.post(KOKORO_ENDPOINT, json=payload, timeout=KOKORO_TIMEOUT)
            if not resp.ok:
                logger.warning("Kokoro HTTP %d response body: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            if attempt < KOKORO_RETRIES:
                logger.warning(
                    "Kokoro attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt, KOKORO_RETRIES, exc, KOKORO_RETRY_DELAY,
                )
                time.sleep(KOKORO_RETRY_DELAY)
            else:
                logger.error(
                    "Kokoro failed after %d attempts: %s", KOKORO_RETRIES, exc
                )
    raise last_exc


def concat_chunks_to_mp3(audio_chunks: list[bytes], output_path: Path) -> None:
    """Concatenate WAV audio chunks into a single MP3 using ffmpeg."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        chunk_paths = []
        for i, data in enumerate(audio_chunks):
            p = tmp / f"chunk_{i:04d}.wav"
            p.write_bytes(data)
            chunk_paths.append(p)

        # Append trailing silence so chapters don't end abruptly
        if CHAPTER_TRAIL_SILENCE > 0:
            silence_path = tmp / "silence.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", f"anullsrc=r=24000:cl=mono",
                    "-t", str(CHAPTER_TRAIL_SILENCE),
                    str(silence_path),
                ],
                check=True,
                capture_output=True,
            )
            chunk_paths.append(silence_path)

        list_file = tmp / "chunks.txt"
        list_file.write_text(
            "\n".join(f"file '{p}'" for p in chunk_paths),
            encoding="utf-8",
        )

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-codec:a", "libmp3lame", "-q:a", "2",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def fire_webhook(payload: dict) -> None:
    """POST job status payload to WEBHOOK_URL if configured. Non-fatal."""
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        logger.info("Webhook fired: %s", WEBHOOK_URL)
    except Exception as exc:
        logger.warning("Webhook failed: %s", exc)


def record_job(job_id: str) -> None:
    job_set(job_id, status="running", started_at=datetime.utcnow().isoformat())
    job = job_get(job_id)
    logger.info("[%s] Job started — file: %s, voice: %s, speed: %s",
                job_id[:8], job["filename"], job["voice"], job["speed"])

    try:
        manuscript_path = BOOKS_DIR / validate_manuscript_filename(job["filename"])
        if not manuscript_path.exists():
            raise FileNotFoundError(f"Manuscript not found: {job['filename']}")

        text = manuscript_path.read_text(encoding="utf-8")
        source_hash = hashlib.md5(text.encode()).hexdigest()[:8]

        voice = job["voice"]
        speed = job["speed"]
        max_chars = job["max_chars"]

        voice_slug = _safe_slug(f"{voice}_{speed:.2f}".replace(".", "_"))
        stem = manuscript_path.stem
        build_id = f"{_safe_slug(stem)}_{voice_slug}_{source_hash}"

        build_dir = OUTPUT_DIR / "chapters" / build_id
        build_dir.mkdir(parents=True, exist_ok=True)

        job_set(job_id, build_id=build_id)

        chapters = split_into_chapters(text)
        job_set(job_id, total_chapters=len(chapters))
        logger.info("[%s] Build ID: %s — %d chapters found",
                    job_id[:8], build_id, len(chapters))

        # Load existing manifest for partial resume
        manifest_path = build_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                completed_indices = {c["chapter_index"] for c in manifest.get("chapters", [])}
                logger.info("[%s] Resuming — %d chapters already done",
                            job_id[:8], len(completed_indices))
            except Exception:
                manifest = None
                completed_indices = set()
        else:
            manifest = None
            completed_indices = set()

        if manifest is None:
            manifest = {
                "book": stem,
                "draft": stem,
                "draft_date": datetime.utcnow().date().isoformat(),
                "source_file": job["filename"],
                "source_hash": source_hash,
                "voice": voice,
                "speed": speed,
                "max_chars": max_chars,
                "build_id": build_id,
                "chapters": [],
            }

        for ch_idx, chapter in enumerate(chapters):
            ch_num = ch_idx + 1
            if _stop_flags.get(job_id):
                logger.info("[%s] Stop requested before chapter %d — halting",
                            job_id[:8], ch_num)
                job_set(job_id, status="stopped")
                return

            if ch_num in completed_indices:
                logger.info("[%s] Skipping chapter %d/%d (already done)",
                            job_id[:8], ch_num, len(chapters))
                job_set(job_id, current_chapter=chapter["title"],
                        current_chapter_index=ch_num)
                continue

            job_set(job_id, current_chapter=chapter["title"],
                    current_chapter_index=ch_num)
            logger.info("[%s] Chapter %d/%d: %s",
                        job_id[:8], ch_num, len(chapters), chapter["title"])

            voices_data = load_voices()
            narrator_profile = voices_data.get("narrator", {"voice": voice, "speed": speed})

            clean_body = clean_markdown(chapter["body"])
            clean_body = apply_pronunciations(clean_body)
            title_line = apply_pronunciations(chapter["title"])
            full_text = f"{title_line}.\n\n{clean_body}"
            segments = parse_segments(full_text)
            all_chunks = [
                (spk, chunk)
                for spk, seg_text in segments
                for chunk in split_into_chunks(seg_text, max_chars)
            ]
            total_chunks = len(all_chunks)
            job_set(job_id, total_chunks=total_chunks, current_chunk=0)
            logger.info("[%s]   %d chunks across %d segments",
                        job_id[:8], total_chunks, len(segments))

            audio_parts: list[bytes] = []

            for chunk_idx, (seg_speaker, chunk) in enumerate(all_chunks):
                if _stop_flags.get(job_id):
                    logger.info("[%s] Stop requested at chunk %d/%d — halting",
                                job_id[:8], chunk_idx + 1, total_chunks)
                    job_set(job_id, status="stopped")
                    return

                job_set(job_id, current_chunk=chunk_idx + 1)
                profile = voices_data.get(seg_speaker, narrator_profile) if voices_data else narrator_profile
                seg_voice = profile.get("voice", voice)
                seg_speed = float(profile.get("speed", speed))
                pitch_ratio = float(profile.get("pitch_ratio", 1.0))
                logger.debug("[%s]   Chunk %d/%d (%d chars) speaker=%s voice=%s",
                             job_id[:8], chunk_idx + 1, total_chunks, len(chunk),
                             seg_speaker, seg_voice)
                try:
                    audio_data = call_kokoro(chunk, seg_voice, seg_speed)
                    if abs(pitch_ratio - 1.0) >= 0.001:
                        audio_data = apply_pitch(audio_data, pitch_ratio)
                except Exception as kokoro_exc:
                    logger.error("[%s]   Kokoro failed on chunk %d/%d: %s",
                                 job_id[:8], chunk_idx + 1, total_chunks, kokoro_exc)
                    raise
                audio_parts.append(audio_data)

            chapter_filename = f"{ch_num:02d}_{_safe_slug(chapter['title'])}.mp3"
            chapter_path = build_dir / chapter_filename
            logger.info("[%s]   Encoding %s", job_id[:8], chapter_filename)
            concat_chunks_to_mp3(audio_parts, chapter_path)
            tag_mp3(chapter_path, title=chapter["title"], track=ch_num, album=stem)

            manifest["chapters"].append(
                {
                    "chapter_index": ch_num,
                    "title": chapter["title"],
                    "chunk_count": total_chunks,
                    "output_mp3": chapter_filename,
                }
            )
            # Write manifest after each chapter so partial resume works
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Build M4B audiobook with chapter markers
        logger.info("[%s] Building M4B audiobook…", job_id[:8])
        try:
            m4b_path = build_m4b(build_dir, manifest["chapters"], stem)
            manifest["m4b"] = m4b_path.name
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            logger.info("[%s] M4B written: %s", job_id[:8], m4b_path.name)
        except Exception as m4b_exc:
            logger.warning("[%s] M4B build failed (non-fatal): %s", job_id[:8], m4b_exc)

        job_set(job_id, status="complete", completed_at=datetime.utcnow().isoformat())
        logger.info("[%s] Job complete — build: %s", job_id[:8], build_id)
        fire_webhook({"event": "complete", "job_id": job_id, "build_id": build_id,
                      "book": stem, "chapters": len(manifest["chapters"])})

    except Exception as exc:
        job_set(job_id, status="error", error=str(exc))
        logger.error("[%s] Job failed: %s", job_id[:8], exc, exc_info=True)
        fire_webhook({"event": "error", "job_id": job_id, "error": str(exc)})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    filename: str
    voice: str = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED
    max_chars: int = DEFAULT_MAX_CHARS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    manuscripts = sorted(
        [f.name for f in BOOKS_DIR.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_MANUSCRIPT_SUFFIXES]
    )
    builds = _list_builds()
    return _render_ui(manuscripts, builds)


@app.post("/upload")
async def upload_manuscript(file: UploadFile = File(...)):
    filename = validate_manuscript_filename(file.filename)
    dest = BOOKS_DIR / filename
    async with aiofiles.open(dest, "wb") as f:
        content = await file.read()
        await f.write(content)

    return JSONResponse({"status": "uploaded", "filename": filename})


@app.post("/api/jobs")
async def create_job(req: JobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    validate_manuscript_filename(req.filename)
    try:
        voice = normalize_voice_blend(req.voice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job_insert({
        "job_id": job_id,
        "filename": req.filename,
        "voice": voice,
        "speed": req.speed,
        "max_chars": req.max_chars,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "completed_at": None,
        "current_chapter": None,
        "current_chapter_index": 0,
        "total_chapters": 0,
        "current_chunk": 0,
        "total_chunks": 0,
        "build_id": None,
        "error": None,
        "stop_requested": 0,
        "rechapter_index": None,
    })
    background_tasks.add_task(record_job, job_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    job = job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}.")
    _stop_flags[job_id] = True
    job_set(job_id, status="stopping", stop_requested=1)
    return {"status": "stopping"}


@app.get("/api/manuscripts")
async def list_manuscripts():
    files = sorted(
        [f.name for f in BOOKS_DIR.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_MANUSCRIPT_SUFFIXES]
    )
    return {"manuscripts": files}


class VoicesUpdateRequest(BaseModel):
    voices: dict


@app.get("/api/voices")
async def get_voices():
    return load_voices()


@app.post("/api/voices")
async def save_voices_route(req: VoicesUpdateRequest):
    try:
        normalized = normalize_voice_profiles(req.voices)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    VOICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    VOICES_FILE.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return {"status": "saved", "count": len(normalized)}


class VoicePreviewRequest(BaseModel):
    text: str = "The sunstone pulsed with a cold light, and Anya felt the Weave tighten around her."
    voice: str = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED
    pitch_ratio: float = 1.0


@app.post("/api/preview/voice")
async def voice_preview(req: VoicePreviewRequest):
    """Render a short text sample and return the WAV audio directly."""
    text = req.text[:500]  # cap at 500 chars for safety
    try:
        audio = call_kokoro(text, req.voice, req.speed)
        if abs(req.pitch_ratio - 1.0) >= 0.001:
            audio = apply_pitch(audio, req.pitch_ratio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kokoro error: {exc}")
    return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")


@app.post("/api/preview/chapters")
async def preview_chapters(req: JobRequest):
    """Return the chapter list (title + word count) without generating any audio."""
    manuscript_path = manuscript_path_for(req.filename)
    text = manuscript_path.read_text(encoding="utf-8")
    chapters = split_into_chapters(text)
    result = [
        {
            "index": i + 1,
            "title": ch["title"],
            "word_count": len(ch["body"].split()),
        }
        for i, ch in enumerate(chapters)
    ]
    return {"filename": req.filename, "chapter_count": len(result), "chapters": result}


@app.get("/api/jobs")
async def list_jobs():
    """Return all jobs, most recent first. Used by the UI to reconnect after a page reload."""
    return jobs_list_recent()


@app.get("/build/{build_id}")
async def get_build(build_id: str):
    build_dir = OUTPUT_DIR / "chapters" / build_id
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/build/{build_id}/download/zip")
async def download_build(build_id: str):
    """Return a ZIP archive of all chapter MP3s for a build."""
    if ".." in build_id or "/" in build_id:
        raise HTTPException(status_code=400, detail="Invalid build ID.")
    build_dir = OUTPUT_DIR / "chapters" / build_id
    if not build_dir.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    mp3_files = sorted(build_dir.glob("*.mp3"))
    if not mp3_files:
        raise HTTPException(status_code=404, detail="No audio files found in build.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for mp3 in mp3_files:
            zf.write(mp3, mp3.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{build_id}.zip"'},
    )


@app.post("/build/{build_id}/rechapter/{chapter_index}")
async def rechapter(build_id: str, chapter_index: int, background_tasks: BackgroundTasks):
    """Re-record a single chapter in an existing build."""
    if ".." in build_id or "/" in build_id:
        raise HTTPException(status_code=400, detail="Invalid build ID.")
    build_dir = OUTPUT_DIR / "chapters" / build_id
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters_meta = manifest.get("chapters", [])
    ch_meta = next((c for c in chapters_meta if c["chapter_index"] == chapter_index), None)
    if ch_meta is None:
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_index} not found in manifest.")

    job_id = str(uuid.uuid4())
    job_insert({
        "job_id": job_id,
        "filename": manifest["source_file"],
        "voice": manifest["voice"],
        "speed": manifest["speed"],
        "max_chars": manifest.get("max_chars", DEFAULT_MAX_CHARS),
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "completed_at": None,
        "current_chapter": ch_meta["title"],
        "current_chapter_index": chapter_index,
        "total_chapters": 1,
        "current_chunk": 0,
        "total_chunks": 0,
        "build_id": build_id,
        "error": None,
        "stop_requested": 0,
        "rechapter_index": chapter_index,
    })
    background_tasks.add_task(record_single_chapter, job_id, build_id, manifest_path)
    return {"job_id": job_id, "chapter_index": chapter_index}


def record_single_chapter(job_id: str, build_id: str, manifest_path: Path) -> None:
    """Re-record one chapter of an existing build in-place."""
    job_set(job_id, status="running", started_at=datetime.utcnow().isoformat())
    job = job_get(job_id)
    chapter_index = job["rechapter_index"]
    logger.info("[%s] Re-recording chapter %d of build %s", job_id[:8], chapter_index, build_id)

    try:
        build_dir = OUTPUT_DIR / "chapters" / build_id
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ch_meta = next(c for c in manifest["chapters"] if c["chapter_index"] == chapter_index)

        manuscript_path = BOOKS_DIR / validate_manuscript_filename(manifest["source_file"])
        text = manuscript_path.read_text(encoding="utf-8")
        all_chapters = split_into_chapters(text)

        chapter = next((c for i, c in enumerate(all_chapters) if i + 1 == chapter_index), None)
        if chapter is None:
            raise ValueError(f"Chapter {chapter_index} not found in manuscript.")

        voice = manifest["voice"]
        speed = manifest["speed"]
        max_chars = manifest.get("max_chars", DEFAULT_MAX_CHARS)
        stem = manifest["book"]

        voices_data = load_voices()
        narrator_profile = voices_data.get("narrator", {"voice": voice, "speed": speed})

        clean_body = clean_markdown(chapter["body"])
        clean_body = apply_pronunciations(clean_body)
        title_line = apply_pronunciations(chapter["title"])
        full_text = f"{title_line}.\n\n{clean_body}"
        segments = parse_segments(full_text)
        all_chunks = [
            (spk, chunk)
            for spk, seg_text in segments
            for chunk in split_into_chunks(seg_text, max_chars)
        ]
        total_chunks = len(all_chunks)
        job_set(job_id, total_chunks=total_chunks)
        logger.info("[%s]   %d chunks across %d segments", job_id[:8], total_chunks, len(segments))

        audio_parts: list[bytes] = []
        for chunk_idx, (seg_speaker, chunk) in enumerate(all_chunks):
            if _stop_flags.get(job_id):
                job_set(job_id, status="stopped")
                return
            job_set(job_id, current_chunk=chunk_idx + 1)
            profile = voices_data.get(seg_speaker, narrator_profile) if voices_data else narrator_profile
            seg_voice = profile.get("voice", voice)
            seg_speed = float(profile.get("speed", speed))
            pitch_ratio = float(profile.get("pitch_ratio", 1.0))
            audio_data = call_kokoro(chunk, seg_voice, seg_speed)
            if abs(pitch_ratio - 1.0) >= 0.001:
                audio_data = apply_pitch(audio_data, pitch_ratio)
            audio_parts.append(audio_data)

        chapter_path = build_dir / ch_meta["output_mp3"]
        logger.info("[%s]   Encoding %s", job_id[:8], ch_meta["output_mp3"])
        concat_chunks_to_mp3(audio_parts, chapter_path)
        tag_mp3(chapter_path, title=chapter["title"], track=chapter_index, album=stem)

        ch_meta["chunk_count"] = total_chunks
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        job_set(job_id, status="complete", completed_at=datetime.utcnow().isoformat())
        logger.info("[%s] Re-record complete — chapter %d", job_id[:8], chapter_index)
        fire_webhook({"event": "rechapter_complete", "job_id": job_id,
                      "build_id": build_id, "chapter_index": chapter_index})

    except Exception as exc:
        job_set(job_id, status="error", error=str(exc))
        logger.error("[%s] Re-record failed: %s", job_id[:8], exc, exc_info=True)
        fire_webhook({"event": "error", "job_id": job_id, "error": str(exc)})


@app.get("/build/{build_id}/download/m4b")
async def download_m4b(build_id: str):
    """Return the M4B audiobook file for a build."""
    if ".." in build_id or "/" in build_id:
        raise HTTPException(status_code=400, detail="Invalid build ID.")
    build_dir = OUTPUT_DIR / "chapters" / build_id
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    m4b_name = manifest.get("m4b")
    if not m4b_name:
        raise HTTPException(status_code=404, detail="No M4B file for this build.")
    m4b_path = build_dir / m4b_name
    if not m4b_path.exists():
        raise HTTPException(status_code=404, detail="M4B file missing on disk.")
    return FileResponse(str(m4b_path), media_type="audio/mp4",
                        headers={"Content-Disposition": f'attachment; filename="{m4b_name}"'})


@app.delete("/build/{build_id}")
async def delete_build(build_id: str):
    """Permanently delete a build directory and all its files."""
    if ".." in build_id or "/" in build_id:
        raise HTTPException(status_code=400, detail="Invalid build ID.")
    build_dir = OUTPUT_DIR / "chapters" / build_id
    if not build_dir.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    shutil.rmtree(build_dir)
    return {"status": "deleted", "build_id": build_id}


@app.get("/audio/{build_id}/{filename}")
async def get_audio(build_id: str, filename: str):
    # Prevent path traversal
    if ".." in build_id or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid path.")
    audio_path = OUTPUT_DIR / "chapters" / build_id / filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found.")
    return FileResponse(str(audio_path), media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Build listing helper
# ---------------------------------------------------------------------------

def _list_builds() -> list[dict]:
    chapters_dir = OUTPUT_DIR / "chapters"
    if not chapters_dir.exists():
        return []
    builds = []
    for d in sorted(chapters_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                builds.append(m)
            except Exception:
                pass
    return builds


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _h(value) -> str:
    return html_escape(str(value), quote=True)


def _url_part(value) -> str:
    return quote(str(value), safe="")


def _render_ui(manuscripts: list[str], builds: list[dict]) -> str:
    manuscript_options = "".join(
        f'<option value="{_h(m)}">{_h(m)}</option>' for m in manuscripts
    )
    if not manuscript_options:
        manuscript_options = '<option value="" disabled selected>No manuscripts uploaded yet</option>'

    build_rows = ""
    for b in builds:
        build_id = str(b["build_id"])
        build_id_url = _url_part(build_id)
        build_id_js = _h(json.dumps(build_id))
        chapters_html = "".join(
            (
            f'<li>'
            f'<audio controls preload="metadata" src="/audio/{build_id_url}/{_url_part(ch["output_mp3"])}"></audio>'
            f' {_h(ch["title"])}'
            f' <a href="/audio/{build_id_url}/{_url_part(ch["output_mp3"])}" download="{_h(ch["output_mp3"])}" title="Download chapter" style="color:#e8c96e;font-size:0.8rem;margin-left:0.4rem;">&#8681;</a>'
            f' <button onclick="rechapter({build_id_js},{int(ch["chapter_index"])},this)" title="Re-record this chapter" style="background:#333;color:#ccc;padding:0.1rem 0.5rem;font-size:0.75rem;margin-left:0.3rem;">&#8635;</button>'
            f'</li>'
            )
            for ch in b.get("chapters", [])
        )
        m4b_link = (
            f' &nbsp;<a href="/build/{build_id_url}/download/m4b" title="Download M4B audiobook" '
            f'style="color:#e8c96e;font-size:0.8rem;text-decoration:none;" download>&#8681; M4B</a>'
            if b.get("m4b") else ""
        )
        delete_btn = (
            f' &nbsp;<button onclick="deleteBuild({build_id_js},this)"'
            f' title="Delete this build" style="background:none;border:none;color:#555;'
            f'font-size:0.85rem;cursor:pointer;padding:0 0.2rem;" '
            f'onmouseover="this.style.color=\'#c0392b\'" onmouseout="this.style.color=\'#555\'">&#128465;</button>'
        )
        build_rows += f"""
        <details>
          <summary><strong>{_h(b.get("book", build_id))}</strong>
            &nbsp;|&nbsp; {_h(b.get("voice"))} @ {_h(b.get("speed"))}
            &nbsp;|&nbsp; {_h(b.get("draft_date", ""))}
            &nbsp;<a href="/build/{build_id_url}/download/zip" title="Download all chapters as ZIP" style="color:#e8c96e;font-size:0.8rem;text-decoration:none;" download>&#8681; ZIP</a>
            {m4b_link}
            {delete_btn}
          </summary>
          <ul class="chapter-list">{chapters_html}</ul>
        </details>
        """
    if not build_rows:
        build_rows = "<p>No builds yet.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ChapterForge TTS</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem; background: #111; color: #ddd; }}
    h1 {{ color: #e8c96e; margin-bottom: 0; }}
    .tagline {{ color: #888; margin-top: 0.2rem; margin-bottom: 2rem; font-size: 0.9rem; }}
    section {{ background: #1a1a1a; border: 1px solid #333; border-radius: 6px; padding: 1.2rem; margin-bottom: 1.5rem; }}
    h2 {{ color: #aaa; font-size: 1rem; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 0; }}
    label {{ display: block; margin-bottom: 0.3rem; font-size: 0.85rem; color: #999; }}
    input, select {{ background: #222; border: 1px solid #444; color: #eee; padding: 0.4rem 0.6rem; border-radius: 4px; width: 100%; box-sizing: border-box; margin-bottom: 0.8rem; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.8rem; }}
    button {{ background: #e8c96e; color: #111; border: none; padding: 0.5rem 1.2rem; border-radius: 4px; cursor: pointer; font-weight: bold; }}
    button.stop {{ background: #c0392b; color: #fff; margin-left: 0.5rem; }}
    button:disabled {{ opacity: 0.4; cursor: default; }}
    #status-box {{ background: #222; border: 1px solid #333; border-radius: 4px; padding: 0.8rem; font-size: 0.85rem; min-height: 3rem; }}
    details {{ margin-bottom: 0.6rem; }}
    summary {{ cursor: pointer; padding: 0.3rem 0; color: #ccc; }}
    ul.chapter-list {{ list-style: none; padding: 0 0 0 1rem; }}
    ul.chapter-list li {{ margin-bottom: 0.5rem; }}
    audio {{ width: 100%; margin-bottom: 0.2rem; }}
    .chapter-preview {{ background:#1a1a1a; border:1px solid #333; border-radius:4px; padding:0.6rem; margin-top:0.6rem; font-size:0.8rem; max-height:220px; overflow-y:auto; display:none; }}
    .chapter-preview table {{ width:100%; border-collapse:collapse; }}
    .chapter-preview td {{ padding:0.2rem 0.5rem; border-bottom:1px solid #2a2a2a; }}
    .chapter-preview td:first-child {{ color:#888; width:2.5rem; }}
    .chapter-preview td:last-child {{ color:#888; text-align:right; width:4rem; }}
    .blend-editor {{ display:flex; flex-direction:column; gap:0.25rem; min-width:15rem; }}
    .blend-slot {{ display:grid; grid-template-columns:minmax(8rem,1fr) 4.2rem; gap:0.3rem; align-items:center; }}
    .blend-slot select, .blend-slot input {{ margin-bottom:0; }}
    #upload-status {{ font-size: 0.8rem; color: #888; margin-top: 0.3rem; }}
  </style>
</head>
<body>
<h1>ChapterForge TTS</h1>
<p class="tagline">Turn manuscript drafts into chapter audio.</p>

<section>
  <h2>Upload Manuscript</h2>
  <input type="file" id="file-input" accept=".md,.txt">
  <button onclick="uploadFile()">Upload</button>
  <div id="upload-status"></div>
</section>

<section>
  <h2>Record</h2>
  <label>Manuscript</label>
  <div style="display:flex;gap:0.5rem;align-items:flex-start;">
    <select id="manuscript-select" style="margin-bottom:0.8rem;flex:1">{manuscript_options}</select>
    <button onclick="previewChapters()" style="white-space:nowrap;background:#333;color:#ccc;">Preview Chapters</button>
  </div>
  <div class="chapter-preview" id="chapter-preview"></div>
  <div class="row">
    <div>
      <label>Voice</label>
      <select id="voice-select">
        <optgroup label="🇺🇸 American Female">
          <option value="af_alloy">af_alloy</option>
          <option value="af_aoede">af_aoede</option>
          <option value="af_bella" selected>af_bella</option>
          <option value="af_heart">af_heart</option>
          <option value="af_nicole">af_nicole</option>
          <option value="af_sarah">af_sarah</option>
          <option value="af_sky">af_sky</option>
        </optgroup>
        <optgroup label="🇺🇸 American Male">
          <option value="am_adam">am_adam</option>
          <option value="am_michael">am_michael</option>
        </optgroup>
        <optgroup label="🇬🇧 British Female">
          <option value="bf_emma">bf_emma</option>
          <option value="bf_isabella">bf_isabella</option>
        </optgroup>
        <optgroup label="🇬🇧 British Male">
          <option value="bm_george">bm_george</option>
          <option value="bm_lewis">bm_lewis</option>
        </optgroup>
      </select>
    </div>
    <div>
      <label>Speed</label>
      <input type="number" id="speed-input" value="0.85" step="0.05" min="0.5" max="2.0">
    </div>
    <div>
      <label>Max chars / chunk</label>
      <input type="number" id="maxchars-input" value="1400" step="100" min="200" max="4000">
    </div>
  </div>
  <button id="start-btn" onclick="startJob()">Start Recording</button>
  <button id="stop-btn" class="stop" onclick="stopJob()" disabled>Stop</button>
</section>

<section>
  <h2>Voice Preview</h2>
  <label>Sample text</label>
  <input type="text" id="preview-text" value="The sunstone pulsed with a cold light, and she felt the Weave tighten.">
  <div style="display:flex;gap:0.5rem;align-items:center;">
    <button onclick="playVoicePreview()" id="preview-btn">▶ Play Preview</button>
    <audio id="preview-player" controls style="flex:1;display:none;"></audio>
  </div>
</section>

<section>
  <h2>Character Voices</h2>
  <p style="font-size:0.8rem;color:#666;margin-top:0;">Tag dialogue with <code style="background:#222;padding:0.1rem 0.3rem;border-radius:3px">::character::</code> at the start of a paragraph. Untagged paragraphs use the narrator voice. Supports blends: <code style="background:#222;padding:0.1rem 0.3rem;border-radius:3px">af_bella(0.6)+bm_george(0.4)</code></p>
  <div id="voices-container">Loading&hellip;</div>
</section>

<section>
  <h2>Current Job</h2>
  <div id="status-box">No job running.</div>
</section>

<section>
  <h2>Audio Builds</h2>
  <div id="builds-section">{build_rows}</div>
</section>

<script>
let currentJobId = null;
let pollTimer = null;
let voicesData = {{}};

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }}[ch]));
}}

// On load, check localStorage for an in-progress job and reconnect if still active
window.addEventListener('DOMContentLoaded', async () => {{
  await loadVoices();
  const saved = localStorage.getItem('chapterforge_job_id');
  if (saved) {{
    const resp = await fetch(`/api/jobs/${{saved}}`);
    if (resp.ok) {{
      const job = await resp.json();
      if (['queued', 'running', 'stopping'].includes(job.status)) {{
        currentJobId = saved;
        document.getElementById('start-btn').disabled = true;
        document.getElementById('stop-btn').disabled = job.status === 'stopping';
        renderStatus(job);
        pollStatus();
      }} else {{
        localStorage.removeItem('chapterforge_job_id');
      }}
    }}
  }}
}});

async function uploadFile() {{
  const input = document.getElementById('file-input');
  const status = document.getElementById('upload-status');
  if (!input.files.length) {{ status.textContent = 'Select a file first.'; return; }}
  const fd = new FormData();
  fd.append('file', input.files[0]);
  status.textContent = 'Uploading…';
  const resp = await fetch('/upload', {{ method: 'POST', body: fd }});
  const data = await resp.json();
  if (resp.ok) {{
    status.textContent = `Uploaded: ${{data.filename}}`;
    await refreshManuscripts();
  }} else {{
    status.textContent = `Error: ${{data.detail}}`;
  }}
}}

async function refreshManuscripts() {{
  const resp = await fetch('/api/manuscripts');
  const data = await resp.json();
  const sel = document.getElementById('manuscript-select');
  sel.innerHTML = '';
  for (const m of data.manuscripts) {{
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  }}
}}

async function startJob() {{
  const filename = document.getElementById('manuscript-select').value;
  const voice = document.getElementById('voice-select').value;
  const speed = parseFloat(document.getElementById('speed-input').value);
  const max_chars = parseInt(document.getElementById('maxchars-input').value);
  if (!filename) {{ alert('Select a manuscript first.'); return; }}
  const resp = await fetch('/api/jobs', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ filename, voice, speed, max_chars }})
  }});
  const data = await resp.json();
  currentJobId = data.job_id;
  localStorage.setItem('chapterforge_job_id', currentJobId);
  document.getElementById('start-btn').disabled = true;
  document.getElementById('stop-btn').disabled = false;
  pollStatus();
}}

async function stopJob() {{
  if (!currentJobId) return;
  await fetch(`/api/jobs/${{currentJobId}}/stop`, {{ method: 'POST' }});
  document.getElementById('stop-btn').disabled = true;
}}

async function playVoicePreview() {{
  const text = document.getElementById('preview-text').value.trim();
  const voice = document.getElementById('voice-select').value;
  const speed = parseFloat(document.getElementById('speed-input').value);
  const btn = document.getElementById('preview-btn');
  const player = document.getElementById('preview-player');
  if (!text) return;
  btn.disabled = true;
  btn.textContent = 'Generating…';
  try {{
    const resp = await fetch('/api/preview/voice', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text, voice, speed }})
    }});
    if (!resp.ok) {{ btn.textContent = '▶ Play Preview'; btn.disabled = false; return; }}
    const blob = await resp.blob();
    player.src = URL.createObjectURL(blob);
    player.style.display = 'block';
    player.play();
  }} finally {{
    btn.textContent = '▶ Play Preview';
    btn.disabled = false;
  }}
}}

async function previewChapters() {{
  const filename = document.getElementById('manuscript-select').value;
  if (!filename) return;
  const voice = document.getElementById('voice-select').value;
  const speed = parseFloat(document.getElementById('speed-input').value);
  const panel = document.getElementById('chapter-preview');
  panel.innerHTML = 'Loading…';
  panel.style.display = 'block';
  const resp = await fetch('/api/preview/chapters', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ filename, voice, speed }})
  }});
  if (!resp.ok) {{ panel.innerHTML = 'Error loading chapters.'; return; }}
  const data = await resp.json();
  const rows = data.chapters.map(ch =>
    `<tr><td>${{ch.index}}</td><td>${{escapeHtml(ch.title)}}</td><td>${{ch.word_count.toLocaleString()}} w</td></tr>`
  ).join('');
  panel.innerHTML = `<strong>${{data.chapter_count}} chapters</strong><table>${{rows}}</table>`;
}}

async function rechapter(buildId, chapterIndex, btn) {{
  if (!confirm(`Re-record chapter ${{chapterIndex}}? This will overwrite the existing audio.`)) return;
  btn.disabled = true;
  btn.textContent = '⏳';
  try {{
    const resp = await fetch(`/build/${{buildId}}/rechapter/${{chapterIndex}}`, {{ method: 'POST' }});
    if (!resp.ok) {{ alert('Failed to start re-record.'); return; }}
    const data = await resp.json();
    pollRechapter(data.job_id, btn);
  }} catch (e) {{
    btn.disabled = false;
    btn.textContent = '&#8635;';
  }}
}}

async function deleteBuild(buildId, btn) {{
  if (!confirm('Permanently delete this build and all its audio files? This cannot be undone.')) return;
  btn.disabled = true;
  try {{
    const resp = await fetch(`/build/${{buildId}}`, {{ method: 'DELETE' }});
    if (resp.ok) {{
      const details = btn.closest('details');
      if (details) details.remove();
    }} else {{
      alert('Failed to delete build.');
      btn.disabled = false;
    }}
  }} catch (e) {{
    alert('Error: ' + e.message);
    btn.disabled = false;
  }}
}}

function pollRechapter(jobId, btn) {{
  const t = setInterval(async () => {{
    const resp = await fetch(`/api/jobs/${{jobId}}`);
    const job = await resp.json();
    if (job.status === 'complete') {{
      clearInterval(t);
      btn.disabled = false;
      btn.textContent = '&#8635;';
      location.reload();
    }} else if (['error', 'stopped'].includes(job.status)) {{
      clearInterval(t);
      btn.disabled = false;
      btn.textContent = '&#8635;';
      alert(`Re-record failed: ${{job.error || job.status}}`);
    }}
  }}, 2000);
}}

function pollStatus() {{
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {{
    if (!currentJobId) return;
    const resp = await fetch(`/api/jobs/${{currentJobId}}`);
    const job = await resp.json();
    renderStatus(job);
    if (['complete', 'stopped', 'error', 'interrupted'].includes(job.status)) {{
      clearInterval(pollTimer);
      localStorage.removeItem('chapterforge_job_id');
      document.getElementById('start-btn').disabled = false;
      document.getElementById('stop-btn').disabled = true;
      if (job.status === 'complete') location.reload();
    }}
  }}, 2000);
}}

function renderStatus(job) {{
  const box = document.getElementById('status-box');
  const lines = [
    `Status: <strong>${{escapeHtml(job.status)}}</strong>`,
    job.current_chapter ? `Chapter: ${{job.current_chapter_index}} / ${{job.total_chapters}} &mdash; ${{escapeHtml(job.current_chapter)}}` : '',
    job.total_chunks ? `Chunk: ${{job.current_chunk}} / ${{job.total_chunks}}` : '',
    job.build_id ? `Build: ${{escapeHtml(job.build_id)}}` : '',
    job.error ? `<span style="color:#c0392b">Error: ${{escapeHtml(job.error)}}</span>` : '',
  ].filter(Boolean).join('<br>');
  box.innerHTML = lines;
}}

async function loadVoices() {{
  try {{
    const resp = await fetch('/api/voices');
    if (resp.ok) voicesData = await resp.json();
    else voicesData = {{}};
  }} catch (e) {{
    voicesData = {{}};
  }}
  try {{
    renderVoicesTable();
  }} catch (e) {{
    const container = document.getElementById('voices-container');
    if (container) container.innerHTML = '<p style="color:#c0392b;font-size:0.85rem">Error rendering voices table: ' + e.message + '</p>';
    console.error('renderVoicesTable error:', e);
  }}
}}

const KOKORO_VOICES = [
  ['🇺🇸 American Female', ['af_alloy','af_aoede','af_bella','af_heart','af_nicole','af_sarah','af_sky']],
  ['🇺🇸 American Male',   ['am_adam','am_echo','am_fenrir','am_liam','am_michael']],
  ['🇬🇧 British Female',  ['bf_emma','bf_isabella']],
  ['🇬🇧 British Male',    ['bm_fable','bm_george','bm_lewis']],
];

// Parse "af_bella(0.6)+bm_george(0.4)" → [{{voice, weight}}, ...]
// Single voice "af_bella" → [{{voice:'af_bella', weight:1.0}}]
function parseBlend(str) {{
  if (!str) return [{{voice: '{DEFAULT_VOICE}', weight: 1.0}}];
  const parts = str.split('+').map(s => s.trim()).filter(Boolean);
  return parts.map(p => {{
    const m = p.match(/^([^\\s+()]+)(?:\\((\\d+(?:\\.\\d+)?|\\.\\d+)\\))?$/);
    if (m) return {{voice: m[1], weight: m[2] ? parseFloat(m[2]) : 1.0}};
    return {{voice: p.replace(/[+()\\s]/g, ''), weight: 1.0}};
  }});
}}

// [{{voice, weight}}, ...] → "af_bella(0.6)+bm_george(0.4)".
function buildBlend(slots) {{
  const filtered = slots
    .filter(s => s.voice)
    .map(s => ({{ voice: s.voice, weight: Number.isFinite(s.weight) && s.weight > 0 ? s.weight : 1.0 }}));
  if (!filtered.length) return '{DEFAULT_VOICE}';
  if (filtered.length === 1) return filtered[0].voice;
  return filtered.map(s => `${{s.voice}}(${{Number(s.weight.toFixed(3))}})`).join('+');
}}

function voiceSelectHtml(id, selectedVal, allowEmpty = false) {{
  const sStyle = 'background:#222;border:1px solid #444;color:#eee;border-radius:3px;font-size:0.8rem;padding:0.2rem 0.4rem;flex:1;min-width:0;';
  let s = '<select id="' + id + '" style="' + sStyle + '">';
  if (allowEmpty) {{
    s += '<option value=""' + (!selectedVal ? ' selected' : '') + '>none</option>';
  }}
  for (const [label, voices] of KOKORO_VOICES) {{
    s += '<optgroup label="' + label + '">';
    for (const v of voices) {{
      s += '<option value="' + v + '"' + (v === selectedVal ? ' selected' : '') + '>' + v + '</option>';
    }}
    s += '</optgroup>';
  }}
  s += '</select>';
  return s;
}}

function blendEditorHtml(prefix, currentVoice) {{
  const slots = parseBlend(currentVoice).slice(0, 3);
  while (slots.length < 3) slots.push({{voice: '', weight: 1.0}});
  let html = '<div class="blend-editor">';
  slots.forEach((slot, idx) => {{
    const voiceId = prefix + '_voice_' + idx;
    const weightId = prefix + '_weight_' + idx;
    const allowEmpty = idx > 0;
    const weightValue = slot.voice ? slot.weight : '';
    html += '<div class="blend-slot">';
    html += voiceSelectHtml(voiceId, slot.voice, allowEmpty);
    html += '<input id="' + weightId + '" type="number" value="' + weightValue + '" step="0.05" min="0.05" max="5" placeholder="w">';
    html += '</div>';
  }});
  html += '</div>';
  return html;
}}

function readBlendFromDom(prefix) {{
  const slots = [];
  for (let idx = 0; idx < 3; idx++) {{
    const vEl = document.getElementById(prefix + '_voice_' + idx);
    const wEl = document.getElementById(prefix + '_weight_' + idx);
    if (!vEl || !vEl.value) continue;
    const weight = wEl ? parseFloat(wEl.value) : 1.0;
    slots.push({{voice: vEl.value, weight: Number.isFinite(weight) && weight > 0 ? weight : 1.0}});
  }}
  return buildBlend(slots);
}}



function renderVoicesTable() {{
  const container = document.getElementById('voices-container');
  if (!container) return;
  const iStyle = 'background:#222;border:1px solid #444;color:#eee;border-radius:3px;font-size:0.8rem;padding:0.2rem 0.4rem;';
  let html = '<table style="width:100%;border-collapse:collapse;font-size:0.85rem">';
  html += '<thead><tr style="color:#666;border-bottom:1px solid #333">';
  html += '<th style="text-align:left;padding:0.2rem 0.4rem">Character</th>';
  html += '<th style="text-align:left;padding:0.2rem 0.4rem">Voice Blend</th>';
  html += '<th style="text-align:left;padding:0.2rem 0.4rem">Speed</th>';
  html += '<th style="text-align:left;padding:0.2rem 0.4rem">Pitch</th>';
  html += '<th></th></tr></thead><tbody>';
  if (Object.keys(voicesData).length === 0) {{
    html += '<tr><td colspan="5" style="color:#555;padding:0.4rem 0.4rem;font-size:0.8rem;font-style:italic">No characters yet. Add a <strong style="color:#666">narrator</strong> row to override the default voice per-book.</td></tr>';
  }}
  for (const [name, p] of Object.entries(voicesData)) {{
    const nc = name === 'narrator' ? '#e8c96e' : '#ccc';
    html += '<tr>';
    html += '<td style="padding:0.3rem 0.4rem;color:' + nc + ';white-space:nowrap">' + name + '</td>';
    html += '<td style="padding:0.3rem 0.4rem">' + blendEditorHtml('vb_' + name, p.voice || '{DEFAULT_VOICE}') + '</td>';
    html += '<td style="padding:0.3rem 0.4rem"><input id="vs_' + name + '" type="number" value="' + (p.speed !== undefined ? p.speed : 0.85) + '" step="0.05" min="0.5" max="2.0" style="width:5.5rem;' + iStyle + '"></td>';
    html += '<td style="padding:0.3rem 0.4rem"><input id="vp_' + name + '" type="number" value="' + (p.pitch_ratio !== undefined ? p.pitch_ratio : 1.0) + '" step="0.01" min="0.7" max="1.3" style="width:5rem;' + iStyle + '"></td>';
    html += '<td style="padding:0.3rem 0.4rem;white-space:nowrap">';
    html += '<button data-name="' + name + '" onclick="testCharVoice(this.dataset.name)" style="background:#333;color:#ccc;padding:0.1rem 0.5rem;font-size:0.75rem;">Test</button>';
    if (name !== 'narrator') {{
      html += ' <button data-name="' + name + '" onclick="removeCharVoice(this.dataset.name)" style="background:#333;color:#c0392b;padding:0.1rem 0.5rem;font-size:0.75rem;">&#10005;</button>';
    }}
    html += '</td></tr>';
  }}
  html += '</tbody><tfoot><tr style="border-top:1px solid #333">';
  html += '<td style="padding:0.4rem 0.4rem"><input id="new-char-name" type="text" placeholder="name" style="width:100%;' + iStyle + '"></td>';
  html += '<td style="padding:0.4rem 0.4rem">' + blendEditorHtml('vb_new', '{DEFAULT_VOICE}') + '</td>';
  html += '<td style="padding:0.4rem 0.4rem"><input id="new-char-speed" type="number" value="{DEFAULT_SPEED}" step="0.05" min="0.5" max="2.0" style="width:5.5rem;' + iStyle + '"></td>';
  html += '<td style="padding:0.4rem 0.4rem"><input id="new-char-pitch" type="number" value="1.0" step="0.01" min="0.7" max="1.3" style="width:5rem;' + iStyle + '"></td>';
  html += '<td style="padding:0.4rem 0.4rem;white-space:nowrap">';
  html += '<button onclick="testNewCharVoice()" style="background:#333;color:#ccc;margin-right:0.3rem;">▶ Test</button>';
  html += '<button onclick="addCharVoice()" style="background:#333;color:#ccc;">+ Add</button>';
  html += '</td>';
  html += '</tr></tfoot></table>';
  html += '<div style="display:flex;justify-content:flex-end;margin-top:0.6rem;">';
  html += '<button onclick="saveVoices()">Save Voices</button>';
  html += '</div>';
  html += '<audio id="char-preview-player" style="display:none;margin-top:0.5rem;width:100%;" controls></audio>';
  container.innerHTML = html;
}}

// Sync DOM values (voice/speed/pitch) back into voicesData for all existing rows.
// voicesData is always the source of truth; DOM is just the edit surface.
function syncDomToVoicesData() {{
  for (const name of Object.keys(voicesData)) {{
    const sEl = document.getElementById('vs_' + name);
    const pEl = document.getElementById('vp_' + name);
    voicesData[name].voice = readBlendFromDom('vb_' + name);
    if (sEl) voicesData[name].speed = parseFloat(sEl.value);
    if (pEl) voicesData[name].pitch_ratio = parseFloat(pEl.value);
  }}
}}

function addCharVoice() {{
  const nameInput = document.getElementById('new-char-name');
  const name = nameInput.value.trim().toLowerCase().replace(/\\s+/g, '_');
  if (!name) return;
  // Persist any edits made to existing rows before re-rendering
  syncDomToVoicesData();
  if (!voicesData.hasOwnProperty(name)) {{
    voicesData[name] = {{
      voice: readBlendFromDom('vb_new'),
      speed: parseFloat(document.getElementById('new-char-speed').value) || {DEFAULT_SPEED},
      pitch_ratio: parseFloat(document.getElementById('new-char-pitch').value) || 1.0,
    }};
  }}
  renderVoicesTable();
}}

function removeCharVoice(name) {{
  syncDomToVoicesData();
  delete voicesData[name];
  renderVoicesTable();
}}

async function saveVoices() {{
  // Sync any in-progress DOM edits into voicesData, then POST the whole object
  syncDomToVoicesData();
  const resp = await fetch('/api/voices', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ voices: voicesData }})
  }});
  if (resp.ok) {{
    alert('Voices saved.');
  }} else {{
    alert('Failed to save voices.');
  }}
}}

async function testNewCharVoice() {{
  const voice = readBlendFromDom('vb_new');
  const speed = parseFloat(document.getElementById('new-char-speed').value) || {DEFAULT_SPEED};
  const pitch_ratio = parseFloat(document.getElementById('new-char-pitch').value) || 1.0;
  const text = document.getElementById('preview-text').value.trim() ||
    'The sunstone pulsed with a cold light, and she felt the Weave tighten.';
  const btn = document.querySelector('button[onclick="testNewCharVoice()"]');
  if (btn) {{ btn.disabled = true; btn.textContent = '\u2026'; }}
  try {{
    const resp = await fetch('/api/preview/voice', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text, voice, speed, pitch_ratio }})
    }});
    if (resp.ok) {{
      const blob = await resp.blob();
      const player = document.getElementById('char-preview-player');
      if (player) {{
        player.src = URL.createObjectURL(blob);
        player.style.display = 'block';
        player.play();
      }}
    }}
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = '\u25b6 Test'; }}
  }}
}}

async function testCharVoice(name) {{
  syncDomToVoicesData();
  const profile = voicesData[name];
  if (!profile) return;
  const text = document.getElementById('preview-text').value.trim() ||
    'The sunstone pulsed with a cold light, and she felt the Weave tighten.';
  const el = document.querySelector('button[data-name="' + name + '"][onclick^="testCharVoice"]');
  if (el) {{ el.disabled = true; el.textContent = '\u2026'; }}
  try {{
    const resp = await fetch('/api/preview/voice', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text, voice: profile.voice, speed: profile.speed, pitch_ratio: profile.pitch_ratio || 1.0 }})
    }});
    if (resp.ok) {{
      const blob = await resp.blob();
      const player = document.getElementById('char-preview-player');
      if (player) {{
        player.src = URL.createObjectURL(blob);
        player.style.display = 'block';
        player.play();
      }}
    }}
  }} finally {{
    if (el) {{ el.disabled = false; el.textContent = 'Test'; }}
  }}
}}
</script>
<footer style="text-align:center;color:#555;font-size:0.75rem;margin-top:2rem;padding-bottom:1rem;">
  ChapterForge TTS &mdash; v{APP_VERSION}
</footer>
</body>
</html>"""
