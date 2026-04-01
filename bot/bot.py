#!/usr/bin/env python3
"""
lil_worker Telegram→Claude Code bridge — streaming edition.

Claude sends tool-call notifications as separate Telegram messages
while it works, then sends the final answer at the end.

Setup:
1. Create .env with TELEGRAM_BOT_TOKEN, ALLOWED_USERS, CLAUDE_MODEL
2. Run: ./run.sh start
"""

import asyncio
import base64
import json
import os
import html
import re
import tempfile
import logging
import time
from pathlib import Path

import mistune
import openai
from lingua import Language, LanguageDetectorBuilder
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = [int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_VOICE_MODEL = os.environ.get("OPENAI_VOICE_MODEL", "gpt-4o-mini-transcribe")

# Optional: one-time token link generator (/token command)
# TOKEN_SERVICE_URL — base URL, token is appended (e.g. https://mysite.com/?t=)
# TOKEN_FILE — path to JSON file with {"available": [...], "issued": [...]}
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "")

SESSION_FILE = Path(__file__).parent / ".sessions.json"
TG_MSG_LIMIT = 4000

# ── Language detection ────────────────────────────────────────────────────────

_lang_detector = LanguageDetectorBuilder.from_languages(
    Language.UKRAINIAN, Language.RUSSIAN, Language.ENGLISH
).build()

_LANG_NAMES = {
    Language.UKRAINIAN: "Ukrainian",
    Language.RUSSIAN: "Russian",
    Language.ENGLISH: "English",
}


def detect_language(text: str) -> str:
    lang = _lang_detector.detect_language_of(text)
    return _LANG_NAMES.get(lang, "Russian")


# ── TTS (text-to-speech) for voice messages ──────────────────────────────────

TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "marin"
TEMP_DIR = Path(tempfile.gettempdir())

# Pattern: [VOICE lang="en"]text to speak[/VOICE]
_VOICE_RE = re.compile(
    r'\[VOICE\s+lang=["\'](\w+)["\']\](.*?)\[/VOICE\]',
    re.DOTALL,
)


def extract_voice_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract [VOICE] blocks from response text.

    Returns (cleaned_text, [(lang, speech_text), ...])
    """
    blocks = []
    for match in _VOICE_RE.finditer(text):
        lang = match.group(1)
        speech_text = match.group(2).strip()
        if speech_text:
            blocks.append((lang, speech_text))
    cleaned = _VOICE_RE.sub("", text).strip()
    return cleaned, blocks


async def synthesize_speech(text: str, user_id: int) -> Path | None:
    """Generate OGG Opus audio via OpenAI TTS."""
    if not OPENAI_API_KEY:
        logger.error("TTS: OPENAI_API_KEY not set")
        return None
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        audio_path = TEMP_DIR / f"tts_{user_id}_{int(time.time())}.ogg"
        async with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            response_format="opus",
        ) as response:
            await response.stream_to_file(audio_path)
        return audio_path
    except Exception:
        logger.exception("TTS synthesis failed")
        return None


async def send_voice_with_indicator(
    message: Message, bot: Bot, vb_text: str, vb_lang: str, user_id: int
):
    """Show record_voice animation, synthesize TTS, send voice message."""
    chat_id = message.chat.id
    stop_event = asyncio.Event()

    async def _record_voice_loop():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    # Cancel any lingering typing indicator before starting record_voice
    try:
        await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    except Exception:
        pass
    await asyncio.sleep(0.3)

    loop_task = asyncio.create_task(_record_voice_loop())
    try:
        logger.info(f"TTS: generating voice ({vb_lang}), {len(vb_text)} chars")
        audio_path = await synthesize_speech(vb_text, user_id)
        if audio_path:
            try:
                await message.answer_voice(voice=FSInputFile(audio_path))
            except Exception:
                logger.exception("Failed to send voice message")
            finally:
                audio_path.unlink(missing_ok=True)
        else:
            await message.answer("❌ TTS generation failed.")
    finally:
        stop_event.set()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Message debounce buffer (merges split long messages) ─────────────────────

DEBOUNCE_DELAY = 1.0  # seconds to wait for more parts

# per user_id: {"parts": [str], "task": asyncio.Task, "reply_msg": Message}
_msg_buffer: dict[int, dict] = {}

# ── Photo album buffer (merges media-group photos into one request) ───────────

PHOTO_DEBOUNCE_DELAY = 1.5  # seconds — albums arrive within ~0.5-1s

# per user_id: {"photos": [file_id], "caption": str, "task": asyncio.Task, "reply_msg": Message}
_photo_buffer: dict[int, dict] = {}


async def _flush_buffer(user_id: int, bot: Bot):
    """Called after DEBOUNCE_DELAY — merges buffered parts and processes."""
    await asyncio.sleep(DEBOUNCE_DELAY)

    buf = _msg_buffer.pop(user_id, None)
    if not buf:
        return

    full_text = "\n".join(buf["parts"])
    reply_msg: Message = buf["reply_msg"]

    logger.info(f"MSG uid={user_id} (merged {len(buf['parts'])} parts): {full_text[:120]!r}")

    sessions = load_sessions()
    session_id = sessions.get(str(user_id))
    lang = detect_language(full_text)
    logger.info(f"Detected language: {lang}")

    response, new_session_id = await run_claude_streaming(
        full_text, session_id, reply_msg, bot, lang=lang
    )

    if new_session_id:
        sessions[str(user_id)] = new_session_id
        save_sessions(sessions)
        logger.info(f"Session saved: uid={user_id}, sid={new_session_id}")
    elif session_id and not new_session_id:
        sessions.pop(str(user_id), None)
        save_sessions(sessions)
        logger.warning(f"Cleared stale session for uid={user_id}")

    # Extract voice blocks (if any) before sending text
    cleaned_response, voice_blocks = extract_voice_blocks(response)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        logger.info(f"Sending final: {len(response_html)} chars")
        await send_long_message(reply_msg, response_html)

    # Send voice messages with record_voice animation
    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(reply_msg, bot, vb_text, vb_lang, user_id)


# ── Session storage ───────────────────────────────────────────────────────────

def load_sessions() -> dict:
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    return {}


def save_sessions(sessions: dict):
    SESSION_FILE.write_text(json.dumps(sessions, indent=2))


# ── Telegram HTML renderer (mistune 2.x) ─────────────────────────────────────

class TelegramRenderer(mistune.HTMLRenderer):
    def heading(self, text, level, **attrs):
        return f"<b>{text}</b>\n\n"

    def paragraph(self, text):
        return f"{text}\n\n"

    def list(self, text, ordered, level, start=None):
        return text + "\n"

    def list_item(self, text, level):
        return f"• {text}\n"

    def block_code(self, code, info=None, **attrs):
        return f"<pre>{html.escape(code.strip())}</pre>\n\n"

    def codespan(self, text):
        return f"<code>{html.escape(text)}</code>"

    def emphasis(self, text):
        return f"<i>{text}</i>"

    def strong(self, text):
        return f"<b>{text}</b>"

    def strikethrough(self, text):
        return f"<s>{text}</s>"

    def link(self, link, text=None, title=None):
        display = text or link
        return f'<a href="{html.escape(link)}">{display}</a>'

    def image(self, src, alt='', title=None):
        return f"[Image: {alt}]"

    def block_quote(self, text):
        return f"<blockquote>{text}</blockquote>\n"

    def thematic_break(self):
        return "\n---\n\n"

    def linebreak(self):
        return "\n"

    def table(self, text):
        return text + "\n"

    def table_head(self, text):
        return text + "—————————————\n"

    def table_body(self, text):
        return text

    def table_row(self, text):
        return text.strip(" |") + "\n"

    def table_cell(self, text, align=None, is_head=False):
        if is_head:
            return f"<b>{text}</b> | "
        return f"{text} | "


md = mistune.create_markdown(
    renderer=TelegramRenderer(escape=False),
    plugins=["strikethrough", "table", "url"],
)


def markdown_to_telegram_html(text: str) -> str:
    try:
        result = md(text)
        return result.strip() if result else ""
    except Exception:
        logger.exception("Markdown conversion failed")
        return html.escape(text)


def split_message(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 4:
            cut = limit
        chunk = text[:cut]
        open_pre = chunk.count("<pre>") - chunk.count("</pre>")
        if open_pre > 0:
            chunk += "</pre>"
            text = "<pre>" + text[cut:].lstrip("\n")
        else:
            text = text[cut:].lstrip("\n")
        parts.append(chunk)
    return parts


async def send_long_message(message: Message, text: str):
    for part in split_message(text):
        if not part.strip():
            continue
        try:
            await message.answer(part, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send with HTML, retrying plain")
            await message.answer(part)


# ── Streaming Claude runner ───────────────────────────────────────────────────

async def keep_typing(bot: Bot, chat_id: int, stop_event: asyncio.Event):
    """Send typing action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break


def format_tool_notification(tool_name: str, tool_input: dict) -> str | None:
    """Return a short human-readable line for significant tool calls only.

    Read / Glob / Grep are micro-steps — not shown to user.
    Bash / Write / Edit / WebFetch / WebSearch are major actions — shown.
    """
    try:
        if tool_name == "Bash":
            desc = tool_input.get("description", "")
            if desc:
                return f"🔧 {html.escape(desc)}"
            cmd = tool_input.get("command", "").split("\n")[0][:120]
            return f"🔧 <code>{html.escape(cmd)}</code>"
        if tool_name == "WebFetch":
            url = tool_input.get("url", "")[:100]
            return f"🌐 {html.escape(url)}"
        if tool_name == "WebSearch":
            q = tool_input.get("query", "")[:100]
            return f"🔍 {html.escape(q)}"
        if tool_name == "Write":
            path = str(tool_input.get("file_path", ""))
            name = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"📝 Создаю: {html.escape(name)}"
        if tool_name == "Edit":
            path = str(tool_input.get("file_path", ""))
            name = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"✏️ Редактирую: {html.escape(name)}"
        # Read, Glob, Grep — micro-steps, skip
    except Exception:
        pass
    return None


def _get_media_type(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")


def _build_image_stdin(prompt: str, files: list[str]) -> bytes:
    """Build a stream-json stdin message with base64-encoded images + text prompt.

    Format: {"type":"user","message":{"role":"user","content":[image_blocks..., text_block]}}
    This is the native Claude CLI stream-json input format — no --file/file_id needed.
    """
    content = []
    for path in files:
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _get_media_type(path),
                    "data": data,
                },
            })
        except Exception:
            logger.exception(f"Failed to encode image {path}")
    if prompt:
        content.append({"type": "text", "text": prompt})
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(msg) + "\n").encode()


async def run_claude_streaming(
    prompt: str,
    session_id: str | None,
    reply_msg: Message,
    bot: Bot,
    files: list[str] | None = None,
    _is_retry: bool = False,
    lang: str = "Russian",
) -> tuple[str, str | None]:
    """
    Run Claude CLI with --output-format stream-json.

    When files (images) are provided, uses --input-format stream-json to pass
    images as base64 content blocks via stdin — no --file / file_id needed.

    As Claude works, sends tool-call notifications to Telegram (small messages).
    Returns (final_result_text, new_session_id) when done.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # Read model config per-request (allows hot-switching without restart)
    _mcfg_path = Path(__file__).parent / "model_config.json"
    try:
        _mcfg = json.loads(_mcfg_path.read_text())
        _current_model = _mcfg.get("model", CLAUDE_MODEL)
    except Exception:
        _current_model = CLAUDE_MODEL
    logger.info(f"Model config: {_current_model}")

    use_stream_input = bool(files)

    if use_stream_input:
        # Image mode: pass prompt + images via stdin as stream-json content blocks
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", _current_model,
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch",
            "--append-system-prompt", f"IMPORTANT: The user's message is in {lang}. You MUST reply in {lang}.",
        ]
    else:
        # Text mode: pass prompt as positional argument (existing behaviour)
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", _current_model,
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch",
            "--append-system-prompt", f"IMPORTANT: The user's message is in {lang}. You MUST reply in {lang}.",
        ]

    if session_id:
        cmd.extend(["--resume", session_id])

    logger.info(
        f"Claude streaming: model={_current_model}, images={len(files) if files else 0}, "
        f"resume={session_id is not None}, retry={_is_retry}"
    )

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, reply_msg.chat.id, stop_typing))

    final_result = ""
    new_session_id = None
    last_notif_t = 0.0

    # Heartbeat: notify user every 2 min of silence so they know Claude is still working
    _hb_event = asyncio.Event()  # set each time a line arrives to reset the timer

    async def _heartbeat():
        total_secs = 0
        while True:
            _hb_event.clear()
            try:
                await asyncio.wait_for(_hb_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                total_secs += 300
                mins = total_secs // 60
                try:
                    await reply_msg.answer(f"⏳ Ещё работаю... ({mins} мин)")
                except Exception:
                    pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if use_stream_input else None,
            env=env,
            cwd=str(Path.home() / "lil_worker"),  # bot's own dir — only its CLAUDE.md is loaded
        )

        # Write image content to stdin and close it
        if use_stream_input:
            stdin_data = _build_image_stdin(prompt, files)
            logger.info(f"Sending stdin: {len(stdin_data)} bytes ({len(files)} images)")
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()

        # Read without buffer-size limits: accumulate chunks, split on \n manually.
        # asyncio.StreamReader.readline() has a 64KB limit — crashes on long JSON lines.
        _buf = b""

        async def _next_line() -> bytes:
            nonlocal _buf
            while b"\n" not in _buf:
                chunk = await proc.stdout.read(65536)
                if not chunk:  # EOF
                    result = _buf
                    _buf = b""
                    return result
                _buf += chunk
            idx = _buf.index(b"\n")
            result = _buf[:idx]
            _buf = _buf[idx + 1:]
            return result

        # Instead of a hard timeout that kills legitimate long requests,
        # poll in 30s intervals checking if the process is still alive.
        # Heartbeat messages keep the user informed.
        # Absolute safety net: 30 min (1800s).
        _stream_start = time.monotonic()
        _ABSOLUTE_TIMEOUT = 1800  # 30 min safety net
        _POLL_INTERVAL = 30       # check process every 30s

        while True:
            try:
                line_bytes = await asyncio.wait_for(_next_line(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                # No output for 30s — check if Claude process is still alive
                if proc.returncode is not None:
                    logger.error(f"Claude process died (rc={proc.returncode})")
                    final_result = "Claude process unexpectedly stopped."
                    break
                elapsed = time.monotonic() - _stream_start
                if elapsed > _ABSOLUTE_TIMEOUT:
                    logger.error(f"Claude absolute timeout ({_ABSOLUTE_TIMEOUT}s)")
                    proc.kill()
                    await proc.communicate()
                    final_result = "⏱ Timeout: Claude не ответил за 30 минут."
                    break
                # Process alive, under limit — keep waiting
                continue

            _hb_event.set()  # reset heartbeat timer — Claude is alive

            if not line_bytes:
                break  # EOF

            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON line: {line[:80]}")
                continue

            etype = event.get("type")

            if etype == "assistant":
                content = event.get("message", {}).get("content", [])
                has_tool_use = any(b.get("type") == "tool_use" for b in content)
                summary_sent_this_turn = False

                for block in content:
                    btype = block.get("type")

                    if btype == "text" and has_tool_use:
                        if not summary_sent_this_turn:
                            text_chunk = block.get("text", "").strip()
                            if text_chunk:
                                now = time.monotonic()
                                if now - last_notif_t > 0.3:
                                    try:
                                        html_chunk = markdown_to_telegram_html(text_chunk)
                                        await reply_msg.answer(html_chunk, parse_mode="HTML")
                                    except Exception:
                                        await reply_msg.answer(text_chunk)
                                    last_notif_t = time.monotonic()
                                    summary_sent_this_turn = True

                    elif btype == "tool_use":
                        notif = format_tool_notification(
                            block.get("name", ""), block.get("input", {})
                        )
                        if notif:
                            now = time.monotonic()
                            if now - last_notif_t > 0.3:
                                try:
                                    await reply_msg.answer(notif, parse_mode="HTML")
                                except Exception:
                                    await reply_msg.answer(notif)
                                last_notif_t = time.monotonic()

            elif etype == "result":
                final_result = event.get("result", "")
                new_session_id = event.get("session_id")
                is_error = event.get("is_error", False)
                logger.info(
                    f"Stream result: {len(final_result)} chars, "
                    f"session={new_session_id}, error={is_error}"
                )

        await proc.wait()
        stderr_data = await proc.stderr.read()
        if stderr_data.strip():
            logger.warning(f"Claude stderr: {stderr_data.decode(errors='replace')[:500]}")

    except Exception:
        logger.exception("Error in Claude streaming")
        final_result = final_result or "Ошибка при запуске Claude."
    finally:
        stop_typing.set()
        typing_task.cancel()
        heartbeat_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    # If we got nothing and had a session — retry fresh (stale session)
    if not final_result and session_id and not _is_retry:
        logger.warning("Empty result with session → retrying without session")
        return await run_claude_streaming(
            prompt, None, reply_msg, bot, files, _is_retry=True
        )

    return final_result or "No response", new_session_id


# ── Router & Handlers ─────────────────────────────────────────────────────────

router = Router()


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


@router.message(Command("new"))
async def cmd_new(message: Message):
    if not is_allowed(message.from_user.id):
        return
    sessions = load_sessions()
    sessions.pop(str(message.from_user.id), None)
    save_sessions(sessions)
    await message.answer("🗑 Session cleared. Next message starts fresh.")


@router.message(Command("token"))
async def cmd_token(message: Message):
    """Generate a one-time access link to a private service.
    Configure via .env: TOKEN_SERVICE_URL and TOKEN_FILE.
    """
    if not is_allowed(message.from_user.id):
        return
    if not TOKEN_SERVICE_URL or not TOKEN_FILE:
        await message.answer("Token service not configured. Set TOKEN_SERVICE_URL and TOKEN_FILE in .env")
        return
    tokens_file = Path(TOKEN_FILE)
    if not tokens_file.exists():
        await message.answer("Token pool file not found.")
        return
    data = json.loads(tokens_file.read_text())
    available = data.get("available", [])
    if not available:
        await message.answer("No tokens left. Generate more.")
        return
    token = available.pop(0)
    data["available"] = available
    data.setdefault("issued", []).append(token)
    tokens_file.write_text(json.dumps(data, indent=2))
    link = f"{TOKEN_SERVICE_URL}{token}"
    remaining = len(available)
    await message.answer(
        f"<code>{link}</code>\n\nLeft: {remaining}",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    sessions = load_sessions()
    has_session = str(message.from_user.id) in sessions
    await message.answer(
        f"🤖 <b>lil_worker status</b>\n"
        f"User ID: <code>{message.from_user.id}</code>\n"
        f"Model: <code>{CLAUDE_MODEL}</code>\n"
        f"Streaming: <code>ON</code>\n"
        f"Session: {'✅ active' if has_session else '❌ none'}",
        parse_mode="HTML",
    )


async def _flush_photo_buffer(user_id: int, bot: Bot):
    """Called after PHOTO_DEBOUNCE_DELAY — downloads all buffered photos and sends to Claude."""
    await asyncio.sleep(PHOTO_DEBOUNCE_DELAY)

    buf = _photo_buffer.pop(user_id, None)
    if not buf:
        return

    reply_msg: Message = buf["reply_msg"]
    caption = buf["caption"]
    photo_ids: list[str] = buf["photos"]
    tmp_paths: list[str] = []

    # Download all photos
    for file_id in photo_ids:
        try:
            file = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                await bot.download_file(file.file_path, tmp.name)
                tmp_paths.append(tmp.name)
        except Exception:
            logger.exception(f"Failed to download photo {file_id}")

    if not tmp_paths:
        await reply_msg.answer("❌ Не вдалося завантажити фото.")
        return

    count = len(tmp_paths)
    logger.info(f"PHOTO BATCH uid={user_id}, count={count}, caption={caption[:60]!r}")

    if count == 1:
        await reply_msg.answer("📷 Отримав фото, обробляю...")
    else:
        await reply_msg.answer(f"📷 Отримав {count} фото, обробляю...")

    sessions = load_sessions()
    session_id = sessions.get(str(user_id))

    if count == 1:
        prompt = f"I'm sending you an image. {caption}"
    else:
        prompt = f"I'm sending you {count} images at once. {caption}"

    lang = detect_language(caption) if caption != "Describe this image." else "Russian"

    response, new_session_id = await run_claude_streaming(
        prompt, session_id, reply_msg, bot, files=tmp_paths, lang=lang
    )

    # Cleanup temp files
    for p in tmp_paths:
        try:
            os.unlink(p)
        except OSError:
            pass

    if new_session_id:
        sessions[str(user_id)] = new_session_id
        save_sessions(sessions)
    elif session_id and not new_session_id:
        sessions.pop(str(user_id), None)
        save_sessions(sessions)

    cleaned_response, voice_blocks = extract_voice_blocks(response)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        await send_long_message(reply_msg, response_html)

    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(reply_msg, bot, vb_text, vb_lang, user_id)


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        await message.answer("Not authorized.")
        return

    user_id = message.from_user.id
    photo = message.photo[-1]  # highest resolution

    # If part of an album (media_group) or standalone — buffer either way
    # This ensures consistent handling and catches rapid single photos too
    if user_id in _photo_buffer:
        # Add to existing buffer
        _photo_buffer[user_id]["task"].cancel()
        _photo_buffer[user_id]["photos"].append(photo.file_id)
        # Caption from first photo with a caption wins
        if message.caption and _photo_buffer[user_id]["caption"] == "Describe this image.":
            _photo_buffer[user_id]["caption"] = message.caption
        logger.info(f"PHOTO uid={user_id} buffered #{len(_photo_buffer[user_id]['photos'])}")
    else:
        _photo_buffer[user_id] = {
            "photos": [photo.file_id],
            "caption": message.caption or "Describe this image.",
            "reply_msg": message,
        }
        logger.info(f"PHOTO uid={user_id} first in buffer")

    task = asyncio.create_task(_flush_photo_buffer(user_id, bot))
    _photo_buffer[user_id]["task"] = task


@router.message((F.voice | F.audio | F.document) & F.caption.startswith("/saveasset"))
async def handle_saveasset(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        return
    parts = (message.caption or "").strip().split()
    if len(parts) < 2:
        await message.answer("Usage: send file with caption /saveasset filename.ogg")
        return
    filename = parts[1]
    if "/" in filename or ".." in filename:
        await message.answer("Invalid filename.")
        return
    save_path = Path(f"/opt/test_bot/assets/{filename}")
    file_obj = message.voice or message.audio or message.document
    tg_file = await bot.get_file(file_obj.file_id)
    await bot.download_file(tg_file.file_path, destination=str(save_path))
    await message.answer(f"Saved: assets/{filename}")


@router.message(F.voice | F.audio)
async def handle_voice(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Not authorized.")
        return

    if not OPENAI_API_KEY:
        await message.answer("❌ OPENAI_API_KEY not configured.")
        return

    voice = message.voice or message.audio
    file = await bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await bot.download_file(file.file_path, tmp.name)
        tmp_path = tmp.name

    try:
        _tcfg_path = Path(__file__).parent / "transcribe_config.json"
        try:
            _tcfg = json.loads(_tcfg_path.read_text())
        except Exception:
            _tcfg = {}
        _tr_language = _tcfg.get("language")
        _tr_temperature = _tcfg.get("temperature", 0.2)
        logger.info(f"Transcribe config: language={_tr_language}, temperature={_tr_temperature}")

        _tr_kwargs = dict(
            model=OPENAI_VOICE_MODEL,
            file=None,
            prompt="The speaker uses Ukrainian, Russian, or English ONLY. Never output other languages.",
            temperature=_tr_temperature,
        )
        if _tr_language:
            _tr_kwargs["language"] = _tr_language

        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio_file:
            _tr_kwargs["file"] = audio_file
            transcription = await client.audio.transcriptions.create(**_tr_kwargs)
        text = transcription.text.strip()
        logger.info(f"VOICE uid={user_id}, transcribed: {text[:120]!r}")
    except Exception:
        logger.exception("Voice transcription failed")
        await message.answer("❌ Ошибка транскрипции голосового.")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not text:
        await message.answer("❌ Не удалось распознать речь.")
        return

    sessions = load_sessions()
    session_id = sessions.get(str(user_id))
    lang = detect_language(text)
    logger.info(f"Detected language (voice): {lang}")

    response, new_session_id = await run_claude_streaming(
        text, session_id, message, bot, lang=lang
    )

    if new_session_id:
        sessions[str(user_id)] = new_session_id
        save_sessions(sessions)
    elif session_id and not new_session_id:
        sessions.pop(str(user_id), None)
        save_sessions(sessions)

    cleaned_response, voice_blocks = extract_voice_blocks(response)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        await send_long_message(message, response_html)

    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(message, bot, vb_text, vb_lang, user_id)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_message(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Not authorized.")
        return

    text = message.text
    if not text:
        return

    if user_id in _msg_buffer:
        _msg_buffer[user_id]["task"].cancel()
        _msg_buffer[user_id]["parts"].append(text)
        logger.info(f"MSG uid={user_id} buffered part #{len(_msg_buffer[user_id]['parts'])}")
    else:
        _msg_buffer[user_id] = {
            "parts": [text],
            "reply_msg": message,
        }

    task = asyncio.create_task(_flush_buffer(user_id, bot))
    _msg_buffer[user_id]["task"] = task


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN in .env")
        return

    # Write own PID file — reliable even when run.sh gets killed mid-restart
    pid_file = Path(__file__).parent / "lil_worker.pid"
    pid_file.write_text(str(os.getpid()))

    print(f"Starting lil_worker bot (model: {CLAUDE_MODEL}, streaming: ON)...")
    print(f"Allowed users: {ALLOWED_USERS or 'Everyone (WARNING: set ALLOWED_USERS!)'}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    print("Bot running. Send a message on Telegram.")

    # Notify users that the bot has started (confirms restart completed successfully)
    # Check for restart reason file — Claude writes it before calling run.sh restart
    restart_reason_file = Path(__file__).parent / "restart_reason.txt"
    startup_msg = "✅ Бот запущен."
    if restart_reason_file.exists():
        try:
            reason = restart_reason_file.read_text().strip()
            if reason:
                startup_msg = f"{reason}\n\n✅ Бот запущен."
            restart_reason_file.unlink()
        except Exception:
            pass
    for uid in ALLOWED_USERS:
        try:
            await bot.send_message(uid, startup_msg)
        except Exception:
            pass

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
