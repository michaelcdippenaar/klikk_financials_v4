"""
Shared vocabulary for the two comment surfaces: receipts (SlipComment) and
findings (AuditFindingComment).

Both the live feed (comment_feed_views) and the outbound webhook
(comment_webhook) need the same three things about any comment:

  * ``kind``       — 'receipt' | 'finding'
  * ``object_id``  — the sha256 or the finding pk, as a string
  * ``object_ref`` — something a HUMAN recognises: "Makro · 2026-08-04 ·
                     R259.00" or "FY26-001 Payments made before bill"

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


def comment_payload(comment) -> dict:
    """The comment body shared by feed events and webhook payloads."""
    created = getattr(comment, 'created_at', None)
    return {
        'id': comment.id,
        'parent_id': comment.parent_id,
        'author': comment.author,
        'text': comment.text,
        'created_at': created.isoformat() if created else None,
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
    """``target`` is a sha256 string for receipts, a finding instance for findings."""
    return receipt_ref(target) if kind == RECEIPT else finding_ref(target)


def console_url(kind: str, object_id: str) -> str:
    """Deep link into the console for the commented-on object."""
    from django.conf import settings

    base = getattr(settings, 'CONSOLE_BASE_URL', '').rstrip('/')
    if kind == RECEIPT:
        return f'{base}/app/pipeline/audit/receipts?sha256={object_id}'
    return f'{base}/app/pipeline/audit/findings?finding={object_id}'
