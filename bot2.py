import glob
import logging
import re
import select
import os
import subprocess
import uuid
import time
import random
import requests
import telegram
import instaloader
import http.cookiejar
from datetime import date
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters, CommandHandler
from telegram.request import HTTPXRequest

# New imports for music feature
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# Config from environment
TOKEN = os.getenv('BOT_TOKEN')
TEMP_FOLDER = './temp'

# Safe ALLOWED_USER_IDS parser (comma-separated; ignores blanks/invalid)
def parse_allowed_users(env_value: str) -> set[int]:
    users = set()
    if not env_value:
        return users
    for part in env_value.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            users.add(int(part))
        except ValueError:
            logging.warning(f"Skipping invalid user id: {part}")
    return users

ALLOWED_USERS = parse_allowed_users(os.getenv('ALLOWED_USER_IDS', ''))

# In-memory stats for process lifetime (reset on container restart)
TOTAL_SUCCESS = 0
TOTAL_FAIL = 0

logging.basicConfig(level=logging.INFO)
# Hide noisy Telegram HTTP logs (httpx)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
httpx_logger.disabled = True
class _NoHttpxFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        return not record.name.startswith('httpx')
logging.getLogger().addFilter(_NoHttpxFilter())

# Ensure temp folder exists
os.makedirs(TEMP_FOLDER, exist_ok=True)

def increment_success() -> None:
    global TOTAL_SUCCESS
    TOTAL_SUCCESS += 1

def increment_fail() -> None:
    global TOTAL_FAIL
    TOTAL_FAIL += 1

def get_stats_text(verbose: bool = False) -> str:
    lines = [f"Stats: {TOTAL_SUCCESS} ✅ / {TOTAL_FAIL} ❌"]
    
    cookie_lines = []
    for path, name in [('./cookie_instagram.txt', 'Insta'), ('./cookie_youtube.txt', 'YouTube'), ('./cookie_threads.txt', 'Threads')]:
        stat = check_cookie_status(path, name, verbose)
        if stat:
            cookie_lines.append(stat)
            
    if cookie_lines:
        lines.append("Cookies:")
        lines.extend(cookie_lines)
        
    return '\n'.join(lines)

def build_status(stage: str, attempt: int | None = None, max_attempts: int | None = None, progress: str | None = None) -> str:
    parts = [stage]
    if attempt and max_attempts:
        parts.append(f"(try {attempt}/{max_attempts})")
    if progress:
        parts.append(progress)
    parts.append(f"— {get_stats_text(verbose=False)}")
    return ' '.join(parts)

# Helper to check cookie expiration
def check_cookie_status(file_path: str, service_name: str, verbose: bool = False) -> str | None:
    if not os.path.exists(file_path):
        return f"{service_name}: ❌ No file" if verbose else None
    
    try:
        cj = http.cookiejar.MozillaCookieJar(file_path)
        cj.load()
        
        # Look for critical cookies first
        critical_names = ['sessionid'] if 'instagram' in service_name.lower() or 'threads' in service_name.lower() else ['SID', '__Secure-3PSID']
        
        min_expiry = None
        
        for cookie in cj:
            if not cookie.expires: continue
            # If it's a critical cookie or we haven't found any expiry yet
            if cookie.name in critical_names or min_expiry is None:
                if min_expiry is None or cookie.expires < min_expiry:
                    min_expiry = cookie.expires
        
        if min_expiry:
            days = (min_expiry - time.time()) / 86400
            if days < 0:
                 return f"{service_name}: 🔴 EXPIRED ({abs(days):.1f} days ago)"
            elif days < 10:
                 return f"{service_name}: 🟠 {days:.1f} days left"
            
            # Healthy
            return f"{service_name}: 🟢 {days:.1f} days left" if verbose else None
        
        return f"{service_name}: 🟢 (Session only)" if verbose else None

    except Exception as e:
        logging.error(f"Cookie check error for {service_name}: {e}")
        return f"{service_name}: ⚠️ Read error"

# Select cookie file by URL
def get_cookie_file(url: str) -> str:
    if 'instagram.com' in url:
        return './cookie_instagram.txt'
    elif 'threads.net' in url or 'threads.com' in url:
        return './cookie_threads.txt'
    elif 'youtube.com' in url or 'youtu.be' in url:
        return './cookie_youtube.txt'
    return ''

async def process_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    shortcode_match = re.search(r'(?:instagram\.com|instagr\.am)/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
    if not shortcode_match:
        await download_and_send_video(update, context)
        return

    shortcode = shortcode_match.group(1)
    msg = await update.message.reply_text(build_status('⏳ Fetching Instagram metadata...'))
    
    try:
        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False, 
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
        )
        
        cookie_file = get_cookie_file(url)
        if os.path.exists(cookie_file):
            cj = http.cookiejar.MozillaCookieJar(cookie_file)
            cj.load()
            L.context._session.cookies = cj

        post = instaloader.Post.from_shortcode(L.context, shortcode)

        if post.typename == 'GraphVideo':
            await msg.delete()
            await download_and_send_video(update, context)
            return

        elif post.typename == 'GraphImage':
            await msg.edit_text(build_status('⏳ Downloading image...'))
            r = L.context._session.get(post.url)
            filename = f"{TEMP_FOLDER}/{shortcode}.jpg"
            with open(filename, 'wb') as f:
                f.write(r.content)
            
            await update.message.reply_photo(open(filename, 'rb'))
            os.remove(filename)
            await msg.edit_text(build_status('✅ Done.'))
            increment_success()

        elif post.typename == 'GraphSidecar':
            await msg.edit_text(build_status('⏳ Downloading carousel...'))
            media_group = []
            files_to_cleanup = []
            
            nodes = list(post.get_sidecar_nodes())
            
            for i, node in enumerate(nodes):
                if node.is_video:
                    video_url = node.video_url
                    fname = f"{TEMP_FOLDER}/{shortcode}_{i}.mp4"
                    r = L.context._session.get(video_url)
                    with open(fname, 'wb') as f:
                        f.write(r.content)
                    media_group.append(InputMediaVideo(open(fname, 'rb')))
                    files_to_cleanup.append(fname)
                else:
                    image_url = node.display_url
                    fname = f"{TEMP_FOLDER}/{shortcode}_{i}.jpg"
                    r = L.context._session.get(image_url)
                    with open(fname, 'wb') as f:
                        f.write(r.content)
                    media_group.append(InputMediaPhoto(open(fname, 'rb')))
                    files_to_cleanup.append(fname)
            
            chunk_size = 10
            for i in range(0, len(media_group), chunk_size):
                chunk = media_group[i:i + chunk_size]
                await update.message.reply_media_group(chunk)
            
            for f in files_to_cleanup:
                if os.path.exists(f): os.remove(f)
                
            await msg.edit_text(build_status('✅ Done.'))
            increment_success()
        
        else:
             await msg.delete()
             await download_and_send_video(update, context)

    except Exception as e:
        logging.error(f"Instaloader error: {e}")
        await msg.edit_text(build_status(f'⚠️ Instaloader failed, trying fallback...'))
        await download_and_send_video(update, context)

async def process_threads_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Normalize URL: force threads.net
    url = update.message.text.replace('threads.com', 'threads.net')
    
    msg = await update.message.reply_text(build_status('⏳ Checking Threads link...'))
    
    try:
        # 1. Try to detect video using yt-dlp (most robust method)
        # We use --dump-json to check if it's a supported video without downloading content yet
        is_video = False
        cookie_file = './cookie_threads.txt'
        
        # Build probe command
        cmd = ['yt-dlp', '--dump-json', '--no-warnings', '--no-playlist']
        if os.path.exists(cookie_file):
            cmd.extend(['--cookies', cookie_file])
        cmd.append(url)
        
        try:
            # Timeout set to 15s to avoid hanging if yt-dlp gets stuck
            probe = subprocess.run(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                text=True, 
                timeout=15
            )
            # If yt-dlp exits with 0 and produces output, it's a video (or supported media)
            if probe.returncode == 0 and probe.stdout.strip():
                is_video = True
        except Exception as e:
            logging.warning(f"yt-dlp probe failed: {e}")

        if is_video:
            await msg.delete()
            # Pass the normalized URL
            await download_and_send_video(update, context, override_url=url)
            return

        # 2. If no video detected via yt-dlp, manually check for video in HTML (JSON blob)
        # Threads often hides video info in a big JSON blob
        await msg.edit_text(build_status('⏳ Checking for content...'))
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        # Load cookies for requests
        cookies = {}
        if os.path.exists(cookie_file):
            try:
                cj = http.cookiejar.MozillaCookieJar(cookie_file)
                cj.load()
                for c in cj:
                    cookies[c.name] = c.value
            except Exception:
                pass

        r = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        html = r.text
        
        # Look for direct MP4 links in the page (usually in JSON)
        # Pattern matches "url":"https://...mp4..." with escaped slashes
        mp4_matches = re.findall(r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"', html)
        
        if mp4_matches:
            # Pick the longest URL as it's often the highest quality or the main video
            # Unescape slashes
            video_url = max(mp4_matches, key=len).replace(r'\/', '/')
            # Ensure it starts with http
            if not video_url.startswith('http'):
                video_url = None
            
            if video_url:
                await msg.delete()
                # Pass the DIRECT video URL to download_and_send_video
                # This bypasses yt-dlp extraction logic for the main page
                await download_and_send_video(update, context, override_url=video_url)
                return
            
        # Check for og:image
        og_image = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        if og_image:
            image_url = og_image.group(1).replace('&amp;', '&')
            await msg.edit_text(build_status('⏳ Downloading image...'))
            
            unique_id = str(uuid.uuid4())
            fname = f"{TEMP_FOLDER}/{unique_id}.jpg"
            
            img_r = requests.get(image_url, headers=headers, timeout=30)
            if img_r.status_code == 200:
                with open(fname, 'wb') as f:
                    f.write(img_r.content)
                
                await update.message.reply_photo(open(fname, 'rb'))
                os.remove(fname)
                await msg.edit_text(build_status('✅ Done.'))
                increment_success()
                return

        # If neither, try fallback to video just in case (using normalized URL)
        await msg.delete()
        await download_and_send_video(update, context, override_url=url)

    except Exception as e:
        logging.error(f"Threads error: {e}")
        # Last resort
        await msg.edit_text(build_status(f'⚠️ Error, trying fallback...'))
        await download_and_send_video(update, context, override_url=url)

# Universal message router
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text('⛔️ You are not allowed to use this bot.')
        logging.warning(f"Access denied for user_id: {user_id}")
        return

    text = update.message.text
    if not text:
        return
    
    # Route to specific downloaders
    if 'instagram.com' in text:
        await process_instagram_link(update, context)
    elif 'threads.net' in text or 'threads.com' in text:
        await process_threads_link(update, context)
    elif 'youtube.com' in text or 'youtu.be' in text:
        await download_and_send_video(update, context)
    # All other text is treated as a song request (name or spotify link)
    else:
        await handle_song_request(update, context)

# Modified function to handle song requests
async def handle_song_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        # This check is redundant if route_message does it, but good for safety
        await update.message.reply_text('⛔️ You are not allowed to use this bot.')
        logging.warning(f"Access denied for user_id: {user_id}")
        return

    query = update.message.text
    msg = await update.message.reply_text(build_status(f'⏳ Searching for "{query}"...'))

    song_title = None
    artist = None

    # Check if it's a spotify URL
    if 'open.spotify.com/track' in query:
        try:
            if not os.getenv('SPOTIPY_CLIENT_ID') or not os.getenv('SPOTIPY_CLIENT_SECRET'):
                await msg.edit_text('❌ Spotify API credentials are not set. This feature is disabled.')
                logging.warning("SPOTIPY_CLIENT_ID or SPOTIPY_CLIENT_SECRET not set.")
                return

            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials())
            track = sp.track(query)
            song_title = track['name']
            artist = track['artists'][0]['name']
            await msg.edit_text(build_status(f'⏳ Found on Spotify: "{song_title}" by {artist}. Searching on YouTube...'))
        except Exception:
            logging.exception("Spotify error")
            await msg.edit_text('❌ Could not process the Spotify link. Make sure it is a valid track link and credentials are correct.')
            increment_fail()
            return
    else:
        song_title = query

    # Search on YouTube using yt-dlp - pass search query directly to download_audio
    search_query = f"{artist} {song_title}" if artist else song_title
    try:
        await msg.edit_text(build_status(f'⏳ Starting download for "{search_query}"...'))
        # Pass the ytsearch query directly to download_audio
        await download_audio(update, context, f'ytsearch1:{search_query}', song_title, artist, msg)

    except Exception:
        logging.exception("YouTube search error")
        await msg.edit_text('❌ An error occurred while searching on YouTube.')
        increment_fail()

# Function to download audio
async def download_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, song_title: str, artist: str | None, msg):
    unique_id = str(uuid.uuid4())
    temp_file_pattern = f'{TEMP_FOLDER}/{unique_id}.%(ext)s'
    
    command = [
        'yt-dlp', '-x', '--audio-format', 'mp3', '--audio-quality', '0',
        '--no-playlist', '--newline',
        '--metadata-from-title', "%(artist)s - %(title)s",
        '--embed-thumbnail', '-o', temp_file_pattern, url
    ]

    last_err_text = ''
    try:
        logging.info(f"Starting yt-dlp for audio: {' '.join(command)}")
        await msg.edit_text(build_status('⏳ Downloading...'))

        start_ts = time.monotonic()
        err_lines: list[str] = []
        
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        try:
            while True:
                # Timeout for the whole download process
                if time.monotonic() - start_ts > 300:
                    proc.kill()
                    raise subprocess.TimeoutExpired(command, timeout=300)

                # Read stderr if available
                if proc.stderr is None:
                    break
                
                rlist, _, _ = select.select([proc.stderr], [], [], 0.5)
                if rlist:
                    line = proc.stderr.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    logging.info(f"yt-dlp: {line.strip()}")
                    err_lines.append(line)
                else:
                    if proc.poll() is not None:
                        break
            
            rc = proc.wait()
            if rc != 0:
                last_err_text = ''.join(err_lines)
                raise subprocess.CalledProcessError(rc, command, output='', stderr=last_err_text)

        finally:
            try:
                if proc.stderr and not proc.stderr.closed:
                    proc.stderr.close()
                if proc.stdout and not proc.stdout.closed:
                    proc.stdout.close()
            except Exception:
                pass

        downloaded_files = glob.glob(f'{TEMP_FOLDER}/{unique_id}.*')
        if not downloaded_files:
            await msg.edit_text('❌ Audio was not downloaded.')
            increment_fail()
            return
            
        temp_file = downloaded_files[0]
        await msg.edit_text(build_status('📤 Uploading audio...'))
        
        final_title = song_title if song_title else "Audio"
        final_artist = artist if artist else None

        with open(temp_file, 'rb') as audio:
            await update.message.reply_audio(audio, title=final_title, performer=final_artist, write_timeout=300, read_timeout=300, connect_timeout=60)

        increment_success()
        await msg.edit_text(build_status('✅ Done.'))

    except subprocess.TimeoutExpired:
        logging.error("Audio download timeout")
        increment_fail()
        await msg.edit_text(build_status('❌ Download timeout.'))
    except subprocess.CalledProcessError as e:
        err_text = (last_err_text or getattr(e, 'stderr', '') or '').strip()
        logging.error(f"Audio download error: {err_text}")
        increment_fail()
        await msg.edit_text(build_status('❌ Failed to download audio.'))
    except Exception as e:
        logging.exception("Unexpected error in audio download")
        increment_fail()
        await msg.edit_text(build_status('❌ Unexpected error.'))
    finally:
        files_to_remove = glob.glob(f'{TEMP_FOLDER}/{unique_id}.*')
        for f in files_to_remove:
            if os.path.exists(f):
                os.remove(f)

# Transcode video to fit Telegram limit
def compress_video(input_path: str, output_path: str) -> bool:
    try:
        command = [
            'ffmpeg', '-i', input_path, '-vf', 'scale=w=640:h=-2', '-c:v', 'libx264',
            '-preset', 'fast', '-crf', '28', '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart', output_path
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return os.path.exists(output_path) and os.path.getsize(output_path) <= 50 * 1024 * 1024
    except Exception as e:
        logging.error(f"Video transcode error: {e}")
        return False

# Download and send video
async def download_and_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE, override_url: str | None = None):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text('⛔️ You are not allowed to use this bot.')
        logging.warning(f"Access denied for user_id: {user_id}")
        return

    url = override_url if override_url else update.message.text
    msg = await update.message.reply_text(build_status('⏳ Downloading video...'))

    unique_id = str(uuid.uuid4())
    temp_file = f'{TEMP_FOLDER}/{unique_id}.mp4'
    compressed_file = f'{TEMP_FOLDER}/{unique_id}_compressed.mp4'
    cookie_file = get_cookie_file(url)

    def build_command() -> list[str]:
        cmd = ['yt-dlp']
        if cookie_file: cmd.extend(['--cookies', cookie_file])
        cmd.extend([
            '-f', 'bestvideo+bestaudio/best', '--merge-output-format', 'mp4',
            '--no-playlist', '--newline', '-o', temp_file
        ])
        if 'instagram.com' in url:
            mobile_ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
            cmd.extend(['--user-agent', mobile_ua, '--referer', 'https://www.instagram.com/'])
        cmd.append(url)
        return cmd

    max_attempts = 3
    last_err_text = ''

    try:
        for attempt in range(1, max_attempts + 1):
            command = build_command()
            logging.info(f"Starting yt-dlp (attempt {attempt}): {' '.join(command)}")
            try:
                await msg.edit_text(build_status('⏳ Downloading...', attempt, max_attempts))
            except Exception: pass
            
            start_ts = time.monotonic()
            err_lines: list[str] = []
            percent_last: str | None = None
            last_update = 0.0
            proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )
            progress_re = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
            try:
                while True:
                    if time.monotonic() - start_ts > 300:
                        proc.kill()
                        raise subprocess.TimeoutExpired(command, timeout=300)
                    if proc.stderr is None: break
                    
                    rlist, _, _ = select.select([proc.stderr], [], [], 0.5)
                    if rlist:
                        line = proc.stderr.readline()
                        if not line and proc.poll() is not None: break
                        err_lines.append(line)
                        m = progress_re.search(line)
                        if m:
                            percent = m.group(1)
                            now = time.monotonic()
                            if percent != percent_last and (now - last_update) >= 1.0:
                                try:
                                    await msg.edit_text(build_status('⏳ Downloading...', attempt, max_attempts, f'[{percent}%]'))
                                except Exception: pass
                                percent_last = percent
                                last_update = now
                    elif proc.poll() is not None:
                        break
                
                rc = proc.wait()
                if rc != 0:
                    last_err_text = ''.join(err_lines)
                    raise subprocess.CalledProcessError(rc, command, output='', stderr=last_err_text)
                
                try: await msg.edit_text(build_status('✅ Downloaded. Processing file...'))
                except Exception: pass
                break
            finally:
                try:
                    if proc.stderr and not proc.stderr.closed: proc.stderr.close()
                except Exception: pass

        if not os.path.exists(temp_file):
            fallback_matches = [p for p in glob.glob(f'{TEMP_FOLDER}/{unique_id}.*') if not p.endswith('.part')]
            if fallback_matches:
                temp_file = max(fallback_matches, key=os.path.getsize)
            else:
                await msg.edit_text('❌ Video was not downloaded.')
                return

        if os.path.getsize(temp_file) > 50 * 1024 * 1024:
            try: await msg.edit_text(build_status('⚙️ Large video, transcoding...'))
            except Exception: pass
            if not compress_video(temp_file, compressed_file):
                await msg.edit_text(build_status('❌ Failed to transcode video.'))
                return
            os.remove(temp_file)
            temp_file = compressed_file

        logging.info(f"Video ready to send: {temp_file}")
        try: await msg.edit_text(build_status('📤 Uploading video...'))
        except Exception: pass

        with open(temp_file, 'rb') as video:
            await update.message.reply_video(video, write_timeout=300, read_timeout=300, connect_timeout=60)

        increment_success()
        try: await msg.edit_text(build_status('✅ Done.'))
        except Exception: pass

    except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
        logging.error(f"Telegram API error: {e}")
        increment_fail()
        await msg.edit_text(build_status('❌ Failed to upload due to a network error.'))
    except subprocess.TimeoutExpired:
        logging.error("Download timeout")
        increment_fail()
        await msg.edit_text(build_status('❌ Download timeout.'))
    except subprocess.CalledProcessError as e:
        err_text = (last_err_text or getattr(e, 'stderr', '') or '').strip()
        logging.error(f"Video download error: {err_text}")
        increment_fail()
        low = err_text.lower()
        if 'login required' in low or 'rate-limit' in low or 'locked' in low:
            await msg.edit_text(build_status('❌ Login required or rate limit.'))
        else:
            await msg.edit_text(build_status('❌ Failed to download.'))
    except Exception as e:
        logging.exception("Unexpected error")
        increment_fail()
        await msg.edit_text(build_status('❌ Unexpected error.'))
    finally:
        for f in [temp_file, compressed_file]:
            if os.path.exists(f): os.remove(f)

# /start — greeting and keyboard
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS: return
    keyboard = ReplyKeyboardMarkup([['Stats']], resize_keyboard=True)
    await update.message.reply_text(
        'Send a link (Instagram, YouTube) to download video, or send a song title/Spotify link to get audio.\n'
        'Tap “Stats” for stats.',
        reply_markup=keyboard
    )

# /stats and “Stats” button
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS: return
    await update.message.reply_text(get_stats_text(verbose=True))


# Generic error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception while handling an update:", exc_info=context.error)

# Main entry point
if __name__ == '__main__':
    if not TOKEN or not ALLOWED_USERS:
        raise ValueError("BOT_TOKEN and ALLOWED_USER_IDS are required")

    t_request = HTTPXRequest(http_version="1.1", connection_pool_size=10, read_timeout=300.0, write_timeout=300.0, connect_timeout=300.0, pool_timeout=300.0)
    app = ApplicationBuilder().token(TOKEN).request(t_request).build()
    
    # Register the error handler
    app.add_error_handler(error_handler)

    # Commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats_command))
    # Stats button (case-insensitive)
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^stats$', re.IGNORECASE)), stats_command))
    # Main message handler/router
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), route_message))

    logging.info('Bot started...')
    app.run_polling()
