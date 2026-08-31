"""
Read-only REST surface over the WhatsApp mirror (schema ``whatsapp``), consumed
by the klikk-financials MCP server so claude.ai / Cowork sessions can read MC's
WhatsApp chats, messages, and attachments.

GET /api/whatsapp/chats/           [auth] list/search chats (name/jid, last_message_time)
GET /api/whatsapp/messages/        [auth] list/search messages (chat, text, sender, dates, media)
GET /api/whatsapp/context/         [auth] one message with surrounding messages
GET /api/whatsapp/attachment/      [auth] one attachment's bytes, base64-encoded

Everything is raw parameterised SQL over ``whatsapp.chats`` / ``whatsapp.messages`` /
``whatsapp.attachments`` — the tables the daily 06:00 SAST sync container maintains.
Nothing here writes; the WhatsApp bridge and sync own those tables. Every endpoint
requires an authenticated caller (the MCP service token qualifies via
``ServiceTokenAuthentication``); the project's DRF default is ``IsAuthenticated``
and each view also opts in explicitly.
"""
import base64
import datetime as dt

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# One attachment response must stay comfortably inside an MCP tool-result:
# refuse anything bigger rather than truncating a binary.
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024


def _int(value, default, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _bool(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    return None


def _fetch_dicts(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def chats_view(request):
    q = (request.query_params.get('q') or '').strip()
    limit = _int(request.query_params.get('limit'), 50, 1, 200)
    offset = _int(request.query_params.get('offset'), 0, 0, 1_000_000)

    where = 'true'
    args = []
    if q:
        where = '(c.name ilike %s or c.jid ilike %s)'
        args = [f'%{q}%', f'%{q}%']

    with connection.cursor() as cur:
        cur.execute(f'select count(*) from whatsapp.chats c where {where}', args)
        total = cur.fetchone()[0]
        cur.execute(
            f'''select c.jid, c.name, c.last_message_time
                from whatsapp.chats c
                where {where}
                order by c.last_message_time desc nulls last, c.jid
                limit %s offset %s''',
            [*args, limit, offset],
        )
        rows = _fetch_dicts(cur)

    return Response({'count': total, 'limit': limit, 'offset': offset, 'results': rows})


def _message_filters(params):
    clauses = []
    args = []

    chat_jid = (params.get('chat_jid') or '').strip()
    if chat_jid:
        clauses.append('m.chat_jid = %s')
        args.append(chat_jid)

    q = (params.get('q') or '').strip()
    if q:
        clauses.append('m.content ilike %s')
        args.append(f'%{q}%')

    sender = (params.get('sender') or '').strip()
    if sender:
        clauses.append('m.sender ilike %s')
        args.append(f'%{sender}%')

    date_from = _date(params.get('date_from'))
    if date_from:
        clauses.append('m.ts::date >= %s')
        args.append(date_from)
    date_to = _date(params.get('date_to'))
    if date_to:
        clauses.append('m.ts::date <= %s')
        args.append(date_to)

    media_only = _bool(params.get('media_only'))
    if media_only is True:
        clauses.append("m.media_type is not null and m.media_type <> ''")

    return (' and '.join(clauses) if clauses else 'true'), args


MESSAGE_SELECT = '''
    select m.id, m.chat_jid, c.name as chat_name, m.ts, m.sender, m.is_from_me,
           m.content, m.media_type, m.filename,
           exists (
               select 1 from whatsapp.attachments a
               where a.message_id = m.id and a.chat_jid = m.chat_jid
           ) as has_attachment
    from whatsapp.messages m
    left join whatsapp.chats c on c.jid = m.chat_jid
'''


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def messages_view(request):
    where, args = _message_filters(request.query_params)
    limit = _int(request.query_params.get('limit'), 50, 1, 500)
    offset = _int(request.query_params.get('offset'), 0, 0, 1_000_000)

    with connection.cursor() as cur:
        cur.execute(f'select count(*) from whatsapp.messages m where {where}', args)
        total = cur.fetchone()[0]
        cur.execute(
            f'{MESSAGE_SELECT} where {where} order by m.ts desc, m.id limit %s offset %s',
            [*args, limit, offset],
        )
        rows = _fetch_dicts(cur)

    return Response({'count': total, 'limit': limit, 'offset': offset, 'results': rows})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def context_view(request):
    chat_jid = (request.query_params.get('chat_jid') or '').strip()
    message_id = (request.query_params.get('message_id') or '').strip()
    if not chat_jid or not message_id:
        return Response({'detail': 'chat_jid and message_id are required'},
                        status=status.HTTP_400_BAD_REQUEST)
    before = _int(request.query_params.get('before'), 5, 0, 50)
    after = _int(request.query_params.get('after'), 5, 0, 50)

    with connection.cursor() as cur:
        cur.execute(
            f'{MESSAGE_SELECT} where m.id = %s and m.chat_jid = %s',
            [message_id, chat_jid],
        )
        target = _fetch_dicts(cur)
        if not target:
            return Response({'detail': 'message not found'}, status=status.HTTP_404_NOT_FOUND)
        target = target[0]

        cur.execute(
            f'''{MESSAGE_SELECT}
                where m.chat_jid = %s and (m.ts, m.id) < (%s, %s)
                order by m.ts desc, m.id desc limit %s''',
            [chat_jid, target['ts'], message_id, before],
        )
        before_rows = list(reversed(_fetch_dicts(cur)))

        cur.execute(
            f'''{MESSAGE_SELECT}
                where m.chat_jid = %s and (m.ts, m.id) > (%s, %s)
                order by m.ts asc, m.id asc limit %s''',
            [chat_jid, target['ts'], message_id, after],
        )
        after_rows = _fetch_dicts(cur)

    return Response({'message': target, 'before': before_rows, 'after': after_rows})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def attachment_view(request):
    chat_jid = (request.query_params.get('chat_jid') or '').strip()
    message_id = (request.query_params.get('message_id') or '').strip()
    if not chat_jid or not message_id:
        return Response({'detail': 'chat_jid and message_id are required'},
                        status=status.HTTP_400_BAD_REQUEST)

    with connection.cursor() as cur:
        cur.execute(
            '''select filename, mime_ext, byte_size, file_bytes
               from whatsapp.attachments
               where message_id = %s and chat_jid = %s''',
            [message_id, chat_jid],
        )
        row = cur.fetchone()

    if not row or row[3] is None:
        return Response({'detail': 'attachment not found'}, status=status.HTTP_404_NOT_FOUND)
    filename, mime_ext, byte_size, data = row
    if len(data) > ATTACHMENT_MAX_BYTES:
        return Response(
            {'detail': f'attachment is {len(data)} bytes; the API serves at most {ATTACHMENT_MAX_BYTES}'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    return Response({
        'message_id': message_id,
        'chat_jid': chat_jid,
        'filename': filename,
        'mime_ext': mime_ext,
        'byte_size': byte_size if byte_size is not None else len(data),
        'base64': base64.b64encode(bytes(data)).decode('ascii'),
    })
