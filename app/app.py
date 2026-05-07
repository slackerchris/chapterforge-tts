"""
ChapterForge TTS
Turn manuscript drafts into chapter audio.
"""

import os
import re
import uuid
import json
import hashlib
import threading
import time
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
import aiofiles
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KOKORO_ENDPOINT = os.environ.get(
    "KOKORO_ENDPOINT", "http://tts.throne.middl.earth/v1/audio/speech"
)
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "af_bella")
DEFAULT_SPEED = float(os.environ.get("DEFAULT_SPEED", "0.85"))
DEFAULT_MAX_CHARS = int(os.environ.get("DEFAULT_MAX_CHARS", "1400"))

BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/app/books"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/output"))

BOOKS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ChapterForge TTS")

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------

jobs: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

CHAPTER_HEADING_RE = re.compile(
    r"^#{1,3}\s+("
    r"chapter\s+[\divxlc]+"       # Chapter 1, Chapter IV
    r"|ch\.?\s*[\divxlc]+"        # Ch 1, Ch. IV
    r"|prologue"
    r"|epilogue"
    r"|part\s+[\divxlc]+"         # Part 1, Part II
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
    # Collapse excess blank lines left by stripping
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_chapters(text: str) -> list[dict]:
    """Split manuscript text into chapters by heading."""
    text = strip_frontmatter(text)
    parts = CHAPTER_HEADING_RE.split(text)
    headings = CHAPTER_HEADING_RE.findall(text)

    chapters = []

    # Text before the first heading becomes a preamble chapter if non-empty
    if parts[0].strip():
        chapters.append({"title": "Preamble", "body": parts[0].strip()})

    for i, heading in enumerate(headings):
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        chapters.append({"title": heading.strip(), "body": body})

    # If no headings were found, treat the whole text as a single chapter
    if not chapters:
        chapters.append({"title": "Chapter 1", "body": text.strip()})

    return [c for c in chapters if c["body"]]


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


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def call_kokoro(text: str, voice: str, speed: float) -> bytes:
    """Send a chunk to Kokoro and return raw audio bytes (WAV)."""
    payload = {
        "model": "kokoro",
        "voice": voice,
        "speed": speed,
        "input": text,
    }
    resp = requests.post(KOKORO_ENDPOINT, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.content


def concat_chunks_to_mp3(audio_chunks: list[bytes], output_path: Path) -> None:
    """Concatenate WAV audio chunks into a single MP3 using ffmpeg."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        chunk_paths = []
        for i, data in enumerate(audio_chunks):
            p = tmp / f"chunk_{i:04d}.wav"
            p.write_bytes(data)
            chunk_paths.append(p)

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


def record_job(job_id: str) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.utcnow().isoformat()

    try:
        manuscript_path = BOOKS_DIR / job["filename"]
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

        job["build_id"] = build_id
        job["build_dir"] = str(build_dir)

        chapters = split_into_chapters(text)
        job["total_chapters"] = len(chapters)

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
            if job.get("stop_requested"):
                job["status"] = "stopped"
                return

            job["current_chapter"] = chapter["title"]
            job["current_chapter_index"] = ch_idx + 1

            clean_body = clean_markdown(chapter["body"])
            chunks = split_into_chunks(clean_body, max_chars)
            total_chunks = len(chunks)
            job["total_chunks"] = total_chunks
            job["current_chunk"] = 0

            audio_parts: list[bytes] = []

            for chunk_idx, chunk in enumerate(chunks):
                if job.get("stop_requested"):
                    job["status"] = "stopped"
                    return

                job["current_chunk"] = chunk_idx + 1
                audio_data = call_kokoro(chunk, voice, speed)
                audio_parts.append(audio_data)

            # Concatenate WAV chunks → MP3 via ffmpeg
            chapter_filename = f"{ch_idx + 1:02d}_{_safe_slug(chapter['title'])}.mp3"
            chapter_path = build_dir / chapter_filename
            concat_chunks_to_mp3(audio_parts, chapter_path)

            manifest["chapters"].append(
                {
                    "chapter_index": ch_idx + 1,
                    "title": chapter["title"],
                    "chunk_count": total_chunks,
                    "output_mp3": chapter_filename,
                }
            )

        # Write manifest
        manifest_path = build_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        job["status"] = "complete"
        job["completed_at"] = datetime.utcnow().isoformat()

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


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
        [f.name for f in BOOKS_DIR.iterdir() if f.is_file() and f.suffix in (".md", ".txt")]
    )
    builds = _list_builds()
    return _render_ui(manuscripts, builds)


@app.post("/upload")
async def upload_manuscript(file: UploadFile = File(...)):
    allowed = {".md", ".txt"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail="Only .md and .txt files are supported.")

    dest = BOOKS_DIR / file.filename
    async with aiofiles.open(dest, "wb") as f:
        content = await file.read()
        await f.write(content)

    return JSONResponse({"status": "uploaded", "filename": file.filename})


@app.post("/api/jobs")
async def create_job(req: JobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id,
        "filename": req.filename,
        "voice": req.voice,
        "speed": req.speed,
        "max_chars": req.max_chars,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "current_chapter": None,
        "current_chapter_index": 0,
        "total_chapters": 0,
        "current_chunk": 0,
        "total_chunks": 0,
        "build_id": None,
        "error": None,
        "stop_requested": False,
    }
    background_tasks.add_task(record_job, job_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}.")
    job["stop_requested"] = True
    job["status"] = "stopping"
    return {"status": "stopping"}


@app.get("/api/manuscripts")
async def list_manuscripts():
    files = sorted(
        [f.name for f in BOOKS_DIR.iterdir() if f.is_file() and f.suffix in (".md", ".txt")]
    )
    return {"manuscripts": files}


@app.get("/build/{build_id}")
async def get_build(build_id: str):
    build_dir = OUTPUT_DIR / "chapters" / build_id
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Build not found.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


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

def _render_ui(manuscripts: list[str], builds: list[dict]) -> str:
    manuscript_options = "".join(
        f'<option value="{m}">{m}</option>' for m in manuscripts
    )
    if not manuscript_options:
        manuscript_options = '<option value="" disabled selected>No manuscripts uploaded yet</option>'

    build_rows = ""
    for b in builds:
        chapters_html = "".join(
            f'<li><audio controls src="/audio/{b["build_id"]}/{ch["output_mp3"]}"></audio> {ch["title"]}</li>'
            for ch in b.get("chapters", [])
        )
        build_rows += f"""
        <details>
          <summary><strong>{b.get("book", b["build_id"])}</strong>
            &nbsp;|&nbsp; {b.get("voice")} @ {b.get("speed")}
            &nbsp;|&nbsp; {b.get("draft_date", "")}
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
  <select id="manuscript-select">{manuscript_options}</select>
  <div class="row">
    <div>
      <label>Voice</label>
      <select id="voice-select">
        <option value="af_bella" selected>af_bella</option>
        <option value="af_heart">af_heart</option>
        <option value="af_nicole">af_nicole</option>
        <option value="af_sarah">af_sarah</option>
        <option value="am_fenrir">am_fenrir</option>
        <option value="am_michael">am_michael</option>
        <option value="bm_fable">bm_fable</option>
        <option value="bf_emma">bf_emma</option>
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
  sel.innerHTML = data.manuscripts.map(m => `<option value="${{m}}">${{m}}</option>`).join('');
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
  document.getElementById('start-btn').disabled = true;
  document.getElementById('stop-btn').disabled = false;
  pollStatus();
}}

async function stopJob() {{
  if (!currentJobId) return;
  await fetch(`/api/jobs/${{currentJobId}}/stop`, {{ method: 'POST' }});
  document.getElementById('stop-btn').disabled = true;
}}

function pollStatus() {{
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {{
    if (!currentJobId) return;
    const resp = await fetch(`/api/jobs/${{currentJobId}}`);
    const job = await resp.json();
    renderStatus(job);
    if (['complete', 'stopped', 'error'].includes(job.status)) {{
      clearInterval(pollTimer);
      document.getElementById('start-btn').disabled = false;
      document.getElementById('stop-btn').disabled = true;
      if (job.status === 'complete') location.reload();
    }}
  }}, 2000);
}}

function renderStatus(job) {{
  const box = document.getElementById('status-box');
  const lines = [
    `Status: <strong>${{job.status}}</strong>`,
    job.current_chapter ? `Chapter: ${{job.current_chapter_index}} / ${{job.total_chapters}} &mdash; ${{job.current_chapter}}` : '',
    job.total_chunks ? `Chunk: ${{job.current_chunk}} / ${{job.total_chunks}}` : '',
    job.build_id ? `Build: ${{job.build_id}}` : '',
    job.error ? `<span style="color:#c0392b">Error: ${{job.error}}</span>` : '',
  ].filter(Boolean).join('<br>');
  box.innerHTML = lines;
}}
</script>
</body>
</html>"""
