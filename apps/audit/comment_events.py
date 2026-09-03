"""
Shared vocabulary for the comment surfaces: receipts (SlipComment), findings
(AuditFindingComment) and cube-comment replies (app.cube_comment_replies, raw
SQL — hence the dict tolerance below).

Both the live feed (comment_feed_views) and the outbound webhook
(comment_webhook) need the same three things about any comment:

  * ``kind``       — 'receipt' | 'finding' | 'cube_comment'
  * ``object_id``  — the sha256, the finding pk, or the cube COMMENT id (never
                     the reply id: the thread is the object, not the message)
  * ``object_ref`` — something a HUMAN recognises: "Makro · 2026-08-04 ·
                     R259.00", "FY26-001 Payments made before bill", or the
                     figure a cube comment is pinned to

They live here rather than in either app so the two surfaces cannot drift into
describing the same comment two different ways — a feed event and a webhook
payload for one comment must be recognisably the same thing.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import connection

logger = logging.getLogger(__name__)

RECEIPT = 'receipt'
FINDING = 'finding'
CUBE_COMMENT = 'cube_comment'


def _field(comment, name):
    """Attribute or key.

    Receipt and finding comments are Django models; a cube reply is a dict off a
    raw-SQL cursor. Both have to produce the SAME payload, and one accessor here
    is better than a second serialiser that can drift from this one.
    """
    if isinstance(comment, dict):
        return comment.get(name)
    return getattr(comment, name, None)


def comment_payload(comment) -> dict:
    """The comment body shared by feed events and webhook payloads."""
    created = _field(comment, 'created_at')
    return {
        'id': _field(comment, 'id'),
        'parent_id': _field(comment, 'parent_id'),
        'author': _field(comment, 'author') or '',
        'text': _field(comment, 'text') or '',
        'created_at': created.isoformat() if hasattr(created, 'isoformat') else (created or None),
    }


def finding_ref(finding) -> str:
    """'FY26-001 — Payments made before supplier bill captured'."""
    ref = (getattr(finding, 'ref', '') or '').strip()
    title = (getattr(finding, 'title', '') or '').strip()
    if ref and title:
        return f'{ref} — {title}'
    return ref or title or f'finding #{getattr(finding, "pk", "?")}'


def _money(raw) -> str:
    try:
        return f'R{Decimal(str(raw)):,.2f}'
    except (InvalidOperation, TypeError, ValueError):
        return ''


def receipt_ref(sha256: str) -> str:
    """'Makro · 2026-08-04 · R259.00', falling back to a short sha256.

    ``whatsapp.klikk_slips`` is an external table maintained by the WhatsApp
    sync — it is not a Django model and it does not exist in the test database.
    Every failure mode (missing table, missing row, malformed ocr) degrades to
    the sha256 prefix rather than raising: a human-readable label is a nicety,
    and it must never be the reason a comment fails to post or a poll 500s.
    """
    short = f'receipt {sha256[:12]}'
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT ocr->>'supplier', ocr->>'total', slip_ts
                FROM whatsapp.klikk_slips WHERE sha256 = %s
                """,
                [sha256],
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 — see docstring
        logger.debug('receipt_ref: could not read klikk_slips for %s', sha256[:12], exc_info=True)
        return short
    if not row:
        return short
    supplier, total, slip_ts = row
    parts = [
        (supplier or '').strip(),
        slip_ts.date().isoformat() if slip_ts else '',
        _money(total),
    ]
    label = ' · '.join(p for p in parts if p)
    return label or short


def object_ref(kind: str, target) -> str:
    """``target``: a sha256 for receipts, a finding instance for findings, and an
    already-resolved label string for cube comments.

    The cube label is computed at the storage layer
    (cube_comment_replies.subject_ref) because it needs the comment row that the
    caller has already fetched — resolving it again here would be a second query
    for a string we are holding.
    """
    if kind == RECEIPT:
        return receipt_ref(target)
    if kind == CUBE_COMMENT:
        return str(target or '')[:300]
    return finding_ref(target)


def console_url(kind: str, object_id: str) -> str:
    """Deep link into the console for the commented-on object."""
    from django.conf import settings

    base = getattr(settings, 'CONSOLE_BASE_URL', '').rstrip('/')
    if kind == RECEIPT:
        return f'{base}/app/pipeline/audit/receipts?sha256={object_id}'
    if kind == CUBE_COMMENT:
        return f'{base}/app/pipeline/audit/comments?comment={object_id}'
    return f'{base}/app/pipeline/audit/findings?finding={object_id}'
