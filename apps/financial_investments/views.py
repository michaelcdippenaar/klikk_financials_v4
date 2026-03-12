from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date

from .models import Symbol
from . import services


@api_view(['GET'])
def symbol_list(request):
    """List all tracked symbols with last_close, prev_close, change, change_pct and Investec mapping when linked."""
    symbols = services.get_symbols_with_latest_prices()
    return Response(symbols)


@api_view(['GET'])
def symbol_detail(request, symbol):
    """Get one symbol with Investec JSE mapping when linked."""
    try:
        s = Symbol.objects.select_related('share_name_mapping').get(symbol=symbol.upper())
    except Symbol.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    row = {
        'symbol': s.symbol,
        'name': s.name,
        'exchange': s.exchange,
        'category': s.category or '',
        'created_at': s.created_at,
        'updated_at': s.updated_at,
    }
    if s.share_name_mapping_id:
        m = s.share_name_mapping
        row['share_name_mapping'] = {
            'share_name': m.share_name,
            'share_name2': m.share_name2,
            'share_name3': m.share_name3,
            'company': m.company,
            'share_code': m.share_code,
        }
    else:
        row['share_name_mapping'] = None
    return Response(row)


@api_view(['GET'])
def symbol_history(request, symbol):
    """Get price history for a symbol. Query params: start_date, end_date (YYYY-MM-DD)."""
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    if start_date:
        start_date = parse_date(start_date)
    if end_date:
        end_date = parse_date(end_date)
    data = services.get_history_from_db(symbol, start_date=start_date, end_date=end_date)
    return Response(data)


@api_view(['POST'])
def symbol_refresh(request, symbol):
    """Fetch from yfinance and store/update price points. Body or query: start_date, end_date (optional)."""
    start_date = request.data.get('start_date') or request.query_params.get('start_date')
    end_date = request.data.get('end_date') or request.query_params.get('end_date')
    if start_date:
        start_date = parse_date(start_date)
    if end_date:
        end_date = parse_date(end_date)
    result = services.fetch_and_store(symbol, start_date=start_date, end_date=end_date)
    if result.get('error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


def _get_symbol_or_404(symbol_str):
    try:
        return Symbol.objects.get(symbol=symbol_str.upper())
    except Symbol.DoesNotExist:
        return None


@api_view(['GET'])
def symbol_dividends(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    data = services.get_dividends_with_yield(symbol)
    return Response(data)


@api_view(['GET'])
def symbol_splits(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    data = [{'date': str(s.date), 'ratio': float(s.ratio)} for s in sym.splits.all()]
    return Response(data)


@api_view(['GET'])
def symbol_info(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        info = sym.info
        return Response({'fetched_at': info.fetched_at, 'data': info.data})
    except Exception:
        return Response({'fetched_at': None, 'data': None})


@api_view(['GET'])
def symbol_financial_statements(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    freq = request.query_params.get('freq', 'yearly')
    data = []
    for stmt in sym.financial_statements.filter(freq=freq):
        data.append({
            'statement_type': stmt.statement_type,
            'period_end': str(stmt.period_end) if stmt.period_end else None,
            'freq': stmt.freq,
            'fetched_at': stmt.fetched_at,
            'data': stmt.data,
        })
    return Response(data)


@api_view(['GET'])
def symbol_earnings(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    freq = request.query_params.get('freq', 'yearly')
    data = []
    for r in sym.earnings_reports.filter(freq=freq):
        data.append({'freq': r.freq, 'period_end': str(r.period_end) if r.period_end else None, 'fetched_at': r.fetched_at, 'data': r.data})
    return Response(data)


@api_view(['GET'])
def symbol_earnings_estimate(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        est = sym.earnings_estimate
        return Response({'fetched_at': est.fetched_at, 'data': est.data})
    except Exception:
        return Response({'fetched_at': None, 'data': None})


@api_view(['GET'])
def symbol_analyst_recommendations(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        rec = sym.analyst_recommendations
        return Response({'fetched_at': rec.fetched_at, 'data': rec.data})
    except Exception:
        return Response({'fetched_at': None, 'data': None})


@api_view(['GET'])
def symbol_analyst_price_target(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        t = sym.analyst_price_target
        return Response({'fetched_at': t.fetched_at, 'data': t.data})
    except Exception:
        return Response({'fetched_at': None, 'data': None})


@api_view(['GET'])
def symbol_ownership(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    data = [{'holder_type': o.holder_type, 'fetched_at': o.fetched_at, 'data': o.data} for o in sym.ownership_snapshots.all()]
    return Response(data)


@api_view(['GET'])
def symbol_news(request, symbol):
    sym = _get_symbol_or_404(symbol)
    if not sym:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    limit = request.query_params.get('limit', 20)
    try:
        limit = min(int(limit), 100)
    except ValueError:
        limit = 20
    items = sym.news_items.all()[:limit]
    data = [
        {'title': n.title, 'link': n.link, 'published_at': n.published_at.isoformat() if n.published_at else None, 'publisher': n.publisher, 'summary': n.summary, 'data': n.data}
        for n in items
    ]
    return Response(data)


@api_view(['POST'])
def symbol_refresh_extra(request, symbol):
    from .services_extra import refresh_extra_data, EXTRA_DATA_TYPES
    types = request.data.get('types') if isinstance(request.data, dict) else None
    if types is not None and not isinstance(types, list):
        types = [t.strip() for t in str(types).split(',') if t.strip()]
    result = refresh_extra_data(symbol, types=types)
    return Response(result)


PREFERENCE_KEY_WATCHLIST_COLUMNS = 'watchlist_columns'


@api_view(['GET'])
def watchlist_preference(request):
    """Get watchlist table preference (e.g. visible_columns). Returns { value }."""
    from .models import WatchlistTablePreference
    pref, _ = WatchlistTablePreference.objects.get_or_create(
        key=PREFERENCE_KEY_WATCHLIST_COLUMNS,
        defaults={'value': {}},
    )
    return Response({'value': pref.value})


@api_view(['POST'])
def watchlist_preference_save(request):
    """Save watchlist table preference. Body: { value } or { visible_columns }."""
    from .models import WatchlistTablePreference
    data = request.data if isinstance(request.data, dict) else {}
    value = data.get('value')
    if value is None and 'visible_columns' in data:
        value = {'visible_columns': data['visible_columns']}
    if value is None:
        value = {}
    pref, _ = WatchlistTablePreference.objects.update_or_create(
        key=PREFERENCE_KEY_WATCHLIST_COLUMNS,
        defaults={'value': value},
    )
    return Response({'value': pref.value})
