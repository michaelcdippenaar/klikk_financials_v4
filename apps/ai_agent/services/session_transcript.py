from __future__ import annotations

from typing import Any

from apps.ai_agent.models import AgentMessage, AgentToolExecutionLog, AgentSession


def build_session_transcript_markdown(
    *,
    session: AgentSession,
    include_tool_executions: bool = True,
    max_messages: int = 500,
    max_chars: int = 250_000,
) -> str:
    """
    Create a markdown transcript of a session that can be stored as a SystemDocument.
    """
    title = session.title or f'Chat {session.id}'
    org = None
    if session.organisation:
        org = f'{session.organisation.tenant_name} ({session.organisation.tenant_id})'

    lines: list[str] = []
    lines.append(f'# Chat transcript: {title}')
    lines.append('')
    lines.append(f'- **Session ID**: `{session.id}`')
    lines.append(f'- **Project**: `{session.project.slug}`' if session.project else '- **Project**: (none)')
    lines.append(f'- **Tenant**: `{org}`' if org else '- **Tenant**: (none)')
    lines.append('')

    messages = list(session.messages.all().order_by('id')[:max_messages])
    lines.append('## Messages')
    lines.append('')

    for m in messages:
        role = m.role
        lines.append(f'### {role} (id={m.id})')
        lines.append('')
        content = m.content or ''
        lines.append(content)
        lines.append('')

    if include_tool_executions:
        lines.append('## Tool executions')
        lines.append('')
        logs = list(session.tool_executions.all().order_by('started_at')[:200])
        for log in logs:
            lines.append(f'### {log.tool_name} ({log.status})')
            lines.append('')
            lines.append('**Input:**')
            lines.append('```json')
            lines.append(_safe_json(log.input_payload))
            lines.append('```')
            lines.append('')
            if log.output_payload:
                lines.append('**Output:**')
                lines.append('```json')
                lines.append(_safe_json(log.output_payload))
                lines.append('```')
                lines.append('')
            if log.error_message:
                lines.append(f'**Error:** `{log.error_message}`')
                lines.append('')

    out = '\n'.join(lines).strip() + '\n'
    if len(out) > max_chars:
        out = out[:max_chars] + '\n... (truncated)\n'
    return out


def _safe_json(payload: Any) -> str:
    try:
        import json
        return json.dumps(payload or {}, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception:
        return '{}'

