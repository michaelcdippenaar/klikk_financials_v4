"""
Server-side pivot over the journal mirror.

Excel for Mac has no Data Model and no OLAP connector, so a native PivotTable
can only ever aggregate the rows physically present on a sheet. This endpoint
does the aggregation in Postgres instead and hands Excel a finished cross-tab,
which is what lets the add-in pivot the whole 271k-row ledger rather than
whatever fits in a worksheet.

Measures are summed exactly as Xero stores them — credits negative — so a total
here ties to the ledger and to Xero without an adjustment step.
"""
import logging

from django.db.models import Q, Sum, Count, Value, CharField
from django.db.models.functions import Coalesce, ExtractYear, ExtractQuarter, ExtractMonth
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.xero.xero_data.models import XeroJournals

logger = logging.getLogger(__name__)

# Hard ceilings. A pivot that blows through these is a mis-specified pivot, not
# a big one — telling the user beats silently truncating their numbers.
MAX_LEAF_ROWS = 5000
MAX_COLS = 250

BLANK = '(none)'


def _r2(v):
    """Round to cents. Source columns are numeric(30,2), so this is exact — and
    it keeps float dust (a grand total of -4.9e-9) out of the worksheet, where it
    would break any `=IF(x=0, ...)` the reader writes against the cell."""
    return round(float(v or 0), 2)


def _label_account(r):
    code = r.get('account__code') or ''
    name = r.get('account__name') or ''
    if code and name:
        return '%s — %s' % (code, name)
    return code or name or BLANK


def _label_month(r):
    y, m = r.get('d_year'), r.get('d_month')
    return '%04d-%02d' % (y, m) if y and m else BLANK


def _label_quarter(r):
    y, q = r.get('d_year'), r.get('d_quarter')
    return '%04d-Q%d' % (y, q) if y and q else BLANK


def _plain(alias):
    def f(r):
        v = r.get(alias)
        return str(v) if v not in (None, '') else BLANK
    return f


# key -> (label for the UI, {annotation alias: expression or None for a plain field}, labeller)
# A `None` expression means the alias is already a real ORM path and needs no annotate().
DIMENSIONS = {
    'entity':             ('Entity',              {'organisation__tenant_name': None},          _plain('organisation__tenant_name')),
    'account_type':       ('Account type',        {'account__type': None},                      _plain('account__type')),
    'account':            ('Account',             {'account__code': None, 'account__name': None}, None),   # special-cased
    'supplier':           ('Supplier / contact',  {'d_supplier': Coalesce('contact__name', 'transaction_source__contact__name', Value(''), output_field=CharField())}, _plain('d_supplier')),
    'journal_type':       ('Journal type',        {'journal_type': None},                       _plain('journal_type')),
    'source_type':        ('Source type',         {'transaction_source__transaction_source': None}, _plain('transaction_source__transaction_source')),
    'year':               ('Year',                {'d_year': ExtractYear('date')},              _plain('d_year')),
    'quarter':            ('Quarter',             {'d_year': ExtractYear('date'), 'd_quarter': ExtractQuarter('date')}, _label_quarter),
    'month':              ('Month',               {'d_year': ExtractYear('date'), 'd_month': ExtractMonth('date')},     _label_month),
    'tracking1_category': ('Tracking 1 category', {'tracking1__name': None},                    _plain('tracking1__name')),
    'tracking1':          ('Tracking 1',          {'tracking1__option': None},                  _plain('tracking1__option')),
    'tracking2_category': ('Tracking 2 category', {'tracking2__name': None},                    _plain('tracking2__name')),
    'tracking2':          ('Tracking 2',          {'tracking2__option': None},                  _plain('tracking2__option')),
}

MEASURES = {
    'amount': ('Amount',       lambda: Sum('amount')),
    'debit':  ('Debit',        lambda: Sum('debit')),
    'credit': ('Credit',       lambda: Sum('credit')),
    'tax':    ('Tax',          lambda: Sum('tax_amount')),
    'count':  ('Journal lines', lambda: Count('id')),
}


def _dim_aliases(key):
    """Real ORM paths this dimension groups on, after annotation."""
    spec = DIMENSIONS[key]
    return list(spec[1].keys())


def _dim_annotations(keys):
    out = {}
    for k in keys:
        for alias, expr in DIMENSIONS[k][1].items():
            if expr is not None:
                out[alias] = expr
    return out


def _labeller(key):
    if key == 'account':
        return _label_account
    return DIMENSIONS[key][2]


def apply_journal_filters(qs, p):
    """Same filter vocabulary as the journal search endpoint, so the pane's
    Filters panel means exactly the same thing in both modes."""
    q = (p.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(description__icontains=q)
            | Q(reference__icontains=q)
            | Q(contact__name__icontains=q)
            | Q(transaction_source__contact__name__icontains=q)
            | Q(account__code__icontains=q)
            | Q(account__name__icontains=q)
            | Q(organisation__tenant_name__icontains=q)
        )

    tenant = (p.get('tenant') or '').strip()
    if tenant:
        qs = qs.filter(
            Q(organisation__tenant_id__icontains=tenant)
            | Q(organisation__tenant_name__icontains=tenant)
        )

    account = (p.get('account') or '').strip()
    if account:
        qs = qs.filter(Q(account__code__icontains=account) | Q(account__name__icontains=account))

    contact = (p.get('contact') or '').strip()
    if contact:
        qs = qs.filter(
            Q(contact__name__icontains=contact)
            | Q(transaction_source__contact__name__icontains=contact)
        )

    for param, field in (('reference', 'reference__icontains'),
                         ('description', 'description__icontains')):
        val = (p.get(param) or '').strip()
        if val:
            qs = qs.filter(**{field: val})

    journal_type = (p.get('journal_type') or '').strip()
    if journal_type:
        qs = qs.filter(journal_type__iexact=journal_type)

    date_from = parse_date(p.get('date_from') or '')
    if date_from:
        qs = qs.filter(date__date__gte=date_from)
    date_to = parse_date(p.get('date_to') or '')
    if date_to:
        qs = qs.filter(date__date__lte=date_to)

    return qs


class XeroJournalPivotView(APIView):
    """
    GET /xero/data/journals/pivot/

    rows      comma-separated dimension keys, outermost first
    cols      comma-separated dimension keys (optional)
    measure   amount | debit | credit | tax | count
    suppress  '1' to drop rows whose cells are all zero
    ...plus every filter the journal search endpoint accepts.

    Returns a fully-assembled cross-tab: ordered columns, and ordered rows where
    each parent consolidation is emitted before its children, the way a cube
    view reads when drilled.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params

        row_dims = [d for d in (p.get('rows') or '').split(',') if d.strip()]
        col_dims = [d for d in (p.get('cols') or '').split(',') if d.strip()]
        bad = [d for d in row_dims + col_dims if d not in DIMENSIONS]
        if bad:
            return Response({'error': 'unknown dimension(s): %s' % ', '.join(bad)},
                            status=status.HTTP_400_BAD_REQUEST)
        if not row_dims:
            return Response({'error': 'at least one row dimension is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(set(row_dims + col_dims)) != len(row_dims + col_dims):
            return Response({'error': 'a dimension cannot appear on both axes'},
                            status=status.HTTP_400_BAD_REQUEST)

        measure = (p.get('measure') or 'amount').strip()
        if measure not in MEASURES:
            return Response({'error': 'unknown measure: %s' % measure},
                            status=status.HTTP_400_BAD_REQUEST)

        qs = apply_journal_filters(XeroJournals.objects.all(), p)

        annotations = _dim_annotations(row_dims + col_dims)
        if annotations:
            qs = qs.annotate(**annotations)

        group_aliases = []
        for d in row_dims + col_dims:
            for a in _dim_aliases(d):
                if a not in group_aliases:
                    group_aliases.append(a)

        grouped = qs.values(*group_aliases).annotate(_v=MEASURES[measure][1]()).order_by()

        row_label = [_labeller(d) for d in row_dims]
        col_label = [_labeller(d) for d in col_dims]

        cells = {}
        row_keys, col_keys = set(), set()
        for rec in grouped.iterator(chunk_size=2000):
            rk = tuple(f(rec) for f in row_label)
            ck = tuple(f(rec) for f in col_label) if col_dims else ('Total',)
            row_keys.add(rk)
            col_keys.add(ck)
            val = rec['_v'] or 0
            cells[(rk, ck)] = cells.get((rk, ck), 0) + val

        ordered_cols = sorted(col_keys)
        cols_truncated = len(ordered_cols) > MAX_COLS
        if cols_truncated:
            ordered_cols = ordered_cols[:MAX_COLS]
        col_index = {c: i for i, c in enumerate(ordered_cols)}
        ncols = len(ordered_cols)

        # Leaf rows, then drop all-zero ones if asked before any consolidation
        # is computed — otherwise a suppressed child still inflates its parent.
        suppress = p.get('suppress') in ('1', 'true', 'True')
        leaves = []
        zero_rows = 0
        for rk in sorted(row_keys):
            vec = [0] * ncols
            for ck, i in col_index.items():
                v = cells.get((rk, ck))
                if v:
                    vec[i] = _r2(v)
            if not any(vec):
                zero_rows += 1
                if suppress:
                    continue
            leaves.append((rk, vec))

        rows_truncated = len(leaves) > MAX_LEAF_ROWS
        if rows_truncated:
            leaves = leaves[:MAX_LEAF_ROWS]

        # Drop columns no surviving row has a value in. Tested per-cell, not on
        # the column total, so a column whose values merely net to zero stays.
        if suppress and ncols:
            live = [any(vec[i] for _, vec in leaves) for i in range(ncols)]
            if not all(live):
                keep = [i for i in range(ncols) if live[i]]
                ordered_cols = [ordered_cols[i] for i in keep]
                leaves = [(rk, [vec[i] for i in keep]) for rk, vec in leaves]
                ncols = len(keep)

        out_rows = self._with_consolidations(leaves, len(row_dims), ncols)

        col_totals = [0.0] * ncols
        for _, vec in leaves:
            for i, v in enumerate(vec):
                col_totals[i] += v
        col_totals = [_r2(v) for v in col_totals]

        return Response({
            'measure': measure,
            'measure_label': MEASURES[measure][0],
            'row_dims': [{'key': d, 'label': DIMENSIONS[d][0]} for d in row_dims],
            'col_dims': [{'key': d, 'label': DIMENSIONS[d][0]} for d in col_dims],
            'cols': [' | '.join(c) for c in ordered_cols],
            'rows': out_rows,
            'col_totals': col_totals,
            'grand_total': _r2(sum(col_totals)),
            'leaf_count': len(leaves),
            'truncated_rows': rows_truncated,
            'truncated_cols': cols_truncated,
            'zero_rows': zero_rows,
            'balancing_hint': self._balancing_hint(row_dims, col_dims, leaves, zero_rows, measure),
        })

    @staticmethod
    def _balancing_hint(row_dims, col_dims, leaves, zero_rows, measure):
        """A journal balances within itself, so summing a signed measure across
        a cut that keeps both legs of an entry together nets to exactly zero.
        Grouping by Supplier alone is the classic case. Without this note an
        empty sheet reads as a broken tool rather than a correct answer."""
        if leaves or not zero_rows or measure not in ('amount',):
            return None
        separators = {'account', 'account_type'}
        if separators & set(row_dims + col_dims):
            return None
        return (
            'Every one of the %d rows netted to exactly zero. That is arithmetic, '
            'not an error: each journal balances within itself, so summing Amount '
            'across a cut that keeps both legs of an entry together always gives '
            'zero. Add Account or Account type to an axis, or switch the measure '
            'to Debit or Credit.' % zero_rows
        )

    @staticmethod
    def _with_consolidations(leaves, depth, ncols):
        """Emit each parent consolidation immediately before its children."""
        if depth <= 1:
            return [{'keys': list(rk), 'depth': 0, 'is_total': False, 'cells': vec}
                    for rk, vec in leaves]

        subtotals = {}
        for rk, vec in leaves:
            for d in range(1, depth):
                prefix = rk[:d]
                acc = subtotals.setdefault(prefix, [0.0] * ncols)
                for i, v in enumerate(vec):
                    acc[i] += v
        for prefix in subtotals:
            subtotals[prefix] = [_r2(v) for v in subtotals[prefix]]

        out = []
        emitted = set()
        for rk, vec in leaves:
            for d in range(1, depth):
                prefix = rk[:d]
                if prefix not in emitted:
                    emitted.add(prefix)
                    out.append({
                        'keys': list(prefix),
                        'depth': d - 1,
                        'is_total': True,
                        'cells': subtotals[prefix],
                    })
            out.append({
                'keys': list(rk),
                'depth': depth - 1,
                'is_total': False,
                'cells': vec,
            })
        return out


class XeroJournalPivotDimensionsView(APIView):
    """The dimension and measure catalogue the add-in builds its pickers from."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'dimensions': [{'key': k, 'label': v[0]} for k, v in DIMENSIONS.items()],
            'measures': [{'key': k, 'label': v[0]} for k, v in MEASURES.items()],
            'max_leaf_rows': MAX_LEAF_ROWS,
            'max_cols': MAX_COLS,
        })
