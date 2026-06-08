# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- **feat: Route Instagram links through @SaveAsBot**: Instagram requests now use a live Telegram user session via Telethon, send the link to `@SaveAsBot`, download the bot response, and relay it to the original requester.
- **fix: Avoid Telethon update parsing failures in the Instagram bridge**: The SaveAsBot bridge now resolves `@SaveAsBot` from the session cache or explicit peer IDs, disables background updates, polls the direct dialog history for replies, and keeps media messages even when `@SaveAsBot` adds service captions.
- **fix: Filter SaveAsBot ads from Instagram video replies**: Reel/Stories responses now drop marketing text, bonus/ad images, and non-video media when the requested response includes video.
- **fix: Return SaveAsBot terminal errors immediately**: Explicit failure messages such as private/age-restricted Instagram publications now end the bridge wait loop immediately and are relayed back as text.
- **fix: Suppress SaveAsBot text after media**: Instagram responses that include media no longer send separate text items after the photo/video; text-only terminal errors are still relayed.
- **chore: Add SaveAsBot Docker configuration**: `docker-compose.yml` now accepts `SAVEASBOT_*` variables, mounts a read-only Telethon session file, and ignores local `.session` files.
- **docs: Update Instagram operation docs**: README files now describe the SaveAsBot bridge and remove Instagram cookie requirements.

## v1.1.0 - Music Download Feature

- **feat: Add song download via name or Spotify URL**: The bot can now download audio from YouTube. Users can send a plain text song name or a Spotify track link.
- **feat: Intelligent request routing**: A single message handler now determines the user's intent (video download, song download) based on the message content, removing the need for special commands.
- **refactor: YouTube search implementation**: Replaced the unstable `youtube-search-python` library with a more robust search-and-download mechanism using `yt-dlp`'s `ytsearch1:` feature. This resolves hanging and dependency conflict issues.
- **fix: Subprocess handling for audio downloads**: Reworked the audio download function to use a non-blocking process handler, preventing the bot from hanging during downloads.
- **chore: Docker configuration updates**:
    - `docker-compose.yml` now supports passing Spotify API credentials.
    - `Dockerfile` now includes `iputils-ping` to simplify network diagnostics.
- **docs: Update documentation**: All README files (`RU`, `EN`, `md`) have been updated to reflect the new functionality and configuration requirements.

## v1.0.0 – Initial public release

- Remove unused `llmbot.py` with hardcoded secrets.
- Add robust Instagram downloading:
  - Mobile User-Agent + referer
  - Up to 3 retries with exponential backoff
  - Improved short error messages
- Live status message with progress percentage and per-step updates.
- In-memory statistics (success/fail) shown in status and `/stats`.
- Safe parsing for `ALLOWED_USER_IDS`.
- Hide noisy `httpx` logs.
- Docker image and Docker Compose support.
- Documentation updates and `.gitignore` to keep cookies/tokens out of repo.
