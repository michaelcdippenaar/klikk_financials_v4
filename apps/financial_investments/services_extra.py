"""
Fetch and store extra yfinance data: dividends, splits, company info, financials, earnings,
analyst data, ownership, news. All stored as rows or JSONB.
"""
from datetime import datetime
from decimal import Decimal

import pandas as pd

from django.db import transaction
from django.utils import timezone

from .models import (
    Symbol,
    Dividend,
    Split,
    SymbolInfo,
    FinancialStatement,
    EarningsReport,
    EarningsEstimate,
    AnalystRecommendation,
    AnalystPriceTarget,
    OwnershipSnapshot,
    NewsItem,
)


def _json_serializable(obj):
    """Convert pandas/numpy types to JSON-serializable Python types. Dict keys must be str for JSON."""
    if obj is None or (isinstance(obj, float) and pd.isna(obj)):
        return None
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat() if hasattr(obj, 'isoformat') else str(obj)
    if isinstance(obj, pd.DataFrame):
        # Convert to dict; orient 'split' gives lists and avoids Timestamp keys, or we stringify keys
        try:
            d = obj.to_dict(orient='split')
            return _json_serializable(d)
        except Exception:
            d = obj.to_dict()
            return {_json_key(k): _json_serializable(v) for k, v in d.items()}
    if isinstance(obj, pd.Series):
        return {_json_key(k): _json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {_json_key(k): _json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_serializable(v) for v in obj]
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and (obj != obj or obj == float('inf') or obj == float('-inf')):
            return None
        return obj
    return obj


def _json_key(k):
    """Ensure key is JSON-serializable (str, int, float, bool, None)."""
    if k is None or isinstance(k, (str, int, float, bool)):
        return k
    if isinstance(k, (pd.Timestamp, datetime)):
        return k.isoformat() if hasattr(k, 'isoformat') else str(k)
    return str(k)


def _get_or_create_symbol(symbol_str):
    symbol_upper = (symbol_str or '').strip().upper()
    if not symbol_upper:
        return None, 'Symbol is required'
    sym, _ = Symbol.objects.get_or_create(
        symbol=symbol_upper,
        defaults={'name': '', 'exchange': ''},
    )
    return sym, None


def fetch_dividends(symbol_str, period='max'):
    """Fetch dividends from yfinance and store. Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        series = ticker.get_dividends(period=period)
    except Exception as e:
        return {'error': str(e)}
    if series is None or series.empty:
        return {'created': 0}
    to_create = []
    for dt, amount in series.items():
        d = dt.date() if hasattr(dt, 'date') else dt
        if amount is None or (isinstance(amount, float) and (amount != amount)):
            continue
        try:
            to_create.append(
                Dividend(symbol=sym, date=d, amount=Decimal(str(amount)), currency='')
            )
        except (ValueError, TypeError):
            continue
    with transaction.atomic():
        Dividend.objects.filter(symbol=sym).delete()
        Dividend.objects.bulk_create(to_create)
    return {'created': len(to_create)}


def fetch_splits(symbol_str, period='max'):
    """Fetch stock splits from yfinance and store. Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        series = ticker.get_splits(period=period)
    except Exception as e:
        return {'error': str(e)}
    if series is None or series.empty:
        return {'created': 0}
    to_create = []
    for dt, ratio in series.items():
        d = dt.date() if hasattr(dt, 'date') else dt
        if ratio is None or (isinstance(ratio, float) and (ratio != ratio)):
            continue
        try:
            to_create.append(Split(symbol=sym, date=d, ratio=Decimal(str(ratio))))
        except (ValueError, TypeError):
            continue
    with transaction.atomic():
        Split.objects.filter(symbol=sym).delete()
        Split.objects.bulk_create(to_create)
    return {'created': len(to_create)}


def fetch_company_info(symbol_str):
    """Fetch ticker.info and store in SymbolInfo.data (JSONB).
    Tries yfinance first; falls back to EOD Historical Data if yfinance fails
    and EOD_API_KEY is configured. Returns {updated, error, source}.
    """
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}

    # Try yfinance first
    info = None
    yf_error = None
    try:
        ticker = yf.Ticker(sym.symbol)
        info = ticker.get_info()
        if info and isinstance(info, dict) and info.get('regularMarketPrice') is not None:
            data = _json_serializable(info)
            SymbolInfo.objects.update_or_create(
                symbol=sym,
                defaults={'data': data, 'fetched_at': timezone.now()},
            )
            return {'updated': True, 'source': 'yfinance'}
        yf_error = 'No usable data from yfinance'
    except Exception as e:
        yf_error = str(e)

    # Fallback to EOD Historical Data
    try:
        from .services_eod import fetch_fundamentals, _get_api_key
        if _get_api_key():
            result = fetch_fundamentals(symbol_str)
            if result.get('updated'):
                return {'updated': True, 'source': 'eodhd'}
    except Exception:
        pass

    # If yfinance returned some data (even partial), store it
    if info and isinstance(info, dict):
        data = _json_serializable(info)
        SymbolInfo.objects.update_or_create(
            symbol=sym,
            defaults={'data': data, 'fetched_at': timezone.now()},
        )
        return {'updated': True, 'source': 'yfinance (partial)'}

    return {'error': yf_error or 'No data from yfinance or EOD', 'updated': False}


def fetch_financial_statements(symbol_str, freq='yearly'):
    """Fetch income_stmt, balance_sheet, cash_flow and store as JSONB. Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    ticker = yf.Ticker(sym.symbol)
    created = 0
    for stmt_type, getter in [
        ('income_stmt', ticker.get_income_stmt),
        ('balance_sheet', ticker.get_balance_sheet),
        ('cash_flow', ticker.get_cash_flow),
    ]:
        try:
            df = getter(freq=freq)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        data = _json_serializable(df)
        period_end = None
        if isinstance(data, dict) and data:
            # orient 'split' gives columns/index/data; otherwise keys may be period dates
            if 'index' in data and data['index']:
                try:
                    first_idx = data['index'][0]
                    if isinstance(first_idx, str):
                        period_end = pd.Timestamp(first_idx).date()
                    elif hasattr(first_idx, 'date'):
                        period_end = first_idx.date() if callable(getattr(first_idx, 'date', None)) else None
                except Exception:
                    pass
            else:
                keys = [k for k in data.keys() if k not in ('columns', 'index', 'data')]
                if keys:
                    try:
                        k0 = keys[0]
                        if isinstance(k0, str) and len(k0) >= 10:
                            period_end = pd.Timestamp(k0).date()
                    except Exception:
                        pass
        FinancialStatement.objects.update_or_create(
            symbol=sym,
            statement_type=stmt_type,
            freq=freq,
            defaults={'data': data, 'period_end': period_end, 'fetched_at': timezone.now()},
        )
        created += 1
    return {'created': created}


def fetch_earnings(symbol_str, freq='yearly'):
    """Fetch get_earnings and store in EarningsReport (JSONB). Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        df = ticker.get_earnings(freq=freq)
    except Exception as e:
        return {'error': str(e)}
    if df is None or df.empty:
        return {'created': 0}
    data = _json_serializable(df)
    with transaction.atomic():
        EarningsReport.objects.filter(symbol=sym, freq=freq).delete()
        EarningsReport.objects.create(symbol=sym, freq=freq, data=data)
    return {'created': 1}


def fetch_earnings_estimate(symbol_str):
    """Fetch get_earnings_estimate and store in EarningsEstimate.data (JSONB). Returns {updated, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        df = ticker.get_earnings_estimate(as_dict=False)
    except Exception as e:
        return {'error': str(e)}
    if df is None or df.empty:
        return {'updated': False}
    data = _json_serializable(df)
    EarningsEstimate.objects.update_or_create(
        symbol=sym,
        defaults={'data': data, 'fetched_at': timezone.now()},
    )
    return {'updated': True}


def fetch_analyst_recommendations(symbol_str):
    """Fetch get_recommendations and store in AnalystRecommendation.data (JSONB list). Returns {updated, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        df = ticker.get_recommendations(as_dict=False)
    except Exception as e:
        return {'error': str(e)}
    if df is None or df.empty:
        return {'updated': False}
    # DataFrame -> list of dicts (one per row)
    try:
        data = df.reset_index().to_dict('records')
    except Exception:
        data = _json_serializable(df)
    data = _json_serializable(data)
    if not isinstance(data, list):
        data = [data] if data is not None else []
    AnalystRecommendation.objects.update_or_create(
        symbol=sym,
        defaults={'data': data, 'fetched_at': timezone.now()},
    )
    return {'updated': True}


def fetch_analyst_price_target(symbol_str):
    """Fetch get_analyst_price_targets and store in AnalystPriceTarget.data (JSONB). Returns {updated, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        targets = ticker.get_analyst_price_targets()
    except Exception as e:
        return {'error': str(e)}
    if not targets:
        return {'updated': False}
    data = _json_serializable(targets)
    AnalystPriceTarget.objects.update_or_create(
        symbol=sym,
        defaults={'data': data, 'fetched_at': timezone.now()},
    )
    return {'updated': True}


def fetch_ownership(symbol_str):
    """Fetch institutional_holders, major_holders, insider_transactions and store as JSONB. Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    ticker = yf.Ticker(sym.symbol)
    created = 0
    getters = [
        ('institutional', lambda: ticker.get_institutional_holders(as_dict=True)),
        ('major', lambda: ticker.get_major_holders(as_dict=True)),
        ('insider_transactions', lambda: ticker.get_insider_transactions(as_dict=True)),
    ]
    for holder_type, getter in getters:
        try:
            raw = getter()
        except Exception:
            continue
        if raw is None:
            continue
        data = _json_serializable(raw)
        OwnershipSnapshot.objects.update_or_create(
            symbol=sym,
            holder_type=holder_type,
            defaults={'data': data, 'fetched_at': timezone.now()},
        )
        created += 1
    return {'created': created}


def fetch_news(symbol_str, count=10):
    """Fetch get_news and store as NewsItem rows. Returns {created, error}."""
    import yfinance as yf
    sym, err = _get_or_create_symbol(symbol_str)
    if err:
        return {'error': err}
    try:
        ticker = yf.Ticker(sym.symbol)
        items = ticker.get_news(count=count)
    except Exception as e:
        return {'error': str(e)}
    if not items:
        return {'created': 0}
    created = 0
    with transaction.atomic():
        NewsItem.objects.filter(symbol=sym).delete()
        for item in items:
            if isinstance(item, dict):
                title = (str(item.get('title') or item.get('link') or '') or '')[:500]
                link = (str(item.get('link') or item.get('url') or '') or '')[:1000]
                pub = item.get('publishTime') or item.get('published_at') or item.get('providerPublishTime')
                publisher = (str(item.get('publisher') or item.get('source') or '') or '')[:200]
                raw_summary = item.get('summary') or item.get('content') or ''
                summary = (str(raw_summary) if raw_summary is not None else '')[:10000]
                data = _json_serializable({k: v for k, v in item.items() if k not in ('title', 'link', 'url', 'publishTime', 'publisher', 'source', 'summary', 'content')})
            else:
                title = (str(getattr(item, 'title', None) or getattr(item, 'link', None) or '') or '')[:500]
                link = (str(getattr(item, 'link', None) or getattr(item, 'url', None) or '') or '')[:1000]
                pub = getattr(item, 'providerPublishTime', None) or getattr(item, 'publishTime', None)
                publisher = (str(getattr(item, 'publisher', None) or getattr(item, 'source', None) or '') or '')[:200]
                raw_summary = getattr(item, 'summary', None) or getattr(item, 'content', None) or ''
                summary = (str(raw_summary) if raw_summary is not None else '')[:10000]
                data = {}
            if pub is not None:
                try:
                    if hasattr(pub, 'timestamp'):
                        published_at = timezone.make_aware(datetime.fromtimestamp(pub))
                    else:
                        published_at = timezone.make_aware(datetime.fromtimestamp(int(pub)))
                except Exception:
                    published_at = None
            else:
                published_at = None
            NewsItem.objects.create(
                symbol=sym,
                title=title or 'Untitled',
                link=link,
                published_at=published_at,
                publisher=publisher,
                summary=summary,
                data=data,
            )
            created += 1
    return {'created': created}


# ---------------------------------------------------------------------------
#  EOD Historical Data wrappers (used when EOD_API_KEY is configured)
# ---------------------------------------------------------------------------

def _fetch_eod_fundamentals(symbol_str):
    """Fetch fundamentals from EOD Historical Data (requires EOD_API_KEY)."""
    from .services_eod import fetch_fundamentals
    return fetch_fundamentals(symbol_str)


def _fetch_eod_dividends(symbol_str):
    """Fetch dividends from EOD Historical Data (requires EOD_API_KEY)."""
    from .services_eod import fetch_dividends_eod
    return fetch_dividends_eod(symbol_str)


def _fetch_eod_prices(symbol_str):
    """Fetch EOD prices from EOD Historical Data (requires EOD_API_KEY)."""
    from .services_eod import fetch_eod_prices
    return fetch_eod_prices(symbol_str)


# Keys for refresh_extra_data(symbol, types=[...])
EXTRA_DATA_TYPES = [
    'dividends',
    'splits',
    'company_info',
    'financial_statements',
    'earnings',
    'earnings_estimate',
    'analyst_recommendations',
    'analyst_price_target',
    'ownership',
    'news',
    # EOD Historical Data alternatives (require EOD_API_KEY)
    'eod_fundamentals',
    'eod_dividends',
    'eod_prices',
]

_FETCHERS = {
    'dividends': fetch_dividends,
    'splits': fetch_splits,
    'company_info': fetch_company_info,
    'financial_statements': fetch_financial_statements,
    'earnings': fetch_earnings,
    'earnings_estimate': fetch_earnings_estimate,
    'analyst_recommendations': fetch_analyst_recommendations,
    'analyst_price_target': fetch_analyst_price_target,
    'ownership': fetch_ownership,
    'news': fetch_news,
    # EOD alternatives
    'eod_fundamentals': _fetch_eod_fundamentals,
    'eod_dividends': _fetch_eod_dividends,
    'eod_prices': _fetch_eod_prices,
}


def refresh_extra_data(symbol_str, types=None):
    """
    Refresh extra yfinance data for a symbol. types: list of EXTRA_DATA_TYPES or None for all.
    Returns dict: { results: { type: result }, errors: { type: error } }.
    """
    if types is None:
        types = EXTRA_DATA_TYPES
    results = {}
    errors = {}
    for t in types:
        if t not in _FETCHERS:
            errors[t] = f'Unknown type: {t}'
            continue
        out = _FETCHERS[t](symbol_str)
        if out.get('error'):
            errors[t] = out['error']
        else:
            results[t] = out
    return {'results': results, 'errors': errors}
