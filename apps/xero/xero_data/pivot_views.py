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

import json

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


def _fy_of(y, m, start_month):
    """Financial year label, named for the calendar year the year ENDS in.

    Each tenant has its own fiscal_year_start_month -- Klikk July, Tremly March,
    Dippenaar Family January -- so this cannot be a constant. A cube spanning
    entities will therefore put different date ranges under the same FY label;
    filter to one entity when that matters.
    """
    if not y or not m:
        return None
    s = start_month or 1
    return y + 1 if (s > 1 and m >= s) else y


def _label_fin_year(r):
    fy = _fy_of(r.get('d_year'), r.get('d_month'),
                r.get('organisation__fiscal_year_start_month'))
    return 'FY%d' % fy if fy else BLANK


def _label_fin_period(r):
    y, m = r.get('d_year'), r.get('d_month')
    s = r.get('organisation__fiscal_year_start_month') or 1
    fy = _fy_of(y, m, s)
    if not fy:
        return BLANK
    return 'FY%d P%02d' % (fy, ((m - s) % 12) + 1)


def _label_report(r):
    # Xero's Class splits the two statements: REVENUE/EXPENSE are the income
    # statement, everything else is the balance sheet.
    g = r.get('account__grouping')
    if not g:
        return BLANK
    return 'Income Statement' if g in ('REVENUE', 'EXPENSE') else 'Balance Sheet'


def _plain(alias):
    def f(r):
        v = r.get(alias)
        return str(v) if v not in (None, '') else BLANK
    return f


# key -> (label for the UI, {annotation alias: expression or None for a plain field}, labeller)
# A `None` expression means the alias is already a real ORM path and needs no annotate().
MAX_MEMBERS = 2000

DIMENSIONS = {
    'entity':             ('Entity',              {'organisation__tenant_name': None},          _plain('organisation__tenant_name')),
    'report':             ('Report (IS / BS)',    {'account__grouping': None},                  _label_report),
    'account_class':      ('Account class',       {'account__grouping': None},                  _plain('account__grouping')),
    'account_type':       ('Account type',        {'account__type': None},                      _plain('account__type')),
    'account':            ('Account',             {'account__code': None, 'account__name': None}, None),   # special-cased
    'supplier':           ('Supplier / contact',  {'d_supplier': Coalesce('contact__name', 'transaction_source__contact__name', Value(''), output_field=CharField())}, _plain('d_supplier')),
    'journal_type':       ('Journal type',        {'journal_type': None},                       _plain('journal_type')),
    'source_type':        ('Source type',         {'transaction_source__transaction_source': None}, _plain('transaction_source__transaction_source')),
    'fin_year':           ('Financial year',      {'d_year': ExtractYear('date'), 'd_month': ExtractMonth('date'), 'organisation__fiscal_year_start_month': None}, _label_fin_year),
    'fin_period':         ('Financial period',    {'d_year': ExtractYear('date'), 'd_month': ExtractMonth('date'), 'organisation__fiscal_year_start_month': None}, _label_fin_period),
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


def parse_dim_filters(p):
    """Dimension filters: ?dimf={"fin_year":["FY2026"],"entity":["Klikk"]}

    Distinct from apply_journal_filters, which speaks the journal search
    vocabulary (tenant, account, date range). This filters on the DIMENSION
    LABELS the cube itself renders -- "FY2026", "Income Statement" -- which is
    what the user is actually looking at, and the only thing that works for
    computed dimensions like fin_year whose label has no column behind it.

    Returns {dim: set(labels)}. Raises ValueError on an unknown dimension.
    """
    raw = (p.get('dimf') or '').strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError('dimf must be JSON')
    if not isinstance(obj, dict):
        raise ValueError('dimf must be a JSON object of dimension -> [values]')
    out = {}
    for k, v in obj.items():
        if k not in DIMENSIONS:
            raise ValueError('unknown dimension in dimf: %s' % k)
        vals = [str(x) for x in (v or []) if str(x) != '']
        if vals:
            # A LIST, not a set: a subset is an ordered sequence. The order the
            # user arranged in the subset editor is how the rows and columns
            # are laid out, so it has to survive the round trip.
            seen, ordered = set(), []
            for x in vals:
                if x not in seen:
                    seen.add(x)
                    ordered.append(x)
            out[k] = ordered
    return out



def _attr_path(obj, path):
    """Walk an ORM alias ('account__grouping') on a MODEL INSTANCE.

    The labellers were written against .values() dicts; the drill needs whole
    objects, so this rebuilds the same dict shape for the few aliases in play.
    """
    cur = obj
    for part in path.split('__'):
        if cur is None:
            return None
        cur = getattr(cur, part, None)
    return cur


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
    else:
        # Xero moved the Journals API to the Advanced tier (Mar 2026), so the
        # 'journal' mirror is frozen at 2025-11-25 and cannot be refreshed. It
        # duplicates the live transaction / manual_journal / system_journal
        # feeds, which are complementary and together form the whole ledger.
        # The trial balance already excludes it (xero_cube/models.py:
        # journal_type != 'journal'); the cube MUST match or the two disagree
        # on every figure. Pass journal_type=journal to inspect the legacy
        # mirror deliberately.
        qs = qs.exclude(journal_type='journal')

    date_from = parse_date(p.get('date_from') or '')
    if date_from:
        qs = qs.filter(date__date__gte=date_from)
    date_to = parse_date(p.get('date_to') or '')
    if date_to:
        qs = qs.filter(date__date__lte=date_to)

    return qs



def _insert_col_totals(ordered_cols, leaves, level, flags):
    """Add a total column after each group of columns sharing a prefix.

    Stacked columns (Financial year over period) previously had no year total
    at all -- only one grand total at the far right -- so reading a year meant
    adding its periods by eye.

    The synthetic column's path is the parent prefix plus 'Total', which means
    it renders under the parent in the stacked header AND gets its own anchor
    ("FY2018 | Total"), distinct from both the periods and the grand total. A
    comment on a year total is therefore a comment on that year, not on
    whichever period happened to sit in that position.
    """
    groups, start = [], 0
    for i in range(1, len(ordered_cols) + 1):
        end = i == len(ordered_cols)
        if end or ordered_cols[i][:level + 1] != ordered_cols[start][:level + 1]:
            groups.append((start, i))
            start = i

    width = max((len(c) for c in ordered_cols), default=level + 1)
    new_cols, index_map, new_flags = [], [], []
    for gstart, gend in groups:
        for i in range(gstart, gend):
            index_map.append(i)
            new_cols.append(ordered_cols[i])
            new_flags.append(flags[i])
        prefix = list(ordered_cols[gstart][:level + 1])
        total_path = tuple(prefix + ['Total'] + [''] * (width - level - 2))
        index_map.append(None)
        new_cols.append(total_path)
        new_flags.append(True)          # synthetic: a total OF other columns

    new_leaves = []
    for rk, vec in leaves:
        out = []
        run = 0.0
        for src in index_map:
            if src is None:
                out.append(_r2(run))
                run = 0.0
            else:
                out.append(vec[src])
                # A nested total already counted its own children, so only real
                # columns feed the running sum -- otherwise an inner total would
                # be added twice into the outer one.
                if not flags[src]:
                    run += vec[src]
        new_leaves.append((rk, out))
    return new_cols, new_leaves, new_flags


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

        try:
            dim_filters = parse_dim_filters(p)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        qs = apply_journal_filters(XeroJournals.objects.all(), p)

        # A filtered dimension must be GROUPED on even when it is on neither
        # axis, otherwise there is no label to test. It is then aggregated away
        # by the cells dict below, so the surviving totals are exact rather
        # than approximated after the fact.
        filter_dims = [d for d in dim_filters if d not in row_dims + col_dims]
        annotations = _dim_annotations(row_dims + col_dims + filter_dims)
        if annotations:
            qs = qs.annotate(**annotations)

        group_aliases = []
        for d in row_dims + col_dims + filter_dims:
            for a in _dim_aliases(d):
                if a not in group_aliases:
                    group_aliases.append(a)

        grouped = qs.values(*group_aliases).annotate(_v=MEASURES[measure][1]()).order_by()

        row_label = [_labeller(d) for d in row_dims]
        col_label = [_labeller(d) for d in col_dims]

        filter_tests = [(_labeller(d), set(dim_filters[d])) for d in dim_filters]

        # label -> position, per dimension. Anything not in a subset sorts after
        # everything that is, alphabetically, so an unsubsetted dimension keeps
        # exactly the behaviour it had before ordering existed.
        order_maps = {d: {v: i for i, v in enumerate(vals)}
                      for d, vals in dim_filters.items()}

        def axis_key(dims):
            def key(path):
                out = []
                for i, d in enumerate(dims):
                    label = path[i] if i < len(path) else ''
                    om = order_maps.get(d)
                    if om is not None and label in om:
                        out.append((0, om[label], ''))
                    else:
                        out.append((1, 0, label))
                return tuple(out)
            return key

        cells = {}
        row_keys, col_keys = set(), set()
        excluded = 0
        for rec in grouped.iterator(chunk_size=2000):
            if filter_tests and any(f(rec) not in keep for f, keep in filter_tests):
                excluded += 1
                continue
            rk = tuple(f(rec) for f in row_label)
            ck = tuple(f(rec) for f in col_label) if col_dims else ('Total',)
            row_keys.add(rk)
            col_keys.add(ck)
            val = rec['_v'] or 0
            cells[(rk, ck)] = cells.get((rk, ck), 0) + val

        # Sorting by the subset order keeps parent prefixes contiguous, which is
        # what _with_consolidations relies on, so drill grouping is unaffected.
        ordered_cols = sorted(col_keys, key=axis_key(col_dims) if col_dims else None)
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
        for rk in sorted(row_keys, key=axis_key(row_dims)):
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

        # Which fields show a total. Absent = every row level (what it always
        # did) and no column totals (likewise) -- so an old client is unchanged.
        def _wanted(param):
            raw = (p.get(param) or '').strip()
            if not raw:
                return None
            return {d.strip() for d in raw.split(',') if d.strip()}

        rtotals = _wanted('rtotals')
        row_levels = None
        if rtotals is not None:
            row_levels = {i + 1 for i, d in enumerate(row_dims[:-1]) if d in rtotals}

        ctotals = _wanted('ctotals') or set()
        # Deepest first: inserting an inner total does not disturb the grouping
        # of the levels outside it, whereas the reverse is not true. The LAST
        # column dimension is skipped -- a total of one leaf is that leaf.
        col_synthetic = [False] * len(ordered_cols)
        for lvl in sorted(
            (i for i, d in enumerate(col_dims[:-1]) if d in ctotals), reverse=True
        ):
            ordered_cols, leaves, col_synthetic = _insert_col_totals(
                ordered_cols, leaves, lvl, col_synthetic)
        ncols = len(ordered_cols)

        out_rows = self._with_consolidations(leaves, len(row_dims), ncols, row_levels)

        col_totals = [0.0] * ncols
        for _, vec in leaves:
            for i, v in enumerate(vec):
                col_totals[i] += v
        col_totals = [_r2(v) for v in col_totals]

        # The grand total sums the REAL columns only. A year-total column is a
        # sum of columns already counted, so including it would silently double
        # the grand total the moment anyone switched year totals on.
        grand = _r2(sum(t for i, t in enumerate(col_totals) if not col_synthetic[i]))

        # Legacy-mirror warning.
        #
        # The cube now excludes the frozen 'journal' mirror by default (see
        # _apply_filters), so the default view matches the trial balance and
        # needs no warning. The remaining feeds -- transaction, manual_journal
        # and system_journal -- are complementary, not mirrors:
        # sync_system_journals only ingests SourceTypes the other two pipelines
        # do not cover. Warn only when someone has deliberately selected the
        # legacy mirror, because its numbers will not tie to any report.
        mirror_hint = None
        selected_type = (p.get('journal_type') or '').strip().lower()
        if measure != 'count' and selected_type == 'journal':
            mirror_hint = (
                'You are viewing the legacy "journal" mirror. Xero moved the '
                'Journals API to the Advanced tier in March 2026, so this feed is '
                'frozen at 2025-11-25 and duplicates the live transaction / '
                'manual_journal / system_journal feeds. The trial balance excludes '
                'it, so these totals will not tie to any report. Clear the journal '
                'type filter to see the real ledger.'
            )

        return Response({
            'mirror_hint': mirror_hint,
            'measure': measure,
            'measure_label': MEASURES[measure][0],
            'dim_filters': {k: sorted(v) for k, v in dim_filters.items()},
            'excluded_groups': excluded,
            'row_dims': [{'key': d, 'label': DIMENSIONS[d][0]} for d in row_dims],
            'col_dims': [{'key': d, 'label': DIMENSIONS[d][0]} for d in col_dims],
            'cols': [' | '.join(c) for c in ordered_cols],
            # The same columns UNFLATTENED, so the client can stack the
            # header the way a PivotTable does -- Financial year spanning
            # its periods rather than 'FY2019 | 2018-07' in one cell. The
            # joined form stays: it is what comment anchors are keyed on.
            'col_paths': [list(c) for c in ordered_cols],
            'rows': out_rows,
            'col_totals': col_totals,
            'grand_total': grand,
            'col_synthetic': col_synthetic,
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
        separators = {'account', 'account_type', 'account_class', 'report'}
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
    def _with_consolidations(leaves, depth, ncols, levels=None):
        """Emit each parent consolidation immediately before its children.

        `levels` is the set of prefix lengths that should produce a subtotal
        row. None means every level, which is what this did before the totals
        became optional. A level that is switched off still has its children
        emitted -- turning off a subtotal hides the summary line, it does not
        hide the data underneath it.
        """
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
                if levels is not None and d not in levels:
                    continue
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


class XeroCubeMembersView(APIView):
    """GET /xero/data/journals/pivot/members/?dim=fin_year

    The distinct values a dimension actually takes under the CURRENT journal
    filters -- so the Filters well offers what is really in the data, not a
    catalogue of everything that ever existed. Same filter vocabulary as the
    pivot itself.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        dim = (p.get('dim') or '').strip()
        if dim not in DIMENSIONS:
            return Response({'error': 'unknown dimension: %s' % dim},
                            status=status.HTTP_400_BAD_REQUEST)

        qs = apply_journal_filters(XeroJournals.objects.all(), p)
        ann = _dim_annotations([dim])
        if ann:
            qs = qs.annotate(**ann)
        grouped = (qs.values(*_dim_aliases(dim))
                     .annotate(_n=Count('id'))
                     .order_by())

        label = _labeller(dim)
        counts = {}
        for rec in grouped.iterator(chunk_size=2000):
            key = label(rec)
            counts[key] = counts.get(key, 0) + (rec['_n'] or 0)

        members = sorted(counts.items())
        truncated = len(members) > MAX_MEMBERS
        if truncated:
            members = members[:MAX_MEMBERS]
        return Response({
            'dim': dim,
            'label': DIMENSIONS[dim][0],
            'count': len(members),
            'truncated': truncated,
            'members': [{'value': k, 'lines': n} for k, n in members],
        })


# A dimension whose label IS the stored value can be filtered in SQL. The rest
# (fin_year, report, month, ...) are computed in Python by their labeller, so
# they are evaluated after the query -- pushing down what we can first keeps
# that set small.
PUSHDOWN = {
    'entity': 'organisation__tenant_name',
    'account_class': 'account__grouping',
    'account_type': 'account__type',
    'journal_type': 'journal_type',
    'source_type': 'transaction_source__transaction_source',
    'tracking1_category': 'tracking1__name',
    'tracking1': 'tracking1__option',
    'tracking2_category': 'tracking2__name',
    'tracking2': 'tracking2__option',
}


class XeroCubeDrillView(APIView):
    """GET /xero/data/journals/pivot/drill/?coords={"account":"406 — Consulting"}

    The journal lines that ADD UP TO one cube cell.

    A comment is written about a figure; this is what the figure is made of.
    Coordinates are the same axis-independent {dimension: label} map the
    comment anchor stores, so a comment can be resolved back to its
    transactions at any time -- and re-resolved later, which is the point: the
    lines behind a figure change when Xero is re-synced, and a stored list of
    ids would quietly go stale while looking authoritative.

    Same journal filter vocabulary as the pivot, so the context that produced
    the number is reproduced exactly.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        try:
            coords = json.loads(p.get('coords') or '{}')
            if not isinstance(coords, dict):
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'coords must be a JSON object of dimension -> value'},
                            status=status.HTTP_400_BAD_REQUEST)

        unknown = [k for k in coords if k not in DIMENSIONS]
        if unknown:
            return Response({'error': 'unknown dimension(s): %s' % ', '.join(unknown)},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            limit = min(max(int(p.get('limit', 500)), 1), 5000)
        except (TypeError, ValueError):
            limit = 500

        # The total must be computed on the SAME measure the cell showed.
        # Summing amount for a comment written on a debit or credit figure
        # reconciles only by luck -- it happens to agree when a line's debit
        # equals its amount, and disagrees silently when it does not.
        measure = (p.get('measure') or 'amount').strip()
        if measure not in MEASURES:
            return Response({'error': 'unknown measure: %s' % measure},
                            status=status.HTTP_400_BAD_REQUEST)
        FIELD = {'amount': 'amount', 'debit': 'debit', 'credit': 'credit',
                 'tax': 'tax_amount'}

        qs = apply_journal_filters(XeroJournals.objects.all(), p)

        # Push down what SQL can do.
        deferred = {}
        for dim, val in coords.items():
            val = '' if val is None else str(val)
            if dim in PUSHDOWN:
                if val == BLANK:
                    # A blank member covers BOTH an empty string and a NULL, and
                    # they need different SQL. `__in=['', None]` compiles to
                    # `IN ('', NULL)`, and nothing is ever equal to NULL — so
                    # every drill on a "(none)" member silently returned no rows
                    # and looked like the ledger had moved underneath it.
                    qs = qs.filter(Q(**{PUSHDOWN[dim]: ''})
                                   | Q(**{PUSHDOWN[dim] + '__isnull': True}))
                else:
                    qs = qs.filter(**{PUSHDOWN[dim]: val})
            elif dim == 'account':
                # Label is "code — name"; the code alone identifies it.
                code = val.split('—')[0].strip() if '—' in val else val.strip()
                qs = qs.filter(account__code=code) if code else qs
                if '—' in val:
                    deferred[dim] = val
            else:
                deferred[dim] = val

        ann = _dim_annotations(list(deferred.keys()))
        if ann:
            qs = qs.annotate(**ann)

        qs = qs.select_related('organisation', 'account', 'contact', 'tracking1', 'tracking2',
                               'transaction_source', 'transaction_source__contact')

        tests = [(d, _labeller(d), v) for d, v in deferred.items()]
        aliases = []
        for d in deferred:
            aliases.extend(_dim_aliases(d))

        rows, total, scanned = [], 0.0, 0
        truncated = False
        for j in qs.iterator(chunk_size=2000):
            scanned += 1
            if tests:
                rec = {a: _attr_path(j, a) for a in aliases}
                if any(f(rec) != v for _, f, v in tests):
                    continue
            if measure == 'count':
                total += 1
            else:
                total += float(getattr(j, FIELD[measure], None) or 0)
            if len(rows) >= limit:
                truncated = True
                continue
            src = j.transaction_source
            supplier = j.contact or (src.contact if src else None)
            acct = j.account
            org = j.organisation
            # Same field set as the journal search endpoint, so a drill result
            # renders through the add-in's existing sheet writer untouched --
            # one column contract, no second renderer to drift.
            fy_rec = {
                'd_year': j.date.year if j.date else None,
                'd_month': j.date.month if j.date else None,
                'organisation__fiscal_year_start_month': (
                    getattr(org, 'fiscal_year_start_month', None) if org else None),
            }
            rows.append({
                'id': j.id,
                'date': j.date.date().isoformat() if j.date else None,
                'journal_number': j.journal_number,
                'journal_type': j.journal_type,
                'fin_year': _label_fin_year(fy_rec),
                'tenant_id': org.tenant_id if org else '',
                'tenant_name': org.tenant_name if org else '',
                'report': (('Income Statement' if acct.grouping in ('REVENUE', 'EXPENSE')
                            else 'Balance Sheet') if acct and acct.grouping else ''),
                'account_class': acct.grouping if acct else '',
                'account_code': acct.code if acct else '',
                'account_name': acct.name if acct else '',
                'account_type': acct.type if acct else '',
                'amount': str(j.amount),
                'debit': str(j.debit),
                'credit': str(j.credit),
                'tax_amount': str(j.tax_amount),
                'contact_name': j.contact.name if j.contact else '',
                'supplier_name': supplier.name if supplier else '',
                'supplier_via': ('journal' if j.contact else ('source' if (src and src.contact) else '')),
                'description': j.description or '',
                'reference': j.reference or '',
                'tracking1_category': j.tracking1.name if j.tracking1 else '',
                'tracking1': j.tracking1.option if j.tracking1 else '',
                'tracking2_category': j.tracking2.name if j.tracking2 else '',
                'tracking2': j.tracking2.option if j.tracking2 else '',
                'transaction_source_type': src.transaction_source if src else '',
                'transaction_source_id': src.transactions_id if src else '',
            })

        return Response({
            'coords': coords,
            'measure': measure,
            'count': len(rows),
            'line_total': _r2(total),
            'truncated': truncated,
            'rows': rows,
        })
