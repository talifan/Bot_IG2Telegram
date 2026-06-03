A bot to fetch Instagram media through `@SaveAsBot`, download YouTube/Threads videos, and download audio by song name or Spotify link.

- **Video**: Accepts links to posts, Reels, and Shorts (Instagram through `@SaveAsBot`, YouTube and Threads through local downloaders).
- **Audio**: Accepts a text-based song name (e.g., `Daft Punk - Around the World`) or a Spotify track link.

Russian version: see README_RU.md

### Requirements
- Python packages: `python-telegram-bot`, `yt-dlp`, `spotipy`, `telethon` (see `requirements.txt`)
- System dependency: `ffmpeg`
  - On Ubuntu/Debian: `apt install ffmpeg`

### Cookies
- For YouTube: `cookie_youtube.txt`
- For Threads: `cookie_threads.txt`
- Instagram no longer uses a cookie file; links are sent to `@SaveAsBot` through an authorized Telethon session.

How to get cookies:
1. Log into your account in a browser.
2. Install the “Get cookies.txt LOCALLY” browser extension.
3. Save cookies into local files (`cookie_youtube.txt`, `cookie_threads.txt`).
4. Do not commit these files to the repository—they are already in `.gitignore`.

### Running the Bot
#### 1) Environment Variables
Create a `.env` file in the project root or export the variables:

- `BOT_TOKEN` — Your Telegram bot token from BotFather.
- `ALLOWED_USER_IDS` — Comma-separated list of user IDs allowed to use the bot.
- `SPOTIPY_CLIENT_ID` — (Optional) Your Spotify app Client ID.
- `SPOTIPY_CLIENT_SECRET` — (Optional) Your Spotify app Client Secret.
- `SAVEASBOT_API_ID` / `SAVEASBOT_API_HASH` — Telegram API credentials for the user Telethon session.
- `SAVEASBOT_SESSION_HOST_PATH` — Host path to the `.session` file mounted into Docker.
- `SAVEASBOT_SESSION_PATH` — Session path inside the container; defaults to `/app/saveasbot.session`.
- `SAVEASBOT_USERNAME` — Commercial bot username; defaults to `SaveAsBot`.
- `SAVEASBOT_SEND_START=1` — Optionally send `/start` to `@SaveAsBot` on the first Instagram request in a process.
- `SAVEASBOT_USER_ID` / `SAVEASBOT_ACCESS_HASH` — Optional explicit `@SaveAsBot` peer if it is missing from the `.session` `entities` cache.
- `SAVEASBOT_POLL_INTERVAL_SEC` — Dialog-history polling interval for `@SaveAsBot`; defaults to `1`.

> **Where to get Spotify keys?**
> 1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
> 2. Create a new app.
> 3. Copy the `Client ID` and `Client Secret` from your app's settings.
> 4. Add them to your `.env` file.
> *Without these keys, only searching by song name will work; Spotify links will not.*

#### 2) Docker (Recommended)
The easiest way to run the bot is with `docker-compose`, which automatically reads the `.env` file:
```bash
docker-compose up -d --build
```
The included `docker-compose.yml` is pre-configured to use these variables. For the Instagram bridge, set `SAVEASBOT_SESSION_HOST_PATH` to an authorized session file, for example one from `tg_crawler`.

### Usage
- **To download video**: Send the bot an Instagram, YouTube, or Threads link.
- Instagram links are handled through a live Telegram user session and `@SaveAsBot`; the bot downloads the `@SaveAsBot` response from that session and sends it to the original requester.
- For Instagram Reels/Stories, when the response includes video, the bot drops `@SaveAsBot` marketing messages and separate non-video attachments.
- **To download audio**: Send the bot a song name (e.g., `Queen - Bohemian Rhapsody`) or a Spotify track link.
- The bot intelligently determines the request type from the message content.
- If a video file exceeds 50 MB, it will be automatically transcoded.
- The `/stats` command shows download statistics, Instagram bridge status, and the expiration status of YouTube/Threads cookie files.
- The bot updates a single message to show the current status (searching, downloading, uploading).

### Security
- Do NOT commit your `.env` file, cookies, or tokens to the repository. The `.gitignore` file is configured to ignore them.
- Cookies are optional for public videos but are often required for private or restricted content.
