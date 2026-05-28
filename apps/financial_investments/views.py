import datetime
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q
from django.utils.text import slugify
from django.utils.dateparse import parse_date

from .market_events import MAJOR_MARKET_EVENTS
from .models import Symbol, DividendCalendar, Dividend, NewsItem
from . import services

log = logging.getLogger(__name__)


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


@api_view(['GET'])
def symbol_buy_transactions(request, symbol):
    """Get Investec JSE buy transactions for a symbol. Query params: start_date, end_date, include_sells."""
    try:
        s = Symbol.objects.select_related('share_name_mapping').get(symbol=symbol.upper())
    except Symbol.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    include_sells = str(request.query_params.get('include_sells', '')).lower() in {'1', 'true', 'yes', 'y'}
    start_date = parse_date(start_date) if start_date else None
    end_date = parse_date(end_date) if end_date else None

    share_names = []
    share_code = ''
    if s.share_name_mapping_id:
        mapping = s.share_name_mapping
        share_code = mapping.share_code or ''
        share_names = [
            value.strip()
            for value in (mapping.share_name, mapping.share_name2, mapping.share_name3, mapping.share_code)
            if value and value.strip()
        ]
    if not share_names:
        share_names = [s.symbol.split('.')[0]]

    seen = set()
    share_names = [
        name
        for name in share_names
        if not (name.upper() in seen or seen.add(name.upper()))
    ]

    from apps.investec.models import InvestecJsePortfolio, InvestecJseTransaction

    name_filter = Q()
    for name in share_names:
        name_filter |= Q(share_name__iexact=name)

    trade_filter = Q(type__iexact='Buy')
    if include_sells:
        trade_filter |= Q(type__iexact='Sell') | Q(type__iexact='Sale') | Q(type__iexact='Sold')
    qs = InvestecJseTransaction.objects.filter(trade_filter).filter(name_filter)
    if start_date:
        qs = qs.filter(date__gte=start_date)
    if end_date:
        qs = qs.filter(date__lte=end_date)
    qs = qs.order_by('date', 'id')

    results = []
    for txn in qs:
        price = txn.value_per_share
        if price is None and txn.quantity:
            try:
                price = abs(txn.value) / txn.quantity
            except Exception:
                price = None
        results.append({
            'id': txn.id,
            'date': txn.date.isoformat() if txn.date else None,
            'share_name': txn.share_name,
            'quantity': float(txn.quantity) if txn.quantity is not None else None,
            'price': float(price) if price is not None else None,
            'value': float(txn.value) if txn.value is not None else None,
            'account_number': txn.account_number,
            'description': txn.description,
            'type': txn.type,
            'source': 'transaction',
        })

    if not results and share_code:
        first_holding = (
            InvestecJsePortfolio.objects
            .filter(share_code__iexact=share_code)
            .exclude(quantity=0)
            .order_by('date', 'id')
            .first()
        )
        if first_holding:
            portfolio_price = first_holding.unit_cost
            if portfolio_price is not None:
                # Portfolio imports are stored one scale lower than transaction prices.
                portfolio_price = portfolio_price * 100
            results.append({
                'id': f'portfolio-{first_holding.id}',
                'date': first_holding.date.isoformat() if first_holding.date else None,
                'share_name': first_holding.company,
                'quantity': float(first_holding.quantity) if first_holding.quantity is not None else None,
                'price': float(portfolio_price) if portfolio_price is not None else None,
                'value': float(first_holding.total_cost) if first_holding.total_cost is not None else None,
                'account_number': '',
                'description': 'Opening holding from Investec portfolio snapshot',
                'type': 'Buy',
                'source': 'portfolio',
            })

    return Response({
        'symbol': s.symbol,
        'share_code': share_code,
        'share_names': share_names,
        'results': results,
    })


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


def _truthy(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {'0', 'false', 'no', 'off'}


def _financial_news_doc_content(*, title, symbol, date, publisher, link, summary, event_type):
    lines = [
        f'# {title}',
        '',
        f'Type: {event_type}',
        f'Symbol: {symbol}',
        f'Date: {date or "unknown"}',
        f'Publisher: {publisher or "unknown"}',
    ]
    if link:
        lines.append(f'Link: {link}')
    if summary:
        lines.extend(['', summary])
    return '\n'.join(lines).strip()


@api_view(['POST'])
def symbol_vectorize_articles(request, symbol):
    """Create vector-search documents for a symbol's news plus global market events."""
    try:
        sym = Symbol.objects.get(symbol=symbol.upper())
    except Symbol.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    limit = request.data.get('limit', 30)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 30
    should_vectorize = _truthy(request.data.get('vectorize'), default=True)

    from apps.ai_agent.models import KnowledgeCorpus, SystemDocument
    from apps.ai_agent.services.vector_store import vectorize_corpus_documents

    corpus, _ = KnowledgeCorpus.objects.update_or_create(
        slug='financial-market-intelligence',
        defaults={
            'name': 'Financial Market Intelligence',
            'description': 'Stock-specific news and global market events for financial investment analysis.',
            'is_active': True,
        },
    )

    documents_written = 0
    stock_news_documents = 0
    market_event_documents = 0

    news_items = list(NewsItem.objects.filter(symbol=sym).order_by('-published_at', '-id')[:limit])
    for item in news_items:
        date_label = item.published_at.date().isoformat() if item.published_at else ''
        doc_slug = f"fi-news-{slugify(sym.symbol)[:24]}-{item.id}"
        content = _financial_news_doc_content(
            title=item.title or 'Untitled',
            symbol=sym.symbol,
            date=date_label,
            publisher=item.publisher,
            link=item.link,
            summary=item.summary,
            event_type='stock news',
        )
        SystemDocument.objects.update_or_create(
            slug=doc_slug[:120],
            defaults={
                'corpus': corpus,
                'title': item.title[:255] or 'Untitled',
                'content_markdown': content,
                'metadata': {
                    'type': 'stock_news',
                    'symbol': sym.symbol,
                    'news_item_id': item.id,
                    'published_at': item.published_at.isoformat() if item.published_at else None,
                    'publisher': item.publisher,
                    'link': item.link,
                },
                'is_active': True,
            },
        )
        documents_written += 1
        stock_news_documents += 1

    for event in MAJOR_MARKET_EVENTS:
        doc_slug = f"fi-market-{slugify(sym.symbol)[:24]}-{event['slug']}"
        content = _financial_news_doc_content(
            title=event['title'],
            symbol=sym.symbol,
            date=event['date'],
            publisher=event['publisher'],
            link=event.get('link', ''),
            summary=f"{event.get('summary', '')}\n\nScope: {event.get('scope', 'global')}",
            event_type='major market event',
        )
        SystemDocument.objects.update_or_create(
            slug=doc_slug[:120],
            defaults={
                'corpus': corpus,
                'title': event['title'][:255],
                'content_markdown': content,
                'metadata': {
                    'type': 'market_event',
                    'symbol': sym.symbol,
                    'event_slug': event['slug'],
                    'date': event['date'],
                    'publisher': event['publisher'],
                    'link': event.get('link', ''),
                    'scope': event.get('scope', ''),
                },
                'is_active': True,
            },
        )
        documents_written += 1
        market_event_documents += 1

    result = {
        'symbol': sym.symbol,
        'corpus_id': corpus.id,
        'corpus_slug': corpus.slug,
        'documents_written': documents_written,
        'stock_news_documents': stock_news_documents,
        'market_event_documents': market_event_documents,
        'vectorized': False,
    }

    if should_vectorize:
        try:
            vec_result = vectorize_corpus_documents(corpus=corpus, force=False)
            result.update({
                'vectorized': True,
                'embedding_model': vec_result.embedding_model,
                'documents_seen': vec_result.documents_seen,
                'chunks_written': vec_result.chunks_written,
                'chunks_deleted': vec_result.chunks_deleted,
            })
        except Exception as exc:
            result['vectorize_error'] = str(exc)

    return Response(result)


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


# ---------------------------------------------------------------------------
# Dividend Forecast Workflow (non-AI)
# ---------------------------------------------------------------------------

@api_view(['GET'])
def dividend_calendar_list(request):
    """List all dividend calendar entries with symbol + share_code info."""
    qs = DividendCalendar.objects.select_related(
        'symbol', 'symbol__share_name_mapping',
    ).order_by('-ex_dividend_date', '-created_at')

    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)

    pending_only = request.query_params.get('pending_tm1')
    if pending_only == '1':
        qs = qs.filter(tm1_adjustment_written=False)

    calendar_entries = list(qs[:200])

    # --- Build prior-year dividend lookup ---
    # Collect all symbol IDs and their ex-dates to find matching prior-year dividends
    symbol_ids = set()
    for dc in calendar_entries:
        if dc.ex_dividend_date:
            symbol_ids.add(dc.symbol_id)

    # Fetch historical dividends for all relevant symbols (past 2 years)
    prior_year_map = {}  # (symbol_id, approx_month_bucket) -> (date, amount_zar)
    if symbol_ids:
        two_years_ago = datetime.date.today() - datetime.timedelta(days=800)
        hist_divs = Dividend.objects.filter(
            symbol_id__in=symbol_ids,
            date__gte=two_years_ago,
        ).order_by('symbol_id', '-date')

        # Group by symbol
        from collections import defaultdict
        divs_by_symbol = defaultdict(list)
        for d in hist_divs:
            divs_by_symbol[d.symbol_id].append(d)

        # For each calendar entry, find the closest dividend ~1 year before
        for dc in calendar_entries:
            if not dc.ex_dividend_date or not dc.amount:
                continue
            target_date = dc.ex_dividend_date - datetime.timedelta(days=365)
            best = None
            best_delta = 999
            for d in divs_by_symbol.get(dc.symbol_id, []):
                delta = abs((d.date - target_date).days)
                if delta < best_delta and delta <= 90:
                    best = d
                    best_delta = delta
            if best:
                # yfinance get_dividends() returns cents for JSE (.JO) stocks,
                # while lastDividendValue (used in DividendCalendar) returns ZAR.
                # Convert historical cents → ZAR for JSE stocks so units match.
                hist_amount = float(best.amount)
                is_jse = dc.symbol.symbol.endswith('.JO')
                if is_jse:
                    hist_amount_zar = hist_amount / 100.0
                else:
                    hist_amount_zar = hist_amount
                prior_year_map[dc.id] = {
                    'amount': round(hist_amount_zar, 6),
                    'date': best.date.isoformat(),
                }

    rows = []
    for dc in calendar_entries:
        m = dc.symbol.share_name_mapping if dc.symbol.share_name_mapping_id else None
        current_amount = float(dc.amount) if dc.amount else None
        prior = prior_year_map.get(dc.id)
        prior_year_dps = prior['amount'] if prior else None
        prior_year_date = prior['date'] if prior else None
        pct_change = None
        if current_amount is not None and prior_year_dps and prior_year_dps != 0:
            pct_change = round(((current_amount - prior_year_dps) / prior_year_dps) * 100, 2)

        rows.append({
            'id': dc.id,
            'symbol': dc.symbol.symbol,
            'symbol_name': dc.symbol.name,
            'share_code': m.share_code if m else '',
            'company': m.company if m else dc.symbol.name,
            'declaration_date': dc.declaration_date.isoformat() if dc.declaration_date else None,
            'ex_dividend_date': dc.ex_dividend_date.isoformat() if dc.ex_dividend_date else None,
            'record_date': dc.record_date.isoformat() if dc.record_date else None,
            'payment_date': dc.payment_date.isoformat() if dc.payment_date else None,
            'amount': current_amount,
            'currency': dc.currency,
            'prior_year_dps': prior_year_dps,
            'prior_year_date': prior_year_date,
            'pct_change': pct_change,
            'status': dc.status,
            'dividend_category': dc.dividend_category,
            'source': dc.source,
            'tm1_adjustment_written': dc.tm1_adjustment_written,
            'tm1_adjustment_value': float(dc.tm1_adjustment_value) if dc.tm1_adjustment_value else None,
            'tm1_target_month': dc.tm1_target_month or '',
            'tm1_written_at': dc.tm1_written_at.isoformat() if dc.tm1_written_at else None,
            'tm1_verified': dc.tm1_verified,
            'tm1_verified_at': dc.tm1_verified_at.isoformat() if dc.tm1_verified_at else None,
            'last_checked_at': dc.last_checked_at.isoformat() if dc.last_checked_at else None,
            'created_at': dc.created_at.isoformat(),
        })
    return Response({'results': rows, 'count': len(rows)})


@api_view(['POST'])
def dividend_calendar_check(request):
    """Check yfinance for newly declared dividends for ALL held shares and save to DividendCalendar."""
    try:
        from django.utils import timezone
        from apps.ai_agent.skills.dividend_forecast import check_declared_dividends
        result = check_declared_dividends(listed_share='')  # always all shares
        # Update last_checked_at on all calendar entries that were just checked
        DividendCalendar.objects.filter(
            tm1_adjustment_written=False,
        ).update(last_checked_at=timezone.now())
        return Response(result)
    except Exception as e:
        log.exception("dividend_calendar_check failed")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
def dividend_forecast_read(request, share_code):
    """Read the current TM1 dividend forecast for a share code."""
    year = request.query_params.get('year', '')
    month = request.query_params.get('month', '')
    if not year or not month:
        return Response({'error': 'year and month query params required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        from apps.ai_agent.skills.dividend_forecast import get_dividend_forecast
        result = get_dividend_forecast(listed_share=share_code, year=year, month=month)
        return Response(result)
    except Exception as e:
        log.exception("dividend_forecast_read failed")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def dividend_forecast_adjust(request):
    """Write a TM1 adjustment for a declared dividend. Body: share_code, declared_dps, year, month, confirm, dividend_category."""
    share_code = request.data.get('share_code', '')
    declared_dps = request.data.get('declared_dps')
    year = request.data.get('year', '')
    month = request.data.get('month', '')
    confirm = request.data.get('confirm', False)
    dividend_category = request.data.get('dividend_category', 'regular')

    if not all([share_code, declared_dps is not None, year, month]):
        return Response({'error': 'share_code, declared_dps, year, month required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        from apps.ai_agent.skills.dividend_forecast import adjust_dividend_forecast
        result = adjust_dividend_forecast(
            listed_share=share_code,
            declared_dps=float(declared_dps),
            year=str(year),
            month=str(month),
            confirm=bool(confirm),
            dividend_category=dividend_category,
        )
        return Response(result)
    except Exception as e:
        log.exception("dividend_forecast_adjust failed")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def dividend_forecast_adjust_pending(request):
    """Write TM1 adjustments for all pending (tm1_adjustment_written=False) dividend calendar entries."""
    try:
        from apps.ai_agent.skills.dividend_forecast import _run_dividend_calendar_update
        result = _run_dividend_calendar_update()
        return Response(result)
    except Exception as e:
        log.exception("dividend_forecast_adjust_pending failed")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def dividend_calendar_update_category(request):
    """Update the dividend_category for a calendar entry. Body: id, dividend_category."""
    entry_id = request.data.get('id')
    category = request.data.get('dividend_category', '')
    if not entry_id or category not in ('regular', 'special', 'foreign'):
        return Response(
            {'error': 'id and dividend_category (regular/special/foreign) required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        dc = DividendCalendar.objects.get(id=entry_id)
        dc.dividend_category = category
        dc.save(update_fields=['dividend_category', 'updated_at'])
        return Response({'id': dc.id, 'dividend_category': dc.dividend_category, 'status': 'updated'})
    except DividendCalendar.DoesNotExist:
        return Response({'error': 'Entry not found'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
def dividend_calendar_update_payment_date(request):
    """Update the payment_date for a calendar entry. Body: id, payment_date (YYYY-MM-DD or null)."""
    entry_id = request.data.get('id')
    payment_date_str = request.data.get('payment_date')
    if not entry_id:
        return Response({'error': 'id required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        dc = DividendCalendar.objects.get(id=entry_id)
        if payment_date_str:
            pd = parse_date(payment_date_str)
            if not pd:
                return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=status.HTTP_400_BAD_REQUEST)
            dc.payment_date = pd
        else:
            dc.payment_date = None
        dc.save(update_fields=['payment_date', 'updated_at'])
        return Response({
            'id': dc.id,
            'payment_date': dc.payment_date.isoformat() if dc.payment_date else None,
            'status': 'updated',
        })
    except DividendCalendar.DoesNotExist:
        return Response({'error': 'Entry not found'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
def dividend_forecast_verify(request):
    """Verify TM1 adjustments: read TM1 for all written entries and confirm values match."""
    from django.utils import timezone

    MONTH_MAP = {
        1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
        7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec',
    }

    entries = DividendCalendar.objects.select_related(
        'symbol', 'symbol__share_name_mapping',
    ).filter(
        tm1_adjustment_written=True,
        symbol__share_name_mapping__share_code__isnull=False,
    ).order_by('-ex_dividend_date')

    if not entries.exists():
        return Response({'message': 'No written adjustments to verify.', 'results': []})

    try:
        from apps.ai_agent.skills.dividend_forecast import get_dividend_forecast
    except ImportError as e:
        return Response({'error': f'Cannot import dividend_forecast: {e}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    results = []
    verified_count = 0
    mismatch_count = 0
    error_count = 0
    now = timezone.now()

    for dc in entries:
        share_code = dc.symbol.share_name_mapping.share_code
        if not dc.ex_dividend_date:
            continue

        # Use tm1_target_month if set (from TM1 probe), else payment_date, else ex_date
        if dc.tm1_target_month:
            month_str = dc.tm1_target_month
            # Derive year from payment_date or ex_date
            target_date = dc.payment_date if dc.payment_date else dc.ex_dividend_date
            year_str = str(target_date.year)
        else:
            target_date = dc.payment_date if dc.payment_date else dc.ex_dividend_date
            year_str = str(target_date.year)
            month_str = MONTH_MAP.get(target_date.month, 'Jan')

        try:
            tm1_data = get_dividend_forecast(
                listed_share=share_code,
                year=year_str,
                month=month_str,
            )
        except Exception as e:
            results.append({
                'id': dc.id, 'share_code': share_code,
                'ex_dividend_date': dc.ex_dividend_date.isoformat(),
                'status': 'error', 'message': str(e),
            })
            error_count += 1
            continue

        if 'error' in tm1_data:
            results.append({
                'id': dc.id, 'share_code': share_code,
                'ex_dividend_date': dc.ex_dividend_date.isoformat(),
                'status': 'error', 'message': tm1_data['error'],
            })
            error_count += 1
            continue

        tm1_adj = tm1_data.get('declared_dividend_dps', 0) or 0
        db_adj = float(dc.tm1_adjustment_value) if dc.tm1_adjustment_value else 0
        match = abs(tm1_adj - db_adj) < 0.000001

        dc.tm1_verified = match
        dc.tm1_verified_at = now
        dc.save(update_fields=['tm1_verified', 'tm1_verified_at', 'updated_at'])

        if match:
            verified_count += 1
        else:
            mismatch_count += 1

        results.append({
            'id': dc.id,
            'share_code': share_code,
            'ex_dividend_date': dc.ex_dividend_date.isoformat(),
            'amount': float(dc.amount) if dc.amount else None,
            'db_adjustment': round(db_adj, 6),
            'tm1_adjustment': round(tm1_adj, 6),
            'tm1_total_dps': tm1_data.get('all_input_types_dps'),
            'tm1_base_dps': tm1_data.get('base_dps'),
            'match': match,
            'status': 'verified' if match else 'mismatch',
        })

    return Response({
        'results': results,
        'total': len(results),
        'verified': verified_count,
        'mismatches': mismatch_count,
        'errors': error_count,
    })
