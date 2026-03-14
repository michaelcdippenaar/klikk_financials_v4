---
name: find-share-data
description: Answer questions about shares, stocks, dividends, portfolios, holdings, transactions, investments, share cubes, and dimensions. Search by share code, company name, or answer broad questions like upcoming dividends, portfolio performance, share analysis, cube data, and dimension lookups. Searches local database tables and the internet for JSE and global share information.
user-invocable: true
argument-hint: [share-code-or-name ...]
allowed-tools: Bash, Read, Grep, Glob, WebSearch, WebFetch
---

# Find Share Data

Research share/stock information for the given share codes or company names: **$ARGUMENTS**

## Step 1: Search Local Database (PostgreSQL)

Connect to the PostgreSQL database and search for share data. Use `psql` commands against `klikk_financials_v4` on host `192.168.1.235` port `5432` user `klikk_user`.

The following Django apps and models hold share data:

**Investec app** (`apps/investec`):
- **Investec JSE Share Name Mappings** → `investec_investecjsesharenamemapping`
- **Investec JSE Transactions** → `investec_investecjsetransaction`
- **Investec JSE Portfolios** → `investec_investecjseportfolio`
- **Investec JSE Share Monthly Performances** → `investec_investecjsesharemonthlyperformance`

**Financial Investments app** (`apps/financial_investments`):
- **Symbol** → `financial_investments_symbol`
- **PricePoint** → `financial_investments_pricepoint`
- **Dividend** → `financial_investments_dividend`
- **SymbolInfo** → `financial_investments_symbolinfo`
- **FinancialStatement** → `financial_investments_financialstatement`
- **EarningsReport** → `financial_investments_earningsreport`
- **AnalystRecommendation** → `financial_investments_analystrecommendation`
- **AnalystPriceTarget** → `financial_investments_analystpricetarget`
- **NewsItem** → `financial_investments_newsitem`

For each share code or name provided, run these queries:

### 1a. Investec JSE Share Name Mappings
```sql
SELECT share_name, share_name2, share_name3, company, share_code
FROM investec_investecjsesharenamemapping
WHERE UPPER(share_code) LIKE UPPER('%<arg>%')
   OR UPPER(company) LIKE UPPER('%<arg>%')
   OR UPPER(share_name) LIKE UPPER('%<arg>%');
```

### 1b. Financial Investments Symbols
```sql
SELECT s.symbol, s.name, s.exchange, s.category
FROM financial_investments_symbol s
WHERE UPPER(s.symbol) LIKE UPPER('%<arg>%')
   OR UPPER(s.name) LIKE UPPER('%<arg>%');
```

### 1c. Investec JSE Portfolios (latest holdings)
```sql
SELECT company, share_code, quantity, currency, unit_cost, total_cost, price, total_value,
       move_percent, portfolio_percent, profit_loss, annual_income_zar, date
FROM investec_investecjseportfolio
WHERE UPPER(share_code) LIKE UPPER('%<arg>%')
   OR UPPER(company) LIKE UPPER('%<arg>%')
ORDER BY date DESC
LIMIT 5;
```

### 1d. Investec JSE Transactions (recent buys/sells/dividends)
```sql
SELECT date, account_number, share_name, type, quantity, value, value_per_share, dividend_ttm
FROM investec_investecjsetransaction
WHERE UPPER(share_name) LIKE UPPER('%<arg>%')
ORDER BY date DESC
LIMIT 10;
```

### 1e. Financial Investments Price Data (latest from yfinance)
```sql
SELECT p.date, p.open, p.high, p.low, p.close, p.volume, p.adjusted_close
FROM financial_investments_pricepoint p
JOIN financial_investments_symbol s ON p.symbol_id = s.id
WHERE UPPER(s.symbol) LIKE UPPER('%<arg>%')
ORDER BY p.date DESC
LIMIT 5;
```

### 1f. Investec JSE Share Monthly Performances (dividends & yield)
```sql
SELECT share_name, date, dividend_type, investec_account, dividend_ttm, closing_price,
       quantity, total_market_value, dividend_yield
FROM investec_investecjsesharemonthlyperformance
WHERE UPPER(share_name) LIKE UPPER('%<arg>%')
ORDER BY date DESC
LIMIT 5;
```

### 1g. Financial Investments Dividends
```sql
SELECT d.date, d.amount, d.currency
FROM financial_investments_dividend d
JOIN financial_investments_symbol s ON d.symbol_id = s.id
WHERE UPPER(s.symbol) LIKE UPPER('%<arg>%')
ORDER BY d.date DESC
LIMIT 10;
```

### 1h. Analyst Recommendations & Price Targets (if available)
```sql
SELECT data FROM financial_investments_analystrecommendation
WHERE symbol_id = (SELECT id FROM financial_investments_symbol WHERE UPPER(symbol) LIKE UPPER('%<arg>%'))
ORDER BY fetched_at DESC LIMIT 1;

SELECT data FROM financial_investments_analystpricetarget
WHERE symbol_id = (SELECT id FROM financial_investments_symbol WHERE UPPER(symbol) LIKE UPPER('%<arg>%'))
ORDER BY fetched_at DESC LIMIT 1;
```

### 1i. Recent News from DB
```sql
SELECT title, publisher, link, published_at
FROM financial_investments_newsitem
WHERE symbol_id = (SELECT id FROM financial_investments_symbol WHERE UPPER(symbol) LIKE UPPER('%<arg>%'))
ORDER BY published_at DESC
LIMIT 5;
```

## Step 2: Search the Internet

Use WebSearch to find current information about each share:

- Current share price and market data
- Recent news and announcements
- Analyst ratings or consensus
- Key financial metrics (PE ratio, dividend yield, market cap)
- Any significant recent events

Search queries to try:
- `"<share_code> JSE share price"` for JSE-listed shares
- `"<share_code> stock price"` for international shares
- `"<company_name> share news"` for recent developments

## Step 3: Compile and Present Results

Present findings in a clear, structured format:

1. **Share Identity** - Share code, company name, exchange, category
2. **Current Holdings** - Quantities held, cost basis, current value, profit/loss
3. **Price Data** - Latest prices from DB and current price from web
4. **Dividends** - TTM dividend, yield
5. **Recent Transactions** - Latest buys/sells
6. **Market Intelligence** - News, analyst views, key metrics from web search
7. **Portal Link** - Reference: http://192.168.1.235:9000/app/pipeline/financial-investments

If a share code is not found in the local database, note this and suggest it may need to be added to the tracked symbols.
