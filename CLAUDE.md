# lil_worker - Telegram -> Claude bridge

## How it works

User sends a Telegram message -> bot.py calls `claude -p` CLI -> Claude responds -> answer goes back to Telegram.

Running as OS user on a VPS (Ubuntu). Model configured via `model_config.json`.

## Files

All bot files are at: `bot/`

| File | Purpose |
|------|---------|
| bot.py | Telegram bot + Claude bridge |
| .env | Config: bot token, allowed users, model |
| run.sh | Process manager: start / stop / restart / status |
| watchdog.sh | Crash recovery: checks bot every 5 min, restarts if dead |
| validate.sh | Pre-restart validation (syntax, imports, dry-run) |
| .sessions.json | Conversation session IDs per user |
| requirements.txt | Python dependencies |
| .venv/ | Python virtual environment |
| model_config.json | Current Claude model |
| transcribe_config.json | Transcription language settings |

## Commands that must NEVER be run

These hang forever and will freeze the bot:
- `run.sh logs` - internally runs `tail -f`, never exits
- `tail -f <anything>` - infinite stream
- `top`, `htop`, `watch`, any interactive/live command
- `less`, `more`, `man`, `nano`, `vim` - interactive pagers/editors
- Any command that requires keyboard input to exit

To check logs: `tail -n 50 bot/lil_worker.log`
To check status: `bot/run.sh status`

## Timeout rule - MANDATORY

Always wrap potentially slow Bash commands with `timeout`:
```
timeout 30 <command>   # for most operations
timeout 10 <command>   # for quick checks
timeout 60 <command>   # for installs/compiles
```

Never retry the same failing action more than once. If something fails twice - stop, explain, ask the user.

## Self-modification

To add features or fix bugs in bot.py:
1. Edit `bot/bot.py`
2. Install dependencies: `bot/.venv/bin/pip install ...`
3. **Run validation** - MANDATORY before restart:
   - Light changes (new function, config, text): `cd bot && ./validate.sh`
   - Heavy changes (streaming, handlers, asyncio, renderer): `cd bot && ./validate.sh --deep`
   - If validation FAILS - do NOT restart, fix or rollback, report to user
4. Output final confirmation text to user (becomes Telegram message immediately)
5. Write restart reason to `bot/restart_reason.txt` (1-3 lines, shown in startup message)
6. Restart: `bot/run.sh restart`

Restart MUST come last - `run.sh restart` kills the current process. If bot doesn't come back and backup exists: `cp bot/bot.py.bak bot/bot.py && bot/run.sh restart`

## Model switching

Edit `bot/model_config.json` - takes effect on next message, no restart needed:
- `{"model": "sonnet"}` - claude-sonnet-4-6 (default, fast)
- `{"model": "opus"}` - claude-opus-4-6 (smartest, slower)
- `{"model": "haiku"}` - claude-haiku-4-5 (fastest, cheapest)

Quick commands - if user's entire message is one of these words, switch immediately:
- `opus` - switch to opus
- `sonnet` - switch to sonnet
- `haiku` - switch to haiku

## Transcription language

Edit `bot/transcribe_config.json`:
- `{"language": null, "temperature": 0.2}` - auto-detect
- `{"language": "uk", "temperature": 0.1}` - fixed Ukrainian
- `{"language": "ru", "temperature": 0.1}` - fixed Russian
- `{"language": "en", "temperature": 0.1}` - fixed English

No restart needed.

## Language rule

Always respond in the same language the user used in their message.
- User writes in Ukrainian - respond in Ukrainian
- User writes in Russian - respond in Russian
- User writes in English - respond in English

## Communication style

**First message**: before calling any tools, output a short 1-2 sentence summary of what you understood. This goes to the user immediately.

Example: "Got it: you want to add a /help command. Working on it."

**Between tool calls**: output NO text. No "Checking...", no "Interesting...". Just call the next tool silently.

**Final answer**: after all tools complete, write the full answer.

## Voice messages - CRITICAL

Bot supports sending voice messages via `[VOICE lang="xx"]text[/VOICE]` markers.

**NEVER generate a `[VOICE]` block unless the user EXPLICITLY asks for a voice message in their current message.**

Explicit triggers only:
- "send a voice message"
- "reply with voice"
- "text and voice"
- "голосовым" / "голосове"

If the user did NOT mention voice in their request - do NOT add `[VOICE]` blocks. Ever.

Format: `[VOICE lang="uk"]Text to speak[/VOICE]`
- Place at the END of response, after all text
- Only ONE voice block per response
- Keep text inside concise, no markdown

## Formatting rules - Telegram

My text gets converted: Markdown -> Telegram HTML -> split at 4000 chars -> sent.

**Supported tags**: `<b>`, `<i>`, `<code>`, `<pre>`, `<s>`, `<a>`, `<blockquote>`

**Rules:**
- No markdown tables (`| col |`) - Telegram doesn't render them, use bullet lists instead
- No long code blocks (` ``` `) - if longer than ~2000 chars it breaks message splitting
- Code blocks only for short actual code snippets
- For long structured content (reports, lists, instructions) - use **bold** headers + plain text
- No raw HTML tags in responses - write Markdown, renderer converts it

## Always confirm task completion

After completing any task (with or without restart), always end with a clear final message:
- What was done (briefly)
- Whether it's working / ready to use

Never go silent after the last tool call.
