"""
Read-only query layer over the WhatsApp Slippies register ``whatsapp.klikk_slips``.

Contract
--------
* ``klikk_slips`` is NOT a Django model and is maintained by an external sync.
  It is read here with parameterised raw SQL only — never written, never altered.
* ``file_bytes`` (the raw blob) is never selected by anything in this module;
  the signed viewer in ``apps.audit.slip_view`` is the only reader of it.
* ``ocr->>'total'`` is a string and is not always numeric, so every numeric use
  goes through ``TOTAL_SQL`` (regex-guarded cast) — never a bare ``::numeric``.
* ``xero_status`` has several ``MATCHED…`` variants; it is normalised to a
  ``status_group`` (MATCHED / PENDING / NOT IN XERO / SKIPPED) for filtering.
* ``journal_number`` is only unique within (organisation, journal_type), so the
  matched-journal join is scoped by BOTH ``JOURNAL_TYPE`` and the organisation
  (from ``xero_org``, defaulting to Klikk), and aggregated to ONE summary row
  per slip (LATERAL + GROUP BY).
* FY runs Jul–Jun and is named by the ending year (FY26 = 2025-07-01..2026-06-30),
  bucketed on ``slip_ts`` in ``SLIP_TZ``.
* ``SlipReview`` / ``SlipComment`` (this app's own tables) are attached via the
  ORM after the raw query, so the raw SQL never depends on them.
"""
from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Count

from apps.audit.slip_view import slip_url

from .models import DECISION_VALUES, SlipComment, SlipReview

SLIP_TZ = ZoneInfo('Africa/Johannesburg')
FY_START_MONTH = 7
FY_LABEL_RE = re.compile(r'^FY(\d{2}|\d{4})$', re.IGNORECASE)

STATUS_GROUPS = ('MATCHED', 'PENDING', 'NOT IN XERO', 'SKIPPED')
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_IDS = 2000  # ceiling for the ids-only list mode (see query_slip_ids)

TOTAL_SQL = "case when s.ocr->>'total' ~ '^[0-9]+(\\.[0-9]+)?$' then (s.ocr->>'total')::numeric end"
STATUS_GROUP_SQL = (
    "case when s.xero_status like 'MATCHED%%' then 'MATCHED' "
    "when s.xero_status like 'PENDING%%' then 'PENDING' "
    "when s.xero_status like 'NOT IN XERO%%' then 'NOT IN XERO' "
    "when s.xero_status ilike 'skipped%%' then 'SKIPPED' "
    "else upper(s.xero_status) end"
)
SUPPLIER_SQL = "s.ocr->>'supplier'"

# ORDER BY is built from this whitelist only; user input is a dict key, never SQL.
ORDERINGS = {
    'slip_ts': 's.slip_ts asc nulls last, s.sha256',
    '-slip_ts': 's.slip_ts desc nulls last, s.sha256',
    'total': f'{TOTAL_SQL} asc nulls last, s.slip_ts desc, s.sha256',
    '-total': f'{TOTAL_SQL} desc nulls last, s.slip_ts desc, s.sha256',
    'supplier': f'lower({SUPPLIER_SQL}) asc nulls last, s.slip_ts desc, s.sha256',
    '-supplier': f'lower({SUPPLIER_SQL}) desc nulls last, s.slip_ts desc, s.sha256',
    'xero_status': 's.xero_status asc, s.slip_ts desc, s.sha256',
    '-xero_status': 's.xero_status desc, s.slip_ts desc, s.sha256',
}
DEFAULT_ORDERING = '-slip_ts'

SLIP_COLUMNS = [
    's.sha256', 's.slip_ts', 's.filename', 's.source', 's.mime_ext', 's.byte_size',
    's.xero_status', f'{STATUS_GROUP_SQL} as status_group', 's.xero_detail', 's.synced_to_xero',
    's.journal_number', 's.xero_org',
    f'{SUPPLIER_SQL} as supplier', f'{TOTAL_SQL} as total', "s.ocr->>'category' as category",
    "s.ocr->>'slip_date' as slip_date", "s.ocr->>'payment_method' as payment_method",
]
JOURNAL_COLUMNS = [
    'jn.journal_number as j_journal_number', 'jn.date as j_date', 'jn.description as j_description',
    'jn.debit as j_debit', 'jn.credit as j_credit', 'jn.amount as j_amount',
    'jn.account_code as j_account_code', 'jn.account_name as j_account_name', 'jn.contact_name as j_contact_name',
]

# The Xero mirror holds four journal_type values (journal / transaction / system_journal /
# manual_journal) and ``journal_number`` is only unique WITHIN (organisation_id, journal_type):
# ~1,225 same-org (number) pairs span more than one type. Joining on number + org alone therefore
# aggregates two unrelated transactions into one summary — amount/debit/credit are their sum and
# description/account come from whichever happened to carry the larger debit (BUG-4).
#
# The Slippies auto-recon writes ``journal_number`` from the Xero *Journals* feed, i.e.
# ``journal_type = 'journal'``. Verified against production on 19 Aug 2026: of the 139 slips
# carrying a journal_number, 30 resolve under 'journal' (28 of them tying to the slip's OCR total
# to the cent and to within 3 days of the slip date), 1 under 'manual_journal' (a false match:
# 8 lines, R1,000 vs a R2,515 slip, dated 19 months earlier) and 0 under 'transaction' —
# 'transaction' rows are invoice/bank-derived and live in a different number space.
JOURNAL_TYPE = 'journal'

# Slips carry the tenant NAME (``xero_org``), not its id. A blank/NULL ``xero_org`` means the recon
# did not record one; those fall back to Klikk, the tenant the register is about. A name that does
# not match a known tenant resolves to NULL and therefore to no journal — never to another tenant's
# lines, which would silently show the wrong money.
KLIKK_TENANT_ID = '41ebfa0e-012e-4ff1-82ba-a9a7585c536c'

# One summary row per slip. The "main" line (description/account/contact) is the largest debit on a
# non-bank account — the expense side of a receipt; the bank line is the credit side. The summary
# aggregates the DEBIT side only (``filter (where j.debit > 0)``), so a credit/bank line can never
# supply the supplier or the account; a journal with no debit line at all falls back to the whole
# journal rather than returning nulls.
_MAIN_LINE_ORDER = "order by coalesce(a.type = 'BANK', false), j.debit desc, j.id"


def _main_line(expr: str) -> str:
    """First value of ``expr`` across the journal's debit lines, falling back to all lines."""
    agg = f'array_agg({expr} {_MAIN_LINE_ORDER})'
    return (f'coalesce(({agg} filter (where j.debit > 0 and {expr} is not null))[1], '
            f'({agg} filter (where {expr} is not null))[1])')


JOURNAL_LATERAL_SQL = f"""
left join lateral (
    select j.journal_number,
           min(j.date)                                       as date,
           {_main_line('j.description')}                     as description,
           sum(j.debit)                                      as debit,
           sum(j.credit)                                     as credit,
           coalesce(sum(j.debit) filter (where j.debit > 0), sum(j.debit))  as amount,
           {_main_line('a.code')}                            as account_code,
           {_main_line('a.name')}                            as account_name,
           {_main_line('c.name')}                            as contact_name
    from xero_data_xerojournals j
    left join xero_metadata_xeroaccount a on a.account_id = j.account_id
    left join xero_metadata_xerocontacts c on c.contacts_id = j.contact_id
    where j.journal_number = s.journal_number
      and j.journal_type = %s
      and j.organisation_id = case
              when coalesce(s.xero_org, '') = '' then %s
              else (select t.tenant_id from xero_core_xerotenant t where t.tenant_name = s.xero_org)
          end
    group by j.journal_number
) jn on s.journal_number is not null
"""
# Bound to the two %s placeholders above; they precede the WHERE-clause args because the
# lateral appears earlier in the statement.
JOURNAL_ARGS: list[Any] = [JOURNAL_TYPE, KLIKK_TENANT_ID]


# --------------------------------------------------------------------------- #
# Fiscal year helpers (pure)
# --------------------------------------------------------------------------- #
def fy_range(fy: str) -> tuple[dt.date, dt.date]:
    """'FY26' -> (date(2025, 7, 1), date(2026, 6, 30)) — inclusive bounds. Raises ValueError."""
    m = FY_LABEL_RE.match((fy or '').strip())
    if not m:
        raise ValueError(f'bad fiscal year label {fy!r} (expected e.g. FY26)')
    digits = m.group(1)
    end_year = int(digits) if len(digits) == 4 else 2000 + int(digits)
    return dt.date(end_year - 1, FY_START_MONTH, 1), dt.date(end_year, FY_START_MONTH, 1) - dt.timedelta(days=1)


def fy_label(value: dt.date | dt.datetime | None) -> str | None:
    """date/datetime -> 'FY26' (FY named by the ending year; Jul–Jun). Datetimes are read in SLIP_TZ."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        value = (value.astimezone(SLIP_TZ) if value.tzinfo else value).date()
    end_year = value.year + 1 if value.month >= FY_START_MONTH else value.year
    return f'FY{end_year % 100:02d}'


# --------------------------------------------------------------------------- #
# Filter building
# --------------------------------------------------------------------------- #
def strip_nul(value: str) -> str:
    """
    Drop NUL (0x00) from a string bound for Postgres.

    JSON and query strings both permit ``\\u0000``; a Postgres ``text`` column cannot hold
    it, and psycopg raises ``ValueError: A string literal cannot contain NUL`` while binding
    the parameter — before the statement is ever sent. Unscrubbed, that surfaces as a raw
    500 from pure client input on every endpoint that puts a user string in the SQL.

    Filters scrub (a NUL in a search box is never intentional, and this module's contract is
    that unparseable filter values are ignored rather than rejected). The write endpoints
    reject instead — see ``views``: silently altering a note the caller sent is worse than
    telling them it was malformed.
    """
    return value.replace('\x00', '') if value else value


def _bool(value) -> bool | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ('1', 'true', 'yes', 'on'):
        return True
    if v in ('0', 'false', 'no', 'off'):
        return False
    return None


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, TypeError, ValueError):
        return None


def _local_date_sql() -> str:
    return f"(s.slip_ts at time zone '{SLIP_TZ.key}')::date"


def build_filters(params) -> tuple[str, list[Any]]:
    """Translate query params into a parameterised WHERE clause over alias ``s``."""
    clauses: list[str] = []
    args: list[Any] = []

    q = strip_nul(params.get('q') or '').strip()
    if q:
        clauses.append(
            "(s.search_tsv @@ plainto_tsquery('simple', %s) or s.filename ilike %s or s.ocr->>'supplier' ilike %s)"
        )
        like = f'%{q}%'
        args += [q, like, like]

    synced = _bool(params.get('synced'))
    if synced is not None:
        clauses.append('s.synced_to_xero = %s')
        args.append(synced)

    status = (params.get('status') or '').strip().upper()
    if status:
        clauses.append(f'{STATUS_GROUP_SQL} = %s')
        args.append(status)

    fy = (params.get('fy') or '').strip()
    if fy:
        try:
            start, end = fy_range(fy)
        except ValueError:
            start = end = None
        if start:
            clauses.append(f'{_local_date_sql()} between %s and %s')
            args += [start, end]

    date_from = _date(params.get('date_from'))
    if date_from:
        clauses.append(f'{_local_date_sql()} >= %s')
        args.append(date_from)
    date_to = _date(params.get('date_to'))
    if date_to:
        clauses.append(f'{_local_date_sql()} <= %s')
        args.append(date_to)

    to_process = _bool(params.get('to_process'))
    if to_process is not None:
        flagged = list(SlipReview.objects.filter(to_process=True).values_list('sha256', flat=True))
        clauses.append('s.sha256 = any(%s)' if to_process else 'not (s.sha256 = any(%s))')
        args.append(flagged)

    decision = (params.get('decision') or '').strip().upper()
    if decision in ('UNDECIDED', 'NONE'):
        decided = list(SlipReview.objects.exclude(decision='').values_list('sha256', flat=True))
        clauses.append('not (s.sha256 = any(%s))')
        args.append(decided)
    elif decision and decision in DECISION_VALUES:
        shas = list(SlipReview.objects.filter(decision=decision).values_list('sha256', flat=True))
        clauses.append('s.sha256 = any(%s)')
        args.append(shas)

    category = strip_nul(params.get('category') or '').strip()
    if category:
        clauses.append("s.ocr->>'category' ilike %s")
        args.append(category)

    min_total = _decimal(params.get('min_total'))
    if min_total is not None:
        clauses.append(f'{TOTAL_SQL} >= %s')
        args.append(min_total)
    max_total = _decimal(params.get('max_total'))
    if max_total is not None:
        clauses.append(f'{TOTAL_SQL} <= %s')
        args.append(max_total)

    where = ' and '.join(clauses) if clauses else 'true'
    return where, args


def resolve_ordering(value) -> str:
    return ORDERINGS.get((value or '').strip(), ORDERINGS[DEFAULT_ORDERING])


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def _fetch_dicts(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _base_select(extra_columns: Iterable[str] = ()) -> str:
    columns = ', '.join([*SLIP_COLUMNS, *JOURNAL_COLUMNS, *extra_columns])
    return f'select {columns} from whatsapp.klikk_slips s {JOURNAL_LATERAL_SQL}'


def query_totals(where: str, args: list[Any]) -> dict[str, Any]:
    sql = f'select count(*), coalesce(sum({TOTAL_SQL}), 0) from whatsapp.klikk_slips s where {where}'
    with connection.cursor() as cur:
        cur.execute(sql, args)
        count, total = cur.fetchone()
    return {'count': int(count), 'sum_total': _money(total)}


def query_slips(where: str, args: list[Any], ordering: str, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    sql = f'{_base_select()} where {where} order by {ordering}'
    params = [*JOURNAL_ARGS, *args]
    if limit is not None:
        sql += ' limit %s offset %s'
        params += [limit, offset]
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return [_shape_row(r) for r in _fetch_dicts(cur)]


def query_slip(sha256: str) -> dict[str, Any] | None:
    sql = f"{_base_select(['s.ocr'])} where s.sha256 = %s"
    with connection.cursor() as cur:
        cur.execute(sql, [*JOURNAL_ARGS, sha256])
        rows = _fetch_dicts(cur)
    if not rows:
        return None
    raw = rows[0]
    row = _shape_row(raw)
    ocr = raw.get('ocr')
    if isinstance(ocr, (str, bytes)):  # Django registers a pass-through jsonb loader on raw cursors
        try:
            ocr = json.loads(ocr)
        except ValueError:
            ocr = None
    ocr = ocr if isinstance(ocr, dict) else {}
    items = ocr.get('items')
    row['ocr'] = ocr
    row['items'] = [
        {'description': it.get('description'), 'amount': it.get('amount')}
        for it in (items or []) if isinstance(it, dict)
    ]
    return row


def query_slip_ids(where: str, args: list[Any], ordering: str, *, limit: int) -> list[str]:
    """The sha256s the filter matches, in ``ordering`` order — nothing else.

    Deliberately NOT built on ``_base_select()`` / ``JOURNAL_LATERAL_SQL`` and deliberately not
    followed by ``attach_review_state()``: the entire point of the ids-only mode is one cheap scan
    of the register. Because there is no lateral, there are no lateral placeholders — do NOT
    "fix" this by prepending ``JOURNAL_ARGS``; the WHERE args bind first, the limit last.
    """
    sql = f'select s.sha256 from whatsapp.klikk_slips s where {where} order by {ordering} limit %s'
    with connection.cursor() as cur:
        cur.execute(sql, [*args, limit])
        return [row[0] for row in cur.fetchall()]


def slip_exists(sha256: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('select 1 from whatsapp.klikk_slips where sha256 = %s', [sha256])
        return cur.fetchone() is not None


def existing_sha256s(sha256s: list[str]) -> set[str]:
    """The subset of ``sha256s`` present in the register. Empty input never hits the DB."""
    if not sha256s:
        return set()
    with connection.cursor() as cur:
        cur.execute('select sha256 from whatsapp.klikk_slips where sha256 = any(%s)', [list(sha256s)])
        return {row[0] for row in cur.fetchall()}


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #
def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal('0.01')))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _shape_row(r: dict[str, Any]) -> dict[str, Any]:
    mime_ext = (r.get('mime_ext') or '').lower()
    mime = mimetypes.guess_type(f'x.{mime_ext}')[0] if mime_ext else None
    journal = None
    if r.get('j_journal_number') is not None:
        journal = {
            'journal_number': r['j_journal_number'],
            'date': _iso(r.get('j_date')),
            'description': r.get('j_description'),
            'debit': _money(r.get('j_debit')),
            'credit': _money(r.get('j_credit')),
            'amount': _money(r.get('j_amount')),
            'account_code': r.get('j_account_code'),
            'account_name': r.get('j_account_name'),
            'contact_name': r.get('j_contact_name'),
        }
    return {
        'sha256': r['sha256'],
        'slip_ts': _iso(r.get('slip_ts')),
        'filename': r.get('filename'),
        'source': r.get('source'),
        'mime_ext': r.get('mime_ext'),
        'mime': mime or 'application/octet-stream',
        'is_pdf': mime_ext == 'pdf',
        'byte_size': r.get('byte_size'),
        'xero_status': r.get('xero_status'),
        'status_group': r.get('status_group'),
        'xero_detail': r.get('xero_detail'),
        'synced_to_xero': bool(r.get('synced_to_xero')),
        'journal_number': r.get('journal_number'),
        'xero_org': r.get('xero_org'),
        'fy': fy_label(r.get('slip_ts')),
        'supplier': r.get('supplier'),
        'total': _money(r.get('total')),
        'category': r.get('category'),
        'slip_date': r.get('slip_date'),
        'payment_method': r.get('payment_method'),
        'view_url': slip_url(r['sha256']),
        'journal': journal,
    }


def review_to_dict(review: SlipReview | None) -> dict[str, Any]:
    if review is None:
        return {'to_process': False, 'decision': '', 'note': '', 'updated_by': '', 'updated_at': None}
    return {
        'to_process': review.to_process,
        'decision': review.decision,
        'note': review.note,
        'updated_by': review.updated_by,
        'updated_at': _iso(review.updated_at),
    }


def comment_to_dict(comment: SlipComment) -> dict[str, Any]:
    return {'id': comment.id, 'text': comment.text, 'author': comment.author, 'created_at': _iso(comment.created_at)}


def attach_review_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``review`` and ``comment_count`` to each shaped row (two ORM queries for the whole page)."""
    shas = [r['sha256'] for r in rows]
    reviews = {rv.sha256: rv for rv in SlipReview.objects.filter(sha256__in=shas)}
    counts = dict(
        SlipComment.objects.filter(sha256__in=shas).values('sha256')
        .annotate(n=Count('id')).values_list('sha256', 'n')
    )
    for r in rows:
        r['review'] = review_to_dict(reviews.get(r['sha256']))
        r['comment_count'] = counts.get(r['sha256'], 0)
    return rows
