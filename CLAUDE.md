# lil_worker - Telegram -> Claude bridge

## How it works

User sends a Telegram message -> bot.py calls `claude -p` CLI -> Claude responds -> answer goes back to Telegram.

## Files

All bot files are at: `bot/`

| File | Purpose |
|------|---------|
| bot.py | Telegram bot + Claude bridge |
| .env | Config: bot token, allowed users, model |
| run.sh | Process manager: start / stop / restart / status |
| .sessions.json | Conversation session IDs per user |
| requirements.txt | Python dependencies |
| .venv/ | Python virtual environment |
| model_config.json | Current Claude model |
| transcribe_config.json | Transcription language settings |

## Commands that must NEVER be run

These hang forever:
- `run.sh logs` - internally runs `tail -f`, never exits
- `tail -f <anything>` - infinite stream
- `top`, `htop`, `watch`, any interactive command
- `less`, `more`, `man`, `nano`, `vim` - interactive pagers/editors

To check logs: `tail -n 50 bot/lil_worker.log`
To check status: `bot/run.sh status`

## Self-modification

To add features:
1. Edit `bot/bot.py`
2. Install dependencies with `bot/.venv/bin/pip install ...`
3. Output confirmation text to user
4. Restart: `bot/run.sh restart`

Restart MUST come last - it kills the current process.

## Model switching

Edit `bot/model_config.json` - takes effect on next message, no restart needed:
- `{"model": "sonnet"}` - claude-sonnet-4-6
- `{"model": "opus"}` - claude-opus-4-6
- `{"model": "haiku"}` - claude-haiku-4-5

## Transcription language

Edit `bot/transcribe_config.json`:
- `{"language": null, "temperature": 0.2}` - auto-detect
- `{"language": "uk", "temperature": 0.1}` - fixed language

## Language rule

Always respond in the same language the user used in their message.
