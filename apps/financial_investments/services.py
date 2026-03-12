"""
Fetch stock data from yfinance and persist to Symbol + PricePoint.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from .models import Symbol, PricePoint, Dividend


def fetch_and_store(symbol, start_date=None, end_date=None):
    """
    Fetch history for symbol from yfinance, ensure Symbol exists, replace date range in DB.
    Returns dict with created, from_date, to_date, and optional error.
    """
    import yfinance as yf

    symbol_upper = symbol.strip().upper()
    if not symbol_upper:
        return {'error': 'Symbol is required'}

    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=365 * 2))  # default 2 years

    try:
        ticker = yf.Ticker(symbol_upper)
        df = ticker.history(start=start_date, end=end_date, auto_adjust=False)
    except Exception as e:
        return {'error': str(e)}

    if df is None or df.empty:
        return {'created': 0, 'from_date': str(start_date), 'to_date': str(end_date), 'message': 'No data returned'}

    sym, _ = Symbol.objects.get_or_create(
        symbol=symbol_upper,
        defaults={'name': '', 'exchange': ''},
    )
    # Optionally update name/exchange from ticker.info (can be slow)
    try:
        info = ticker.info
        if info:
            if info.get('longName'):
                sym.name = (info['longName'] or '')[:255]
            if info.get('exchange'):
                sym.exchange = (info['exchange'] or '')[:50]
            sym.save(update_fields=['name', 'exchange', 'updated_at'])
    except Exception:
        pass

    # Delete existing points in range so we replace with fresh data
    PricePoint.objects.filter(symbol=sym, date__gte=start_date, date__lte=end_date).delete()

    # Map DataFrame columns (yfinance uses Capitalized names)
    points = []
    for dt, row in df.iterrows():
        if hasattr(dt, 'date'):
            d = dt.date() if hasattr(dt, 'date') else dt
        else:
            d = dt
        open_ = row.get('Open')
        high = row.get('High')
        low = row.get('Low')
        close = row.get('Close')
        vol = row.get('Volume')
        adj = row.get('Adj Close') if 'Adj Close' in row else None
        if open_ is None or high is None or low is None or close is None:
            continue
        try:
            points.append(
                PricePoint(
                    symbol=sym,
                    date=d,
                    open=Decimal(str(open_)),
                    high=Decimal(str(high)),
                    low=Decimal(str(low)),
                    close=Decimal(str(close)),
                    volume=int(vol) if vol is not None and not (isinstance(vol, float) and (vol != vol)) else None,
                    adjusted_close=Decimal(str(adj)) if adj is not None else None,
                )
            )
        except (ValueError, TypeError):
            continue

    with transaction.atomic():
        PricePoint.objects.bulk_create(points)

    return {
        'created': len(points),
        'from_date': str(start_date),
        'to_date': str(end_date),
    }


def get_history_from_db(symbol, start_date=None, end_date=None):
    """
    Return list of dicts with date, open, high, low, close, volume, adjusted_close for the symbol.
    """
    symbol_upper = symbol.strip().upper()
    qs = PricePoint.objects.filter(symbol__symbol=symbol_upper).order_by('date')
    if start_date:
        qs = qs.filter(date__gte=start_date)
    if end_date:
        qs = qs.filter(date__lte=end_date)
    return [
        {
            'date': str(pp.date),
            'open': float(pp.open),
            'high': float(pp.high),
            'low': float(pp.low),
            'close': float(pp.close),
            'volume': pp.volume,
            'adjusted_close': float(pp.adjusted_close) if pp.adjusted_close is not None else None,
        }
        for pp in qs
    ]


def get_symbols_with_latest_prices():
    """
    Return list of symbol dicts with last_close, prev_close, change, change_pct,
    pe_ratio, forward_pe, dividend_yield, recommendation (buy/hold/sell), and Investec mapping.
    """
    from .models import SymbolInfo, AnalystRecommendation

    symbols = Symbol.objects.select_related('share_name_mapping').prefetch_related(
        'info', 'analyst_recommendations'
    ).order_by('symbol')
    result = []
    one_year_ago = date.today() - timedelta(days=365)
    for s in symbols:
        points = list(PricePoint.objects.filter(symbol=s).order_by('-date')[:2])
        last_close = prev_close = change = change_pct = None
        if len(points) >= 1:
            last_close = float(points[0].close)
        if len(points) >= 2:
            prev_close = float(points[1].close)
            if prev_close and prev_close != 0:
                change = last_close - prev_close
                change_pct = round((change / prev_close) * 100, 2)

        pe_ratio = forward_pe = dividend_yield = recommendation = None
        try:
            info = s.info
            if info and info.data:
                d = info.data
                if d.get('trailingPE') is not None:
                    try:
                        pe_ratio = round(float(d['trailingPE']), 2)
                    except (TypeError, ValueError):
                        pass
                if d.get('forwardPE') is not None:
                    try:
                        forward_pe = round(float(d['forwardPE']), 2)
                    except (TypeError, ValueError):
                        pass
                for key in ('yield', 'dividendYield', 'trailingAnnualDividendYield', 'dividendRate'):
                    if d.get(key) is not None:
                        try:
                            val = float(d[key])
                            if key == 'dividendRate' and last_close and last_close > 0:
                                dividend_yield = round(val / last_close * 100, 2)
                            else:
                                dividend_yield = round(val * 100, 2) if val <= 1 else round(val, 2)
                            break
                        except (TypeError, ValueError):
                            pass
                rec = d.get('recommendationKey') or d.get('recommendation')
                if rec and isinstance(rec, str):
                    recommendation = rec.strip().lower()
                    if recommendation in ('strong_buy', 'buy'):
                        recommendation = 'Buy'
                    elif recommendation in ('hold', 'neutral'):
                        recommendation = 'Hold'
                    elif recommendation in ('sell', 'strong_sell'):
                        recommendation = 'Sell'
                    else:
                        recommendation = rec.strip().title()
        except Exception:
            pass

        if dividend_yield is None and last_close and last_close > 0:
            from django.db.models import Sum
            agg = s.dividends.filter(date__gte=one_year_ago).aggregate(Sum('amount'))
            trailing_total = agg.get('amount__sum')
            if trailing_total is not None and trailing_total > 0:
                try:
                    dividend_yield = round(float(trailing_total) / last_close * 100, 2)
                except (TypeError, ValueError):
                    pass

        if recommendation is None:
            try:
                rec_obj = s.analyst_recommendations
                if rec_obj and isinstance(rec_obj.data, list) and len(rec_obj.data) > 0:
                    first = rec_obj.data[0]
                    if isinstance(first, dict):
                        for key in ('toGrade', 'grade', 'recommendation', 'newGrade'):
                            if first.get(key):
                                g = str(first[key]).strip().upper()
                                if 'BUY' in g or 'STRONG' in g:
                                    recommendation = 'Buy'
                                elif 'HOLD' in g or 'NEUTRAL' in g:
                                    recommendation = 'Hold'
                                elif 'SELL' in g:
                                    recommendation = 'Sell'
                                else:
                                    recommendation = str(first[key]).strip().title()
                                break
            except Exception:
                pass

        row = {
            'symbol': s.symbol,
            'name': s.name,
            'exchange': s.exchange,
            'category': s.category or '',
            'created_at': s.created_at,
            'updated_at': s.updated_at,
            'last_close': last_close,
            'prev_close': prev_close,
            'change': change,
            'change_pct': change_pct,
            'pe_ratio': pe_ratio,
            'forward_pe': forward_pe,
            'dividend_yield': dividend_yield,
            'recommendation': recommendation,
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
        result.append(row)
    return result


def _close_on_or_before(symbol_obj, d):
    """Return close price on date d or the most recent trading day on or before d, or None."""
    pp = PricePoint.objects.filter(symbol=symbol_obj, date__lte=d).order_by('-date').first()
    return float(pp.close) if pp and pp.close else None


def get_dividends_with_yield(symbol_str):
    """
    Return dividends for the symbol with paid_on (ex-date) and yield_pct.
    yield_pct = (amount / close on ex-date) * 100. Also returns trailing_dividend_yield_pct
    (sum of dividends in last 12 months / latest price) when price data exists.
    """
    symbol_upper = (symbol_str or '').strip().upper()
    if not symbol_upper:
        return {'dividends': [], 'trailing_dividend_yield_pct': None}
    try:
        sym = Symbol.objects.get(symbol=symbol_upper)
    except Symbol.DoesNotExist:
        return {'dividends': [], 'trailing_dividend_yield_pct': None}
    dividends_qs = sym.dividends.all()
    latest_price = _close_on_or_before(sym, date.today())
    one_year_ago = date.today() - timedelta(days=365)
    dividends_list = []
    trailing_total = 0
    for d in dividends_qs:
        close = _close_on_or_before(sym, d.date)
        yield_pct = None
        if close and close > 0:
            try:
                yield_pct = round(float(d.amount) / close * 100, 2)
            except (TypeError, ValueError):
                pass
        if d.date >= one_year_ago and d.amount:
            try:
                trailing_total += float(d.amount)
            except (TypeError, ValueError):
                pass
        dividends_list.append({
            'date': str(d.date),
            'paid_on': str(d.date),
            'amount': float(d.amount),
            'currency': d.currency or '',
            'yield_pct': yield_pct,
            'price_on_date': close,
        })
    trailing_dividend_yield_pct = None
    if latest_price and latest_price > 0 and trailing_total > 0:
        trailing_dividend_yield_pct = round(trailing_total / latest_price * 100, 2)
    return {
        'dividends': dividends_list,
        'trailing_dividend_yield_pct': trailing_dividend_yield_pct,
    }
