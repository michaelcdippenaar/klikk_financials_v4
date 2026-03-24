"""
Skill: Dividend Forecast — read and adjust budget DPS in listed_share_pln_forecast.

When a company declares its dividend, this skill computes the adjustment needed
(declared DPS minus base DPS) and writes it to the adjustment_declared_dividend
input_type so that TM1 rules produce the correct total forecast.

Also provides a daily background job that checks yfinance for newly declared
dividends and auto-writes adjustments to TM1.
"""
from __future__ import annotations

import asyncio
import datetime
import decimal
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import psycopg2
import psycopg2.extras

import logging

from TM1py import TM1Service
from apps.ai_agent.agent.config import TM1_CONFIG, settings

log = logging.getLogger('ai_agent')

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

CUBE_NAME = "listed_share_pln_forecast"
DEFAULT_ENTITY = "41ebfa0e-012e-4ff1-82ba-a9a7585c536c"  # Klikk (Pty) Ltd
DEFAULT_VERSION = "budget"
INPUT_TYPE_ADJUSTMENT = "adjustment_declared_dividend"
MEASURE_DPS = "dividends_per_share"

# TM1 transaction type leaf elements per dividend category
TXN_TYPE_MAP = {
    "regular": "Dividend",
    "special": "Special Dividend",
    "foreign": "Foreign Dividend",
}

_FINANCIALS_DSN = dict(
    host=settings.pg_financials_host,
    port=settings.pg_financials_port,
    dbname=settings.pg_financials_db,
    user=settings.pg_financials_user,
    password=settings.pg_financials_password,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _serialize(v: Any) -> Any:
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _get_all_consolidator(tm1: TM1Service, dimension: str) -> str:
    """Find the 'All_*' consolidator element for a dimension."""
    elements = tm1.elements.get_element_names(dimension, dimension)
    for el in elements:
        if el.startswith("All_"):
            return el
    return elements[0] if elements else dimension


def _read_dps_values(
    tm1: TM1Service,
    listed_share: str,
    year: str,
    month: str,
    entity: str,
    version: str,
) -> dict[str, float | None]:
    """Read All_Input_Type and adjustment_declared_dividend DPS values from TM1."""
    txn_type_all = _get_all_consolidator(tm1, "listed_share_transaction_type")

    # Read All_Input_Type (consolidation = total forecast)
    try:
        all_input_types_dps = tm1.cells.get_value(
            CUBE_NAME,
            f"{year},{month},{version},{entity},{listed_share},{txn_type_all},All_Input_Type,{MEASURE_DPS}",
        )
    except Exception:
        all_input_types_dps = None

    # Read current declared_dividend value
    try:
        adjustment_dps = tm1.cells.get_value(
            CUBE_NAME,
            f"{year},{month},{version},{entity},{listed_share},{txn_type_all},{INPUT_TYPE_ADJUSTMENT},{MEASURE_DPS}",
        )
    except Exception:
        adjustment_dps = None

    def _to_float(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "all_input_types_dps": _to_float(all_input_types_dps),
        "declared_dividend_dps": _to_float(adjustment_dps),
    }


MONTH_MAP = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}
MONTH_MAP_REV = {v: k for k, v in MONTH_MAP.items()}


def _find_calculated_month(
    tm1: TM1Service,
    listed_share: str,
    ex_date: datetime.date,
    entity: str,
    version: str,
) -> tuple[str, str, float]:
    """Probe TM1 to find the month with non-zero calculated base DPS.

    Checks the ex-date month and the following month. Returns (year_str, month_str, base_dps).
    Falls back to ex-date month if neither has a calculated value.
    """
    candidates = [(ex_date.year, ex_date.month)]
    if ex_date.month == 12:
        candidates.append((ex_date.year + 1, 1))
    else:
        candidates.append((ex_date.year, ex_date.month + 1))

    for year, month in candidates:
        year_str = str(year)
        month_str = MONTH_MAP[month]
        values = _read_dps_values(tm1, listed_share, year_str, month_str, entity, version)
        all_dps = values["all_input_types_dps"] or 0.0
        current_adj = values["declared_dividend_dps"] or 0.0
        base_dps = all_dps - current_adj
        if base_dps != 0.0:
            log.info("TM1 probe: %s has calculated base %.4f in %s %s",
                     listed_share, base_dps, month_str, year_str)
            return year_str, month_str, base_dps

    # No calculated value found — fall back to ex_date month
    fallback_year = str(ex_date.year)
    fallback_month = MONTH_MAP[ex_date.month]
    log.info("TM1 probe: %s has no calculated base in %s or %s — using ex_date month %s",
             listed_share, MONTH_MAP[candidates[0][1]], MONTH_MAP[candidates[1][1]], fallback_month)
    return fallback_year, fallback_month, 0.0


# ---------------------------------------------------------------------------
#  Tool A: get_dividend_forecast (read-only)
# ---------------------------------------------------------------------------

def get_dividend_forecast(
    listed_share: str,
    year: str,
    month: str,
    entity: str = DEFAULT_ENTITY,
    version: str = DEFAULT_VERSION,
) -> dict[str, Any]:
    """
    Read the current dividend forecast for a listed share from TM1.
    Shows the total DPS (All_Input_Type), any adjustment_declared_dividend, and the base DPS.

    listed_share: Share code element (e.g. 'ABG', 'SBK').
    year: Year element (e.g. '2026').
    month: Month element (e.g. 'Mar').
    entity: Entity GUID. Default: Klikk (Pty) Ltd.
    version: Version element. Default: 'budget'.
    """
    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            # Validate listed_share exists
            if not tm1.elements.exists("listed_share", "listed_share", listed_share):
                return {"error": f"Element '{listed_share}' not found in listed_share dimension."}

            values = _read_dps_values(tm1, listed_share, year, month, entity, version)
    except Exception as e:
        return {"error": f"TM1 connection failed: {e}"}

    all_dps = values["all_input_types_dps"] or 0.0
    adj_dps = values["declared_dividend_dps"] or 0.0
    base_dps = all_dps - adj_dps

    return {
        "cube": CUBE_NAME,
        "listed_share": listed_share,
        "year": year,
        "month": month,
        "entity": entity,
        "version": version,
        "all_input_types_dps": round(all_dps, 6),
        "declared_dividend_dps": round(adj_dps, 6),
        "base_dps": round(base_dps, 6),
    }


# ---------------------------------------------------------------------------
#  Tool B: adjust_dividend_forecast (write with dry-run)
# ---------------------------------------------------------------------------

def adjust_dividend_forecast(
    listed_share: str,
    declared_dps: float,
    year: str,
    month: str,
    entity: str = DEFAULT_ENTITY,
    confirm: bool = False,
    dividend_category: str = "regular",
) -> dict[str, Any]:
    """
    Adjust the budget DPS forecast when a company declares its dividend.
    Computes adjustment = declared_dps - base_dps (where base = All_Input_Type - current adjustment).
    Writes to listed_share_pln_forecast at input_type:adjustment_declared_dividend, measure:dividends_per_share.

    Writes to the correct TM1 transaction type leaf element based on dividend_category:
      - regular  → 'Dividend'
      - special  → 'Special Dividend'
      - foreign  → 'Foreign Dividend'

    IMPORTANT: set confirm=True to actually write. Default is safe dry-run.

    listed_share: Share code element (e.g. 'ABG').
    declared_dps: The declared dividend per share.
    year: Year element (e.g. '2026').
    month: Month in which dividend is expected (e.g. 'Mar').
    entity: Entity GUID. Default: Klikk (Pty) Ltd.
    confirm: Set True to write. Default False (dry-run).
    dividend_category: 'regular', 'special', or 'foreign'. Determines TM1 transaction type.
    """
    txn_type_write = TXN_TYPE_MAP.get(dividend_category, "Dividend")

    try:
        with TM1Service(**TM1_CONFIG) as tm1:
            # Validate listed_share exists
            if not tm1.elements.exists("listed_share", "listed_share", listed_share):
                return {"error": f"Element '{listed_share}' not found in listed_share dimension."}

            txn_type_all = _get_all_consolidator(tm1, "listed_share_transaction_type")
            values = _read_dps_values(tm1, listed_share, year, month, entity, DEFAULT_VERSION)

            all_dps = values["all_input_types_dps"] or 0.0
            current_adj = values["declared_dividend_dps"] or 0.0
            base_dps = all_dps - current_adj

            # Compute the new adjustment value
            new_adjustment = declared_dps - base_dps

            # For recently-purchased shares with no base data, base_dps will be 0
            # and the full declared_dps becomes the adjustment — this is correct behaviour
            if base_dps == 0.0 and current_adj == 0.0:
                log.info("Zero base DPS for %s %s %s — likely recently purchased, "
                         "using full declared DPS as adjustment", listed_share, year, month)

            coordinates = (
                year, month, DEFAULT_VERSION, entity, listed_share,
                txn_type_write, INPUT_TYPE_ADJUSTMENT, MEASURE_DPS,
            )

            result = {
                "cube": CUBE_NAME,
                "listed_share": listed_share,
                "year": year,
                "month": month,
                "entity": entity,
                "declared_dps": declared_dps,
                "base_dps": round(base_dps, 6),
                "current_adjustment": round(current_adj, 6),
                "new_adjustment": round(new_adjustment, 6),
                "resulting_total_dps": round(base_dps + new_adjustment, 6),
                "dividend_category": dividend_category,
                "txn_type": txn_type_write,
                "coordinates": list(coordinates),
                "zero_base": base_dps == 0.0 and current_adj == 0.0,
            }

            if not confirm:
                result["status"] = "dry_run"
                result["message"] = "Dry run only. Set confirm=True to write the adjustment."
                return result

            # Write the adjustment
            tm1.cells.write_value(
                new_adjustment,
                CUBE_NAME,
                coordinates,
            )

            result["status"] = "written"
            result["message"] = f"Adjustment {new_adjustment:.6f} written to TM1 ({txn_type_write})."
            return result

    except Exception as e:
        return {"error": f"Failed: {e}"}


# ---------------------------------------------------------------------------
#  Tool C: check_declared_dividends
# ---------------------------------------------------------------------------

def check_declared_dividends(
    listed_share: str = "",
) -> dict[str, Any]:
    """
    Check yfinance for declared/upcoming dividends for held shares.
    Saves new declarations to the DividendCalendar table in PostgreSQL.

    listed_share: Optional — check a specific share code only. If empty, checks all held shares.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance is not installed. Run: pip install yfinance"}

    # Get share-to-symbol mappings from PostgreSQL
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                if listed_share:
                    cur.execute("""
                        SELECT s.id, s.symbol, m.share_code
                        FROM financial_investments_symbol s
                        LEFT JOIN investec_investecjsesharenamemapping m
                            ON s.share_name_mapping_id = m.id
                        WHERE UPPER(m.share_code) = UPPER(%s)
                           OR UPPER(s.symbol) LIKE UPPER(%s)
                        LIMIT 10
                    """, (listed_share, f"%{listed_share}%"))
                else:
                    # Get all symbols that have a share_code mapping (i.e. held shares)
                    cur.execute("""
                        SELECT s.id, s.symbol, m.share_code
                        FROM financial_investments_symbol s
                        JOIN investec_investecjsesharenamemapping m
                            ON s.share_name_mapping_id = m.id
                        WHERE m.share_code IS NOT NULL
                    """)
                symbols = [dict(row) for row in cur.fetchall()]
    except Exception as e:
        return {"error": f"PostgreSQL query failed: {e}"}

    if not symbols:
        return {"message": "No symbols found to check.", "results": []}

    def _fetch_ticker_info(symbol_str: str) -> dict:
        try:
            return yf.Ticker(symbol_str).info or {}
        except Exception as e:
            return {"_error": str(e)}

    # Fetch yfinance data concurrently (8 workers, 10s timeout per ticker)
    ticker_infos: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_sym = {
            pool.submit(_fetch_ticker_info, row["symbol"]): row["symbol"]
            for row in symbols
        }
        for future in as_completed(future_to_sym):
            sym = future_to_sym[future]
            try:
                ticker_infos[sym] = future.result(timeout=10)
            except Exception as e:
                ticker_infos[sym] = {"_error": str(e)}

    results = []
    for sym_row in symbols:
        symbol_str = sym_row["symbol"]
        share_code = sym_row.get("share_code") or ""
        symbol_id = sym_row["id"]

        info = ticker_infos.get(symbol_str, {})
        if "_error" in info:
            results.append({
                "symbol": symbol_str,
                "share_code": share_code,
                "error": info["_error"],
            })
            continue

        ex_date_ts = info.get("exDividendDate")
        dividend_rate = info.get("dividendRate")
        last_div_value = info.get("lastDividendValue")

        ex_date = None
        if ex_date_ts:
            try:
                if isinstance(ex_date_ts, (int, float)):
                    ex_date = datetime.date.fromtimestamp(ex_date_ts)
                elif isinstance(ex_date_ts, str):
                    ex_date = datetime.date.fromisoformat(ex_date_ts[:10])
            except Exception:
                pass

        pay_date = None
        pay_date_ts = info.get("dividendDate")
        if pay_date_ts:
            try:
                if isinstance(pay_date_ts, (int, float)):
                    pay_date = datetime.date.fromtimestamp(pay_date_ts)
            except Exception:
                pass

        amount = last_div_value or dividend_rate
        if amount is None:
            results.append({
                "symbol": symbol_str,
                "share_code": share_code,
                "status": "no_dividend_info",
            })
            continue

        # Auto-detect dividend category based on exchange
        # Non-.JO symbols are foreign; .JO symbols are domestic (regular by default)
        # Special dividends must be manually classified — yfinance doesn't distinguish
        dividend_category = "foreign" if not symbol_str.upper().endswith(".JO") else "regular"

        saved = False
        if ex_date:
            try:
                with psycopg2.connect(**_FINANCIALS_DSN) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT id, tm1_adjustment_written FROM financial_investments_dividendcalendar
                            WHERE symbol_id = %s AND ex_dividend_date = %s
                        """, (symbol_id, ex_date))
                        existing = cur.fetchone()

                        if not existing:
                            cur.execute("""
                                INSERT INTO financial_investments_dividendcalendar
                                (symbol_id, ex_dividend_date, payment_date, amount, currency,
                                 status, dividend_category, source, tm1_adjustment_written, created_at, updated_at)
                                VALUES (%s, %s, %s, %s, %s, 'declared', %s, 'yfinance', false, NOW(), NOW())
                            """, (symbol_id, ex_date, pay_date, amount,
                                  info.get("currency", ""), dividend_category))
                            conn.commit()
                            saved = True

                            cur.execute("""
                                INSERT INTO financial_investments_dividend
                                (symbol_id, date, amount, currency, dividend_type)
                                VALUES (%s, %s, %s, %s, 'dividend_declared')
                                ON CONFLICT DO NOTHING
                            """, (symbol_id, ex_date, amount, info.get("currency", "")))
                            conn.commit()
                        else:
                            saved = False
            except Exception as e:
                log.warning("Failed to save dividend calendar for %s: %s", symbol_str, e)

        results.append({
            "symbol": symbol_str,
            "share_code": share_code,
            "ex_dividend_date": _serialize(ex_date),
            "payment_date": _serialize(pay_date),
            "amount": float(amount) if amount else None,
            "dividend_rate": float(dividend_rate) if dividend_rate else None,
            "currency": info.get("currency", ""),
            "dividend_category": dividend_category,
            "new_record_saved": saved,
        })

    return {"results": results, "checked": len(symbols)}


# ---------------------------------------------------------------------------
#  Daily background job
# ---------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="div-cal")


def _run_dividend_calendar_update() -> dict[str, Any]:
    """Synchronous function that checks for declared dividends and auto-writes TM1 adjustments."""
    log.info("Dividend calendar update: starting daily check")
    t0 = time.monotonic()

    # 1. Check for declared dividends
    check_result = check_declared_dividends()
    if "error" in check_result:
        log.error("Dividend calendar update failed: %s", check_result["error"])
        return check_result

    # 2. Find calendar entries that need TM1 adjustment
    # Special dividends are excluded — they are NOT budgeted, only input once declared manually
    adjustments_written = 0
    skipped_special = 0
    try:
        with psycopg2.connect(**_FINANCIALS_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT dc.id, dc.amount, dc.ex_dividend_date, dc.payment_date,
                           dc.currency, dc.dividend_category,
                           s.symbol, m.share_code
                    FROM financial_investments_dividendcalendar dc
                    JOIN financial_investments_symbol s ON dc.symbol_id = s.id
                    LEFT JOIN investec_investecjsesharenamemapping m
                        ON s.share_name_mapping_id = m.id
                    WHERE dc.tm1_adjustment_written = false
                      AND dc.status = 'declared'
                      AND dc.amount IS NOT NULL
                      AND m.share_code IS NOT NULL
                """)
                pending = [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error("Dividend calendar update: DB query failed: %s", e)
        return {"error": str(e)}

    for entry in pending:
        share_code = entry["share_code"]
        declared_dps = float(entry["amount"])
        ex_date = entry["ex_dividend_date"]
        pay_date = entry.get("payment_date")
        dividend_category = entry.get("dividend_category") or "regular"

        # Skip special dividends — they are NOT budgeted for
        if dividend_category == "special":
            skipped_special += 1
            log.info("Dividend calendar: skipping special dividend for %s (%s)",
                     share_code, ex_date)
            continue

        # Determine the correct TM1 month:
        # 1. Manual payment_date (highest priority)
        # 2. TM1 probe — find month with non-zero calculated base
        # 3. Ex-date month (fallback)
        if pay_date:
            year_str = str(pay_date.year)
            month_str = MONTH_MAP.get(pay_date.month, "Jan")
            log.info("Dividend calendar: using payment_date %s for %s", pay_date, share_code)
        else:
            try:
                with TM1Service(**TM1_CONFIG) as tm1_probe:
                    year_str, month_str, probed_base = _find_calculated_month(
                        tm1_probe, share_code, ex_date, DEFAULT_ENTITY, DEFAULT_VERSION)
            except Exception as e:
                log.warning("TM1 probe failed for %s, falling back to ex_date: %s", share_code, e)
                year_str = str(ex_date.year)
                month_str = MONTH_MAP.get(ex_date.month, "Jan")

        # Write adjustment to TM1, routing to the correct transaction type element
        result = adjust_dividend_forecast(
            listed_share=share_code,
            declared_dps=declared_dps,
            year=year_str,
            month=month_str,
            entity=DEFAULT_ENTITY,
            confirm=True,
            dividend_category=dividend_category,
        )

        if result.get("status") == "written":
            # Mark as written in DB with adjustment value and timestamp
            try:
                with psycopg2.connect(**_FINANCIALS_DSN) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE financial_investments_dividendcalendar
                            SET tm1_adjustment_written = true,
                                tm1_adjustment_value = %s,
                                tm1_written_at = NOW(),
                                tm1_target_month = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (result.get("new_adjustment", 0), month_str, entry["id"]))
                        conn.commit()
                adjustments_written += 1
                log.info("Dividend calendar: wrote %s adjustment for %s (%s %s): %s",
                         dividend_category, share_code, month_str, year_str,
                         result.get("new_adjustment"))
            except Exception as e:
                log.warning("Failed to update tm1_adjustment_written for calendar %s: %s", entry["id"], e)
        elif "error" in result:
            log.warning("Dividend calendar: TM1 write failed for %s: %s", share_code, result["error"])

    duration = int((time.monotonic() - t0) * 1000)
    log.info("Dividend calendar update complete: %d adjustments written (%dms)",
             adjustments_written, duration)

    return {
        "checked": check_result.get("checked", 0),
        "adjustments_written": adjustments_written,
        "skipped_special": skipped_special,
        "pending_found": len(pending),
        "duration_ms": duration,
    }


async def dividend_calendar_loop(interval_hours: int = 24):
    """Background async loop that runs the dividend calendar update once per day."""
    log.info("Dividend calendar loop started (interval=%dh)", interval_hours)

    # Initial delay to let the app start up
    await asyncio.sleep(30)

    loop = asyncio.get_event_loop()

    while True:
        try:
            result = await loop.run_in_executor(_executor, _run_dividend_calendar_update)
            log.info("Dividend calendar loop result: %s", result)
        except Exception:
            log.warning("Dividend calendar loop error", exc_info=True)

        await asyncio.sleep(interval_hours * 3600)


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_dividend_forecast",
        "description": (
            "Read the current dividend forecast for a listed share from TM1. "
            "Shows the total DPS (All_Input_Type), any adjustment_declared_dividend, "
            "and the base DPS (calculated by rules). "
            "Use this to see what the budget currently expects before making adjustments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "listed_share": {"type": "string", "description": "Share code element in listed_share dimension (e.g. 'ABG', 'SBK')."},
                "year": {"type": "string", "description": "Year element (e.g. '2026')."},
                "month": {"type": "string", "description": "Month element (e.g. 'Mar')."},
                "entity": {"type": "string", "description": "Entity GUID. Default: Klikk (Pty) Ltd.", "default": DEFAULT_ENTITY},
                "version": {"type": "string", "description": "Version element. Default: 'budget'.", "default": DEFAULT_VERSION},
            },
            "required": ["listed_share", "year", "month"],
        },
    },
    {
        "name": "adjust_dividend_forecast",
        "description": (
            "Adjust the budget DPS forecast when a company declares its dividend. "
            "Computes adjustment = declared_dps - base_dps and writes to "
            "listed_share_pln_forecast (input_type:adjustment_declared_dividend, measure:dividends_per_share). "
            "IMPORTANT: set confirm=True to actually write. Default is safe dry-run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "listed_share": {"type": "string", "description": "Share code element (e.g. 'ABG')."},
                "declared_dps": {"type": "number", "description": "The declared dividend per share."},
                "year": {"type": "string", "description": "Year element (e.g. '2026')."},
                "month": {"type": "string", "description": "Month in which dividend is expected (e.g. 'Mar')."},
                "entity": {"type": "string", "description": "Entity GUID. Default: Klikk (Pty) Ltd.", "default": DEFAULT_ENTITY},
                "confirm": {"type": "boolean", "description": "Set True to write. Default False (dry-run).", "default": False},
            },
            "required": ["listed_share", "declared_dps", "year", "month"],
        },
    },
    {
        "name": "check_declared_dividends",
        "description": (
            "Check yfinance for declared/upcoming dividends for held shares. "
            "Saves new declarations to the DividendCalendar table in PostgreSQL. "
            "Optionally filter to a specific share code. "
            "The daily background job calls this automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "listed_share": {"type": "string", "description": "Optional share code to check (e.g. 'ABG'). If empty, checks all held shares."},
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "get_dividend_forecast": get_dividend_forecast,
    "adjust_dividend_forecast": adjust_dividend_forecast,
    "check_declared_dividends": check_declared_dividends,
}
