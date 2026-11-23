import json
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from threading import BoundedSemaphore, Lock
from urllib.parse import urlsplit
from zipfile import ZipFile

import requests
import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file
from itsdangerous import BadData, URLSafeTimedSerializer
from yt_dlp.utils import DownloadError


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

ROOT = Path(__file__).resolve().parent
SPOTIFY_API = "https://api.spotify.com/v1"
MAX_DURATION = 15 * 60
MAX_AUDIO_SIZE = 24 * 1024 * 1024
DOWNLOAD_SLOTS = BoundedSemaphore(2)
TRACK_LIMIT = 100
PROGRESS = {}
PROGRESS_LOCK = Lock()
TRACK_TOKENS = URLSafeTimedSerializer(os.urandom(32), salt="track-selection")

TOKEN = ""
TOKEN_EXPIRES = 0.0


def read_settings():
    settings = {}
    path = ROOT / ".env"

    if not path.is_file():
        return settings

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        settings[key] = value

    return settings


SETTINGS = read_settings()


def setting(key):
    return os.environ.get(key, SETTINGS.get(key, "")).strip()


def parse_spotify_url(value):
    try:
        url = urlsplit(value.strip())
        match = None
        if url.scheme == "https" and url.netloc.lower() == "open.spotify.com":
            match = re.fullmatch(
                r"/(?:intl-[A-Za-z-]+/)?(track|album|playlist)/([A-Za-z0-9]{22})/?",
                url.path,
            )
    except ValueError:
        match = None

    if not match:
        raise ValueError(
            "Вставьте ссылку open.spotify.com на трек, альбом или плейлист."
        )

    return match.groups()


def spotify_token():
    global TOKEN, TOKEN_EXPIRES

    if TOKEN and time.monotonic() < TOKEN_EXPIRES:
        return TOKEN

    client_id = setting("SPOTIFY_CLIENT_ID")
    secret = setting("SPOTIFY_CLIENT_SECRET")
    if not client_id or not secret:
        raise ValueError(
            "Задайте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в настройках Portainer или в локальном .env."
        )

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        timeout=10,
    )

    if response.status_code != 200:
        raise ValueError(
            f"Spotify не выдал токен (HTTP {response.status_code}). "
            "Проверьте Client ID и Client Secret."
        )

    data = response.json()
    TOKEN = data["access_token"]
    TOKEN_EXPIRES = time.monotonic() + max(0, int(data["expires_in"]) - 30)
    return TOKEN


def resolve_tracks(value):
    if not value or len(value) > 500:
        raise ValueError("Введите ссылку на трек или название и исполнителя (до 500 символов).")

    if not re.match(r"^(?:[a-z][a-z0-9+.-]*://|(?:open\.)?spotify[.:])", value, re.I):
        parts = re.split(r"\s+[-–—]\s+", value, maxsplit=1)
        if len(parts) != 2 or not all(map(normalize, parts)):
            raise ValueError("Введите название и исполнителя через тире: Название - Исполнитель.")
        title, artist = parts
        return [(title, artist, None)], None

    kind, spotify_id = parse_spotify_url(value)
    if kind == "track":
        return [spotify_track(spotify_get(f"/tracks/{spotify_id}"))], None

    path = f"/{kind}s/{spotify_id}/{'tracks' if kind == 'album' else 'items'}"
    tracks, offset = [], 0
    try:
        while offset < TRACK_LIMIT:
            page = spotify_get(path, {"limit": min(50, TRACK_LIMIT - offset), "offset": offset})
            batch = page.get("items") or []
            for entry in batch:
                if not entry or entry.get("is_local"):
                    continue
                track = (entry.get("item") or entry.get("track")) if kind == "playlist" else entry
                if track and not track.get("is_local") and track.get("type", "track") == "track":
                    try:
                        tracks.append(spotify_track(track))
                    except ValueError:
                        continue
            offset += len(batch)
            if not batch or not page.get("next"):
                return tracks, None
        return tracks[:TRACK_LIMIT], f"Показаны первые {TRACK_LIMIT} позиций списка Spotify."
    except PermissionError:
        # Spotify limits playlist access for some apps; the public player may still list tracks.
        response = requests.get(f"https://open.spotify.com/embed/{kind}/{spotify_id}", timeout=12)
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.S)
        if response.ok and match:
            entity = json.loads(match[1]).get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            for entry in (entity.get("trackList") or [])[:TRACK_LIMIT]:
                title, artist = entry.get("title"), (entry.get("subtitle") or "").split(",\u00a0", 1)[0]
                if title and artist and (entry.get("uri") or "").startswith("spotify:track:"):
                    duration = entry.get("duration")
                    tracks.append((title, artist, duration / 1000 if duration else None))
            if tracks:
                return tracks, "Показаны треки из публичного плеера Spotify. Список может быть неполным."
        raise ValueError("Spotify не открыл этот список. Проверьте ссылку и доступ к списку.")


def spotify_get(path, params=None):
    response = requests.get(
        SPOTIFY_API + path,
        headers={"Authorization": f"Bearer {spotify_token()}"},
        params=params,
        timeout=12,
    )

    status = response.status_code
    if status in (401, 403, 404):
        raise PermissionError(
            f"Spotify не предоставил доступ (HTTP {status}). "
            "Проверьте ссылку и доступ приложения или введите название и исполнителя."
        )
    if status == 429:
        raise ValueError("Spotify ограничил частоту запросов. Повторите позже.")
    if status != 200:
        raise ValueError(f"Spotify вернул ошибку HTTP {status}.")

    return response.json()


def spotify_track(track):
    title = track.get("name")
    artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
    if not title or not artists:
        raise ValueError("Spotify не вернул название и исполнителя трека.")
    duration_ms = track.get("duration_ms")
    return title, artists[0], duration_ms / 1000 if duration_ms else None


def normalize(text):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def rank_matches(entries, title, artist, duration, id_pattern):
    title_key, artist_key = normalize(title), normalize(artist)
    ranked = []
    for video in entries:
        if not video or not re.fullmatch(id_pattern, str(video.get("id") or "")):
            continue
        length = video.get("duration")
        if not length or not 0 < length <= MAX_DURATION:
            continue
        difference = abs(length - duration) if duration else 0
        if duration and difference > max(12, duration * 0.05):
            continue

        name = video.get("title") or ""
        channel = normalize(video.get("channel") or video.get("uploader") or "")
        name = re.sub(
            r"\s*(?:\(|\[|[-–—])\s*(?:official\s+(?:music\s+)?(?:audio|video)|lyrics?|audio)\s*[)\]]?\s*$",
            "", name, flags=re.I,
        )
        name_key = normalize(name)
        topic = channel == f"{artist_key} topic"
        artist_channel = topic or channel in (artist_key, f"{artist_key}vevo", f"{artist_key} official")
        title_with_artist = name_key in (f"{artist_key} {title_key}", f"{title_key} {artist_key}")
        if not (title_with_artist or (name_key == title_key and artist_channel)):
            continue

        ranked.append(((not topic, not artist_channel, difference), video["id"]))

    return list(dict.fromkeys(video_id for _, video_id in sorted(ranked, key=lambda item: item[0])))


def download_audio(title, artist, duration, report, folder):
    if not normalize(title) or not normalize(artist):
        raise ValueError("Укажите название и исполнителя буквами или цифрами.")
    if duration and duration > MAX_DURATION:
        raise ValueError("Поддерживаются треки длительностью до 15 минут.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Для MP3 установите FFmpeg с ffprobe и добавьте их в PATH.")
    runtimes = {name: {} for name in ("deno", "node") if shutil.which(name)}
    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "socket_timeout": 12, "retries": 1, "extractor_retries": 1, "fragment_retries": 1,
        "skip_unavailable_fragments": False,
        "js_runtimes": runtimes,
    }
    sources = [
        ("scsearch", r"[0-9]+", "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A{}"),
    ]
    if runtimes:
        sources.insert(0, ("ytsearch", r"[A-Za-z0-9_-]{11}", "https://www.youtube.com/watch?v={}"))

    def download_progress(info):
        if info["status"] == "finished":
            report(0.93, "Конвертация в MP3")
        elif info["status"] == "downloading":
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            fraction = (info.get("downloaded_bytes") or 0) / total if total else 0
            report(0.12 + 0.77 * min(fraction, 1), "Загрузка аудио")

    source_failed = False
    for search, id_pattern, url_template in sources:
        source = "YouTube" if search == "ytsearch" else "SoundCloud"
        report(0.03, f"Поиск на {source}")
        try:
            with yt_dlp.YoutubeDL({**options, "extract_flat": "in_playlist"}) as downloader:
                results = downloader.extract_info(f"{search}12:{artist} {title}", download=False)
        except DownloadError:
            app.logger.info("Music search failed for %s", search)
            source_failed = True
            continue

        candidates = rank_matches(results.get("entries") or [], title, artist, duration, id_pattern)
        for track_id in candidates[:3]:
            report(0.09, f"Подключение к {source}")
            with TemporaryDirectory(dir=folder) as attempt:
                output = Path(attempt) / "track.mp3"
                config = {
                    **options, "format": "bestaudio/best",
                    "max_filesize": MAX_AUDIO_SIZE, "outtmpl": str(Path(attempt) / "track.%(ext)s"),
                    "progress_hooks": [download_progress],
                    "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                        "preferredquality": "192"}],
                }
                try:
                    with yt_dlp.YoutubeDL(config) as downloader:
                        downloader.extract_info(url_template.format(track_id), download=True)
                except DownloadError as exc:
                    app.logger.info("Could not download %s candidate %s", search, track_id)
                    if search == "ytsearch" and "sign in to confirm" in str(exc).lower():
                        break
                    continue
                if output.is_file() and 0 < output.stat().st_size <= MAX_AUDIO_SIZE:
                    return output.replace(Path(folder) / "track.mp3")
        source_failed |= bool(candidates)

    if source_failed:
        raise DownloadError("No source returned a downloadable match")
    raise LookupError("Не найдено надёжного совпадения. Проверьте название и исполнителя.")


@app.get("/")
def index():
    return render_template("index.html")


@app.before_request
def reject_cross_site_post():
    if request.method == "POST" and request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify(error="Запрос с другого сайта отклонён."), 403


@app.post("/search")
def search():
    lines = [line.strip() for line in request.form.get("query", "").splitlines() if line.strip()]
    if not 1 <= len(lines) <= 10:
        return jsonify(error="Добавьте от 1 до 10 ссылок или названий, по одному на строку."), 400

    rows, notes, seen = [], [], set()
    for number, line in enumerate(lines, 1):
        try:
            tracks, note = resolve_tracks(line)
            if note:
                notes.append(note)
        except PermissionError as exc:
            notes.append(f"Строка {number}: {exc}")
            continue
        except requests.RequestException:
            notes.append(f"Строка {number}: не удалось получить ответ Spotify.")
            continue
        except ValueError as exc:
            notes.append(f"Строка {number}: {exc}")
            continue
        for title, artist, duration in tracks:
            key = (normalize(title), normalize(artist))
            if key in seen:
                continue
            if len(rows) >= TRACK_LIMIT:
                notes.append(f"В результатах можно показать не больше {TRACK_LIMIT} песен.")
                break
            seen.add(key)
            available = not duration or duration <= MAX_DURATION
            rows.append({"title": title, "artist": artist, "duration": duration,
                         "token": TRACK_TOKENS.dumps([title, artist, duration]) if available else None})
        if len(rows) >= TRACK_LIMIT:
            if number < len(lines):
                notes.append("Достигнут лимит списка. Остальные строки не обработаны.")
            break
    if not rows:
        return jsonify(error=" ".join(notes) or "В списке нет музыкальных треков."), 400
    return jsonify(tracks=rows, notes=list(dict.fromkeys(notes)))


@app.get("/progress/<job>")
def progress(job):
    return jsonify(PROGRESS.get(job, {})), {"Cache-Control": "no-store"}


@app.post("/download/<job>")
def download(job):
    payload = request.get_json(silent=True)
    tokens = payload.get("tracks") if isinstance(payload, dict) else None
    if (not re.fullmatch(r"[a-f0-9]{32}", job) or not isinstance(tokens, list)
            or not 1 <= len(tokens) <= TRACK_LIMIT or not all(isinstance(token, str) for token in tokens)):
        return jsonify(error="Выберите треки из списка."), 400
    try:
        tracks = [TRACK_TOKENS.loads(token, max_age=24 * 3600) for token in dict.fromkeys(tokens)]
    except BadData:
        return jsonify(error="Список устарел. Найдите треки ещё раз."), 400
    with PROGRESS_LOCK:
        if job in PROGRESS:
            return jsonify(error="Эта загрузка уже выполняется."), 409
        if not DOWNLOAD_SLOTS.acquire(blocking=False):
            return jsonify(error="Уже идут загрузки. Повторите позже."), 503
        PROGRESS[job] = {}

    total = len(tracks)
    failures, used_names = [], set()
    multiple = total > 1
    result = None
    try:
        result = TemporaryFile(mode="w+b")
        with TemporaryDirectory(prefix="spotify-audio-") as folder:
            archive = ZipFile(result, "w") if multiple else None
            try:
                for index, (title, artist, duration) in enumerate(tracks):
                    def report(fraction, stage):
                        percent = int((index + fraction) / total * 96)
                        percent = max(percent, PROGRESS.get(job, {}).get("percent", 0))
                        PROGRESS[job] = {"percent": percent, "current": index + 1, "total": total,
                                         "stage": stage, "title": title}

                    report(0, "Подготовка")
                    try:
                        audio = download_audio(title, artist, duration, report, folder)
                    except DownloadError:
                        reason = "Источники не отдали аудио."
                    except (LookupError, ValueError) as exc:
                        reason = str(exc)
                    else:
                        name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]+', "_", f"{artist} - {title}")[:140].strip(". ")
                        filename = f"{name}.mp3"
                        if archive is not None:
                            report(0.98, "Добавление в ZIP")
                            suffix = 2
                            while filename.casefold() in used_names:
                                filename = f"{name} ({suffix}).mp3"
                                suffix += 1
                            used_names.add(filename.casefold())
                            archive.write(audio, filename)
                        else:
                            with audio.open("rb") as stream:
                                shutil.copyfileobj(stream, result)
                        audio.unlink()
                        continue
                    failures.append(f"{artist} - {title}: {reason}")
                if archive is not None and failures and len(failures) < total:
                    archive.writestr("not-downloaded.txt", "\n".join(failures))
            finally:
                if archive is not None:
                    archive.close()
        if len(failures) == total:
            return jsonify(error=failures[0] if total == 1 else "Не удалось скачать ни одного трека.",
                           failures=failures), 422
        size = result.tell()
        result.seek(0)
        response = send_file(result, mimetype="application/zip" if multiple else "audio/mpeg",
                             as_attachment=True, download_name="Granite.zip" if multiple else filename,
                             conditional=False)
        response.content_length = size
        response.headers["X-Failed-Tracks"] = str(len(failures))
        result = None  # The response owns the open file from here.
        return response
    except OSError:
        app.logger.exception("Unable to prepare a download")
        return jsonify(error="Не удалось подготовить файл. Проверьте свободное место и FFmpeg."), 500
    finally:
        if result is not None:
            result.close()
        with PROGRESS_LOCK:
            PROGRESS.pop(job, None)
            DOWNLOAD_SLOTS.release()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
