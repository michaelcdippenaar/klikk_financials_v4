"""
The cube-comment register and its reply threads, on the AUDIT surface.

    GET  /audit/cube-comments/                       the register (+ reply_count)
    GET  /audit/cube-comments/<id>/replies/          one thread, oldest first
    POST /audit/cube-comments/<id>/replies/          {text, parent_id?}
    GET  /audit/cube-comments/<id>/context/          the lines captured at raise time

Why here and not under /xero/data/: the comment register lives in the Xero app
because that is where cube cells are, but ``/xero/data/...`` is the general
ledger and auditor accounts are 403 on every path under it. Mounting the
read + reply routes under ``/audit/`` is what makes them reachable — the prefix
rule in AuditorGateMiddleware is the access control, so these routes must stay
where they are (and AUDITOR_READ_PREFIXES must NOT be widened to compensate for
a route put in the wrong place).

Consequence, consciously accepted: an auditor can now read every comment in the
register, including the figure a cube comment is pinned to. That is the point —
they are being asked to work the queue — but it IS a window onto GL figures from
an account that is otherwise blocked from the GL, so it is named here rather
than left as a surprise.

Writes stay minimal: an auditor may POST a reply and nothing else. Replies are
append-only (no PATCH, no DELETE anywhere in this module), authorship is stamped
from ``request.user``, and an ``author`` in the body is ignored — the same rule
as finding and receipt comments.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.activity import models as A
from apps.activity.services import record_activity, record_auditor_read
from apps.xero.xero_data import cube_comment_replies as replies
from apps.xero.xero_data import pivot_comments

from .comment_events import CUBE_COMMENT
from .comment_webhook import notify_comment_created
from .findings_views import actor

_BODY_NOT_OBJECT = 'request body must be a JSON object'
_BAD_PARENT = 'parent_id must be a reply on this comment'
MAX_TEXT = 20_000


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cube_comments_view(request):
    """The register, with a reply count on every row.

    Same filters and same row shape as ``/xero/data/comments/`` — literally the
    same query (``pivot_comments.page_comments``), so the auditor's list cannot
    drift away from the one the console and the add-in see. That includes the
    paging: ``page_size`` is validated against the same closed set here, so an
    auditor cannot be served a quietly different window from the one MC sees.
    """
    try:
        body = pivot_comments.page_comments(
            dict(request.query_params.items()), with_reply_counts=True)
    except pivot_comments.PageSizeError as exc:
        return Response({'detail': str(exc),
                         'page_sizes': list(pivot_comments.PAGE_SIZES)},
                        status=status.HTTP_400_BAD_REQUEST)
    return Response(body)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def cube_comment_replies_view(request, comment_id: int):
    comment = replies.get_comment(comment_id)
    if comment is None:
        return Response({'detail': 'comment not found'}, status=status.HTTP_404_NOT_FOUND)
    ref = replies.subject_ref(comment)

    if request.method == 'GET':
        # Auditor reads are logged, standard users' are not — the trail exists
        # for accountability of the outside parties given access to the books,
        # not to watch the team. Same rule as finding detail.
        record_auditor_read(
            request, A.CUBE_COMMENT_VIEWED, target_kind='cube_comment',
            target_id=comment_id, target_ref=ref,
        )
        return Response({'comment_id': comment_id, 'replies': replies.fetch_replies(comment_id)})

    data = request.data
    if not isinstance(data, dict):
        return Response({'detail': _BODY_NOT_OBJECT}, status=status.HTTP_400_BAD_REQUEST)
    text = str(data.get('text') or '').strip()
    if not text:
        return Response({'detail': 'text is required'}, status=status.HTTP_400_BAD_REQUEST)
    if '\x00' in text:
        # JSON permits \u0000; a Postgres text column cannot store it, and psycopg
        # raises while binding — pure client input would become a raw 500.
        return Response({'detail': 'text must not contain NUL (0x00) characters'},
                        status=status.HTTP_400_BAD_REQUEST)
    if len(text) > MAX_TEXT:
        return Response({'detail': f'text must be at most {MAX_TEXT} characters'},
                        status=status.HTTP_400_BAD_REQUEST)

    parent_id, error = replies.parent_error(comment_id, data.get('parent_id'))
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)

    author = actor(request)

    # Opt-in idempotency, for the Excel sync only (see excel_addin/app.js): a
    # re-sync re-reads every reply on the sheet, so without this the same reply
    # would be appended on every pass. Deliberately NOT the default — two people
    # can legitimately write the same short answer, and collapsing that silently
    # would be the API deciding what someone meant.
    if _truthy(data.get('dedupe')):
        existing = replies.find_identical(comment_id, author, text)
        if existing is not None:
            return Response(existing, status=status.HTTP_200_OK)

    with transaction.atomic():
        reply = replies.create_reply(comment_id, author, text, parent_id)
        notify_comment_created(CUBE_COMMENT, reply, target=ref, object_id=str(comment_id))

    record_activity(
        request, A.CUBE_COMMENT_REPLIED, target_kind='cube_comment', target_id=comment_id,
        target_ref=ref,
        changes={'reply_id': reply['id'], 'parent_id': reply['parent_id']},
    )
    return Response(reply, status=status.HTTP_201_CREATED)


def _truthy(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cube_comment_context_view(request, comment_id: int):
    """The lines behind a point, as captured when it was raised.

    This is the route that makes the preparer loop workable. The live drill
    lives under ``/xero/data/``, where the auditor role is 403 on every path --
    so a bookkeeper could be assigned a point and answer it, but never see the
    transaction it was about, which is the single thing that would have made
    her day easier.

    Granting her the drill would have meant opening the general ledger to an
    account deliberately kept out of it. Reading a CAPTURED context does not:
    the evidence was resolved once, by someone who already had access, and is
    served here as stored data. Her access does not widen by one route.

    READ ONLY, and there is no POST here on purpose. Capturing runs a ledger
    query, and the auditor gate exists precisely so that an outside party
    cannot make this server do work against the books. Capture stays on the
    cube door under ``/xero/data/``, where only a full-access account reaches
    it -- the same split as the register itself.
    """
    comment = replies.get_comment(comment_id)
    if comment is None:
        return Response({'detail': 'comment not found'}, status=status.HTTP_404_NOT_FOUND)
    record_auditor_read(
        request, A.CUBE_COMMENT_VIEWED, target_kind='cube_comment',
        target_id=comment_id, target_ref=replies.subject_ref(comment),
    )
    context = pivot_comments.fetch_context(comment_id)
    if context is None:
        # An empty shape rather than a 404: "nothing was captured for this
        # point" is a different answer from "no such point", and a preparer
        # seeing 404 would reasonably think the point had been withdrawn.
        return Response({'comment_id': comment_id, 'captured_at': None,
                         'lines': [], 'line_count': 0})
    return Response(context)
