# Changelog

All notable changes to ChapterForge TTS are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.6.3] - 2026-05-07
### Fixed
- `loadVoices()` silently swallowed errors thrown inside `renderVoicesTable()`, leaving the section stuck on "Loading…". Added a dedicated try/catch so errors are displayed inline and logged to the console.

---

## [0.6.2] - 2026-05-07
### Fixed
- Second JS comment in f-string still contained bare `{voice, weight}` — escaped to `{{voice, weight}}` to prevent `NameError` on page render.

---

## [0.6.1] - 2026-05-07
### Fixed
- JS comment `[{voice, weight}]` in the Python f-string caused a `NameError: name 'voice' is not defined` 500 on page load. Escaped to `[{{voice, weight}}]`.

---

## [0.6.0] - 2026-05-07
### Added
- Voice dropdowns in the Character Voices table replace the free-text voice input to prevent typos sending invalid voice names to Kokoro.
- **Blend builder UI**: each character row now shows one or more dropdown + weight pairs. Click `+` to add a second voice to blend, `✕` to remove. The `af_bella(0.6)+bm_george(0.4)` blend string is assembled automatically on Save/Test/Add.
- Existing blend strings in `voices.json` are parsed back into individual slots when the table renders.

---

## [0.5.2] - 2026-05-07
### Added
- **▶ Test** button on the add-row in the Character Voices table — preview a voice before adding the character, using whatever voice/speed is currently set in those fields.
- Character Voices table is now always visible with an inline add-row (name, voice, speed, pitch, Test, + Add). No longer requires adding a character before seeing the fields.

---

## [0.5.1] - 2026-05-07 (prior session)
### Fixed
- JS quote escaping bug: `onclick="testCharVoice('name')"` broke the entire script block. Replaced with `data-name` attributes and `this.dataset.name`.

---

## [0.5.0] - 2026-05-07 (prior session)
### Added
- **Phase 5: Character voice profiles**
  - `voices.json` file for per-character voice, speed, and pitch ratio
  - `::character::` speaker tags at the start of paragraphs
  - Per-segment TTS dispatch using character profile
  - Voice blending support (`af_bella(0.6)+bm_george(0.4)` syntax)
  - Per-character pitch shifting via ffmpeg `asetrate` filter
  - `GET /api/voices` and `POST /api/voices` routes
  - Character Voices UI section with table editor, Test, Remove, Save
  - `VOICES_FILE` env var (default `/app/books/voices.json`)

---

## [0.4.1] - prior session
### Added
- **Phase 4b: SQLite persistence + partial resume**
  - Job state persisted to `/app/output/chapterforge.db` (survives container restarts)
  - Interrupted jobs resume from the last completed chapter on restart
  - Jobs running or queued at startup are marked `interrupted`
  - `DB_PATH` env var

### Fixed
- `###` sub-headings (e.g. scene breaks) were splitting chapters incorrectly. Changed `CHAPTER_HEADING_RE` from `#{1,3}` to `#{1,2}` — only `#` and `##` headings trigger chapter splits.
- Play button displayed `&#9654;` as literal text. Replaced HTML entity with the literal `▶` character.

---

## [0.4.0] - prior session
### Added
- **Phase 4a: M4B export**
  - Chapters concatenated into a single `.m4b` audiobook with ffmetadata chapter markers
  - Download M4B button per build

---

## [0.3.0] - prior session
### Added
- **Phase 3: Draft library**
  - Source hash change detection — new builds only created when manuscript changes
  - Build manifests with source hash, voice, speed, date

---

## [0.2.0] - prior session
### Added
- **Phase 2: GHCR + Portainer deployment**
  - GitHub Actions workflow publishing to `ghcr.io/slackerchris/chapterforge-tts`
  - `docker-compose.prod.yml` for Portainer stack
  - `VERSION` file drives `APP_VERSION` build arg

---

## [0.1.0] - prior session
### Added
- **Phase 1: MVP**
  - Upload `.md` / `.txt` manuscripts
  - Auto-split on `#` / `##` headings
  - Generate MP3 per chapter via Kokoro TTS (OpenAI-compatible API)
  - Start / Stop jobs
  - Live progress polling
  - In-browser chapter audio playback
  - Rotating log files
  - Webhook notification on job complete/error
