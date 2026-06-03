Бот для получения медиа из Instagram через `@SaveAsBot`, скачивания видео из YouTube/Threads и аудио по названию песни или ссылке из Spotify.

- **Видео**: Принимает ссылки на посты, Reels, Shorts (Instagram через `@SaveAsBot`, YouTube и Threads через локальный downloader).
- **Аудио**: Принимает текстовое название песни (например, `Daft Punk - Around the World`) или ссылку на трек из Spotify.

English version: see README_EN.md

### Требования
- Python-пакеты: `python-telegram-bot`, `yt-dlp`, `spotipy`, `telethon` (см. `requirements.txt`)
- В системе должен быть установлен `ffmpeg`
  - Ubuntu/Debian: `apt install ffmpeg`

### Куки для загрузки
- Для YouTube: файл `cookie_youtube.txt`
- Для Threads: файл `cookie_threads.txt`
- Instagram больше не использует cookie-файл: ссылка отправляется в `@SaveAsBot` через авторизованную Telethon-сессию.

Как получить cookie:
1. Войти в аккаунт в браузере.
2. Установить расширение «Get cookies.txt LOCALLY».
3. Сохранить куки в локальные файлы (`cookie_youtube.txt`, `cookie_threads.txt`).
4. Не коммитить эти файлы в репозиторий — они добавлены в `.gitignore`.

### Запуск бота
#### 1) Переменные окружения
Создайте файл `.env` в корне проекта или экспортируйте переменные:

- `BOT_TOKEN` — токен Telegram-бота от BotFather.
- `ALLOWED_USER_IDS` — список ID пользователей, которым разрешён доступ (через запятую).
- `SPOTIPY_CLIENT_ID` — (Опционально) Client ID вашего приложения Spotify.
- `SPOTIPY_CLIENT_SECRET` — (Опционально) Client Secret вашего приложения Spotify.
- `SAVEASBOT_API_ID` / `SAVEASBOT_API_HASH` — Telegram API credentials для пользовательской Telethon-сессии.
- `SAVEASBOT_SESSION_HOST_PATH` — путь на хосте к `.session`, который Docker смонтирует в контейнер.
- `SAVEASBOT_SESSION_PATH` — путь к session внутри контейнера, по умолчанию `/app/saveasbot.session`.
- `SAVEASBOT_USERNAME` — коммерческий бот, по умолчанию `SaveAsBot`.
- `SAVEASBOT_SEND_START=1` — опционально отправлять `/start` в `@SaveAsBot` при первом Instagram-запросе процесса.
- `SAVEASBOT_USER_ID` / `SAVEASBOT_ACCESS_HASH` — опциональный явный peer `@SaveAsBot`, если его нет в cache таблице `entities` внутри `.session`.
- `SAVEASBOT_POLL_INTERVAL_SEC` — интервал polling истории диалога с `@SaveAsBot`, по умолчанию `1`.

> **Где взять ключи Spotify?**
> 1. Зайдите на [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
> 2. Создайте новое приложение (Create an app).
> 3. Скопируйте `Client ID` и `Client Secret` из настроек вашего приложения.
> 4. Добавьте их в ваш `.env` файл.
> *Без этих ключей будет работать только поиск по названию, но не по ссылкам на Spotify.*

#### 2) Docker (рекомендуемый способ)
Пример команды для запуска через `docker-compose` (предпочтительно, так как он автоматически подхватит `.env` файл):
```bash
docker-compose up -d --build
```
`docker-compose.yml` уже настроен для использования этих переменных. Для Instagram-моста укажите `SAVEASBOT_SESSION_HOST_PATH`, например путь к авторизованной session из `tg_crawler`.

### Схема Instagram-моста
```mermaid
flowchart LR
    U[Пользователь] -->|Instagram URL| B[Bot IG2Telegram]
    B -->|Telethon send_message, receive_updates=false| S[@SaveAsBot]
    B -->|get_messages polling| S
    S -->|media/text response in dialog history| B
    B -->|reply_photo / reply_video / reply_document| U

    B -. использует .-> TS[read-only Telethon .session]
    TS -. копируется в runtime .-> TMP[/tmp saveasbot session copy]
```

### Использование
- **Для скачивания видео**: Отправьте боту ссылку из Instagram, YouTube или Threads.
- Instagram-ссылки обрабатываются через живую Telegram-сессию и `@SaveAsBot`; ответ `@SaveAsBot` бот скачивает из user-session и отправляет исходному пользователю.
- Для Instagram Reels/Stories при наличии видео бот отбрасывает рекламные сообщения `@SaveAsBot` и отдельные не-видео вложения.
- **Для скачивания аудио**: Отправьте боту название песни (например, `Queen - Bohemian Rhapsody`) или ссылку на трек из Spotify.
- Бот интеллектуально определяет тип запроса по содержимому сообщения.
- Если видеофайл превышает 50 МБ, он будет автоматически сжат.
- Команда `/stats` показывает статистику загрузок, статус Instagram-моста и срок действия cookie-файлов YouTube/Threads.
- Бот обновляет одно сообщение, показывая текущий статус (поиск, скачивание, загрузка).

### Безопасность
- Файлы cookie, `.env` и токены не должны попадать в репозиторий. `.gitignore` настроен для их игнорирования.
- Cookie-файлы опциональны для публичных видео, но часто нужны для приватных/ограниченных материалов.
