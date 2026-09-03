"""
Outbound webhook for new comments, with a delivery log.

Setting ``COMMENT_WEBHOOK_URL`` (env) turns it on; empty is the default and
means nothing is POSTed anywhere. ``COMMENT_WEBHOOK_SECRET``, when set, signs
the body with HMAC-SHA256 in ``X-Klikk-Signature: sha256=<hex>``.

Two rules this module exists to enforce:

1. **A webhook failure must never fail the comment.** The whole thing runs
   inside ``transaction.on_commit`` (so nothing fires for a POST that rolls
   back) and every exception is caught. Someone leaving a comment on a receipt
   does not care that a downstream endpoint is down, and losing their comment
   over it would be indefensible.
2. **Every attempt is recorded**, success or failure, in
   ``CommentWebhookDelivery`` — including the case where the request never
   completed. A webhook that silently stopped delivering is precisely what the
   log is for.

Fire-and-forget and SYNCHRONOUS-after-commit: the request thread pays up to
``COMMENT_WEBHOOK_TIMEOUT`` seconds. That is deliberate — Celery is available
but a comment webhook is not worth a queue round trip, and the timeout bounds
the cost. If the endpoint turns out to be slow in practice, move the
``_deliver`` call onto a task; the delivery log makes that visible.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import transaction

from .comment_events import (
    CUBE_COMMENT, FINDING, RECEIPT, comment_payload, console_url, object_ref,
)

logger = logging.getLogger(__name__)


def _config():
    url = (getattr(settings, 'COMMENT_WEBHOOK_URL', '') or '').strip()
    secret = (getattr(settings, 'COMMENT_WEBHOOK_SECRET', '') or '').strip()
    timeout = float(getattr(settings, 'COMMENT_WEBHOOK_TIMEOUT', 5) or 5)
    return url, secret, timeout


def build_payload(kind: str, comment, target, object_id: str | None = None) -> dict:
    """The JSON body.

    ``target`` is a sha256 (receipts), a finding instance (findings), or a label
    string (cube comments). A cube reply's OBJECT is the comment it hangs off,
    and that id cannot be read back out of a label — so the caller passes
    ``object_id`` explicitly there rather than this function guessing.
    """
    if object_id is None:
        object_id = str(target if kind == RECEIPT else target.pk)
    return {
        'event': 'comment.created',
        'kind': kind,
        'object_id': object_id,
        'object_ref': object_ref(kind, target),
        'comment': comment_payload(comment),
        'url': console_url(kind, object_id),
    }


def sign(body: bytes, secret: str) -> str:
    return 'sha256=' + hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _log(kind, comment_id, author, url, *, status_code=None, error='', snippet=''):
    from .models import CommentWebhookDelivery

    try:
        CommentWebhookDelivery.objects.create(
            comment_kind=kind,
            comment_id=comment_id,
            author=author or '',
            target_url=url[:500],
            status_code=status_code,
            error=(error or '')[:1000],
            response_snippet=(snippet or '')[:CommentWebhookDelivery.SNIPPET_MAX],
        )
    except Exception:  # noqa: BLE001 — the log must not be able to break the app either
        logger.exception('Could not write CommentWebhookDelivery for %s#%s', kind, comment_id)


def _deliver(kind: str, comment_id: int, author: str, payload: dict) -> None:
    import requests

    url, secret, timeout = _config()
    body = json.dumps(payload).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if secret:
        headers['X-Klikk-Signature'] = sign(body, secret)
    try:
        response = requests.post(url, data=body, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — connection/timeout/DNS all land here
        logger.warning('Comment webhook POST failed for %s#%s: %s', kind, comment_id, exc)
        _log(kind, comment_id, author, url, error=f'{type(exc).__name__}: {exc}')
        return
    snippet = ''
    try:
        snippet = (response.text or '')[:1000]
    except Exception:  # noqa: BLE001 — a body that will not decode is not fatal
        snippet = '<unreadable response body>'
    _log(kind, comment_id, author, url, status_code=response.status_code, snippet=snippet)


def notify_comment_created(kind: str, comment, target=None, object_id: str | None = None) -> None:
    """Queue a webhook for one new comment. Never raises.

    Call from inside the same transaction as the comment write — delivery is
    deferred to ``on_commit`` so nothing fires for a POST that rolls back.

    ``comment`` is a model instance (receipts, findings) or a dict (a cube reply,
    which is raw SQL); ``comment_payload`` handles both.
    """
    try:
        url, _secret, _timeout = _config()
        if not url:
            return
        if target is None and kind != CUBE_COMMENT:
            target = comment.sha256 if kind == RECEIPT else comment.finding
        payload = build_payload(kind, comment, target, object_id)
        body = payload['comment']
        comment_id, author = body['id'], body['author']
        transaction.on_commit(lambda: _deliver(kind, comment_id, author, payload))
    except Exception:  # noqa: BLE001 — a comment must never fail over its webhook
        logger.exception('Could not queue comment webhook for %s comment', kind)


def notify_comments_created(kind: str, comments, targets=None) -> None:
    """Bulk fan-out (bulk-comment endpoints). One webhook per comment.

    ``targets`` maps comment -> target when the caller already has them; without
    it each comment resolves its own, which is one extra query per comment on a
    path that is already doing N writes.
    """
    for comment in comments:
        target = None
        if targets is not None:
            target = targets.get(comment.id) if hasattr(targets, 'get') else None
        notify_comment_created(kind, comment, target)


__all__ = [
    'CUBE_COMMENT',
    'FINDING',
    'RECEIPT',
    'build_payload',
    'notify_comment_created',
    'notify_comments_created',
    'sign',
]
