# ChapterForge TTS

> Turn manuscript drafts into chapter audio.

A lightweight self-hosted web app that takes a Markdown or plain-text manuscript, splits it into chapters, and generates MP3 audio per chapter via the [Kokoro TTS API](https://github.com/remsky/Kokoro-FastAPI). Built for listening to drafts in the car, catching awkward prose, pacing problems, and dialogue that sounds like cardboard wearing boots.

---

## Features

- Upload `.md` or `.txt` manuscript files
- Auto-split into chapters on `#` / `##` headings
- Generate MP3 audio per chapter via Kokoro TTS
- Combine all chapters into a single `.m4b` audiobook with chapter markers
- Per-job voice and speed selection
- **Character voice profiles** — tag paragraphs with `::character::` to use per-character voice, speed, and pitch
- **Voice blending** — mix multiple Kokoro voices with weighted blends
- **Voice presets** — save named blends and reuse them from dropdowns
- **Pitch shifting** via ffmpeg (per character, no re-recording needed)
- Start / stop jobs between chunks
- **Partial resume** — interrupted jobs pick up where they left off
- SQLite job persistence — survives container restarts
- Live job progress polling
- In-browser chapter audio playback
- Webhook notification on job complete / error
- Rotating log files

---

## Quick Start (Docker Compose)

```bash
git clone https://github.com/slackerchris/chapterforge-tts.git
cd chapterforge-tts
docker compose up --build
```

Open `http://localhost:8890`.

Manuscripts go in `./books/`, output (MP3s, builds, DB) lands in `./output/`.

---

## Production Deploy (GHCR + Portainer)

```yaml
services:
  chapterforge-tts:
    image: ghcr.io/slackerchris/chapterforge-tts:latest
    container_name: chapterforge-tts
    ports:
      - "8890:8890"
    environment:
      KOKORO_ENDPOINT: "http://your-kokoro-host/v1/audio/speech"
      DEFAULT_VOICE: "af_bella"
      DEFAULT_SPEED: "0.85"
      DEFAULT_MAX_CHARS: "1400"
    volumes:
      - /mnt/chapterforge/books:/app/books
      - /mnt/chapterforge/output:/app/output
      - /mnt/chapterforge/logs:/app/logs
    restart: unless-stopped
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KOKORO_ENDPOINT` | `http://tts.throne.middl.earth/v1/audio/speech` | Kokoro TTS API URL (OpenAI-compatible) |
| `DEFAULT_VOICE` | `af_bella` | Default voice for jobs with no narrator profile |
| `DEFAULT_SPEED` | `0.85` | Default speed (0.5 – 2.0) |
| `DEFAULT_MAX_CHARS` | `1400` | Max characters per TTS chunk |
| `CHAPTER_TRAIL_SILENCE` | `3.0` | Seconds of silence appended to each chapter |
| `KOKORO_RETRIES` | `3` | Attempts per chunk before failing |
| `KOKORO_RETRY_DELAY` | `10.0` | Seconds between retries |
| `KOKORO_TIMEOUT` | `300.0` | Per-request timeout in seconds |
| `WEBHOOK_URL` | _(empty)_ | POST target for job complete / error notifications |
| `VOICES_FILE` | `/app/books/voices.json` | Path to the character voices config file |
| `VOICE_PRESETS_FILE` | `/app/books/voice_presets.json` | Path to named voice blend presets |
| `DB_PATH` | `/app/output/chapterforge.db` | SQLite database path |

---

## Character Voices

Define per-character voice profiles in `voices.json` (stored alongside your manuscripts). Add a `narrator` entry to override the default voice for a book.

### voices.json format

```json
{
  "narrator": {
    "voice": "af_bella",
    "speed": 0.85,
    "pitch_ratio": 1.0
  },
  "elara": {
    "voice": "af_heart",
    "speed": 0.9,
    "pitch_ratio": 1.05
  },
  "gareth": {
    "voice": "am_fenrir(0.7)+bm_fable(0.3)",
    "speed": 0.8,
    "pitch_ratio": 0.95
  }
}
```

### Tagging dialogue in your manuscript

Add `::character::` at the start of a paragraph to assign it to a character:

```markdown
# Chapter 1

The fire had burned low by the time Elara spoke.

::elara::
"We can't stay here much longer," she said, her voice barely above a whisper.

::gareth::
"Then we don't." He kicked dirt over the embers.

Silence fell between them like a third presence in the room.
```

- Untagged paragraphs use the **narrator** profile (or the job's default voice if no narrator profile exists)
- Tags are stripped from the generated audio
- Consecutive paragraphs with the same tag are merged into a single TTS request

### Voice blending

Kokoro supports mixing voices with weighted blends. Use the blend string anywhere a voice is specified:

```
af_bella(0.6)+bm_george(0.4)
af_bella(0.5)+af_sky(0.3)+bm_george(0.2)
```

Weights don't need to sum to 1.0 — Kokoro normalises them.

### Voice presets

Use **Voice Presets** in the web UI to save a blend as a reusable named voice. Presets are stored in `voice_presets.json`:

```json
{
  "warm_bard": "af_bella(0.65)+bm_george(0.35)",
  "soft_elder": "af_nicole(0.5)+bm_fable(0.5)"
}
```

Saved presets appear in the Record voice dropdown and in character voice blend selectors. Delete a preset with the `×` button in the Voice Presets section.

### Pitch shifting

`pitch_ratio` applies an ffmpeg `asetrate` filter after generation — values above `1.0` raise pitch, below `1.0` lower it. Useful range is roughly `0.85` – `1.15`. The default is `1.0` (no shift).

---

## Supported Kokoro Voices

| Voice | Style |
|---|---|
| `af_bella` | Warm American female |
| `af_heart` | Soft American female |
| `af_nicole` | Clear American female |
| `af_sarah` | Energetic American female |
| `af_sky` | Airy American female |
| `am_adam` | American male |
| `am_fenrir` | Deep American male |
| `am_michael` | Neutral American male |
| `am_echo` | American male |
| `am_liam` | American male |
| `bm_fable` | British male |
| `bm_george` | British male |
| `bf_emma` | British female |

---

## Chapter Detection

Chapters split on `#` and `##` headings matching common fiction patterns:

```
# Chapter 1
## Chapter One
# Prologue
## Epilogue
# Part 1 — The Rift
```

`###` sub-headings (e.g. scene breaks, part labels within a chapter) are **not** treated as chapter splits — they stay in the chapter body.

---

## Output Structure

```
output/
  chapterforge.db          ← SQLite job database
  chapters/
    <build_id>/
      manifest.json
      01_chapter_title.mp3
      02_chapter_title.mp3
      ...
      audiobook.m4b        ← combined M4B with chapter markers
```

Build IDs are derived from manuscript name + voice + speed + source hash. Old builds are never overwritten. Interrupted jobs resume from the last completed chapter.

---

## Logs

Rotating log files written to `/app/logs/`:

| File | Level | Max size |
|---|---|---|
| `chapterforge.log` | INFO+ | 10 MB × 5 |
| `chapterforge.errors.log` | WARNING+ | 5 MB × 5 |

---

## Roadmap

- [x] Phase 1 — MVP: upload, split, generate, play
- [x] Phase 2 — GHCR + Portainer deployment
- [x] Phase 3 — Draft library with source hash change detection
- [x] Phase 4a — M4B export with chapter markers
- [x] Phase 4b — SQLite persistence + partial resume
- [x] Phase 5 — Character voice profiles (speaker tags, voice blending, pitch ratio)
- [ ] Phase 6 — n8n automation (notifications, NAS sync, Audiobookshelf scan)
- [ ] Phase 7 — Sentence-level proof-reading playback mode

---

## What This Is Not

- A professional audiobook production tool
- A replacement for the Kokoro TTS engine — you need a running Kokoro instance
- A Gradio or React app
