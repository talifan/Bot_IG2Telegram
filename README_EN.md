A bot to download videos from Instagram/YouTube and audio by song name or Spotify link.

- **Video**: Accepts links to posts, Reels, and Shorts.
- **Audio**: Accepts a text-based song name (e.g., `Daft Punk - Around the World`) or a Spotify track link.

Russian version: see README_RU.md

### Requirements
- Python packages: `python-telegram-bot`, `yt-dlp`, `spotipy` (see `requirements.txt`)
- System dependency: `ffmpeg`
  - On Ubuntu/Debian: `apt install ffmpeg`

### Cookies
- For Instagram: `cookie_instagram.txt`
- For YouTube: `cookie_youtube.txt`

How to get cookies:
1. Log into your account in a browser.
2. Install the “Get cookies.txt LOCALLY” browser extension.
3. Save cookies into local files (`cookie_instagram.txt`, `cookie_youtube.txt`).
4. Do not commit these files to the repository—they are already in `.gitignore`.

### Running the Bot
#### 1) Environment Variables
Create a `.env` file in the project root or export the variables:

- `BOT_TOKEN` — Your Telegram bot token from BotFather.
- `ALLOWED_USER_IDS` — Comma-separated list of user IDs allowed to use the bot.
- `SPOTIPY_CLIENT_ID` — (Optional) Your Spotify app Client ID.
- `SPOTIPY_CLIENT_SECRET` — (Optional) Your Spotify app Client Secret.

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
The included `docker-compose.yml` is pre-configured to use these variables.

### Usage
- **To download video**: Send the bot a link to an Instagram or YouTube video.
- **To download audio**: Send the bot a song name (e.g., `Queen - Bohemian Rhapsody`) or a Spotify track link.
- The bot intelligently determines the request type from the message content.
- If a video file exceeds 50 MB, it will be automatically transcoded.
- The `/stats` command shows download statistics and the expiration status of your cookie files (Active, Warning, Expired).
- The bot updates a single message to show the current status (searching, downloading, uploading).

### Security
- Do NOT commit your `.env` file, cookies, or tokens to the repository. The `.gitignore` file is configured to ignore them.
- Cookies are optional for public videos but are often required for private or restricted content.