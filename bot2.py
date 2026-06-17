import asyncio
import contextlib
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
import shutil
import telegram
import instaloader
import http.cookiejar
import json
import tempfile
import sqlite3
from datetime import date
from pathlib import Path
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, InputMediaVideo, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters, CommandHandler
from telegram.request import HTTPXRequest

# New imports for music feature
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.tl.types import InputPeerUser
except ImportError:  # pragma: no cover - runtime dependency is optional until Instagram bridge is used
    TelegramClient = None  # type: ignore[assignment]
    RPCError = Exception  # type: ignore[assignment]
    InputPeerUser = None  # type: ignore[assignment]

# Config from environment
TOKEN = os.getenv('BOT_TOKEN')
TEMP_FOLDER = './temp'

def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logging.warning(f"Invalid integer value for {name}: {value!r}; using {default}")
        return default

def get_env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logging.warning(f"Invalid float value for {name}: {value!r}; using {default}")
        return default

def get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}

SAVEASBOT_USERNAME = os.getenv('SAVEASBOT_USERNAME', 'SaveAsBot')
SAVEASBOT_API_ID = os.getenv('SAVEASBOT_API_ID') or os.getenv('API_ID')
SAVEASBOT_API_HASH = os.getenv('SAVEASBOT_API_HASH') or os.getenv('API_HASH')
SAVEASBOT_SESSION_PATH = os.getenv('SAVEASBOT_SESSION_PATH', '')
SAVEASBOT_TIMEOUT_SEC = get_env_int('SAVEASBOT_TIMEOUT_SEC', 120)
SAVEASBOT_TEXT_FOLLOWUP_SEC = get_env_float('SAVEASBOT_TEXT_FOLLOWUP_SEC', 30.0)
SAVEASBOT_RESPONSE_IDLE_SEC = get_env_float('SAVEASBOT_RESPONSE_IDLE_SEC', 5.0)
SAVEASBOT_MAX_RESPONSES = get_env_int('SAVEASBOT_MAX_RESPONSES', 10)
SAVEASBOT_SEND_START = get_env_bool('SAVEASBOT_SEND_START', False)
SAVEASBOT_POLL_INTERVAL_SEC = get_env_float('SAVEASBOT_POLL_INTERVAL_SEC', 1.0)
SAVEASBOT_USER_ID = os.getenv('SAVEASBOT_USER_ID')
SAVEASBOT_ACCESS_HASH = os.getenv('SAVEASBOT_ACCESS_HASH')

_SAVEASBOT_CLIENT = None
_SAVEASBOT_RUNTIME_SESSION: Path | None = None
_SAVEASBOT_LOCK: asyncio.Lock | None = None
_SAVEASBOT_STARTED = False

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

# Robustly load cookies into a MozillaCookieJar
def load_cookies_to_jar(file_path: str) -> http.cookiejar.MozillaCookieJar:
    cj = http.cookiejar.MozillaCookieJar(file_path)
    if not os.path.exists(file_path):
        return cj

    # 1. Try standard load
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
        if len(cj) > 0:
            return cj
    except Exception:
        pass

    # 2. Try detection and conversion
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read().strip()
            
        if not content:
            return cj

        # Detect JSON
        if content.startswith('[') and content.endswith(']'):
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for c in data:
                        name = c.get('name')
                        value = c.get('value')
                        domain = c.get('domain')
                        if not (name and value and domain): continue
                        
                        path = c.get('path', '/')
                        # Handle different formats for expiration date
                        expires = c.get('expirationDate') or c.get('expiry') or c.get('expires')
                        # Convert string expiry to int if needed (some extensions do this)
                        if isinstance(expires, str) and expires.isdigit():
                            expires = int(expires)
                        
                        ck = http.cookiejar.Cookie(
                            version=0, name=name, value=value,
                            port=None, port_specified=False,
                            domain=domain, domain_specified=True,
                            domain_initial_dot=domain.startswith('.'),
                            path=path, path_specified=True,
                            secure=c.get('secure', False),
                            expires=expires,
                            discard=False, comment=None, comment_url=None, 
                            rest={'HttpOnly': c.get('httpOnly', False)}, rfc2109=False
                        )
                        cj.set_cookie(ck)
                    if len(cj) > 0:
                        return cj
            except Exception:
                pass
        
        # Detect Netscape missing header
        lines = content.splitlines()
        has_tab_cols = False
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'): continue
            if len(line.split('\t')) >= 7:
                has_tab_cols = True
                break
        
        if has_tab_cols:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                tmp.write("# Netscape HTTP Cookie File\n" + content)
                tmp_name = tmp.name
            try:
                cj_tmp = http.cookiejar.MozillaCookieJar(tmp_name)
                cj_tmp.load(ignore_discard=True, ignore_expires=True)
                os.unlink(tmp_name)
                return cj_tmp
            except Exception:
                if os.path.exists(tmp_name): os.unlink(tmp_name)

    except Exception as e:
        logging.error(f"Error loading cookies from {file_path}: {e}")

    return cj

# Get a cookie file that is guaranteed to be in Netscape format for yt-dlp
def get_safe_cookie_file(url: str) -> str | None:
    original_path = get_cookie_file(url)
    if not original_path or not os.path.exists(original_path):
        return None
        
    try:
        with open(original_path, 'r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline()
            if first_line.startswith('# Netscape'):
                return original_path
    except Exception:
        pass
        
    # Need conversion
    try:
        cj = load_cookies_to_jar(original_path)
        if len(cj) > 0:
            temp_path = os.path.join(TEMP_FOLDER, os.path.basename(original_path) + ".converted.txt")
            cj.save(temp_path, ignore_discard=True, ignore_expires=True)
            return temp_path
    except Exception as e:
        logging.error(f"Failed to prepare safe cookie file: {e}")
        
    return original_path

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
    saveas_status = check_saveasbot_status(verbose)
    if saveas_status:
        cookie_lines.append(saveas_status)
    for path, name in [('./cookie_youtube.txt', 'YouTube'), ('./cookie_threads.txt', 'Threads')]:
        stat = check_cookie_status(path, name, verbose)
        if stat:
            cookie_lines.append(stat)
            
    if cookie_lines:
        lines.append("Cookies:")
        lines.extend(cookie_lines)
        
    return '\n'.join(lines)

def check_saveasbot_status(verbose: bool = False) -> str | None:
    if not verbose:
        return None
    missing = []
    if not (SAVEASBOT_API_ID or os.getenv('API_ID')):
        missing.append('API_ID')
    if not (SAVEASBOT_API_HASH or os.getenv('API_HASH')):
        missing.append('API_HASH')
    if not SAVEASBOT_SESSION_PATH:
        missing.append('SAVEASBOT_SESSION_PATH')
    elif not Path(SAVEASBOT_SESSION_PATH).is_file():
        missing.append('session file')

    if missing:
        return f"Instagram: ⚠️ SaveAsBot bridge missing {', '.join(missing)}"
    return f"Instagram: 🟢 via @{SAVEASBOT_USERNAME}"

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
        cj = load_cookies_to_jar(file_path)
        
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

def build_safe_audio_filename(song_title: str | None, artist: str | None) -> str:
    base_name = f"{artist} - {song_title}" if artist and song_title else (song_title or artist or "Audio")
    safe_name = re.sub(r'[\\\\/:*?\"<>|]+', '_', base_name)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip().strip('.')
    if not safe_name:
        safe_name = "Audio"
    return f"{safe_name}.mp3"

def build_safe_video_filename(url: str) -> str:
    if 'instagram.com' in url or 'instagr.am' in url:
        base_name = 'instagram_video'
    elif 'threads.net' in url or 'threads.com' in url:
        base_name = 'threads_video'
    elif 'youtube.com' in url or 'youtu.be' in url:
        base_name = 'youtube_video'
    else:
        base_name = 'video'
    return f"{base_name}.mp4"

def clean_saveasbot_text(text: str | None) -> str:
    if not text:
        return ''
    cleaned = text.replace('\u200b', '').strip()
    if cleaned.lower() == 'рад был помочь! ваш, @saveasbot':
        return ''
    return cleaned

def is_instagram_video_request(url: str) -> bool:
    normalized = url.lower()
    return any(part in normalized for part in ('/reel/', '/reels/', '/tv/', '/stories/'))

def is_saveasbot_marketing_text(text: str | None) -> bool:
    cleaned = clean_saveasbot_text(text).lower()
    if not cleaned:
        return False
    marketing_markers = (
        'нравится бот',
        'поддержите его автора',
        'донатом',
        'бонусную подписку',
        'отключение рекламы',
        'отсутствие лимитов',
        'семейка ботов',
        'familybots',
        'переходите и подписывайтесь',
        'заберите бонус',
    )
    return any(marker in cleaned for marker in marketing_markers)

def is_saveasbot_service_text(text: str | None) -> bool:
    cleaned = clean_saveasbot_text(text)
    if not cleaned:
        return False
    service_prefixes = (
        'Для ценителей качества',
        'Нажмите, чтобы получить текст поста',
        'Привет! Наш бот всегда был бесплатным',
        'Спасибо, что пользуетесь',
    )
    return any(cleaned.startswith(prefix) for prefix in service_prefixes) or is_saveasbot_marketing_text(cleaned)

def is_saveasbot_quality_document_text(text: str | None) -> bool:
    return clean_saveasbot_text(text).startswith('Для ценителей качества')

def is_saveasbot_terminal_error_text(text: str | None) -> bool:
    cleaned = clean_saveasbot_text(text).lower()
    if not cleaned:
        return False
    terminal_markers = (
        'не удалось получить информацию о публикации',
        'не удалось скачать',
        'публикация недоступна',
        'закрытый (приватный) аккаунт',
        'возрастные ограничения',
    )
    return any(marker in cleaned for marker in terminal_markers)

def should_skip_saveasbot_media(text: str, kind: str, source_url: str, has_video_media: bool, has_primary_media: bool) -> bool:
    if is_saveasbot_marketing_text(text):
        return True
    if has_primary_media and is_saveasbot_quality_document_text(text):
        return True
    if is_instagram_video_request(source_url) and has_video_media and kind != 'video':
        return True
    return False

def trim_caption(text: str | None) -> str | None:
    cleaned = clean_saveasbot_text(text)
    if not cleaned:
        return None
    return cleaned[:1024]

def get_saveasbot_lock() -> asyncio.Lock:
    global _SAVEASBOT_LOCK
    if _SAVEASBOT_LOCK is None:
        _SAVEASBOT_LOCK = asyncio.Lock()
    return _SAVEASBOT_LOCK

def get_saveasbot_api_id() -> int:
    if not SAVEASBOT_API_ID:
        raise RuntimeError("SAVEASBOT_API_ID or API_ID is not configured")
    try:
        return int(SAVEASBOT_API_ID)
    except ValueError as exc:
        raise RuntimeError("SAVEASBOT_API_ID/API_ID must be an integer") from exc

def normalize_telegram_username(username: str) -> str:
    return username.strip().lstrip('@').lower()

def build_saveasbot_peer(user_id: str | int, access_hash: str | int):
    if InputPeerUser is None:
        raise RuntimeError("Telethon is not installed; install requirements.txt")
    try:
        return InputPeerUser(int(user_id), int(access_hash))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SAVEASBOT_USER_ID and SAVEASBOT_ACCESS_HASH must be integers") from exc

def get_saveasbot_peer_from_env():
    if not (SAVEASBOT_USER_ID or SAVEASBOT_ACCESS_HASH):
        return None
    if not (SAVEASBOT_USER_ID and SAVEASBOT_ACCESS_HASH):
        logging.warning("Both SAVEASBOT_USER_ID and SAVEASBOT_ACCESS_HASH are required; ignoring partial peer config")
        return None
    return build_saveasbot_peer(SAVEASBOT_USER_ID, SAVEASBOT_ACCESS_HASH)

def get_saveasbot_peer_from_session():
    if _SAVEASBOT_RUNTIME_SESSION:
        session_path = _SAVEASBOT_RUNTIME_SESSION
    elif SAVEASBOT_SESSION_PATH:
        session_path = Path(SAVEASBOT_SESSION_PATH)
    else:
        return None

    if not session_path.is_file():
        return None

    username = normalize_telegram_username(SAVEASBOT_USERNAME)
    try:
        with sqlite3.connect(f"file:{session_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "select id, hash from entities where lower(username) = ? limit 1",
                (username,),
            ).fetchone()
    except sqlite3.Error as exc:
        logging.warning(f"Could not read SaveAsBot peer from session DB: {exc}")
        return None

    if not row:
        return None
    return build_saveasbot_peer(row[0], row[1])

async def get_saveasbot_peer(client):
    peer = get_saveasbot_peer_from_env()
    if peer:
        return peer

    peer = get_saveasbot_peer_from_session()
    if peer:
        return peer

    try:
        return await client.get_input_entity(SAVEASBOT_USERNAME)
    except Exception as exc:
        raise RuntimeError(
            "SaveAsBot peer is not in the Telegram session cache. "
            "Open @SaveAsBot once from the tg_crawler account or set "
            "SAVEASBOT_USER_ID and SAVEASBOT_ACCESS_HASH."
        ) from exc

def prepare_saveasbot_runtime_session() -> Path:
    global _SAVEASBOT_RUNTIME_SESSION
    if _SAVEASBOT_RUNTIME_SESSION and _SAVEASBOT_RUNTIME_SESSION.exists():
        return _SAVEASBOT_RUNTIME_SESSION

    if not SAVEASBOT_SESSION_PATH:
        raise RuntimeError("SAVEASBOT_SESSION_PATH is not configured")

    source = Path(SAVEASBOT_SESSION_PATH)
    if not source.exists() or not source.is_file():
        raise RuntimeError(f"SAVEASBOT_SESSION_PATH does not point to a session file: {source}")

    runtime = Path(tempfile.gettempdir()) / f"saveasbot_{source.stem}_{os.getpid()}.session"
    shutil.copy2(source, runtime)
    _SAVEASBOT_RUNTIME_SESSION = runtime
    return runtime

async def ensure_saveasbot_client():
    global _SAVEASBOT_CLIENT
    if TelegramClient is None:
        raise RuntimeError("Telethon is not installed; install requirements.txt")
    if not SAVEASBOT_API_HASH:
        raise RuntimeError("SAVEASBOT_API_HASH or API_HASH is not configured")

    if _SAVEASBOT_CLIENT:
        try:
            if _SAVEASBOT_CLIENT.is_connected():
                return _SAVEASBOT_CLIENT
        except Exception:
            with contextlib.suppress(Exception):
                await _SAVEASBOT_CLIENT.disconnect()
            _SAVEASBOT_CLIENT = None

    runtime_session = prepare_saveasbot_runtime_session()
    client = TelegramClient(
        str(runtime_session),
        get_saveasbot_api_id(),
        SAVEASBOT_API_HASH,
        receive_updates=False,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("SaveAsBot Telegram session is not authorized")

    _SAVEASBOT_CLIENT = client
    return client

async def collect_saveasbot_responses(client, peer, after_message_id: int, timeout_sec: float, max_responses: int):
    responses_by_id = {}
    first_seen_at = None
    last_seen_at = None
    deadline = asyncio.get_running_loop().time() + max(timeout_sec, 10)
    history_limit = max(max_responses * 3, 20)

    while len(responses_by_id) < max_responses:
        now = asyncio.get_running_loop().time()
        remaining = deadline - now
        if remaining <= 0:
            break

        saw_new = False
        messages = await client.get_messages(peer, limit=history_limit)
        for message in sorted(messages, key=lambda msg: getattr(msg, 'id', 0) or 0):
            message_id = getattr(message, 'id', 0) or 0
            if message_id <= after_message_id or getattr(message, 'out', False):
                continue
            if message_id in responses_by_id:
                continue

            responses_by_id[message_id] = message
            saw_new = True
            now = asyncio.get_running_loop().time()
            if first_seen_at is None:
                first_seen_at = now
            last_seen_at = now

            if len(responses_by_id) >= max_responses:
                break
            text = clean_saveasbot_text(getattr(message, 'message', '') or '')
            if not getattr(message, 'media', None) and is_saveasbot_terminal_error_text(text):
                return list(responses_by_id.values())

        responses = list(responses_by_id.values())
        if responses and not saw_new:
            has_media = any(getattr(message, 'media', None) for message in responses)
            idle_limit = SAVEASBOT_RESPONSE_IDLE_SEC if has_media else SAVEASBOT_TEXT_FOLLOWUP_SEC
            wait_started_at = last_seen_at if has_media else first_seen_at
            if wait_started_at is not None and now - wait_started_at >= idle_limit:
                break

        await asyncio.sleep(min(max(SAVEASBOT_POLL_INTERVAL_SEC, 0.2), max(remaining, 0.2)))

    return list(responses_by_id.values())

async def warmup_saveasbot_dialog(client, peer) -> None:
    global _SAVEASBOT_STARTED
    if _SAVEASBOT_STARTED or not SAVEASBOT_SEND_START:
        return

    try:
        sent = await client.send_message(peer, '/start')
        with contextlib.suppress(Exception):
            await collect_saveasbot_responses(client, peer, getattr(sent, 'id', 0) or 0, 20, 5)
    except Exception as exc:
        logging.warning(f"SaveAsBot warmup failed: {exc}")
    finally:
        _SAVEASBOT_STARTED = True

def classify_saveasbot_media(message) -> str:
    file_obj = getattr(message, 'file', None)
    mime_type = getattr(file_obj, 'mime_type', '') or ''
    if mime_type.startswith('image/'):
        return 'photo'
    if mime_type.startswith('video/'):
        return 'video'
    if mime_type.startswith('audio/'):
        return 'audio'
    return 'document'

async def request_saveasbot_items(url: str, msg = None) -> list[dict[str, object]]:
    lock = get_saveasbot_lock()
    async with lock:
        client = await ensure_saveasbot_client()
        peer = await get_saveasbot_peer(client)
        await warmup_saveasbot_dialog(client, peer)

        logging.info(f"Sending Instagram URL to SaveAsBot: {url}")
        sent = await client.send_message(peer, url)
        sent_id = getattr(sent, 'id', 0) or 0
        responses = await collect_saveasbot_responses(
            client,
            peer,
            sent_id,
            SAVEASBOT_TIMEOUT_SEC,
            SAVEASBOT_MAX_RESPONSES,
        )
        logging.info(f"SaveAsBot returned {len(responses)} response message(s) for sent_id={sent_id}")

        items: list[dict[str, object]] = []
        response_meta = [
            (
                message,
                clean_saveasbot_text(getattr(message, 'message', '') or ''),
                classify_saveasbot_media(message) if getattr(message, 'media', None) else '',
            )
            for message in responses
        ]
        has_primary_media = any(
            kind and not is_saveasbot_quality_document_text(text) and not is_saveasbot_marketing_text(text)
            for _, text, kind in response_meta
        )
        has_video_media = any(
            kind == 'video' and not is_saveasbot_marketing_text(text)
            for _, text, kind in response_meta
        )

        for message, text, kind in response_meta:
            if getattr(message, 'media', None):
                if should_skip_saveasbot_media(text, kind, url, has_video_media, has_primary_media):
                    logging.info(
                        "Skipping SaveAsBot non-request media: "
                        f"id={getattr(message, 'id', None)} kind={kind} text={text[:80]!r}"
                    )
                    continue
                downloaded_path = await client.download_media(message, file=TEMP_FOLDER)
                if downloaded_path:
                    items.append({
                        'kind': kind,
                        'path': downloaded_path,
                        'text': '' if is_saveasbot_service_text(text) else text,
                    })
            elif text and not is_saveasbot_service_text(text):
                items.append({'kind': 'text', 'text': text})

        # Fallback: check if we have a direct video link in the text messages and no video has been downloaded yet
        if not any(item.get('kind') == 'video' for item in items):
            extracted_video_url = None
            for message, text, kind in response_meta:
                if getattr(message, 'entities', None):
                    for ent in message.entities:
                        if ent.__class__.__name__ == 'MessageEntityTextUrl':
                            ent_url = getattr(ent, 'url', '') or ''
                            ent_text = ''
                            if message.message and ent.offset is not None and ent.length is not None:
                                ent_text = message.message[ent.offset : ent.offset + ent.length].lower()
                            if 'скачать' in ent_text or 'download' in ent_text or '.mp4' in ent_url.lower() or 'video' in ent_text:
                                if 'fbcdn.net' in ent_url or '.mp4' in ent_url or 'instagram' in ent_url:
                                    extracted_video_url = ent_url
                                    break
                    if extracted_video_url:
                        break
            
            if extracted_video_url:
                logging.info(f"Found direct video URL from SaveAsBot: {extracted_video_url}")
                if msg:
                    with contextlib.suppress(Exception):
                        await msg.edit_text(build_status('⚠️ Слишком большое видео (>50MB). Переключение на прямую ссылку...'))
                        await asyncio.sleep(1.0)
                        await msg.edit_text(build_status('⏳ Закачка видео по прямой ссылке...'))
                
                downloaded_path = os.path.join(TEMP_FOLDER, f"{uuid.uuid4()}.mp4")
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
                    }
                    def download_file(src_url, dest_path):
                        r = requests.get(src_url, headers=headers, stream=True, timeout=60)
                        r.raise_for_status()
                        with open(dest_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                    
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, download_file, extracted_video_url, downloaded_path)
                    
                    if os.path.exists(downloaded_path) and os.path.getsize(downloaded_path) > 0:
                        if msg:
                            with contextlib.suppress(Exception):
                                await msg.edit_text(build_status('⚙️ Перекодирование видео (оптимизация размера и качества)...'))
                        
                        compressed_path = os.path.join(TEMP_FOLDER, f"{uuid.uuid4()}_compressed.mp4")
                        logging.info(f"Normalizing direct video file: {downloaded_path}")
                        def transcode():
                            return compress_video(downloaded_path, compressed_path, prefer_vertical=None)
                        
                        success = await loop.run_in_executor(None, transcode)
                        if success:
                            os.remove(downloaded_path)
                            downloaded_path = compressed_path
                        
                        items.append({
                            'kind': 'video',
                            'path': downloaded_path,
                            'text': '',
                        })
                except Exception as e:
                    logging.exception(f"Failed to download or transcode direct video URL: {e}")
                    if os.path.exists(downloaded_path):
                        try:
                            os.remove(downloaded_path)
                        except Exception:
                            pass

        logging.info(f"Prepared {len(items)} SaveAsBot item(s) for Telegram reply")
        return items

async def send_text_chunks(update: Update, text: str) -> None:
    for start in range(0, len(text), 3900):
        chunk = text[start:start + 3900]
        if chunk:
            await update.message.reply_text(chunk)

async def send_saveasbot_items(update: Update, items: list[dict[str, object]]) -> bool:
    # Filter items to only keep video and photo items, and clear their captions
    # "не пропускай ничего кроме видео/фото в ответы в чате мне."
    allowed_items = []
    for item in items:
        if item.get('kind') in {'video', 'photo'}:
            item['text'] = ''  # Clear text/caption
            allowed_items.append(item)
    items = allowed_items

    media_items = [item for item in items if item.get('path')]
    text_items = [str(item.get('text') or '') for item in items if item.get('kind') == 'text' and item.get('text')]
    if media_items and text_items:
        logging.info(f"Dropping {len(text_items)} SaveAsBot text item(s) because media was returned")
        text_items = []

    if media_items and len(media_items) > 1 and all(item.get('kind') in {'photo', 'video'} for item in media_items):
        handles = []
        try:
            media_group = []
            for idx, item in enumerate(media_items[:10]):
                path = str(item['path'])
                handle = open(path, 'rb')
                handles.append(handle)
                caption = trim_caption(str(item.get('text') or '')) if idx == 0 else None
                if item.get('kind') == 'video':
                    media_group.append(InputMediaVideo(handle, caption=caption, supports_streaming=True))
                else:
                    media_group.append(InputMediaPhoto(handle, caption=caption))
            await update.message.reply_media_group(media_group)
        finally:
            for handle in handles:
                with contextlib.suppress(Exception):
                    handle.close()

        for item in media_items[10:]:
            await send_single_saveasbot_media(update, item)
    else:
        for item in media_items:
            await send_single_saveasbot_media(update, item)

    for text in text_items:
        await send_text_chunks(update, text)

    return bool(media_items or text_items)

async def send_single_saveasbot_media(update: Update, item: dict[str, object]) -> None:
    path = str(item['path'])
    caption = trim_caption(str(item.get('text') or ''))
    kind = str(item.get('kind') or 'document')

    with open(path, 'rb') as media:
        if kind == 'photo':
            await update.message.reply_photo(media, caption=caption, write_timeout=300, read_timeout=300, connect_timeout=60)
        elif kind == 'video':
            await update.message.reply_video(
                media,
                caption=caption,
                supports_streaming=True,
                filename=build_safe_video_filename(update.message.text or ''),
                write_timeout=300,
                read_timeout=300,
                connect_timeout=60
            )
        elif kind == 'audio':
            await update.message.reply_audio(media, caption=caption, write_timeout=300, read_timeout=300, connect_timeout=60)
        else:
            await update.message.reply_document(media, caption=caption, write_timeout=300, read_timeout=300, connect_timeout=60)

def cleanup_saveasbot_items(items: list[dict[str, object]]) -> None:
    for item in items:
        path = item.get('path')
        if path and os.path.exists(str(path)):
            with contextlib.suppress(Exception):
                os.remove(str(path))

def resolve_audio_source(query: str) -> str:
    if not query.startswith('ytsearch'):
        return query

    safe_cookie_file = get_safe_cookie_file('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    search_command = [
        'yt-dlp',
        '--flat-playlist',
        '--print', '%(id)s\t%(title)s',
        query.replace('ytsearch1:', 'ytsearch10:', 1)
    ]
    if safe_cookie_file:
        search_command[1:1] = ['--cookies', safe_cookie_file]
    search_command[1:1] = ['--js-runtimes', 'node']

    search_proc = subprocess.run(search_command, check=True, capture_output=True, text=True)
    candidates = [line.strip() for line in search_proc.stdout.splitlines() if line.strip()]
    if not candidates:
        raise RuntimeError("No YouTube search results found")

    probe_base = ['yt-dlp', '--skip-download', '--print', '%(id)s']
    if safe_cookie_file:
        probe_base.extend(['--cookies', safe_cookie_file])
    probe_base.extend(['--js-runtimes', 'node'])

    for candidate in candidates:
        video_id = candidate.split('\t', 1)[0].strip()
        if not video_id:
            continue
        candidate_url = f'https://www.youtube.com/watch?v={video_id}'
        probe = subprocess.run(probe_base + [candidate_url], capture_output=True, text=True)
        if probe.returncode == 0:
            logging.info(f"Selected audio source candidate: {candidate_url}")
            return candidate_url
        stderr = (probe.stderr or '').strip()
        logging.info(f"Skipping audio candidate {video_id}: {stderr}")

    raise RuntimeError("No downloadable YouTube candidates found")

def probe_video_metadata(input_path: str) -> dict[str, object]:
    command = [
        'ffprobe', '-v', 'error',
        '-print_format', 'json',
        '-show_streams',
        '-show_format',
        input_path
    ]
    proc = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = json.loads(proc.stdout)
    streams = data.get('streams', [])
    video_stream = next((stream for stream in streams if stream.get('codec_type') == 'video'), None)
    if not video_stream:
        raise RuntimeError("No video stream found")

    rotation = 0
    tags = video_stream.get('tags') or {}
    if isinstance(tags, dict) and tags.get('rotate'):
        try:
            rotation = int(float(tags['rotate']))
        except (TypeError, ValueError):
            rotation = 0

    if rotation == 0:
        for side_data in video_stream.get('side_data_list') or []:
            side_rotation = side_data.get('rotation')
            if side_rotation is None:
                continue
            try:
                rotation = int(float(side_rotation))
                break
            except (TypeError, ValueError):
                continue

    rotation = rotation % 360
    width = int(video_stream.get('width') or 0)
    height = int(video_stream.get('height') or 0)
    sample_aspect_ratio = str(video_stream.get('sample_aspect_ratio') or '1:1')
    display_aspect_ratio = str(video_stream.get('display_aspect_ratio') or '0:1')
    if rotation in (90, 270):
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height

    return {
        'rotation': rotation,
        'width': width,
        'height': height,
        'display_width': display_width,
        'display_height': display_height,
        'sample_aspect_ratio': sample_aspect_ratio,
        'display_aspect_ratio': display_aspect_ratio,
    }

def build_video_normalization_filter(video_meta: dict[str, object], target_width: int, target_height: int) -> str:
    filters: list[str] = []
    rotation = int(video_meta.get('rotation') or 0)
    if rotation == 90:
        filters.append('transpose=1')
    elif rotation == 180:
        filters.extend(['hflip', 'vflip'])
    elif rotation == 270:
        filters.append('transpose=2')

    filters.append(
        f"scale=w={target_width}:h={target_height}:force_original_aspect_ratio=decrease"
    )
    filters.append(f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2")
    filters.append('setsar=1')
    filters.append(f"setdar={target_width}/{target_height}")
    return ','.join(filters)

def build_video_profiles(is_vertical: bool) -> list[dict[str, object]]:
    if is_vertical:
        return [
            {'width': 720, 'height': 1280, 'crf': 18, 'audio_bitrate': '128k'},
            {'width': 540, 'height': 960, 'crf': 21, 'audio_bitrate': '96k'},
            {'width': 480, 'height': 854, 'crf': 24, 'audio_bitrate': '80k'},
            {'width': 360, 'height': 640, 'crf': 26, 'audio_bitrate': '64k'},
        ]

    return [
        {'width': 1280, 'height': 720, 'crf': 18, 'audio_bitrate': '128k'},
        {'width': 960, 'height': 540, 'crf': 21, 'audio_bitrate': '96k'},
        {'width': 854, 'height': 480, 'crf': 24, 'audio_bitrate': '80k'},
        {'width': 640, 'height': 360, 'crf': 26, 'audio_bitrate': '64k'},
    ]

async def process_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    msg = await update.message.reply_text(build_status('⏳ Sending Instagram link to SaveAsBot...'))
    items: list[dict[str, object]] = []

    try:
        items = await request_saveasbot_items(url, msg=msg)
        await msg.edit_text(build_status('📤 Отправка готового видео...'))
        sent_anything = await send_saveasbot_items(update, items)
        if sent_anything:
            increment_success()
            await msg.edit_text(build_status('✅ Done.'))
        else:
            increment_fail()
            await msg.edit_text(build_status('❌ SaveAsBot did not return media or text.'))
    except RPCError as e:
        logging.error(f"SaveAsBot Telegram RPC error: {e}")
        increment_fail()
        await msg.edit_text(build_status('❌ Telegram user session failed to send the link.'))
    except Exception as e:
        logging.exception(f"SaveAsBot bridge error: {e}")
        increment_fail()
        await msg.edit_text(build_status('❌ SaveAsBot bridge failed.'))
    finally:
        cleanup_saveasbot_items(items)

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
                cj = load_cookies_to_jar(cookie_file)
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
    if update.effective_user is None or update.message is None:
        logging.warning("Ignoring update without effective user or message")
        return

    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text('⛔️ You are not allowed to use this bot.')
        logging.warning(f"Access denied for user_id: {user_id}")
        return

    text = update.message.text
    if not text:
        return
    
    # Route to specific downloaders
    if 'instagram.com' in text or 'instagr.am' in text:
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
        audio_source = resolve_audio_source(f'ytsearch1:{search_query}')
        await download_audio(update, context, audio_source, song_title, artist, msg)

    except Exception:
        logging.exception("YouTube search error")
        await msg.edit_text('❌ An error occurred while searching on YouTube.')
        increment_fail()

# Function to download audio
async def download_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, song_title: str, artist: str | None, msg):
    unique_id = str(uuid.uuid4())
    temp_file_pattern = f'{TEMP_FOLDER}/{unique_id}.%(ext)s'
    safe_cookie_file = get_safe_cookie_file(url if not url.startswith('ytsearch') else 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    
    command = [
        'yt-dlp', '-x', '--audio-format', 'mp3', '--audio-quality', '0',
        '--no-playlist', '--newline',
        '--metadata-from-title', "%(artist)s - %(title)s",
        '--embed-thumbnail', '-o', temp_file_pattern
    ]
    if safe_cookie_file:
        command.extend(['--cookies', safe_cookie_file])
    command.extend(['--js-runtimes', 'node', url])

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
        file_name = build_safe_audio_filename(song_title, artist)

        with open(temp_file, 'rb') as audio:
            telegram_audio = InputFile(audio, filename=file_name)
            await update.message.reply_audio(telegram_audio, title=final_title, performer=final_artist, write_timeout=300, read_timeout=300, connect_timeout=60)

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
        low = err_text.lower()
        if 'sign in to confirm your age' in low or 'login required' in low:
            await msg.edit_text(build_status('❌ YouTube login/age confirmation required.'))
        else:
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

# Normalize/transcode video to fit Telegram limit and remove problematic metadata
def compress_video(input_path: str, output_path: str, prefer_vertical: bool | None = None) -> bool:
    max_size = 50 * 1024 * 1024
    video_meta = probe_video_metadata(input_path)
    display_width = int(video_meta.get('display_width') or 0)
    display_height = int(video_meta.get('display_height') or 0)
    is_vertical = prefer_vertical if prefer_vertical is not None else display_height >= display_width
    profiles = build_video_profiles(is_vertical)

    for idx, profile in enumerate(profiles, start=1):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)

            target_width = int(profile['width'])
            target_height = int(profile['height'])
            vf = build_video_normalization_filter(video_meta, target_width, target_height)
            command = [
                'ffmpeg', '-y', '-i', input_path,
                '-map', '0:v:0', '-map', '0:a?',
                '-map_metadata', '-1',
                '-vf', vf,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', str(profile['crf']),
                '-pix_fmt', 'yuv420p',
                '-profile:v', 'high',
                '-level', '4.0',
                '-c:a', 'aac',
                '-b:a', profile['audio_bitrate'],
                '-metadata:s:v:0', 'rotate=0',
                '-movflags', '+faststart',
                output_path
            ]
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if os.path.exists(output_path):
                size = os.path.getsize(output_path)
                logging.info(
                    f"Transcode profile {idx} produced {size / (1024 * 1024):.2f} MiB "
                    f"(target={target_width}x{target_height}, crf={profile['crf']}, audio={profile['audio_bitrate']}, "
                    f"rotation={video_meta.get('rotation')}, sar={video_meta.get('sample_aspect_ratio')}, "
                    f"dar={video_meta.get('display_aspect_ratio')})"
                )
                if size <= max_size:
                    normalized_meta = probe_video_metadata(output_path)
                    logging.info(
                        f"Normalized output meta: {normalized_meta.get('display_width')}x{normalized_meta.get('display_height')}, "
                        f"rotation={normalized_meta.get('rotation')}, sar={normalized_meta.get('sample_aspect_ratio')}, "
                        f"dar={normalized_meta.get('display_aspect_ratio')}"
                    )
                    return True
        except subprocess.CalledProcessError as e:
            stderr_tail = (e.stderr or '').strip()[-1200:]
            logging.error(f"Video transcode error on profile {idx}: {stderr_tail}")
        except Exception as e:
            logging.error(f"Video transcode error on profile {idx}: {e}")

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
    safe_cookie_file = get_safe_cookie_file(url)

    def build_command() -> list[str]:
        cmd = ['yt-dlp']
        if safe_cookie_file: cmd.extend(['--cookies', safe_cookie_file])
        
        # Optimize: Try to get best quality that fits within ~50MB limit to avoid slow transcoding
        # We target video < 40MB and audio < 10MB approx, or single file < 50MB
        format_selector = 'bestvideo[filesize<40M]+bestaudio[filesize<10M]/best[filesize<50M]/bestvideo+bestaudio/best'
        
        cmd.extend([
            '-f', format_selector, 
            '--merge-output-format', 'mp4',
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

        needs_instagram_normalization = 'instagram.com' in url or 'instagr.am' in url
        if needs_instagram_normalization or os.path.getsize(temp_file) > 50 * 1024 * 1024:
            stage_text = '⚙️ Normalizing video...' if needs_instagram_normalization else '⚙️ Large video, transcoding...'
            try: await msg.edit_text(build_status(stage_text))
            except Exception: pass
            if not compress_video(temp_file, compressed_file, prefer_vertical=None):
                await msg.edit_text(build_status('❌ Failed to transcode video.'))
                return
            os.remove(temp_file)
            temp_file = compressed_file

        logging.info(f"Video ready to send: {temp_file}")
        try: await msg.edit_text(build_status('📤 Uploading video...'))
        except Exception: pass

        output_meta = probe_video_metadata(temp_file)
        video_width = int(output_meta.get('display_width') or output_meta.get('width') or 0) or None
        video_height = int(output_meta.get('display_height') or output_meta.get('height') or 0) or None
        video_filename = build_safe_video_filename(url)

        with open(temp_file, 'rb') as video:
            await update.message.reply_video(
                video,
                width=video_width,
                height=video_height,
                supports_streaming=True,
                filename=video_filename,
                write_timeout=300,
                read_timeout=300,
                connect_timeout=60
            )

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
