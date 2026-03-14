"""
EOD Historical Data (eodhd.com) integration — fetch stock data, fundamentals,
and exchange ticker lists as an alternative/complement to yfinance.

API docs: https://eodhd.com/financial-apis/

JSE exchange code: "JSE" (e.g. NED.JSE, SOL.JSE)

Usage:
    from apps.financial_investments.services_eod import (
        fetch_exchange_tickers,
        fetch_fundamentals,
        fetch_eod_prices,
        fetch_dividends_eod,
        bulk_fundamentals,
    )

Requires EOD_API_KEY in settings or environment.
"""
import logging
import os
from datetime import date, timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Symbol,
    PricePoint,
    Dividend,
    SymbolInfo,
)

logger = logging.getLogger(__name__)

EOD_BASE_URL = "https://eodhd.com/api"


def _get_api_key():
    """Retrieve EOD API key from Django settings or environment."""
    key = getattr(settings, "EOD_API_KEY", None)
    if key:
        return key.strip()
    return os.environ.get("EOD_API_KEY", "").strip()


def _eod_get(endpoint, params=None, timeout=30):
    """Make authenticated GET request to EOD Historical Data API."""
    api_key = _get_api_key()
    if not api_key:
        return None, "EOD_API_KEY not configured. Set it in .env or Django settings."

    url = f"{EOD_BASE_URL}/{endpoint}"
    params = params or {}
    params["api_token"] = api_key
    params.setdefault("fmt", "json")

    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json(), None


def _jse_to_eod(symbol_str):
    """Convert yfinance-style symbol (NED.JO) to EOD format (NED.JSE)."""
    s = (symbol_str or "").strip().upper()
    if s.endswith(".JO"):
        return s[:-3] + ".JSE"
    if "." not in s:
        return s + ".JSE"
    return s


def _eod_to_yfinance(eod_code, exchange="JSE"):
    """Convert EOD ticker (NED) + exchange (JSE) to yfinance format (NED.JO)."""
    code = (eod_code or "").strip().upper()
    if exchange.upper() == "JSE":
        return f"{code}.JO"
    return code


# ---------------------------------------------------------------------------
#  Exchange ticker list — solve the "full JSE stock list" problem
# ---------------------------------------------------------------------------

def fetch_exchange_tickers(exchange="JSE"):
    """
    Fetch all tickers listed on an exchange from EOD Historical Data.

    Returns list of dicts: [{"Code": "NED", "Name": "Nedbank Group", "Exchange": "JSE", ...}, ...]
    This solves the problem of getting a complete JSE stock list.
    """
    data, err = _eod_get(f"exchange-symbol-list/{exchange}")
    if err:
        return {"tickers": [], "error": err}
    if not isinstance(data, list):
        return {"tickers": [], "error": "Unexpected response format"}
    return {"tickers": data, "count": len(data)}


def import_exchange_tickers(exchange="JSE", category="equity", dry_run=False):
    """
    Fetch all tickers from an exchange and create Symbol records.

    Returns dict with created, skipped, total counts.
    """
    result = fetch_exchange_tickers(exchange)
    if result.get("error"):
        return result

    tickers = result["tickers"]
    existing = set(Symbol.objects.values_list("symbol", flat=True))

    created = 0
    skipped = 0
    imported = []

    for item in tickers:
        code = (item.get("Code") or "").strip().upper()
        name = (item.get("Name") or "")[:255]
        item_type = (item.get("Type") or "").lower()
        item_exchange = (item.get("Exchange") or exchange).upper()

        if not code:
            continue

        # Map EOD type to our category
        cat = category
        if item_type == "etf":
            cat = "etf"
        elif item_type in ("index", "indices"):
            cat = "index"
        elif item_type in ("forex", "currency"):
            cat = "forex"

        yf_symbol = _eod_to_yfinance(code, item_exchange)

        if yf_symbol in existing:
            skipped += 1
            continue

        if not dry_run:
            Symbol.objects.get_or_create(
                symbol=yf_symbol,
                defaults={
                    "name": name,
                    "exchange": item_exchange,
                    "category": cat,
                },
            )
        created += 1
        imported.append({"code": code, "symbol": yf_symbol, "name": name, "type": cat})

    return {
        "exchange": exchange,
        "total_on_exchange": len(tickers),
        "created": created,
        "skipped": skipped,
        "imported": imported[:50],  # cap preview
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
#  Fundamentals — P/E, dividend yield, sector, market cap, etc.
# ---------------------------------------------------------------------------

def fetch_fundamentals(symbol_str):
    """
    Fetch company fundamentals from EOD and store in SymbolInfo.data.

    Includes: P/E, dividend yield, sector, market cap, EPS, beta, etc.
    Maps EOD fields to the same keys yfinance uses so investment_screen works unchanged.
    """
    eod_sym = _jse_to_eod(symbol_str)

    data, err = _eod_get(f"fundamentals/{eod_sym}", params={"filter": "General,Highlights,Valuation,SharesStats,Technicals"})
    if err:
        return {"error": err}

    if not data or not isinstance(data, dict):
        return {"error": f"No fundamentals data for {eod_sym}"}

    general = data.get("General") or {}
    highlights = data.get("Highlights") or {}
    valuation = data.get("Valuation") or {}
    shares = data.get("SharesStats") or {}
    technicals = data.get("Technicals") or {}

    # Map to yfinance-compatible keys for compatibility with investment_screen
    info = {
        "longName": general.get("Name"),
        "sector": general.get("Sector"),
        "industry": general.get("Industry"),
        "country": general.get("CountryName"),
        "currency": general.get("CurrencyCode"),
        "exchange": general.get("Exchange"),
        "isin": general.get("ISIN"),
        "description": general.get("Description"),
        "marketCap": highlights.get("MarketCapitalization"),
        "trailingPE": highlights.get("PERatio"),
        "forwardPE": valuation.get("ForwardPE"),
        "dividendYield": _safe_decimal_to_float(highlights.get("DividendYield")),
        "dividendShare": highlights.get("DividendShare"),
        "payoutRatio": _safe_decimal_to_float(valuation.get("PayoutRatio")),
        "trailingEps": highlights.get("EarningsShare"),
        "beta": technicals.get("Beta"),
        "fiftyTwoWeekHigh": highlights.get("52WeekHigh"),
        "fiftyTwoWeekLow": highlights.get("52WeekLow"),
        "averageVolume": shares.get("SharesFloat"),
        "sharesOutstanding": shares.get("SharesOutstanding"),
        "bookValue": highlights.get("BookValue"),
        "priceToBook": valuation.get("PriceBookMRQ"),
        "returnOnEquity": highlights.get("ReturnOnEquityTTM"),
        "returnOnAssets": highlights.get("ReturnOnAssetsTTM"),
        "revenuePerShare": highlights.get("RevenuePerShareTTM"),
        "profitMargin": highlights.get("ProfitMargin"),
        "operatingMargin": highlights.get("OperatingMarginTTM"),
        "wallStreetTargetPrice": highlights.get("WallStreetTargetPrice"),
        # Preserve EOD source marker
        "_source": "eodhd",
        "_eod_symbol": eod_sym,
    }

    # Remove None values
    info = {k: v for k, v in info.items() if v is not None}

    # Store in SymbolInfo
    yf_symbol = symbol_str.strip().upper()
    sym, _ = Symbol.objects.get_or_create(
        symbol=yf_symbol,
        defaults={"name": info.get("longName", ""), "exchange": info.get("exchange", "")},
    )

    # Update symbol name/exchange if we got better data
    updated_fields = []
    if info.get("longName") and not sym.name:
        sym.name = info["longName"][:255]
        updated_fields.append("name")
    if info.get("exchange") and not sym.exchange:
        sym.exchange = info["exchange"][:50]
        updated_fields.append("exchange")
    if updated_fields:
        updated_fields.append("updated_at")
        sym.save(update_fields=updated_fields)

    SymbolInfo.objects.update_or_create(
        symbol=sym,
        defaults={"data": info, "fetched_at": timezone.now()},
    )

    return {"updated": True, "symbol": yf_symbol, "fields": list(info.keys())}


def _safe_decimal_to_float(val):
    """Convert EOD decimal values; EOD returns dividendYield as 0.05 for 5%."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  EOD Prices — historical OHLCV data
# ---------------------------------------------------------------------------

def fetch_eod_prices(symbol_str, start_date=None, end_date=None):
    """
    Fetch historical end-of-day prices from EOD Historical Data.
    Stores in PricePoint table (same as yfinance).
    """
    eod_sym = _jse_to_eod(symbol_str)
    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=365 * 2))

    data, err = _eod_get(f"eod/{eod_sym}", params={
        "from": str(start_date),
        "to": str(end_date),
        "period": "d",
    })
    if err:
        return {"error": err}

    if not data or not isinstance(data, list):
        return {"created": 0, "message": "No price data returned"}

    yf_symbol = symbol_str.strip().upper()
    sym, _ = Symbol.objects.get_or_create(
        symbol=yf_symbol,
        defaults={"name": "", "exchange": ""},
    )

    # Delete existing points in range
    PricePoint.objects.filter(symbol=sym, date__gte=start_date, date__lte=end_date).delete()

    points = []
    for row in data:
        try:
            d = date.fromisoformat(row["date"])
            points.append(PricePoint(
                symbol=sym,
                date=d,
                open=Decimal(str(row.get("open", 0))),
                high=Decimal(str(row.get("high", 0))),
                low=Decimal(str(row.get("low", 0))),
                close=Decimal(str(row.get("close", 0))),
                volume=int(row["volume"]) if row.get("volume") else None,
                adjusted_close=Decimal(str(row["adjusted_close"])) if row.get("adjusted_close") else None,
            ))
        except (ValueError, TypeError, KeyError):
            continue

    with transaction.atomic():
        PricePoint.objects.bulk_create(points)

    return {"created": len(points), "from_date": str(start_date), "to_date": str(end_date)}


# ---------------------------------------------------------------------------
#  EOD Dividends
# ---------------------------------------------------------------------------

def fetch_dividends_eod(symbol_str):
    """Fetch dividend history from EOD and store in Dividend table."""
    eod_sym = _jse_to_eod(symbol_str)

    data, err = _eod_get(f"div/{eod_sym}", params={"from": "2010-01-01"})
    if err:
        return {"error": err}

    if not data or not isinstance(data, list):
        return {"created": 0}

    yf_symbol = symbol_str.strip().upper()
    sym, _ = Symbol.objects.get_or_create(
        symbol=yf_symbol,
        defaults={"name": "", "exchange": ""},
    )

    to_create = []
    for row in data:
        try:
            d = date.fromisoformat(row["date"])
            amount = Decimal(str(row.get("value", 0)))
            currency = row.get("currency", "")
            to_create.append(Dividend(symbol=sym, date=d, amount=amount, currency=currency))
        except (ValueError, TypeError, KeyError):
            continue

    with transaction.atomic():
        Dividend.objects.filter(symbol=sym).delete()
        Dividend.objects.bulk_create(to_create)

    return {"created": len(to_create)}


# ---------------------------------------------------------------------------
#  Bulk operations
# ---------------------------------------------------------------------------

def bulk_fundamentals(symbols=None, exchange="JSE", delay=0.3):
    """
    Fetch fundamentals for multiple symbols. If symbols is None, fetch for all
    Symbol records in the database.

    Returns summary dict.
    """
    import time

    if symbols is None:
        symbols = list(Symbol.objects.values_list("symbol", flat=True).order_by("symbol"))

    ok = 0
    errors = {}
    for sym in symbols:
        result = fetch_fundamentals(sym)
        if result.get("error"):
            errors[sym] = result["error"]
        else:
            ok += 1
        time.sleep(delay)

    return {"ok": ok, "errors": errors, "total": len(symbols)}
