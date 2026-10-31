#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["telethon==1.45.0", "qrcode==8.2", "python-socks[asyncio]>=2.4,<3"]
# ///
"""Personal-account Telegram login, bot materials, and resumable announcement delivery."""

import argparse
import asyncio
import contextlib
import hashlib
import html
import io
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit
import uuid
import webbrowser


class WorkflowError(Exception):
    pass


def state_dir():
    override = os.environ.get("MCY_MUSIC_TELEGRAM_HOME")
    if override:
        path = Path(override).expanduser().resolve()
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/church-skills/team-announce/telegram"
    elif os.name == "nt":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "church-skills/team-announce/telegram"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "church-skills/team-announce/telegram"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def save_json(path, data):
    """Atomic private write; also used before every externally mutating step."""
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def account_lock(root):
    # OS lock releases on process exit, including a crash. Keep the inode in place.
    with open(root / "account.lock", "a+b") as stream:
        os.chmod(root / "account.lock", 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkflowError("Аккаунт уже используется другим запуском скилла.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_config(root):
    path = root / "config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    for key, env in (("api_id", "TELEGRAM_API_ID"), ("api_hash", "TELEGRAM_API_HASH"), ("proxy", "MCY_MUSIC_TELEGRAM_PROXY")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return data


def validate_config(data):
    try:
        api_id = int(data.get("api_id", 0))
    except (ValueError, TypeError):
        api_id = 0
    api_hash = str(data.get("api_hash", ""))
    if api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise WorkflowError("Нужны api_id и api_hash вашего приложения с my.telegram.org/apps.")
    try:
        port = int(data.get("port", 443))
    except (ValueError, TypeError):
        port = 0
    if not 1 <= port <= 65535:
        raise WorkflowError("Порт Telegram должен быть от 1 до 65535.")
    result = {"api_id": api_id, "api_hash": api_hash, "port": port}
    if data.get("proxy"):
        proxy_tuple(data["proxy"])
        result["proxy"] = data["proxy"]
    return result


def proxy_tuple(raw):
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        kind = {"socks5": "socks5", "socks5h": "socks5", "http": "http"}[parsed.scheme]
        if not parsed.hostname or not parsed.port:
            raise ValueError()
    except (ValueError, KeyError) as exc:
        raise WorkflowError("Прокси: ожидается socks5://host:port или http://host:port.") from exc
    return (kind, parsed.hostname, parsed.port, True,
            unquote(parsed.username) if parsed.username else None,
            unquote(parsed.password) if parsed.password else None)


def make_client(root, config):
    from telethon import TelegramClient, connection

    class SelectedConnection(connection.ConnectionTcpObfuscated):
        def __init__(self, ip, port, dc_id, **kwargs):
            super().__init__(ip, config.get("port", 443), dc_id, **kwargs)

    return TelegramClient(
        str(root / "personal.session"), config["api_id"], config["api_hash"],
        proxy=proxy_tuple(config.get("proxy")), device_model="mcy-music-team-announce",
        connection=SelectedConnection,
        system_version=sys.platform, app_version="1.0", flood_sleep_threshold=0,
        request_retries=0, connection_retries=1, timeout=15,
    )


def private_session(root):
    for path in root.glob("personal.session*"):
        path.chmod(0o600)


def identity(user):
    return {"id": user.id, "username": getattr(user, "username", None),
            "name": getattr(user, "title", None) or getattr(user, "first_name", ""),
            "bot": bool(getattr(user, "bot", False))}


LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCY Music — вход в Telegram</title>
<style>body{font:18px system-ui;max-width:560px;margin:40px auto;padding:0 20px}input,button{font:inherit;padding:10px;margin:6px 0;width:100%;box-sizing:border-box}img{max-width:300px;width:100%}#error{color:#b22}</style>
<h1>Вход в личный Telegram</h1><p>Отдельная сессия для mcy-music-team-announce. Данные остаются на этом компьютере.</p>
<p id="status">Подключение…</p><p id="error"></p><img id="qr" hidden>
<form id="config" hidden><p>Ключи приложения из <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a>:</p>
<input name="api_id" placeholder="api_id" required inputmode="numeric"><input name="api_hash" placeholder="api_hash" required type="password" autocomplete="off"><input name="proxy" placeholder="Прокси (необязательно)"><button>Продолжить</button></form>
<form id="password" hidden><input name="password" type="password" placeholder="Облачный пароль Telegram" required autocomplete="off"><button>Войти</button></form>
<script>
const base=location.pathname; let qrVersion='';
for(const id of ['config','password'])document.getElementById(id).onsubmit=async e=>{e.preventDefault();const form=e.target;await fetch(base+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:id,...Object.fromEntries(new FormData(form))})});form.reset();};
async function poll(){try{const s=await(await fetch(base+'/state',{cache:'no-store'})).json();document.getElementById('status').textContent=s.message;document.getElementById('error').textContent=s.error||'';for(const id of ['config','password'])document.getElementById(id).hidden=s.phase!==id;const img=document.getElementById('qr');img.hidden=s.phase!=='qr';if(s.phase==='qr'&&s.version!==qrVersion){qrVersion=s.version;img.src=base+'/qr?v='+qrVersion;}if(s.phase==='done'||s.phase==='failed')return;}catch(e){}setTimeout(poll,1000);}poll();
</script></html>"""


class LoginServer:
    """Loopback-only, token-scoped form; never log credentials or QR tokens."""
    def __init__(self):
        self.token = "/" + secrets.token_urlsafe(32)
        self.state = {"phase": "connecting", "message": "Подключение к Telegram…"}
        self.qr = b""
        self.actions = queue.Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body, content_type):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                return self.headers.get("Host") == f"127.0.0.1:{owner.server.server_port}"

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if not self.allowed():
                    return self.reply(403, b"Forbidden", "text/plain")
                if path == owner.token:
                    return self.reply(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")
                if path == owner.token + "/state":
                    return self.reply(200, json.dumps(owner.state, ensure_ascii=False).encode(), "application/json")
                if path == owner.token + "/qr":
                    return self.reply(200, owner.qr, "image/svg+xml")
                self.reply(404, b"Not found", "text/plain")

            def do_POST(self):
                expected_origin = f"http://127.0.0.1:{owner.server.server_port}"
                if not self.allowed() or self.path != owner.token + "/action" or self.headers.get("Origin") != expected_origin:
                    return self.reply(403, b"Forbidden", "text/plain")
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 8192:
                        raise ValueError()
                    data = json.loads(self.rfile.read(length))
                    if data.get("kind") != owner.state["phase"] or owner.actions.qsize():
                        raise ValueError()
                    owner.actions.put_nowait(data)
                except (ValueError, TypeError, AttributeError):
                    return self.reply(400, b"Invalid request", "text/plain")
                self.reply(200, b"{}", "application/json")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}{self.token}"

    async def action(self, kind, message):
        self.state = {"phase": kind, "message": message}
        while True:
            try:
                return self.actions.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.2)

    def close(self):
        self.qr = b""
        self.server.shutdown()
        self.server.server_close()


async def login(root, open_browser):
    import qrcode
    import qrcode.image.svg
    from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
    server = LoginServer()
    url = server.start()
    print(json.dumps({"login_url": url}, ensure_ascii=False), flush=True)
    if open_browser:
        webbrowser.open(url)
    client = None
    try:
        data = load_config(root)
        while True:
            try:
                config = validate_config(data)
                break
            except WorkflowError:
                data = await server.action("config", "Укажите ключи приложения Telegram.")
        save_json(root / "config.json", config)
        client = make_client(root, config)
        await client.connect()
        if not await client.is_user_authorized():
            qr = await client.qr_login()
            while True:
                image = qrcode.make(qr.url, image_factory=qrcode.image.svg.SvgPathImage)
                buf = io.BytesIO()
                image.save(buf)
                server.qr = buf.getvalue()
                server.state = {"phase": "qr", "message": "Telegram на телефоне → Настройки → Устройства → Подключить устройство. Отсканируйте QR-код.", "version": uuid.uuid4().hex}
                print('{"login_state": "qr_ready"}', flush=True)
                try:
                    await qr.wait()
                    break
                except asyncio.TimeoutError:
                    await qr.recreate()
                except SessionPasswordNeededError:
                    while True:
                        action = await server.action("password", "Введите облачный пароль Telegram в этой локальной форме.")
                        try:
                            await client.sign_in(password=action.pop("password"))
                            break
                        except PasswordHashInvalidError:
                            server.state["error"] = "Пароль не подошел. Повторите ввод."
                    break
        user = await client.get_me()
        if user.bot:
            raise WorkflowError("Нужен личный аккаунт, вход ботом не поддерживается.")
        server.state = {"phase": "done", "message": f"Вход выполнен: {user.first_name}. Можно закрыть вкладку."}
        print(json.dumps({"authorized": True, "account": identity(user)}, ensure_ascii=False), flush=True)
        await asyncio.sleep(2)
    except BaseException:
        server.state = {"phase": "failed", "message": "Вход прерван. Проверьте результат запуска и откройте страницу нового входа."}
        await asyncio.sleep(1.2)
        raise
    finally:
        if client:
            await client.disconnect()
        private_session(root)
        server.close()


def read_plan(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkflowError("План должен быть JSON-объектом.")
    songs = data.get("songs")
    if not isinstance(songs, list) or not 1 <= len(songs) <= 10:
        raise WorkflowError("Бот принимает от 1 до 10 песен одним запросом.")
    for song in songs:
        if not isinstance(song, dict) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(song.get("number", ""))):
            raise WorkflowError("У каждой песни нужен библиотечный номер обычными цифрами.")
        song["number"] = str(song["number"])
        if not isinstance(song.get("title"), str) or not song["title"].strip() or "\n" in song["title"]:
            raise WorkflowError("У каждой песни нужно однозначное название в одну строку.")
        if "new" in song and not isinstance(song["new"], bool):
            raise WorkflowError("Поле new должно быть true/false после проверки новизны.")
    if not isinstance(data.get("date"), str) or not re.fullmatch(r"\d{2}\.\d{2}(?:\.\d{4})?", data["date"], flags=re.ASCII):
        raise WorkflowError("Нужна дата DD.MM или DD.MM.YYYY.")
    if not isinstance(data.get("topic"), str) or not data["topic"].strip() or "\n" in data["topic"]:
        raise WorkflowError("Нужна тема сообщения в одну строку.")
    target = data.get("target")
    if not (isinstance(target, (str, int)) and not isinstance(target, bool) and str(target).strip()):
        raise WorkflowError("Нужно явно задать target: me, @username или числовой ID чата.")
    if data.get("bot", "mcy_music_bot") != "mcy_music_bot":
        raise WorkflowError("Этот адаптер поддерживает только @mcy_music_bot.")
    if "test" in data and not isinstance(data["test"], bool):
        raise WorkflowError("Поле test должно быть true/false.")
    return data


def announcement(data):
    lines = ["Тест отправки — пример программы", ""] if data.get("test") else []
    lines += [f"Список гимнов на {html.escape(data['date'])}:", ""]
    for index, song in enumerate(data["songs"], 1):
        lines.append(f"{index}. [<code>{song['number']}</code>] {html.escape(song['title'])}" + (" 🆕" if song.get("new") else ""))
    lines += ["", "Список для бота: <code>" + " ".join(s["number"] for s in data["songs"]) + "</code>", "", f'Тема: "{html.escape(data["topic"])}"']
    result = "\n".join(lines)
    if len(html.unescape(re.sub(r"</?code>", "", result)).encode("utf-16-le")) // 2 > 4096:
        raise WorkflowError("Анонс превышает лимит одного сообщения Telegram.")
    return result


def bot_numbers(message):
    # The bot puts library IDs in title lines: "Название (149)".
    return re.findall(r"^.+ \(([1-9][0-9]*)\)\s*$", message.raw_text or "", flags=re.M)


def fingerprint(message):
    media = getattr(message, "photo", None) or getattr(message, "document", None)
    entities = [entity.to_dict() for entity in (getattr(message, "entities", None) or [])]
    raw = json.dumps({"text": message.raw_text or "", "media": getattr(media, "id", None),
                      "group": getattr(message, "grouped_id", None), "entities": entities,
                      "noforwards": bool(getattr(message, "noforwards", False))}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def button_data(message):
    return {button.data for row in (message.buttons or []) for button in row if getattr(button, "data", None)}


class BotReader:
    def __init__(self, client, bot, timeout=180):
        self.client, self.bot, self.timeout = client, bot, timeout
        self.outgoing = set()
        self.floor = 0

    async def send(self, text):
        sent = await asyncio.wait_for(self.client.send_message(self.bot, text, parse_mode=None), timeout=30)
        self.outgoing.add(sent.id)
        self.floor = sent.id
        return sent

    async def messages(self, floor):
        # Paginate so busy background audio cannot hide the start of a response.
        async def read():
            return [m async for m in self.client.iter_messages(self.bot, min_id=floor, reverse=True, limit=300)]
        messages = await asyncio.wait_for(read(), timeout=30)
        if len(messages) == 300:
            raise WorkflowError("Слишком много посторонних ответов бота; повторите подготовку отдельно.")
        if any(m.out and m.id not in self.outgoing for m in messages):
            raise WorkflowError("В диалоге с ботом появился другой запрос. Подготовка остановлена.")
        return [m for m in messages if not m.out and m.sender_id == self.bot.id]

    async def wait(self, floor, selector, description, settle=0):
        deadline, signature, stable = time.monotonic() + self.timeout, None, None
        while time.monotonic() < deadline:
            messages = await asyncio.wait_for(self.messages(floor), timeout=max(0.01, deadline - time.monotonic()))
            selected = selector(messages)
            if selected:
                current = [(m.id, fingerprint(m), sorted(button_data(m))) for m in selected]
                if current != signature:
                    signature, stable = current, time.monotonic()
                if time.monotonic() - stable >= settle:
                    return selected
            else:
                signature, stable = None, None
            await asyncio.sleep(1)
        raise WorkflowError(f"Не дождались ответа бота: {description}. В целевой чат ничего не отправлено.")

    async def start_request(self, command, numbers):
        command_message = await self.send(command)
        await self.wait(command_message.id, lambda ms: [m for m in ms if "Введи номер гимна" in (m.raw_text or "")], "запрос номеров")
        return await self.send(" ".join(numbers))

    async def materials(self, numbers):
        request = await self.start_request("/info", numbers)
        wanted = Counter(numbers)

        def select_info(messages):
            selected = [m for m in messages if not m.media and bot_numbers(m)]
            found = Counter(n for m in selected for n in bot_numbers(m))
            errors = [m.raw_text for m in messages if not m.media and not bot_numbers(m) and not (m.raw_text or "").startswith("Звук не скачался:")]
            if errors:
                raise WorkflowError("Бот не подготовил полную справку: " + " | ".join(errors)[:500])
            return selected if found == wanted else []

        info = await self.wait(request.id, select_info, "полная справка по всем номерам", settle=2)
        request = await self.start_request("/chords", numbers)
        question = (await self.wait(request.id, lambda ms: [m for m in ms if b"chords:full" in button_data(m)], "выбор всего текста с аккордами"))[0]
        await asyncio.wait_for(question.click(data=b"chords:full"), timeout=30)
        modes = await self.wait(request.id, lambda ms: [m for m in ms if m.id == question.id and b"chordmode:full:default" in button_data(m)], "выбор режима по умолчанию")
        await asyncio.wait_for(modes[0].click(data=b"chordmode:full:default"), timeout=30)

        def select_chords(messages):
            photos = [m for m in messages if m.photo and m.id > question.id]
            if len(photos) > len(numbers):
                raise WorkflowError("Получено больше картинок, чем песен. Проверьте ответы бота.")
            errors = [m.raw_text for m in messages if m.id > question.id and m.raw_text and not m.media and not m.raw_text.startswith("Звук не скачался:")]
            if errors:
                raise WorkflowError("Бот не подготовил полный набор аккордов: " + " | ".join(errors)[:500])
            return photos if len(photos) == len(numbers) else []

        chords = await self.wait(request.id, select_chords, "аккорды всех песен", settle=3)
        return info, chords


def peer_snapshot(entity):
    from telethon import utils
    return {"peer_id": utils.get_peer_id(entity), "name": getattr(entity, "title", None) or getattr(entity, "first_name", ""),
            "username": getattr(entity, "username", None)}


async def prepare(client, root, plan_path, timeout):
    data = read_plan(plan_path)
    body = announcement(data)
    target_arg = data["target"]
    if isinstance(target_arg, str) and re.fullmatch(r"-?[0-9]+", target_arg):
        target_arg = int(target_arg)
    destination = await resolve_entity(client, target_arg)
    me = await client.get_me()
    if getattr(destination, "bot", False):
        raise WorkflowError("Целью анонса не должен быть бот.")
    if getattr(destination, "broadcast", False):
        raise WorkflowError("Для отправки от личного имени выберите группу или личный чат, а не канал.")
    if getattr(destination, "forum", False):
        raise WorkflowError("Отправка в темы форума пока не поддерживается. Выберите обычную группу или личный чат.")
    bot = await resolve_entity(client, "mcy_music_bot")
    if not getattr(bot, "bot", False):
        raise WorkflowError("@mcy_music_bot не является ботом; отправка остановлена.")
    numbers = [s["number"] for s in data["songs"]]
    info, chords = await BotReader(client, bot, timeout).materials(numbers)
    if any(getattr(m, "noforwards", False) for m in info + chords):
        raise WorkflowError("Telegram запретил пересылку материалов бота. Анонс не отправлен.")
    job = {"version": 1, "id": uuid.uuid4().hex, "created_at": time.time(), "account_id": me.id,
           "target": peer_snapshot(destination), "bot_id": bot.id, "plan": data, "html": body,
           "batches": [[{"id": m.id, "hash": fingerprint(m)} for m in batch] for batch in (info, chords)],
           "steps": []}
    for kind, count in (("announcement", 1), ("info", len(info)), ("chords", len(chords))):
        job["steps"].append({"kind": kind, "state": "pending", "message_ids": [],
                             "random_ids": [secrets.randbits(63) for _ in range(count)]})
    save_json(root / (job["id"] + ".json"), job)
    return {"job": job["id"], "account": identity(me), "target": job["target"], "html": body,
            "numbers": numbers, "info_messages": len(info), "chord_photos": len(chords), "sent_to_target": False}


async def send_personal(client, destination, step, body=None, messages=None, bot=None):
    from telethon import functions, types
    from telethon.extensions import html as telegram_html
    peer = await client.get_input_entity(destination)
    # Supergroups may have a channel or anonymous identity selected by default.
    send_as = types.InputPeerSelf() if getattr(destination, "megagroup", False) else None
    if body is not None:
        text, entities = telegram_html.parse(body)
        request = functions.messages.SendMessageRequest(
            peer, text, entities=entities, no_webpage=True, send_as=send_as,
            random_id=step["random_ids"][0],
        )
    else:
        request = functions.messages.ForwardMessagesRequest(
            from_peer=await client.get_input_entity(bot), id=[m.id for m in messages],
            to_peer=peer, send_as=send_as, random_id=step["random_ids"],
        )
    result = await asyncio.wait_for(client(request), timeout=45)
    if isinstance(result, types.UpdateShortSentMessage):
        return [await client.get_messages(destination, ids=result.id)]
    sent = client._get_response_message(request, result, peer)
    return sent if isinstance(sent, list) else [sent]


async def deliver(client, root, job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise WorkflowError("Некорректный ID подготовленной отправки.")
    path = root / (job_id + ".json")
    job = json.loads(path.read_text())
    me = await client.get_me()
    if me.id != job["account_id"]:
        raise WorkflowError("Аккаунт отличается от аккаунта подготовки.")
    if any(step["state"] == "uncertain" for step in job["steps"]):
        raise WorkflowError("У предыдущей попытки неизвестен результат. Сначала проверьте целевой чат; автоматический повтор запрещен.")
    if all(step["state"] == "sent" for step in job["steps"]):
        return {"job": job_id, "already_sent": True, "steps": job["steps"]}
    if time.time() - job["created_at"] > 86400:
        raise WorkflowError("Подготовке больше суток. Подготовьте актуальный анонс заново.")
    destination = await client.get_entity(job["target"]["peer_id"])
    if peer_snapshot(destination)["peer_id"] != job["target"]["peer_id"]:
        raise WorkflowError("Изменилась цель отправки.")
    bot = await client.get_entity(job["bot_id"])
    # Validate *all* source messages before the first target-side mutation.
    batches = []
    for records in job["batches"]:
        messages = await client.get_messages(bot, ids=[r["id"] for r in records])
        if len(messages) != len(records) or any(not m or m.sender_id != bot.id or m.out or getattr(m, "noforwards", False) or fingerprint(m) != r["hash"] for m, r in zip(messages, records)):
            raise WorkflowError("Материалы бота удалены или изменились. Нужна новая подготовка.")
        batches.append(messages)
    for index, step in enumerate(job["steps"]):
        if step["state"] == "sent":
            continue
        # Write intent before sending: a lost response must never trigger a blind duplicate.
        step["state"] = "uncertain"
        save_json(path, job)
        if index == 0:
            sent = await send_personal(client, destination, step, body=job["html"])
        else:
            sent = await send_personal(client, destination, step, messages=batches[index - 1], bot=bot)
        if len(sent) != len(step["random_ids"]) or any(message is None for message in sent):
            raise WorkflowError("Telegram вернул неполный результат. Проверьте целевой чат перед повтором.")
        step["state"], step["message_ids"] = "sent", [m.id for m in sent]
        save_json(path, job)
    return {"job": job_id, "target": job["target"], "sent": True, "steps": job["steps"]}


async def inspect_job(client, root, job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise WorkflowError("Некорректный ID подготовленной отправки.")
    job = json.loads((root / (job_id + ".json")).read_text())
    me = await client.get_me()
    if me.id != job["account_id"]:
        raise WorkflowError("Аккаунт отличается от аккаунта подготовки.")
    bot = await client.get_entity(job["bot_id"])
    records = job["batches"][1]
    messages = await client.get_messages(bot, ids=[r["id"] for r in records])
    if len(messages) != len(records) or any(not m or fingerprint(m) != record["hash"] for m, record in zip(messages, records)):
        raise WorkflowError("Аккорды изменились после подготовки.")
    media_dir = root / job_id
    media_dir.mkdir(mode=0o700, exist_ok=True)
    paths = []
    for i, message in enumerate(messages, 1):
        path = media_dir / f"chords-{i}.jpg"
        downloaded = await asyncio.wait_for(client.download_media(message, file=str(path)), timeout=60)
        if not downloaded:
            raise WorkflowError("Не удалось получить картинку для просмотра аккордов.")
        Path(downloaded).chmod(0o600)
        paths.append(str(Path(downloaded).resolve()))
    return {"job": job_id, "expected_numbers": [s["number"] for s in job["plan"]["songs"]],
            "chord_images": paths, "sent_to_target": False}


async def resolve_entity(client, value):
    # get_entity(username) always resolves over the network; prefer the session cache.
    peer = await asyncio.wait_for(client.get_input_entity(value), timeout=30)
    return await asyncio.wait_for(client.get_entity(peer), timeout=30)


async def connected(args, root):
    config = validate_config(load_config(root))
    client = make_client(root, config)
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            raise WorkflowError("Сначала выполните login и войдите в личный аккаунт.")
        me = await client.get_me()
        if me.bot:
            raise WorkflowError("Нужен личный аккаунт.")
        if args.command == "status":
            return {"authorized": True, "account": identity(me)}
        if args.command == "chats":
            groups = []
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.is_group and (not args.query or args.query.casefold() in dialog.name.casefold()):
                    groups.append(peer_snapshot(dialog.entity))
            return {"groups": groups, "scope": "До 200 последних диалогов; без содержимого сообщений."}
        if args.command == "prepare":
            return await prepare(client, root, args.input, args.timeout)
        if args.command == "inspect":
            return await inspect_job(client, root, args.job)
        return await deliver(client, root, args.job)
    finally:
        await client.disconnect()
        private_session(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    login_parser = sub.add_parser("login", help="Local browser form + QR login; no chat secrets")
    login_parser.add_argument("--open-browser", action="store_true")
    sub.add_parser("status", help="Check account identity; no messages sent")
    chats_parser = sub.add_parser("chats", help="Find destination groups by title; no messages sent")
    chats_parser.add_argument("--query", default="")
    prepare_parser = sub.add_parser("prepare", help="Ask bot for materials, save preview; no target sends")
    prepare_parser.add_argument("--input", required=True)
    prepare_parser.add_argument("--timeout", type=int, default=180)
    send_parser = sub.add_parser("send", help="Send the prepared announcement and forward bot results")
    send_parser.add_argument("--job", required=True)
    inspect_parser = sub.add_parser("inspect", help="Download prepared chord images for visual verification; no sends")
    inspect_parser.add_argument("--job", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = state_dir()
    try:
        with account_lock(root):
            cooldown_path = root / "cooldown.json"
            if cooldown_path.exists():
                cooldown = json.loads(cooldown_path.read_text())
                remaining = cooldown["retry_at"] - time.time()
                if remaining > 0:
                    raise WorkflowError(f"Telegram требует паузу еще {int(remaining) + 1} с. Новые запросы не отправлены.")
                cooldown_path.unlink()
            if args.command == "login":
                asyncio.run(asyncio.wait_for(login(root, args.open_browser), timeout=600))
                return 0
            result = asyncio.run(connected(args, root))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, ValueError, OSError, asyncio.TimeoutError) as exc:
        print(json.dumps({"error": str(exc) or "Истекло время ожидания."}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)
        return 130
    except Exception as exc:
        if type(exc).__name__ in {"FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"}:
            seconds = int(exc.seconds)
            cooldown = {"retry_at": time.time() + seconds, "retry_after_seconds": seconds,
                        "request": type(getattr(exc, "request", None)).__name__}
            save_json(root / "cooldown.json", cooldown)
            print(json.dumps({"error": type(exc).__name__, **cooldown,
                              "detail": "Telegram требует паузу. Не повторяйте запросы до retry_at."}, ensure_ascii=False), file=sys.stderr)
            return 1
        # Telegram exceptions can carry request parameters: keep only the error class.
        print(json.dumps({"error": type(exc).__name__, "detail": "Операция прервана. Для send проверьте сохраненное состояние перед повтором."}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
