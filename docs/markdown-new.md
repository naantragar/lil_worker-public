# markdown-new — Docs

## TL;DR

Convert any public URL to clean Markdown — much less tokens than raw HTML.

Command: `python3 ~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py '<URL>'`
Methods: `--method auto` (default), `ai`, `browser` (for SPA/JS pages)
Public HTTPS only. No login required. No tokens/secrets in URL.
Save to file: add `--output /tmp/result.md`

No API key needed. Free, 500 requests/day/IP.

## Script location

`~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py`

No installation needed — uses only Python standard library.

## Command

```
python3 ~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py '<URL>'
```

## Parameters

- `--method auto|ai|browser` — conversion method (auto by default)
- `--output <file>` — save result to file
- `--deliver-md` — wrap in `<url>...</url>` tags, useful for long content
- `--retain-images` — keep images (omit for summaries, use for diagrams/instructions)
- `--timeout 45` — request timeout in seconds

## Examples

Read an article:
```
python3 ~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py 'https://example.com/article'
```

JS/SPA page:
```
python3 ~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py 'https://example.com' --method browser
```

Long doc, save to file:
```
python3 ~/lil_worker/skills/markdown-new/scripts/markdown_new_fetch.py 'https://docs.example.com' --output /tmp/docs.md --deliver-md
```

## Typical use cases

- "Read the docs for X and explain" -> yes
- "Article / GitHub README / public documentation" -> yes
- "Personal account / internal URL / 192.168.x.x" -> no (see policy)
- "Link with ?token= / ?signature=" -> no (see policy)
- "Legal text, need exact wording" -> no (see policy)
