from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImportResult:
    transcript_path: str
    redacted_text: str
    redacted_char_count: int
    summary: str
    summary_char_count: int
    redaction_counts: dict[str, int]


_OPENAI_KEY_RE = re.compile(r'\bsk-[A-Za-z0-9_-]{10,}\b')
_OPENAI_PROJ_KEY_RE = re.compile(r'\bsk-proj-[A-Za-z0-9_-]{10,}\b')
_GOOGLE_API_KEY_RE = re.compile(r'\bAIza[0-9A-Za-z\-_]{20,}\b')
_PASSWORD_INLINE_RE = re.compile(r'(?i)\b(password|pass)\s*[:=]\s*([^\s,;]+)')
_BEARER_RE = re.compile(r'(?i)\bBearer\s+([A-Za-z0-9\-_\.=]{20,})')


def _project_tokens(project_name: str) -> list[str]:
    base = (project_name or '').strip()
    if not base:
        return []
    return list({base, base.replace('_', '-'), base.replace('-', '_')})


def find_latest_cursor_transcript(*, project_name: str | None = None) -> str | None:
    """
    Best-effort lookup of latest Cursor agent transcript under ~/.cursor/projects/**/agent-transcripts/*.txt.
    Prefers paths containing the current project name (with _/- variants) if provided.
    """
    home = Path.home()
    base_dir = home / '.cursor' / 'projects'
    if not base_dir.exists():
        return None

    candidates: list[Path] = []
    tokens = _project_tokens(project_name or '')

    # Avoid deep expensive walks: projects dir is typically small.
    for p in base_dir.glob('**/agent-transcripts/*.txt'):
        if p.is_file():
            candidates.append(p)

    if not candidates:
        return None

    if tokens:
        preferred = [p for p in candidates if any(t in str(p) for t in tokens)]
        if preferred:
            candidates = preferred

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def _apply_redactions(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {
        'openai_key': 0,
        'google_api_key': 0,
        'password': 0,
        'bearer': 0,
    }

    def sub_count(pattern: re.Pattern, repl: str, key: str, s: str) -> str:
        new_s, n = pattern.subn(repl, s)
        counts[key] += n
        return new_s

    out = text or ''
    out = sub_count(_OPENAI_PROJ_KEY_RE, 'sk-proj-[REDACTED]', 'openai_key', out)
    out = sub_count(_OPENAI_KEY_RE, 'sk-[REDACTED]', 'openai_key', out)
    out = sub_count(_GOOGLE_API_KEY_RE, 'AIza[REDACTED]', 'google_api_key', out)
    out = sub_count(_PASSWORD_INLINE_RE, r'\1: [REDACTED]', 'password', out)
    out = sub_count(_BEARER_RE, 'Bearer [REDACTED]', 'bearer', out)

    return out, counts


def _simple_summary(redacted_text: str, *, max_chars: int = 3500) -> str:
    """
    Deterministic summary: keeps last ~N dialogue lines and a short header.
    Avoids calling an LLM (so it works even when no quota).
    """
    text = (redacted_text or '').strip()
    if not text:
        return 'No transcript content.'

    lines = text.splitlines()
    # Prefer the most recent user/assistant lines if present.
    dialogue = [ln for ln in lines if ln.lower().startswith(('user:', 'assistant:'))]
    tail_source = dialogue if len(dialogue) >= 10 else lines
    tail = tail_source[-120:]
    body = '\n'.join(tail).strip()
    header = (
        'Cursor chat transcript imported (redacted). '
        'Below are the most recent lines for context.\n'
    )
    summary = header + '\n' + body
    if len(summary) > max_chars:
        summary = summary[:max_chars] + '\n... (truncated)'
    return summary


def import_cursor_chat_transcript(
    *,
    transcript_path: str | None,
    project_name: str | None = None,
    max_transcript_chars: int = 200_000,
    max_summary_chars: int = 3500,
) -> ImportResult:
    path = (transcript_path or '').strip()
    if not path:
        path = find_latest_cursor_transcript(project_name=project_name) or ''
    if not path:
        raise FileNotFoundError('No transcript_path provided and no Cursor transcripts could be found.')

    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f'Transcript file not found: {path}')

    raw = p.read_text(encoding='utf-8', errors='replace')
    if len(raw) > max_transcript_chars:
        raw = raw[:max_transcript_chars] + '\n... (truncated)'

    redacted, redaction_counts = _apply_redactions(raw)
    summary = _simple_summary(redacted, max_chars=max_summary_chars)

    return ImportResult(
        transcript_path=str(p),
        redacted_text=redacted,
        redacted_char_count=len(redacted),
        summary=summary,
        summary_char_count=len(summary),
        redaction_counts=redaction_counts,
    )

