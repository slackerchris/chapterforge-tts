# ChapterForge TTS Plan

A practical build plan for **ChapterForge TTS**, a lightweight manuscript-to-audio tool that uses a local Kokoro TTS API to generate draft audiobook-style chapter audio.

This is not meant to become a professional audiobook production suite yet. This is for listening to drafts in the car, catching awkward prose, pacing problems, repeated phrases, and dialogue that sounds like cardboard wearing boots.

---

## 1. Goal

Build a small web app that can:

- Upload or select a manuscript draft
- Split the manuscript into chapters
- Generate MP3 audio per chapter using Kokoro TTS
- Start and stop recording jobs
- Show job progress
- Play generated chapter audio in the browser
- Support multiple books, drafts, voices, and later character voice profiles
- Run as a Docker container
- Be pushed to GitHub Container Registry
- Be managed by Portainer

---

## 2. Current Working Pieces

### Kokoro TTS Server

The existing Kokoro API is already working.

Current endpoint:

```text
http://tts.throne.middl.earth/v1/audio/speech
```

Local endpoint when running tools on the Kokoro server itself:

```text
http://127.0.0.1:8880/v1/audio/speech
```

The current Kokoro `server.py` supports dynamic voices:

```python
voice: str = "af_bella"
generator = pipeline(req.input, voice=req.voice)
```

That means built-in Kokoro voices can be selected by changing the request payload:

```json
{
  "model": "kokoro",
  "voice": "af_heart",
  "speed": 0.85,
  "input": "Text to read."
}
```

### Working Prototype

Prototype location on Kokoro server:

```text
~/apps/manuscript-recorder/
```

Working test input:

```text
~/apps/manuscript-recorder/books/test.md
```

Working output:

```text
~/apps/manuscript-recorder/output/chapters/test_af_bella_0_85_3838621b7ee043d4/
  01_chapter_1.mp3
  02_chapter_2.mp3
```

Prototype already proves:

```text
Markdown manuscript
→ chapter split
→ Kokoro API request
→ WAV chunk files
→ MP3 chapter output
```

---

## 3. Reference Repo

Cloned reference repo:

```text
~/apps/kokoro-audiobook-reference/
```

Source:

```text
https://github.com/solveditnpc/Kokoro-82M-audiobooks
```

Keep this repo as a **parts shelf**, not the main app.

### Useful ideas to borrow

- Text chunking
- Failed chunk tracking
- MP3/AAC/WAV output logic
- ffmpeg conversion
- Silence between chunks
- Voice selection ideas
- Voice interpolation ideas for later

### Do not directly adopt yet

- Its model loader
- Its local `voices/` folder assumption
- Its PDF-first workflow
- Its terminal menu
- Its Gradio/dependency stack
- Its 150-character chunk size

Its chunk size is too small for manuscript listening. For ChapterForge TTS, start around:

```text
1200–1600 characters per chunk
```

---

## 4. Naming

Chosen app name:

```text
ChapterForge TTS
```

Recommended technical names:

```text
Repo:    chapterforge-tts
Image:   ghcr.io/slackerchris/chapterforge-tts:latest
Domain:  chapterforge.throne.middl.earth
```

Possible subtitle:

```text
Turn manuscript drafts into chapter audio.
```

Or:

```text
Draft audiobook generation for writers who need to hear the chapter before they trust it.
```

---

## 5. Desired Architecture

```text
Browser
  ↓
ChapterForge TTS web app
  ↓
Kokoro TTS API
  ↓
MP3 chapter files
  ↓
Browser playback / phone / car / Audiobookshelf / Jellyfin
```

Recommended service split:

```text
tts.throne.middl.earth
  Kokoro TTS engine only

chapterforge.throne.middl.earth
  ChapterForge TTS GUI, job control, playback, draft library
```

Do not shove the web app into the Kokoro server unless testing. Keep Kokoro as the robot throat. ChapterForge is the dashboard with buttons.

---

## 6. Docker/GHCR Deployment Plan

### Build flow

```text
Local source repo
→ Dockerfile
→ GitHub Actions builds image
→ Push to GHCR
→ Portainer pulls/runs image
→ NPM proxies app
→ App calls Kokoro API
```

### Recommended GitHub repo layout

```text
chapterforge-tts/
  app/
    app.py
    requirements.txt
  Dockerfile
  docker-compose.yml
  .github/
    workflows/
      docker-publish.yml
  README.md
```

---

## 7. Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8890

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8890"]
```

---

## 8. Python Requirements

`app/requirements.txt`:

```text
fastapi
uvicorn[standard]
requests
python-multipart
```

Later additions may include:

```text
pydub
aiofiles
sqlmodel
jinja2
```

But do not add them until needed. Dependency bloat is how tiny tools become haunted.

---

## 9. GitHub Actions for GHCR

`.github/workflows/docker-publish.yml`:

```yaml
name: Build and publish Docker image

on:
  push:
    branches:
      - main
  workflow_dispatch:

env:
  IMAGE_NAME: chapterforge-tts

jobs:
  build-and-push:
    runs-on: ubuntu-latest

    permissions:
      contents: read
      packages: write

    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Log in to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Set lowercase image owner
        run: echo "IMAGE_OWNER=${GITHUB_REPOSITORY_OWNER,,}" >> $GITHUB_ENV

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ env.IMAGE_OWNER }}/${{ env.IMAGE_NAME }}:latest
            ghcr.io/${{ env.IMAGE_OWNER }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
```

Resulting images:

```text
ghcr.io/slackerchris/chapterforge-tts:latest
ghcr.io/slackerchris/chapterforge-tts:<commit-sha>
```

---

## 10. Portainer Stack

Initial Portainer stack:

```yaml
services:
  chapterforge-tts:
    image: ghcr.io/slackerchris/chapterforge-tts:latest
    container_name: chapterforge-tts
    ports:
      - "8890:8890"
    environment:
      KOKORO_ENDPOINT: "http://tts.throne.middl.earth/v1/audio/speech"
      DEFAULT_VOICE: "af_bella"
      DEFAULT_SPEED: "0.85"
    volumes:
      - /mnt/chapterforge/books:/app/books
      - /mnt/chapterforge/output:/app/output
    restart: unless-stopped
```

If container-to-proxy routing gets weird, use the Kokoro LXC directly:

```yaml
KOKORO_ENDPOINT: "http://10.0.0.80:8880/v1/audio/speech"
```

Raw LAN IP may be less pretty, but it often avoids reverse proxy drama.

---

## 11. Nginx Proxy Manager

Proxy host:

```text
Domain: chapterforge.throne.middl.earth
Scheme: http
Forward Hostname/IP: <docker-host-ip>
Forward Port: 8890
```

Optional later:

```text
chapterforge.middl.earth
```

Only expose externally if access control is handled. Manuscript drafts do not need to be wandering around the internet in a tiny cloak.

---

## 12. Storage Plan

Do not store important drafts or generated audio only inside the container.

Initial simple host paths:

```text
/mnt/chapterforge/books
/mnt/chapterforge/output
```

Better long-term NAS layout:

```text
/mnt/Mordor/manuscripts/
  echo_of_the_sunstone/
  draft_of_shadows/
  threads_of_the_weave/

/mnt/Mordor/audiobooks/drafts/
  echo_of_the_sunstone/
  draft_of_shadows/
  threads_of_the_weave/
```

Suggested app-internal paths:

```text
/app/books
/app/output
```

---

## 13. Project/Draft Model

Do not design this as one manuscript equals one audio folder.

The real shape is:

```text
Project / Book
  Draft
    Manuscript file
    Audio builds
      Voice/speed/source hash
```

Example:

```text
Echo of the Sunstone
  Beta Reader Draft - 2026-05-06
    manuscript.md
    audio/
      af_bella_0.85/
      af_heart_0.80/

Draft of Shadows
  Working Draft - 2026-05-06
    manuscript.md
    audio/

Threads of the Weave
  Outline - 2026-05-06
    outline.md
    audio/
```

Suggested output structure:

```text
output/
  chapters/
    echo_of_the_sunstone/
      beta_reader_2026_05_06/
        af_bella_0_85_<source_hash>/
          01_chapter_1.mp3
          02_chapter_2.mp3
```

Keep every audio build tied to:

- Book
- Draft
- Voice
- Speed
- Source hash
- Build date/time

Never overwrite audio builds. Create a new build.

---

## 14. Manifest Format

Each audio build should write a manifest.

Example:

```json
{
  "book": "Echo of the Sunstone",
  "draft": "Beta Reader Draft",
  "draft_date": "2026-05-06",
  "source_file": "manuscript.md",
  "source_hash": "abc123",
  "voice": "af_bella",
  "speed": 0.85,
  "max_chars": 1400,
  "build_id": "echo_beta_af_bella_0_85_abc123",
  "chapters": [
    {
      "chapter_index": 1,
      "title": "Chapter 1",
      "chunk_count": 18,
      "output_mp3": "01_chapter_1.mp3"
    }
  ]
}
```

The source hash matters. It lets the app later detect:

```text
This chapter changed since the last recording.
```

Then only changed chapters need to be re-recorded.

---

## 15. Web App MVP

Minimum useful UI:

```text
ChapterForge TTS

Upload manuscript
[Choose File] [Upload]

Record
File:      [echo_beta.md ▼]
Voice:     [af_bella]
Speed:     [0.85]
Max chars: [1400]

[Start Recording]
[Stop]

Current Job
status: running
chapter: Chapter 7
chunk: 12 / 31

Audio Builds
- echo_beta_af_bella_0_85_abc123
  ▶ 01_chapter_1.mp3
  ▶ 02_chapter_2.mp3
```

MVP features:

- Upload manuscript
- Select manuscript
- Set voice
- Set speed
- Set max chunk size
- Start job
- Stop job between chunks
- Poll job status
- List audio builds
- Play MP3s in browser

Do not start with React. Use FastAPI + plain HTML + vanilla JS first. This is an internal tool, not a venture-backed button farm.

---

## 16. API Endpoints

Initial endpoints:

```text
GET  /
POST /upload

POST /api/jobs
GET  /api/jobs/{job_id}
POST /api/jobs/{job_id}/stop

GET  /build/{build_id}
GET  /audio/{build_id}/{filename}
```

Later endpoints:

```text
GET  /api/books
POST /api/books

GET  /api/drafts
POST /api/drafts

POST /api/chapters/{chapter_id}/record
GET  /api/builds
GET  /api/voices
POST /api/settings
```

---

## 17. Job System

The first version can use an in-memory job dictionary and background threads.

Good enough for MVP:

```python
jobs = {}
Thread(target=record_job, daemon=True).start()
```

Later, move to SQLite-backed jobs.

Stop behavior:

- Stop between chunks
- Do not try to interrupt active Kokoro request
- Mark job as `stopping`
- Recorder checks `stop_requested` before each chunk
- Mark job as `stopped`

Statuses:

```text
queued
running
stopping
stopped
complete
error
```

---

## 18. Chunking Strategy

Do not send whole chapters to Kokoro.

Start with:

```text
max_chars: 1200–1600
```

Recommended defaults:

```text
Voice: af_bella
Speed: 0.85
Max chars: 1400
Output: MP3 per chapter
```

Chunking rules:

- Split by chapter headings
- Clean Markdown syntax
- Split into paragraph groups
- Keep chunks below max character limit
- Split very long paragraphs by sentence
- Add short silence between chunks later

The borrowed reference repo uses around 150-character chunks. That is too short for this use case and will sound choppy.

---

## 19. Chapter Splitting

Support headings like:

```md
# Chapter 1
## Chapter 1
# Ch 1
## Prologue
## Epilogue
```

Eventually support custom chapter patterns.

Possible frontmatter later:

```yaml
---
book: Echo of the Sunstone
draft: Beta Reader Draft
date: 2026-05-06
---
```

---

## 20. Voice Plan

Start with one voice per recording.

Useful built-in Kokoro voices to test:

```text
af_bella
af_heart
af_nicole
af_sarah
am_fenrir
am_michael
bm_fable
bf_emma
```

Recommended starting defaults:

```text
Narration:
  af_bella, speed 0.85

Alternate:
  af_heart, speed 0.80–0.85
```

If a voice is missing locally, using it may download/cache it automatically. If not, manually place the `.pt` voice file beside the cached official voices.

Known cache location from current setup:

```text
/home/chris/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff3571791e39611d31c381e3a41a3af07b4987/voices/
```

---

## 21. Custom Voice Plan

Later, explore:

- Kokoro-compatible `.pt` voicepacks
- Voice interpolation/mixing
- Generated voicepacks from community tools

Good signs for custom voices:

```text
- .pt file
- clear Kokoro compatibility
- clear license
- sample audio
- no celebrity/person impersonation nonsense
```

Bad signs:

```text
- random zip with no source
- no license
- WAV/MP3 only
- claims to clone a real person
- requires replacing model weights
```

The reference repo has `custom_interpolation.py`, which may be useful later for blending existing Kokoro voices.

---

## 22. Character Voice Roadmap

This is a future feature, not MVP.

Goal:

```text
Manuscript text
→ speaker/voice tags
→ per-character voice profile
→ Kokoro generation
→ optional pitch/speed post-processing
→ stitched chapter audio
```

### Simple speaker tag syntax

```md
::anya:: "We do not have time for this."

::bjorn:: "Old magic always says that right before it asks for blood."

The silence that followed was old enough to resent them.
```

Anything without a tag uses the default narrator profile.

### Alternative syntax

```md
@voice narrator

The grove did not answer quickly. It never had.

@voice anya
"We do not have time for this."

@voice bjorn
"Then we should stop wasting it."
```

### Voice profile file

Example `voices.json`:

```json
{
  "default": {
    "voice": "af_heart",
    "speed": 0.85
  },
  "anya": {
    "voice": "af_bella",
    "speed": 0.88,
    "pitch_ratio": 1.02
  },
  "bjorn": {
    "voice": "am_fenrir",
    "speed": 0.82,
    "pitch_ratio": 0.97
  },
  "elara": {
    "voice": "af_nicole",
    "speed": 0.86
  },
  "lianor": {
    "voice": "bm_fable",
    "speed": 0.80
  },
  "kell": {
    "voice": "af_sarah",
    "speed": 0.92
  }
}
```

### Pitch processing

Kokoro may not support pitch directly through the current API.

Possible ffmpeg pitch-ish shift:

Higher pitch:

```bash
ffmpeg -i input.wav -filter:a "asetrate=24000*1.04,aresample=24000,atempo=0.96" output.wav
```

Lower pitch:

```bash
ffmpeg -i input.wav -filter:a "asetrate=24000*0.96,aresample=24000,atempo=1.04" output.wav
```

Hide this behind simple profile values like:

```json
"pitch_ratio": 1.04
```

Do not start with automatic speaker detection. Start with explicit tags. Auto-detection can come later after the tool is useful.

---

## 23. n8n Plan

Do not start with n8n.

Use n8n later to orchestrate finished workflows.

Good n8n jobs:

```text
- Trigger recording from a webhook
- Copy finished MP3s to NAS
- Copy finished MP3s to Google Drive
- Notify phone/email/Telegram when complete
- Trigger Audiobookshelf/Jellyfin library scan
- Archive old builds
```

Bad n8n jobs:

```text
- Splitting manuscript intelligently
- Stitching audio
- Running ffmpeg
- Managing long-running TTS chunks
```

n8n should call ChapterForge, not replace ChapterForge.

Future n8n shape:

```text
Webhook/manual trigger
→ POST /api/jobs
→ Poll /api/jobs/{job_id}
→ On complete, copy/sync files
→ Notify
```

---

## 24. Development Phases

### Phase 0: Prototype complete

Already done:

- Local recorder script
- Test Markdown input
- Chapter MP3 output

### Phase 1: Dockerized web app MVP

Build:

- FastAPI app
- Upload manuscript
- Start job
- Stop job
- Progress polling
- Audio playback
- Dockerfile
- Local Docker test

### Phase 2: GHCR + Portainer

Build:

- GitHub repo
- GitHub Actions workflow
- GHCR image
- Portainer stack
- NPM proxy

### Phase 3: Draft library

Add:

- Books
- Drafts
- Metadata
- Source hashes
- Build history
- Do not overwrite builds

### Phase 4: NAS integration

Add:

- Durable manuscript folder
- Durable audio output folder
- Optional Audiobookshelf/Jellyfin target

### Phase 5: Character voice profiles

Add:

- `voices.json`
- Speaker tags
- Per-character voice/speed
- Optional ffmpeg pitch ratio

### Phase 6: n8n automation

Add:

- Completion notifications
- Drive sync
- Audiobookshelf/Jellyfin scan
- Scheduled draft builds

---

## 25. Immediate Next Steps

1. Create GitHub repo:

```text
chapterforge-tts
```

2. Add files:

```text
app/app.py
app/requirements.txt
Dockerfile
docker-compose.yml
.github/workflows/docker-publish.yml
README.md
```

3. Copy the working logic from:

```text
~/apps/manuscript-recorder/recorder.py
```

into the FastAPI app.

4. Build locally:

```bash
docker build -t ghcr.io/slackerchris/chapterforge-tts:dev .
```

5. Run locally:

```bash
docker run --rm -p 8890:8890 \
  -e KOKORO_ENDPOINT="http://tts.throne.middl.earth/v1/audio/speech" \
  -v "$PWD/books:/app/books" \
  -v "$PWD/output:/app/output" \
  ghcr.io/slackerchris/chapterforge-tts:dev
```

6. Open:

```text
http://<docker-host-ip>:8890
```

7. Upload `test.md`.

8. Generate MP3s.

9. Confirm playback in browser.

10. Push to GitHub and let GHCR build it.

11. Deploy via Portainer.

---

## 26. Things Not To Do Yet

Do not:

- Rewrite Kokoro
- Replace the working Kokoro API
- Install the whole reference repo into the current Kokoro venv
- Add React before the basic web app works
- Build n8n first
- Add automatic speaker detection first
- Try to make professional audiobook narration yet
- Overwrite old builds
- Store important files only inside the container

---

## 27. Success Criteria for MVP

The MVP is successful when:

- You can open `chapterforge.throne.middl.earth`
- Upload/select a manuscript
- Pick voice and speed
- Press Start
- See progress
- Stop between chunks if needed
- Play generated chapter MP3s in the browser
- Copy/download those MP3s for car listening

That is enough.

Everything after that is gravy, and probably another container.
