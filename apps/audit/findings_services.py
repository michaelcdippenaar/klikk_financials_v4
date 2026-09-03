"""
Query + validation layer for the audit findings register (``AuditFinding``).

The findings register is the human-curated companion to the deterministic
check registry in ``services.py``: internal-audit runs, codex sweeps and MC
raise findings here, work them through OPEN → RESOLVED, and hang comments and
file evidence off them. Views (``findings_views.py``) orchestrate; everything
that filters, validates, shapes or allocates lives in this module so the REST
views, the export view and the seed command all share one implementation.

Conventions in force:
* Client input NEVER raises out of ``build_queryset`` — bad values become
  human-readable strings in the returned ``errors`` list (the view 400s on
  ``errors[0]``). Write-path validation lives in ``clean_payload`` and returns
  an error message instead of raising, for the same reason.
* Money is ``Decimal`` end to end and serialised as a 2dp STRING (or null) —
  never float.
* Refs (``FY26-012``) are allocated under a Postgres advisory xact lock and
  are never reused or renumbered: the sequence is ``max(existing suffix)+1``,
  not ``count(*)+1``, so a withdrawn finding cannot cause a collision.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Case, Count, F, IntegerField, Max, Q, QuerySet, Sum, TextField, Value, When
from django.db.models.functions import Cast

from .models import AuditFinding, AuditFindingAttachment, AuditFindingComment
from .services import fiscal_year_for_today

SEVERITIES: tuple[str, ...] = ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO')
STATUSES: tuple[str, ...] = ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'ACCEPTED', 'WITHDRAWN')
EVIDENCE_TYPES: tuple[str, ...] = ('journal', 'slip', 'invoice', 'bank', 'file', 'url', 'note')
SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
BULK_MAX = 500  # finding ids per bulk request, counted AFTER de-duplication

ORDERING_FIELDS: tuple[str, ...] = (
    'ref', '-ref', 'fy', '-fy', 'severity', '-severity', 'status', '-status',
    'amount', '-amount', 'due_date', '-due_date', 'created_at', '-created_at',
    'updated_at', '-updated_at', 'title', '-title',
)

FY_MIN, FY_MAX = 2015, 2100

# PATCH may touch these; POST accepts them too (plus fy). ``fy``/``ref`` are immutable after create.
EDITABLE_FIELDS = (
    'status', 'owner', 'due_date', 'amount', 'severity', 'category', 'title',
    'description', 'evidence', 'check_code', 'asana_gid', 'source', 'currency',
)

# (field, max_length) for the plain text columns; None = unlimited (TextField).
_TEXT_LIMITS = {
    'title': 300, 'category': 32, 'currency': 8, 'description': None, 'owner': 150,
    'source': 200, 'check_code': 16, 'asana_gid': 64,
}


def current_fy() -> int:
    """The FY today falls in (Klikk FY N = 1 Jul (N-1) .. 30 Jun N)."""
    return fiscal_year_for_today()


# DECIMAL(18,2): 16 integer digits + 2 decimals. Anything larger is rejected at the
# door -- unbounded input passed validation, reached Postgres, and turned a bad
# request into a 500 (found by the adversarial suite).
MAX_AMOUNT = Decimal('9999999999999999.99')


def resolve_default_fy() -> int:
    """The FY a READ defaults to: the most recent FY that actually has findings,
    falling back to the current FY when the register is empty.

    Deliberately NOT ``current_fy()``. MC and the agents ask "what's open" during
    the year-end wrap of the FY that just CLOSED, so on 20 Aug 2026 the current FY
    (2027) is two months old and empty -- defaulting to it is technically correct
    and practically useless. ``?fy=`` always overrides, ``fy=all`` spans
    everything, and every response echoes both the resolved ``fy`` and the true
    ``current_fy`` so a caller is never guessing which year it is looking at.

    WRITES do NOT use this -- see the module note on clean_payload. A read that
    resolves to the wrong year is visible immediately; a WRITE that does silently
    misfiles the finding and is only discovered at audit time.
    """
    latest = AuditFinding.objects.aggregate(m=Max('fy'))['m']
    return int(latest) if latest is not None else current_fy()


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #
def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal('0.01')))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def finding_to_dict(f: AuditFinding, *, counts: bool = True) -> dict:
    d = {
        'id': f.id,
        'fy': f.fy,
        'ref': f.ref,
        'title': f.title,
        'severity': f.severity,
        'status': f.status,
        'category': f.category,
        'amount': _money(f.amount),
        'currency': f.currency,
        'description': f.description,
        'evidence': f.evidence or [],
        'owner': f.owner,
        'due_date': _iso(f.due_date),
        'source': f.source,
        'check_code': f.check_code,
        'asana_gid': f.asana_gid,
        'created_by': f.created_by,
        'updated_by': f.updated_by,
        'created_at': _iso(f.created_at),
        'updated_at': _iso(f.updated_at),
    }
    if counts:
        # Views may annotate comment_count/attachment_count for the list page;
        # fall back to per-row queries on the detail path.
        cc = getattr(f, 'comment_count', None)
        ac = getattr(f, 'attachment_count', None)
        lc = getattr(f, 'link_count', None)
        d['comment_count'] = cc if cc is not None else f.comments.count()
        d['attachment_count'] = ac if ac is not None else f.attachments.count()
        d['link_count'] = lc if lc is not None else f.links.count()
    return d


def comment_to_dict(c: AuditFindingComment) -> dict:
    return {
        'id': c.id,
        'finding_id': c.finding_id,
        'parent_id': c.parent_id,
        'text': c.text,
        'author': c.author,
        'created_at': _iso(c.created_at),
    }


def attachment_to_dict(a: AuditFindingAttachment) -> dict:
    return {
        'id': a.id,
        'finding_id': a.finding_id,
        'original_name': a.original_name,
        'content_type': a.content_type,
        'size': a.size,
        'note': getattr(a, 'note', '') or '',
        'uploaded_by': a.uploaded_by,
        'created_at': _iso(a.created_at),
        'view_url': attachment_url(a.pk),
    }


# --------------------------------------------------------------------------- #
# Signed attachment viewer URL (same recipe as slip_view.slip_url)
# --------------------------------------------------------------------------- #
def attachment_signature(pk: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f'auditfinding:{pk}'.encode(), hashlib.sha256).hexdigest()[:32]


def attachment_url(pk: int) -> str:
    base = getattr(settings, 'SLIP_VIEW_BASE_URL', 'https://console.8-bit.space/backend')
    return f'{base}/audit/findings/attachments/{pk}/file/?s={attachment_signature(pk)}'


# --------------------------------------------------------------------------- #
# Ref allocation
# --------------------------------------------------------------------------- #
def allocate_ref(fy: int) -> str:
    """
    Next ref for ``fy`` (``FY26-012``), serialised per-FY by an advisory lock.

    MUST be called inside ``transaction.atomic()`` — ``pg_advisory_xact_lock``
    is held until the surrounding transaction commits, which is exactly what
    makes two concurrent creates for the same FY queue instead of colliding.
    The sequence is parsed out of the existing refs (max suffix + 1), NOT
    ``count(*)+1``: a withdrawn/deleted finding must never cause a ref reuse.
    The (fy, ref) unique constraint is the backstop; callers retry on
    IntegrityError (3 attempts).
    """
    if not connection.in_atomic_block:
        raise RuntimeError('allocate_ref() must be called inside transaction.atomic()')
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('audit_finding_ref'), %s)", [fy])
    seq = 0
    for ref in AuditFinding.objects.filter(fy=fy).values_list('ref', flat=True):
        try:
            seq = max(seq, int(ref.rsplit('-', 1)[-1]))
        except (TypeError, ValueError):
            continue  # malformed legacy ref — never blocks allocation
    return f'FY{fy % 100:02d}-{seq + 1:03d}'


# --------------------------------------------------------------------------- #
# List filtering / ordering
# --------------------------------------------------------------------------- #
def severity_rank_case():
    """CASE expression mapping severity → rank (CRITICAL=0 … INFO=4) for ORDER BY."""
    return Case(
        *[When(severity=s, then=Value(r)) for s, r in SEVERITY_RANK.items()],
        default=Value(len(SEVERITIES)),
        output_field=IntegerField(),
    )


def _strip_nul(value: str) -> str:
    # Read paths scrub NUL (a 0x00 in a search box is never intentional and
    # psycopg refuses to bind it); write paths reject instead — see clean_payload.
    return value.replace('\x00', '') if value else value


def _csv_upper(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.split(',') if part.strip()]


def build_queryset(params) -> tuple[QuerySet, dict]:
    """
    Translate list-endpoint query params into (queryset, meta).

    ``meta = {'fy': int | None, 'errors': [str]}`` — ``fy`` is the effective FY
    filter (None when ``fy=all``); a non-empty ``errors`` means the view should
    400 with ``errors[0]``. NEVER raises on client input.
    """
    errors: list[str] = []
    qs = AuditFinding.objects.all()

    raw_fy = _strip_nul(str(params.get('fy') or '')).strip()
    if not raw_fy:
        fy: int | None = resolve_default_fy()
    elif raw_fy.lower() == 'all':
        fy = None
    else:
        try:
            fy = int(raw_fy)
        except ValueError:
            fy = None
            errors.append(f"fy must be a year between {FY_MIN} and {FY_MAX}, or 'all'")
        else:
            if not FY_MIN <= fy <= FY_MAX:
                errors.append(f'fy must be between {FY_MIN} and {FY_MAX}')
    if fy is not None:
        qs = qs.filter(fy=fy)

    raw_status = _strip_nul(str(params.get('status') or '')).strip()
    if raw_status:
        values = _csv_upper(raw_status)
        bad = [v for v in values if v not in STATUSES]
        if bad:
            errors.append(f"invalid status {bad[0]!r} (allowed: {', '.join(STATUSES)})")
        elif values:
            qs = qs.filter(status__in=values)

    raw_severity = _strip_nul(str(params.get('severity') or '')).strip()
    if raw_severity:
        values = _csv_upper(raw_severity)
        bad = [v for v in values if v not in SEVERITIES]
        if bad:
            errors.append(f"invalid severity {bad[0]!r} (allowed: {', '.join(SEVERITIES)})")
        elif values:
            qs = qs.filter(severity__in=values)

    raw_category = _strip_nul(str(params.get('category') or '')).strip()
    if raw_category:
        values = _csv_upper(raw_category)
        if values:
            qs = qs.filter(category__in=values)

    owner = _strip_nul(str(params.get('owner') or '')).strip()
    if owner:
        qs = qs.filter(owner__icontains=owner)

    check_code = _strip_nul(str(params.get('check_code') or '')).strip().upper()
    if check_code:
        qs = qs.filter(check_code=check_code)

    q = _strip_nul(str(params.get('q') or '')).strip()
    if q:
        # jsonb → text cast so evidence refs/notes are searchable. Verified on
        # this stack (Django 5 / psycopg2 / PG16): Cast(JSONField, TextField)
        # renders as ``(evidence)::text`` and icontains works on the result.
        qs = qs.annotate(evidence_text=Cast('evidence', TextField())).filter(
            Q(title__icontains=q) | Q(description__icontains=q) | Q(ref__icontains=q)
            | Q(owner__icontains=q) | Q(source__icontains=q) | Q(evidence_text__icontains=q)
        )

    bounds: dict[str, Decimal] = {}
    for name in ('amount_min', 'amount_max'):
        raw = _strip_nul(str(params.get(name) or '')).strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
            if not value.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append(f'{name} must be a number')
            continue
        bounds[name] = value
    if bounds:
        qs = qs.filter(amount__isnull=False)  # rows without an amount are outside any bound
        if 'amount_min' in bounds:
            qs = qs.filter(amount__gte=bounds['amount_min'])
        if 'amount_max' in bounds:
            qs = qs.filter(amount__lte=bounds['amount_max'])

    qs = _apply_ordering(qs, str(params.get('ordering') or ''))
    return qs, {'fy': fy, 'errors': errors}


def _apply_ordering(qs: QuerySet, raw: str) -> QuerySet:
    key = _strip_nul(raw).strip()
    if key not in ORDERING_FIELDS:
        key = ''  # unknown ordering falls back to the default silently
    if not key:
        return qs.order_by('-fy', 'ref')
    desc = key.startswith('-')
    field = key.lstrip('-')
    if field == 'severity':
        # Rank order, not alphabetical: 'severity' = CRITICAL first, '-severity' = INFO first.
        qs = qs.annotate(_severity_rank=severity_rank_case())
        primary = F('_severity_rank').desc() if desc else F('_severity_rank').asc()
    elif field in ('amount', 'due_date'):
        # NULLs last in both directions (contract mandates ascending; descending
        # follows suit so the console never leads with blank rows).
        primary = F(field).desc(nulls_last=True) if desc else F(field).asc(nulls_last=True)
    else:
        primary = F(field).desc() if desc else F(field).asc()
    return qs.order_by(primary, '-fy', 'ref')


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
def totals_for(qs: QuerySet) -> dict:
    """Whole-filter totals: ``{'count': int, 'amount': '0.00'}`` (Sum ignores NULL amounts)."""
    agg = qs.order_by().aggregate(count=Count('id'), amount=Sum('amount'))
    return {'count': agg['count'] or 0, 'amount': _money(agg['amount'] or Decimal('0'))}


def _grouped(qs: QuerySet, field: str) -> list[dict]:
    rows = qs.order_by().values(field).annotate(count=Count('id'), amount=Sum('amount'))
    return [
        {'key': r[field], 'count': r['count'], 'amount': _money(r['amount'] or Decimal('0'))}
        for r in rows
    ]


def summary_for(qs: QuerySet, fy: int | None) -> dict:
    """The summary payload minus ``current_fy``/``fy_options`` (the view adds those)."""
    totals = totals_for(qs)
    by_severity = sorted(_grouped(qs, 'severity'), key=lambda r: SEVERITY_RANK.get(r['key'], len(SEVERITIES)))
    by_status = sorted(
        _grouped(qs, 'status'),
        key=lambda r: STATUSES.index(r['key']) if r['key'] in STATUSES else len(STATUSES),
    )
    by_category = sorted(_grouped(qs, 'category'), key=lambda r: (-r['count'], r['key']))
    by_owner = sorted(_grouped(qs, 'owner'), key=lambda r: (-r['count'], r['key']))
    return {
        'fy': fy,
        'count': totals['count'],
        'amount': totals['amount'],
        'open_count': qs.order_by().filter(status='OPEN').count(),
        'by_severity': by_severity,
        'by_status': by_status,
        'by_category': by_category,
        'by_owner': by_owner,
    }


def fy_options() -> list[int]:
    """Distinct FYs with at least one finding, descending, always including the current FY."""
    fys = set(AuditFinding.objects.order_by().values_list('fy', flat=True).distinct())
    fys.add(current_fy())
    return sorted(fys, reverse=True)


# --------------------------------------------------------------------------- #
# Write-path validation
# --------------------------------------------------------------------------- #
def _nul_message(field: str, *values: str) -> str | None:
    if any('\x00' in v for v in values if isinstance(v, str)):
        return f'{field} must not contain NUL (0x00) characters'
    return None


def clean_payload(data, *, partial: bool) -> tuple[dict, str | None]:
    """
    Validate a POST (``partial=False``) or PATCH (``partial=True``) body.

    Returns ``(fields, None)`` — model-ready values, vocab fields upper-cased,
    amount quantised to 2dp — or ``({}, error_message)``. On PATCH, ``fy`` and
    ``ref`` are silently dropped (immutable); on POST a client-supplied ``ref``
    is ignored and ``fy`` defaults to the current FY.
    """
    if not isinstance(data, dict):
        return {}, 'request body must be a JSON object'
    data = {k: v for k, v in data.items() if k != 'ref'}
    if partial:
        data.pop('fy', None)
        if not any(k in data for k in EDITABLE_FIELDS):
            return {}, 'at least one editable field must be provided'

    fields: dict[str, Any] = {}

    if not partial:
        raw_fy = data.get('fy', current_fy())
        try:
            fy = int(raw_fy)
            if isinstance(raw_fy, bool) or not FY_MIN <= fy <= FY_MAX:
                raise ValueError
        except (TypeError, ValueError):
            return {}, f'fy must be a year between {FY_MIN} and {FY_MAX}'
        fields['fy'] = fy

    # --- vocab fields ------------------------------------------------------
    for name, vocab, create_default in (
        ('severity', SEVERITIES, None),       # required on create
        ('status', STATUSES, 'OPEN'),
    ):
        if name in data:
            value = data[name]
            if not isinstance(value, str) or value.strip().upper() not in vocab:
                return {}, f"{name} must be one of {', '.join(vocab)}"
            fields[name] = value.strip().upper()
        elif not partial:
            if create_default is None:
                return {}, f'{name} is required'
            fields[name] = create_default

    # --- plain text fields ---------------------------------------------------
    required_on_create = ('title', 'category', 'source')
    for name, limit in _TEXT_LIMITS.items():
        if name in data:
            value = data[name]
            if value is None:
                value = ''
            if not isinstance(value, str):
                return {}, f'{name} must be a string'
            msg = _nul_message(name, value)
            if msg:
                return {}, msg
            value = value.strip()
            if limit is not None and len(value) > limit:
                return {}, f'{name} must be at most {limit} characters'
            if name == 'category':
                value = value.upper()
            if name == 'check_code':
                value = value.upper()  # list filter matches upper-cased, so store upper
            if not value and name in required_on_create:
                # required on create, and un-blankable on PATCH — same validation both ways
                return {}, f'{name} is required'
            if name == 'currency' and not value:
                value = 'ZAR'
            fields[name] = value
        elif not partial:
            if name in required_on_create:
                return {}, f'{name} is required'
            fields[name] = 'ZAR' if name == 'currency' else ''

    # --- amount --------------------------------------------------------------
    if 'amount' in data:
        raw = data['amount']
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            fields['amount'] = None
        else:
            try:
                if isinstance(raw, bool):
                    raise InvalidOperation
                value = Decimal(str(raw).strip())
                if not value.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                return {}, 'amount must be a number or null'
            # Range check BEFORE quantize, not after. quantize() on a value with
            # more digits than the decimal context allows (e.g. 40 nines) raises
            # InvalidOperation from OUTSIDE the try above, so checking afterwards
            # still turned pure client input into a 500. Guards DECIMAL(18,2).
            if abs(value) > MAX_AMOUNT:
                return {}, 'amount is out of range (maximum 16 digits before the decimal point)'
            try:
                value = value.quantize(Decimal('0.01'))
            except InvalidOperation:
                return {}, 'amount must be a number or null'
            fields['amount'] = value
    elif not partial:
        fields['amount'] = None

    # --- due_date --------------------------------------------------------------
    if 'due_date' in data:
        raw = data['due_date']
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            fields['due_date'] = None
        else:
            try:
                if not isinstance(raw, str) or len(raw.strip()) != 10:
                    raise ValueError
                fields['due_date'] = dt.date.fromisoformat(raw.strip())
            except (TypeError, ValueError):
                return {}, 'due_date must be YYYY-MM-DD or null'
    elif not partial:
        fields['due_date'] = None

    # --- evidence --------------------------------------------------------------
    if 'evidence' in data:
        raw = data['evidence']
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            return {}, 'evidence must be a list of {type, ref, note} objects'
        items = []
        for item in raw:
            if not isinstance(item, dict):
                return {}, 'evidence must be a list of {type, ref, note} objects'
            etype = item.get('type')
            if not isinstance(etype, str) or etype.strip().lower() not in EVIDENCE_TYPES:
                return {}, f"evidence type must be one of {', '.join(EVIDENCE_TYPES)}"
            ref = item.get('ref', '')
            note = item.get('note', '')
            if not isinstance(ref, str) or not isinstance(note, str):
                return {}, 'evidence ref and note must be strings'
            msg = _nul_message('evidence', ref, note)
            if msg:
                return {}, msg
            items.append({'type': etype.strip().lower(), 'ref': ref, 'note': note})
        fields['evidence'] = items
    elif not partial:
        fields['evidence'] = []

    return fields, None
