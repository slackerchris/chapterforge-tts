# ChapterForge TTS

> Turn manuscript drafts into chapter audio.

A lightweight web app that takes a Markdown manuscript, splits it into chapters, and generates MP3 audio per chapter via the [Kokoro TTS API](https://github.com/remsky/Kokoro-FastAPI). Built for listening to drafts in the car, catching awkward prose, pacing problems, and dialogue that sounds like cardboard wearing boots.

---

## Features (MVP)

- Upload `.md` or `.txt` manuscript files
- Split manuscript into chapters automatically
- Generate MP3 audio per chapter via Kokoro TTS
- Select voice and speed per recording job
- Start / stop jobs between chunks
- Live job progress polling
- In-browser audio playback
- Build manifests with source hashes (never overwrites old builds)

---

## Quick Start (Docker Compose)

```bash
git clone https://github.com/slackerchris/chapterforge-tts.git
cd chapterforge-tts
mkdir -p books output
docker compose up --build
```

Open `http://localhost:8890`.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KOKORO_ENDPOINT` | `http://tts.throne.middl.earth/v1/audio/speech` | Kokoro TTS API URL |
| `DEFAULT_VOICE` | `af_bella` | Default Kokoro voice |
| `DEFAULT_SPEED` | `0.85` | Default playback speed |
| `DEFAULT_MAX_CHARS` | `1400` | Max characters per TTS chunk |

---

## Supported Kokoro Voices

| Voice | Character |
|---|---|
| `af_bella` | Warm female narrator |
| `af_heart` | Softer female |
| `af_nicole` | Clear female |
| `af_sarah` | Energetic female |
| `am_fenrir` | Deep male |
| `am_michael` | Neutral male |
| `bm_fable` | British male |
| `bf_emma` | British female |

---

## Output Structure

```
output/
  chapters/
    <build_id>/
      manifest.json
      01_chapter_1.mp3
      02_chapter_2.mp3
      ...
```

Build IDs are derived from manuscript name + voice + speed + source hash. Old builds are never overwritten.

---

## Chapter Detection

Chapters are detected by Markdown headings matching:

```
# Chapter 1
## Chapter 1
# Ch 1
# Prologue
## Epilogue
# Part 1
```

---

## Portainer Stack

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

---

## Roadmap

- [ ] Phase 1 — MVP (current)
- [ ] Phase 2 — GHCR + Portainer deployment
- [ ] Phase 3 — Draft library with source hash change detection
- [ ] Phase 4 — NAS integration (Audiobookshelf / Jellyfin)
- [ ] Phase 5 — Character voice profiles with per-speaker voice/speed
- [ ] Phase 6 — n8n automation (notifications, sync, scan triggers)

---

## What This Is Not (Yet)

- A professional audiobook production tool
- A Gradio app
- A React app
- A replacement for the Kokoro TTS engine
