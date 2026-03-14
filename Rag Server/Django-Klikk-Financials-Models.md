# Django Klikk Financials Models (RAG Reference)

This document is generated from Django model classes under `apps/**/models.py` in `klikk_financials_v4`.
It is intended as a retrieval-friendly reference for your RAG server.

- Generated source root: `/home/mc/apps/klikk_financials_v4`
- Model files scanned: `14`
- Model classes documented: `68`

## Apps Covered

- `ai_agent`
- `financial_investments`
- `investec`
- `planning_analytics`
- `xero/xero_auth`
- `xero/xero_core`
- `xero/xero_cube`
- `xero/xero_data`
- `xero/xero_metadata`
- `xero/xero_sync`
- `xero/xero_validation`
- `xero/xero_webhooks`

## File: `apps/ai_agent/models.py`

### Model: `AgentApprovalRequest`

- Meta:
  - `ordering`: `['-created_at', '-id']`
- Fields:
  - `session`: `ForeignKey` -> `AgentSession` (on_delete=models.CASCADE, related_name='approval_requests')
  - `tool_execution`: `OneToOneField` -> `AgentToolExecutionLog` (on_delete=models.CASCADE, related_name='approval_request', null=True, blank=True)
  - `action_name`: `CharField` (max_length=120)
  - `payload`: `JSONField` (default=dict, blank=True)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
  - `requested_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_agent_approval_requests')
  - `reviewed_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_agent_approvals_reviewed')
  - `review_note`: `TextField` (blank=True, default='')
  - `reviewed_at`: `DateTimeField` (null=True, blank=True)
  - `created_at`: `DateTimeField`

### Model: `AgentMessage`

- Meta:
  - `ordering`: `['id']`
- Fields:
  - `session`: `ForeignKey` -> `AgentSession` (on_delete=models.CASCADE, related_name='messages')
  - `role`: `CharField` (max_length=20, choices=ROLE_CHOICES)
  - `content`: `TextField`
  - `metadata`: `JSONField` (default=dict, blank=True)
  - `created_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_agent_messages')
  - `created_at`: `DateTimeField`

### Model: `AgentProject`

- Meta:
  - `ordering`: `['-updated_at', '-id']`
- Fields:
  - `slug`: `SlugField` (max_length=120, unique=True)
  - `name`: `CharField` (max_length=255)
  - `description`: `TextField` (blank=True, default='')
  - `memory`: `JSONField` (default=dict, blank=True)
  - `default_corpus`: `ForeignKey` -> `KnowledgeCorpus` (on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_projects')
  - `is_active`: `BooleanField` (default=True)
  - `created_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='created_ai_agent_projects')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `AgentSession`

- Meta:
  - `ordering`: `['-updated_at', '-id']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, null=True, blank=True, related_name='ai_agent_sessions')
  - `project`: `ForeignKey` -> `AgentProject` (on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions')
  - `title`: `CharField` (max_length=255, blank=True, default='')
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
  - `memory`: `JSONField` (default=dict, blank=True)
  - `created_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='created_ai_agent_sessions')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `AgentToolExecutionLog`

- Meta:
  - `ordering`: `['-started_at', '-id']`
- Fields:
  - `session`: `ForeignKey` -> `AgentSession` (on_delete=models.SET_NULL, null=True, blank=True, related_name='tool_executions')
  - `message`: `ForeignKey` -> `AgentMessage` (on_delete=models.SET_NULL, null=True, blank=True, related_name='tool_executions')
  - `tool_name`: `CharField` (max_length=120)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
  - `input_payload`: `JSONField` (default=dict, blank=True)
  - `output_payload`: `JSONField` (default=dict, blank=True)
  - `error_message`: `TextField` (blank=True, default='')
  - `executed_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_agent_tool_executions')
  - `started_at`: `DateTimeField`
  - `finished_at`: `DateTimeField` (null=True, blank=True)

### Model: `AgentViewState`

- Fields:
  - `session`: `ForeignKey` -> `AgentSession` (on_delete=models.CASCADE, null=True, blank=True, related_name='view_states')
  - `cube_name`: `CharField` (max_length=255)
  - `server_name`: `CharField` (max_length=255, blank=True, default='')
  - `query_state`: `TextField` (blank=True, default='')
  - `updated_at`: `DateTimeField`

### Model: `ConversationContext`

- Meta:
  - `ordering`: `['-created_at']`
- Fields:
  - `session`: `ForeignKey` -> `AgentSession` (on_delete=models.CASCADE, null=True, blank=True, related_name='conversation_contexts')
  - `session_external_id`: `CharField` (max_length=120, blank=True, default='')
  - `role`: `CharField` (max_length=20, blank=True, default='')
  - `content`: `TextField`
  - `embedding`: `JSONField` (default=list, blank=True)
  - `metadata`: `JSONField` (default=dict, blank=True)
  - `created_at`: `DateTimeField`

### Model: `Credential`

- Meta:
  - `ordering`: `['key']`
- Fields:
  - `key`: `CharField` (max_length=120, unique=True)
  - `value`: `TextField` (default='', blank=True)
  - `label`: `CharField` (max_length=255, blank=True, default='')
  - `updated_at`: `DateTimeField`

### Model: `GlobalContext`

- Meta:
  - `ordering`: `['-created_at']`
- Fields:
  - `content`: `TextField`
  - `metadata`: `JSONField` (default=dict, blank=True)
  - `embedding`: `JSONField` (default=list, blank=True)
  - `created_at`: `DateTimeField`

### Model: `GlossaryRefreshRequest`

- Fields:
  - `requested_at`: `DateTimeField`
  - `organisation_id`: `IntegerField` (null=True, blank=True)

### Model: `KnowledgeChunkEmbedding`

- Meta:
  - `ordering`: `['system_document_id', 'chunk_index']`
- Fields:
  - `corpus`: `ForeignKey` -> `KnowledgeCorpus` (on_delete=models.CASCADE, related_name='chunks')
  - `project`: `ForeignKey` -> `AgentProject` (on_delete=models.CASCADE, related_name='knowledge_chunks', null=True, blank=True)
  - `system_document`: `ForeignKey` -> `SystemDocument` (on_delete=models.CASCADE, related_name='knowledge_chunks')
  - `embedding_model`: `CharField` (max_length=120, default='text-embedding-3-small')
  - `source_hash`: `CharField` (max_length=64, db_index=True)
  - `chunk_index`: `PositiveIntegerField`
  - `chunk_text`: `TextField`
  - `embedding`: `JSONField` (default=list, blank=True)
  - `embedded_at`: `DateTimeField`

### Model: `KnowledgeCorpus`

- Meta:
  - `ordering`: `['-updated_at', '-id']`
- Fields:
  - `slug`: `SlugField` (max_length=120, unique=True)
  - `name`: `CharField` (max_length=255)
  - `description`: `TextField` (blank=True, default='')
  - `is_active`: `BooleanField` (default=True)
  - `created_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='created_ai_knowledge_corpora')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `SkillRegistry`

- Meta:
  - `ordering`: `['sort_order', 'module_name']`
  - `verbose_name`: `'Skill Registry Entry'`
  - `verbose_name_plural`: `'Skill Registry'`
- Fields:
  - `module_name`: `CharField` (max_length=120, unique=True)
  - `import_path`: `CharField` (max_length=255)
  - `display_name`: `CharField` (max_length=255, blank=True, default='')
  - `description`: `TextField` (blank=True, default='')
  - `keywords`: `JSONField` (default=list, blank=True)
  - `always_on`: `BooleanField` (default=False)
  - `enabled`: `BooleanField` (default=True)
  - `sort_order`: `IntegerField` (default=100)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `SystemDocument`

- Meta:
  - `ordering`: `['-updated_at', '-id']`
- Fields:
  - `project`: `ForeignKey` -> `AgentProject` (on_delete=models.SET_NULL, null=True, blank=True, related_name='system_documents')
  - `corpus`: `ForeignKey` -> `KnowledgeCorpus` (on_delete=models.SET_NULL, null=True, blank=True, related_name='system_documents')
  - `slug`: `SlugField` (max_length=120, unique=True)
  - `title`: `CharField` (max_length=255, blank=True, default='')
  - `content_markdown`: `TextField` (blank=True, default='')
  - `pin_to_context`: `BooleanField` (default=False)
  - `context_order`: `IntegerField` (default=0)
  - `metadata`: `JSONField` (default=dict, blank=True)
  - `is_active`: `BooleanField` (default=True)
  - `created_by`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (on_delete=models.SET_NULL, null=True, blank=True, related_name='created_system_documents')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

## File: `apps/financial_investments/models.py`

### Model: `AnalystPriceTarget`

- Meta:
  - `verbose_name`: `'Analyst price target'`
  - `verbose_name_plural`: `'Analyst price targets'`
- Fields:
  - `symbol`: `OneToOneField` -> `Symbol` (on_delete=models.CASCADE, related_name='analyst_price_target', db_index=True)
  - `fetched_at`: `DateTimeField`
  - `data`: `JSONField` (default=dict, blank=True)

### Model: `AnalystRecommendation`

- Meta:
  - `verbose_name`: `'Analyst recommendation'`
  - `verbose_name_plural`: `'Analyst recommendations'`
- Fields:
  - `symbol`: `OneToOneField` -> `Symbol` (on_delete=models.CASCADE, related_name='analyst_recommendations', db_index=True)
  - `fetched_at`: `DateTimeField`
  - `data`: `JSONField` (default=list, blank=True)

### Model: `Dividend`

- Meta:
  - `ordering`: `['-date']`
  - `verbose_name`: `'Dividend'`
  - `verbose_name_plural`: `'Dividends'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='dividends', db_index=True)
  - `date`: `DateField` (db_index=True)
  - `amount`: `DecimalField`
  - `currency`: `CharField` (max_length=10, blank=True)
  - `dividend_type`: `CharField` (max_length=20, choices=TYPE_CHOICES, default=TYPE_PAID, db_index=True)

### Model: `DividendCalendar`

- Meta:
  - `ordering`: `['-ex_dividend_date', '-declaration_date']`
  - `verbose_name`: `'Dividend calendar'`
  - `verbose_name_plural`: `'Dividend calendar entries'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='dividend_calendar', db_index=True)
  - `declaration_date`: `DateField` (null=True, blank=True)
  - `ex_dividend_date`: `DateField` (null=True, blank=True, db_index=True)
  - `record_date`: `DateField` (null=True, blank=True)
  - `payment_date`: `DateField` (null=True, blank=True, db_index=True)
  - `amount`: `DecimalField` (null=True, blank=True)
  - `currency`: `CharField` (max_length=10, blank=True)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default=STATUS_DECLARED, db_index=True)
  - `source`: `CharField` (max_length=50, blank=True)
  - `tm1_adjustment_written`: `BooleanField` (default=False)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `EarningsEstimate`

- Meta:
  - `verbose_name`: `'Earnings estimate'`
  - `verbose_name_plural`: `'Earnings estimates'`
- Fields:
  - `symbol`: `OneToOneField` -> `Symbol` (on_delete=models.CASCADE, related_name='earnings_estimate', db_index=True)
  - `fetched_at`: `DateTimeField`
  - `data`: `JSONField` (default=dict, blank=True)

### Model: `EarningsReport`

- Meta:
  - `ordering`: `['-period_end']`
  - `verbose_name`: `'Earnings report'`
  - `verbose_name_plural`: `'Earnings reports'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='earnings_reports', db_index=True)
  - `period_end`: `DateField` (null=True, blank=True, db_index=True)
  - `freq`: `CharField` (max_length=20)
  - `data`: `JSONField` (default=dict, blank=True)
  - `fetched_at`: `DateTimeField`

### Model: `FinancialStatement`

- Meta:
  - `ordering`: `['-period_end']`
  - `verbose_name`: `'Financial statement'`
  - `verbose_name_plural`: `'Financial statements'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='financial_statements', db_index=True)
  - `statement_type`: `CharField` (max_length=20, choices=TYPE_CHOICES, db_index=True)
  - `period_end`: `DateField` (null=True, blank=True, db_index=True)
  - `freq`: `CharField` (max_length=20, choices=FREQ_CHOICES, default=FREQ_YEARLY)
  - `data`: `JSONField` (default=dict, blank=True)
  - `fetched_at`: `DateTimeField`

### Model: `NewsItem`

- Meta:
  - `ordering`: `['-published_at']`
  - `verbose_name`: `'News item'`
  - `verbose_name_plural`: `'News items'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='news_items', db_index=True)
  - `title`: `CharField` (max_length=500)
  - `link`: `URLField` (max_length=1000, blank=True)
  - `published_at`: `DateTimeField` (null=True, blank=True, db_index=True)
  - `publisher`: `CharField` (max_length=200, blank=True)
  - `summary`: `TextField` (blank=True)
  - `data`: `JSONField` (default=dict, blank=True)
  - `created_at`: `DateTimeField`

### Model: `OwnershipSnapshot`

- Meta:
  - `ordering`: `['-fetched_at']`
  - `verbose_name`: `'Ownership snapshot'`
  - `verbose_name_plural`: `'Ownership snapshots'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='ownership_snapshots', db_index=True)
  - `holder_type`: `CharField` (max_length=30, choices=HOLDER_CHOICES, db_index=True)
  - `fetched_at`: `DateTimeField`
  - `data`: `JSONField` (default=dict, blank=True)

### Model: `PricePoint`

- Meta:
  - `ordering`: `['-date']`
  - `verbose_name`: `'Price point'`
  - `verbose_name_plural`: `'Price points'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='price_points', db_index=True)
  - `date`: `DateField` (db_index=True)
  - `open`: `DecimalField`
  - `high`: `DecimalField`
  - `low`: `DecimalField`
  - `close`: `DecimalField`
  - `volume`: `BigIntegerField` (null=True, blank=True)
  - `adjusted_close`: `DecimalField` (null=True, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `Split`

- Meta:
  - `ordering`: `['-date']`
  - `verbose_name`: `'Split'`
  - `verbose_name_plural`: `'Splits'`
- Fields:
  - `symbol`: `ForeignKey` -> `Symbol` (on_delete=models.CASCADE, related_name='splits', db_index=True)
  - `date`: `DateField` (db_index=True)
  - `ratio`: `DecimalField`

### Model: `Symbol`

- Meta:
  - `ordering`: `['symbol']`
  - `verbose_name`: `'Symbol'`
  - `verbose_name_plural`: `'Symbols'`
- Fields:
  - `symbol`: `CharField` (max_length=20, unique=True, db_index=True)
  - `name`: `CharField` (max_length=255, blank=True)
  - `exchange`: `CharField` (max_length=50, blank=True)
  - `category`: `CharField` (max_length=20, blank=True, choices=CATEGORY_CHOICES, db_index=True)
  - `share_name_mapping`: `OneToOneField` -> `investec.InvestecJseShareNameMapping` (null=True, blank=True, on_delete=models.SET_NULL, related_name='financial_symbol')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `SymbolInfo`

- Meta:
  - `verbose_name`: `'Symbol info'`
  - `verbose_name_plural`: `'Symbol infos'`
- Fields:
  - `symbol`: `OneToOneField` -> `Symbol` (on_delete=models.CASCADE, related_name='info', db_index=True)
  - `fetched_at`: `DateTimeField`
  - `data`: `JSONField` (default=dict, blank=True)

### Model: `WatchlistTablePreference`

- Meta:
  - `verbose_name`: `'Watchlist table preference'`
  - `verbose_name_plural`: `'Watchlist table preferences'`
- Fields:
  - `key`: `CharField` (max_length=64, unique=True, db_index=True, default='default')
  - `value`: `JSONField` (default=dict, blank=True)
  - `updated_at`: `DateTimeField`

## File: `apps/investec/models.py`

### Model: `InvestecBankAccount`

- Meta:
  - `ordering`: `['account_number']`
  - `verbose_name`: `'Investec Bank Account'`
  - `verbose_name_plural`: `'Investec Bank Accounts'`
- Fields:
  - `account_id`: `CharField` (max_length=40, unique=True, db_index=True)
  - `account_number`: `CharField` (max_length=40)
  - `account_name`: `CharField` (max_length=70, blank=True)
  - `reference_name`: `CharField` (max_length=70, blank=True)
  - `product_name`: `CharField` (max_length=70, blank=True)
  - `kyc_compliant`: `BooleanField` (default=False)
  - `profile_id`: `CharField` (max_length=70, blank=True)
  - `profile_name`: `CharField` (max_length=70, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `InvestecBankSyncLog`

- Meta:
  - `verbose_name`: `'Investec Bank Sync Log'`
  - `verbose_name_plural`: `'Investec Bank Sync Logs'`
- Fields:
  - `key`: `CharField` (max_length=32, unique=True, default='default')
  - `last_synced_at`: `DateTimeField` (null=True, blank=True)

### Model: `InvestecBankTransaction`

- Meta:
  - `ordering`: `['-posting_date', '-posted_order']`
  - `verbose_name`: `'Investec Bank Transaction'`
  - `verbose_name_plural`: `'Investec Bank Transactions'`
- Fields:
  - `account`: `ForeignKey` -> `InvestecBankAccount` (on_delete=models.CASCADE, related_name='transactions', db_index=True)
  - `type`: `CharField` (max_length=10, choices=TYPE_CHOICES)
  - `transaction_type`: `CharField` (max_length=40, blank=True, db_index=True)
  - `status`: `CharField` (max_length=10, choices=STATUS_CHOICES, db_index=True)
  - `description`: `CharField` (max_length=255, blank=True)
  - `card_number`: `CharField` (max_length=40, blank=True)
  - `posted_order`: `IntegerField` (null=True, blank=True)
  - `posting_date`: `DateField` (null=True, blank=True)
  - `value_date`: `DateField` (null=True, blank=True)
  - `action_date`: `DateField` (null=True, blank=True)
  - `transaction_date`: `DateField` (null=True, blank=True, db_index=True)
  - `amount`: `DecimalField`
  - `running_balance`: `DecimalField` (null=True, blank=True)
  - `uuid`: `CharField` (max_length=40, blank=True, null=True, unique=True, db_index=True)
  - `fallback_key`: `CharField` (max_length=64, blank=True, null=True, unique=True, db_index=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `InvestecJsePortfolio`

- Meta:
  - `ordering`: `['-date', 'company']`
  - `verbose_name`: `'Investec Jse Portfolio'`
  - `verbose_name_plural`: `'Investec Jse Portfolios'`
- Fields:
  - `date`: `DateField`
  - `year`: `IntegerField` (null=True, blank=True)
  - `month`: `IntegerField` (null=True, blank=True)
  - `day`: `IntegerField` (null=True, blank=True)
  - `company`: `CharField` (max_length=100)
  - `share_code`: `CharField` (max_length=20)
  - `quantity`: `DecimalField`
  - `currency`: `CharField` (max_length=10, default='ZAR')
  - `unit_cost`: `DecimalField`
  - `total_cost`: `DecimalField`
  - `price`: `DecimalField`
  - `total_value`: `DecimalField`
  - `exchange_rate`: `DecimalField` (null=True, blank=True)
  - `move_percent`: `DecimalField` (null=True, blank=True)
  - `portfolio_percent`: `DecimalField` (null=True, blank=True)
  - `profit_loss`: `DecimalField` (null=True, blank=True)
  - `annual_income_zar`: `DecimalField` (null=True, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `InvestecJseShareMonthlyPerformance`

- Meta:
  - `ordering`: `['-date', 'share_name']`
  - `verbose_name`: `'Investec Jse Share Monthly Performance'`
  - `verbose_name_plural`: `'Investec Jse Share Monthly Performances'`
- Fields:
  - `share_name`: `CharField` (max_length=100, db_index=True)
  - `date`: `DateField`
  - `year`: `IntegerField` (null=True, blank=True)
  - `month`: `IntegerField` (null=True, blank=True)
  - `dividend_type`: `CharField` (max_length=50, db_index=True)
  - `investec_account`: `CharField` (max_length=50, blank=True, null=True, db_index=True)
  - `dividend_ttm`: `DecimalField`
  - `closing_price`: `DecimalField` (null=True, blank=True)
  - `quantity`: `DecimalField` (null=True, blank=True)
  - `total_market_value`: `DecimalField` (null=True, blank=True)
  - `dividend_yield`: `DecimalField` (null=True, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `InvestecJseShareNameMapping`

- Meta:
  - `ordering`: `['share_name']`
  - `verbose_name`: `'Investec Jse Share Name Mapping'`
  - `verbose_name_plural`: `'Investec Jse Share Name Mappings'`
- Fields:
  - `share_name`: `CharField` (max_length=100, unique=True, db_index=True)
  - `share_name2`: `CharField` (max_length=100, blank=True, null=True, db_index=True)
  - `share_name3`: `CharField` (max_length=100, blank=True, null=True, db_index=True)
  - `company`: `CharField` (max_length=100, blank=True, null=True, db_index=True)
  - `share_code`: `CharField` (max_length=20, blank=True, null=True, db_index=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `InvestecJseTransaction`

- Meta:
  - `ordering`: `['-date', '-created_at']`
  - `verbose_name`: `'Investec Jse Transaction'`
  - `verbose_name_plural`: `'Investec Jse Transactions'`
- Fields:
  - `date`: `DateField`
  - `year`: `IntegerField` (null=True, blank=True)
  - `month`: `IntegerField` (null=True, blank=True)
  - `day`: `IntegerField` (null=True, blank=True)
  - `account_number`: `CharField` (max_length=50)
  - `description`: `CharField` (max_length=255)
  - `share_name`: `CharField` (max_length=100, blank=True)
  - `type`: `CharField` (max_length=50)
  - `quantity`: `DecimalField`
  - `value`: `DecimalField`
  - `value_per_share`: `DecimalField` (null=True, blank=True)
  - `value_calculated`: `DecimalField` (null=True, blank=True)
  - `dividend_ttm`: `DecimalField` (null=True, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

## File: `apps/planning_analytics/models.py`

### Model: `TM1ProcessConfig`

- Meta:
  - `ordering`: `['sort_order', 'id']`
  - `verbose_name`: `'TM1 Process Config'`
  - `verbose_name_plural`: `'TM1 Process Configs'`
- Fields:
  - `process_name`: `CharField` (max_length=300)
  - `enabled`: `BooleanField` (default=True)
  - `sort_order`: `PositiveSmallIntegerField` (default=0)
  - `parameters`: `JSONField` (default=dict, blank=True)

### Model: `TM1ServerConfig`

- Meta:
  - `verbose_name`: `'TM1 Server Config'`
  - `verbose_name_plural`: `'TM1 Server Configs'`
- Fields:
  - `base_url`: `URLField` (max_length=500)
  - `username`: `CharField` (max_length=200, blank=True, default='')
  - `password`: `CharField` (max_length=200, blank=True, default='')
  - `is_active`: `BooleanField` (default=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `UserTM1Credentials`

- Meta:
  - `verbose_name`: `'User TM1 Credentials'`
  - `verbose_name_plural`: `'User TM1 Credentials'`
- Fields:
  - `user`: `OneToOneField` -> `settings.AUTH_USER_MODEL` (on_delete=models.CASCADE, related_name='tm1_credentials')
  - `tm1_username`: `CharField` (max_length=200)
  - `tm1_password`: `CharField` (max_length=200, blank=True, default='')
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

## File: `apps/xero/xero_auth/models.py`

### Model: `XeroAuthSettings`

- Fields:
  - `access_token_url`: `CharField` (max_length=255)
  - `refresh_url`: `CharField` (max_length=255)
  - `auth_url`: `CharField` (max_length=255)

### Model: `XeroClientCredentials`

- Fields:
  - `user`: `ForeignKey` -> `settings.AUTH_USER_MODEL` (related_name='xero_client_credentials', on_delete=models.CASCADE)
  - `client_id`: `CharField` (max_length=100)
  - `client_secret`: `CharField` (max_length=100)
  - `scope`: `JSONField` (blank=True)
  - `token`: `JSONField` (blank=True, null=True)
  - `refresh_token`: `CharField` (max_length=1000, blank=True, null=True)
  - `expires_at`: `DateTimeField` (blank=True, null=True)
  - `tenant_tokens`: `JSONField` (default=dict, blank=True)
  - `active`: `BooleanField` (default=True)

### Model: `XeroTenantToken`

- Fields:
  - `tenant`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='tenant_tokens')
  - `credentials`: `ForeignKey` -> `XeroClientCredentials` (on_delete=models.CASCADE, related_name='xero_tenant_tokens')
  - `token`: `JSONField`
  - `refresh_token`: `CharField` (max_length=1000)
  - `expires_at`: `DateTimeField`
  - `connected_at`: `DateTimeField`

## File: `apps/xero/xero_core/models.py`

### Model: `XeroTenant`

- Fields:
  - `tenant_id`: `CharField` (max_length=100, unique=True, primary_key=True)
  - `tenant_name`: `CharField` (max_length=100)
  - `tracking_category_1_id`: `CharField` (max_length=64, blank=True, null=True)
  - `tracking_category_2_id`: `CharField` (max_length=64, blank=True, null=True)
  - `fiscal_year_start_month`: `IntegerField` (null=True, blank=True)

## File: `apps/xero/xero_cube/models.py`

### Model: `XeroBalanceSheet`

- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='balance_sheets')
  - `date`: `DateField` (blank=True, null=True)
  - `year`: `IntegerField` (blank=True, null=True)
  - `month`: `IntegerField` (blank=True, null=True)
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='balance_sheets_accounts')
  - `contact`: `ForeignKey` -> `XeroContacts` (on_delete=models.DO_NOTHING, null=True, blank=True, related_name='balance_sheets')
  - `amount`: `DecimalField`
  - `balance`: `DecimalField`

### Model: `XeroPnlByTracking`

- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='pnl_by_tracking')
  - `tracking`: `ForeignKey` -> `XeroTracking` (on_delete=models.CASCADE, null=True, blank=True, related_name='pnl_by_tracking')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='pnl_by_tracking')
  - `year`: `IntegerField`
  - `month`: `IntegerField`
  - `xero_amount`: `DecimalField` (default=0)
  - `imported_at`: `DateTimeField`

### Model: `XeroTrailBalance`

- Meta:
  - `ordering`: `['organisation', 'account', 'year', 'month', 'contact']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='trail_balances')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='trail_balances')
  - `date`: `DateField` (blank=True, null=True)
  - `year`: `IntegerField`
  - `month`: `IntegerField`
  - `fin_year`: `IntegerField`
  - `fin_period`: `IntegerField` (blank=True, null=True)
  - `contact`: `ForeignKey` -> `XeroContacts` (on_delete=models.DO_NOTHING, null=True, blank=True, related_name='trail_balances')
  - `tracking1`: `ForeignKey` -> `XeroTracking` (on_delete=models.DO_NOTHING, related_name='trail_balances_track1', blank=True, null=True)
  - `tracking2`: `ForeignKey` -> `XeroTracking` (on_delete=models.DO_NOTHING, related_name='trail_balances_track2', blank=True, null=True)
  - `amount`: `DecimalField`
  - `debit`: `DecimalField` (default=0)
  - `credit`: `DecimalField` (default=0)
  - `tax_amount`: `DecimalField` (default=0, blank=True)
  - `balance_to_date`: `DecimalField` (null=True, blank=True)

## File: `apps/xero/xero_data/models.py`

### Model: `XeroDocument`

- Meta:
  - `ordering`: `['transaction_source', 'file_name']`
  - `verbose_name`: `'Xero Document'`
  - `verbose_name_plural`: `'Xero Documents'`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='xero_documents')
  - `transaction_source`: `ForeignKey` -> `XeroTransactionSource` (on_delete=models.CASCADE, related_name='documents')
  - `file_name`: `CharField` (max_length=255)
  - `file`: `FileField` (max_length=500, blank=True)
  - `content_type`: `CharField` (max_length=128, blank=True)
  - `xero_attachment_id`: `CharField` (max_length=64, blank=True, null=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `XeroJournals`

- Meta:
  - `ordering`: `['organisation', 'date', 'journal_number']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='journals')
  - `journal_id`: `CharField` (max_length=200)
  - `journal_number`: `IntegerField`
  - `journal_type`: `CharField` (max_length=20, choices=JOURNAL_TYPE_CHOICES, default='journal')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='journals')
  - `transaction_source`: `ForeignKey` -> `XeroTransactionSource` (on_delete=models.CASCADE, related_name='journals', blank=True, null=True)
  - `journal_source`: `ForeignKey` -> `XeroJournalsSource` (on_delete=models.CASCADE, related_name='journals', blank=True, null=True)
  - `contact`: `ForeignKey` -> `XeroContacts` (on_delete=models.DO_NOTHING, related_name='journals', blank=True, null=True)
  - `date`: `DateTimeField`
  - `tracking1`: `ForeignKey` -> `XeroTracking` (on_delete=models.DO_NOTHING, related_name='journals_track1', blank=True, null=True)
  - `tracking2`: `ForeignKey` -> `XeroTracking` (on_delete=models.DO_NOTHING, related_name='journals_track2', blank=True, null=True)
  - `description`: `TextField` (blank=True)
  - `reference`: `TextField` (blank=True)
  - `amount`: `DecimalField`
  - `debit`: `DecimalField` (default=0)
  - `credit`: `DecimalField` (default=0)
  - `tax_amount`: `DecimalField`

### Model: `XeroJournalsSource`

- Meta:
  - `ordering`: `['organisation', 'journal_number']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='journals_sources')
  - `journal_id`: `CharField` (max_length=51)
  - `journal_number`: `IntegerField`
  - `journal_type`: `CharField` (max_length=20, choices=JOURNAL_TYPE_CHOICES, default='journal')
  - `collection`: `JSONField` (blank=True, null=True)
  - `processed`: `BooleanField` (default=False)

### Model: `XeroTransactionSource`

- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='transaction_sources')
  - `transactions_id`: `CharField` (max_length=51, unique=True)
  - `transaction_source`: `CharField` (max_length=51)
  - `contact`: `ForeignKey` -> `XeroContacts` (on_delete=models.DO_NOTHING, null=True, blank=True, related_name='transaction_sources')
  - `collection`: `JSONField` (blank=True, null=True)

## File: `apps/xero/xero_metadata/models.py`

### Model: `XeroAccount`

- Meta:
  - `ordering`: `['organisation', 'code']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='accounts')
  - `account_id`: `CharField` (primary_key=True, max_length=40, unique=True)
  - `business_unit`: `ForeignKey` -> `XeroBusinessUnits` (on_delete=models.DO_NOTHING, null=True, blank=True)
  - `reporting_code`: `TextField` (blank=True)
  - `reporting_code_name`: `TextField` (blank=True)
  - `bank_account_number`: `CharField` (max_length=40, blank=True, null=True)
  - `grouping`: `CharField` (max_length=30, blank=True)
  - `code`: `CharField` (max_length=10, blank=True)
  - `name`: `CharField` (max_length=150, blank=True)
  - `type`: `CharField` (max_length=30, blank=True)
  - `collection`: `JSONField` (blank=True, null=True)
  - `attr_entry_type`: `CharField` (max_length=30, blank=True, null=True)
  - `attr_occurrence`: `CharField` (max_length=30, blank=True, null=True)

### Model: `XeroBusinessUnits`

- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='business_units')
  - `division_code`: `CharField` (max_length=1, blank=True, null=True)
  - `business_unit_code`: `CharField` (max_length=1)
  - `division_description`: `CharField` (max_length=100, blank=True, null=True)
  - `business_unit_description`: `CharField` (max_length=100)

### Model: `XeroContacts`

- Meta:
  - `ordering`: `['organisation', 'name']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='contacts')
  - `contacts_id`: `CharField` (max_length=55, unique=True, primary_key=True)
  - `name`: `TextField`
  - `collection`: `JSONField` (blank=True, null=True)

### Model: `XeroTracking`

- Meta:
  - `ordering`: `['organisation', 'option']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='tracking')
  - `option_id`: `TextField` (max_length=1024)
  - `name`: `TextField` (max_length=1024, blank=True, null=True)
  - `option`: `TextField` (max_length=1024, blank=True, null=True)
  - `collection`: `JSONField` (blank=True, null=True)
  - `tracking_category_id`: `CharField` (max_length=64, blank=True, null=True)
  - `category_slot`: `PositiveSmallIntegerField` (null=True, blank=True)

## File: `apps/xero/xero_sync/models.py`

### Model: `ProcessTree`

- Meta:
  - `ordering`: `['name']`
- Fields:
  - `name`: `CharField` (max_length=100, unique=True)
  - `description`: `TextField` (blank=True)
  - `process_tree_data`: `JSONField`
  - `response_variables`: `JSONField` (default=dict, blank=True)
  - `cache_enabled`: `BooleanField` (default=True)
  - `enabled`: `BooleanField` (default=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`
  - `dependent_trees`: `ManyToManyField` -> `self` (related_name='parent_trees', blank=True)
  - `sibling_trees`: `ManyToManyField` -> `self` (blank=True)
  - `trigger`: `ForeignKey` -> `Trigger` (on_delete=models.SET_NULL, null=True, blank=True, related_name='process_trees')

### Model: `ProcessTreeSchedule`

- Meta:
  - `ordering`: `['process_tree__name']`
- Fields:
  - `process_tree`: `OneToOneField` -> `ProcessTree` (on_delete=models.CASCADE, related_name='schedule')
  - `enabled`: `BooleanField` (default=True)
  - `interval_minutes`: `IntegerField` (default=60)
  - `start_time`: `TimeField` (default=datetime.time(0, 0))
  - `last_run`: `DateTimeField` (null=True, blank=True)
  - `next_run`: `DateTimeField` (null=True, blank=True)
  - `context`: `JSONField` (default=dict, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `Trigger`

- Meta:
  - `ordering`: `['name']`
- Fields:
  - `name`: `CharField` (max_length=200, unique=True)
  - `trigger_type`: `CharField` (max_length=50, choices=TRIGGER_TYPES, default='condition')
  - `enabled`: `BooleanField` (default=True)
  - `description`: `TextField` (blank=True)
  - `configuration`: `JSONField` (default=dict, blank=True)
  - `xero_last_update`: `ForeignKey` -> `XeroLastUpdate` (on_delete=models.SET_NULL, null=True, blank=True, related_name='triggers')
  - `process_tree`: `ForeignKey` -> `ProcessTree` (on_delete=models.CASCADE, null=True, blank=True, related_name='triggers')
  - `state`: `CharField` (max_length=20, choices=TRIGGER_STATES, default='pending')
  - `last_checked`: `DateTimeField` (null=True, blank=True)
  - `last_triggered`: `DateTimeField` (null=True, blank=True)
  - `last_fired_manually`: `DateTimeField` (null=True, blank=True)
  - `trigger_count`: `IntegerField` (default=0)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

### Model: `XeroApiCallLog`

- Meta:
  - `ordering`: `['-created_at']`
- Fields:
  - `process`: `CharField` (max_length=50, choices=PROCESS_CHOICES)
  - `tenant`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='api_call_logs', null=True, blank=True)
  - `api_calls`: `IntegerField` (default=0)
  - `created_at`: `DateTimeField`

### Model: `XeroLastUpdate`

- Fields:
  - `name`: `CharField` (max_length=200, blank=True, null=True, unique=True)
  - `end_point`: `CharField` (max_length=100, choices=ENDPOINT_CHOICES)
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='last_updates')
  - `date`: `DateTimeField` (blank=True, null=True)

### Model: `XeroTaskExecutionLog`

- Meta:
  - `ordering`: `['-started_at']`
- Fields:
  - `tenant`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='task_logs')
  - `task_type`: `CharField` (max_length=20, choices=TASK_TYPES)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default='pending')
  - `started_at`: `DateTimeField`
  - `completed_at`: `DateTimeField` (null=True, blank=True)
  - `duration_seconds`: `FloatField` (null=True, blank=True)
  - `records_processed`: `IntegerField` (null=True, blank=True)
  - `error_message`: `TextField` (null=True, blank=True)
  - `stats`: `JSONField` (default=dict, blank=True)
  - `created_at`: `DateTimeField`

### Model: `XeroTenantSchedule`

- Meta:
  - `ordering`: `['tenant__tenant_name']`
- Fields:
  - `tenant`: `OneToOneField` -> `XeroTenant` (on_delete=models.CASCADE, related_name='schedule')
  - `enabled`: `BooleanField` (default=True)
  - `update_interval_minutes`: `IntegerField` (default=60)
  - `update_start_time`: `TimeField` (default=datetime.time(0, 0))
  - `last_update_run`: `DateTimeField` (null=True, blank=True)
  - `last_process_run`: `DateTimeField` (null=True, blank=True)
  - `next_update_run`: `DateTimeField` (null=True, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`

## File: `apps/xero/xero_validation/models.py`

### Model: `ProfitAndLossComparison`

- Meta:
  - `ordering`: `['report', 'period_index', 'account__code']`
- Fields:
  - `report`: `ForeignKey` -> `XeroProfitAndLossReport` (on_delete=models.CASCADE, related_name='comparisons')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='pnl_comparisons')
  - `period_index`: `IntegerField`
  - `period_date`: `DateField`
  - `xero_value`: `DecimalField`
  - `db_value`: `DecimalField`
  - `difference`: `DecimalField`
  - `match_status`: `CharField` (max_length=20, choices=[('match', 'Match'), ('mismatch', 'Mismatch'), ('missing_in_db', 'Missing in DB'), ('missing_in_xero', 'Missing in Xero')])
  - `notes`: `TextField` (blank=True)
  - `compared_at`: `DateTimeField`

### Model: `TrailBalanceComparison`

- Meta:
  - `ordering`: `['-difference', 'account__code']`
- Fields:
  - `report`: `ForeignKey` -> `XeroTrailBalanceReport` (on_delete=models.CASCADE, related_name='comparisons')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='trail_balance_comparisons')
  - `xero_value`: `DecimalField`
  - `db_value`: `DecimalField`
  - `difference`: `DecimalField`
  - `match_status`: `CharField` (max_length=20, choices=[('match', 'Match'), ('mismatch', 'Mismatch'), ('missing_in_db', 'Missing in DB'), ('missing_in_xero', 'Missing in Xero')])
  - `notes`: `TextField` (blank=True)
  - `compared_at`: `DateTimeField`

### Model: `XeroProfitAndLossReport`

- Meta:
  - `ordering`: `['-to_date', '-imported_at']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='xero_pnl_reports')
  - `from_date`: `DateField`
  - `to_date`: `DateField`
  - `periods`: `IntegerField` (default=12)
  - `timeframe`: `CharField` (max_length=20, default='MONTH')
  - `imported_at`: `DateTimeField`
  - `raw_data`: `JSONField` (null=True, blank=True)

### Model: `XeroProfitAndLossReportLine`

- Meta:
  - `ordering`: `['report', 'account_code', 'id']`
- Fields:
  - `report`: `ForeignKey` -> `XeroProfitAndLossReport` (on_delete=models.CASCADE, related_name='lines')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='xero_pnl_report_lines', null=True, blank=True)
  - `account_code`: `CharField` (max_length=50, blank=True)
  - `account_name`: `CharField` (max_length=255)
  - `account_type`: `CharField` (max_length=50, null=True, blank=True)
  - `row_type`: `CharField` (max_length=50)
  - `section_title`: `CharField` (max_length=255, blank=True)
  - `period_values`: `JSONField` (default=dict)
  - `raw_cell_data`: `JSONField` (null=True, blank=True)

### Model: `XeroTrailBalanceReport`

- Meta:
  - `ordering`: `['-report_date', '-imported_at']`
- Fields:
  - `organisation`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='xero_trail_balance_reports')
  - `report_date`: `DateField`
  - `report_type`: `CharField` (max_length=50, default='TrialBalance')
  - `imported_at`: `DateTimeField`
  - `raw_data`: `JSONField` (null=True, blank=True)
  - `parsed_json`: `JSONField` (null=True, blank=True)

### Model: `XeroTrailBalanceReportLine`

- Meta:
  - `ordering`: `['account_code']`
- Fields:
  - `report`: `ForeignKey` -> `XeroTrailBalanceReport` (on_delete=models.CASCADE, related_name='lines')
  - `account`: `ForeignKey` -> `XeroAccount` (on_delete=models.CASCADE, related_name='xero_report_lines', null=True, blank=True)
  - `account_code`: `CharField` (max_length=50)
  - `account_name`: `CharField` (max_length=255)
  - `account_type`: `CharField` (max_length=50, null=True, blank=True)
  - `debit`: `DecimalField` (default=0)
  - `credit`: `DecimalField` (default=0)
  - `value`: `DecimalField` (default=0)
  - `period_debit`: `DecimalField` (default=0)
  - `period_credit`: `DecimalField` (default=0)
  - `ytd_debit`: `DecimalField` (default=0)
  - `ytd_credit`: `DecimalField` (default=0)
  - `db_value`: `DecimalField` (null=True, blank=True)
  - `row_type`: `CharField` (max_length=50, null=True, blank=True)
  - `raw_cell_data`: `JSONField` (null=True, blank=True)

## File: `apps/xero/xero_webhooks/models.py`

### Model: `WebhookEvent`

- Meta:
  - `ordering`: `['-received_at']`
  - `verbose_name`: `'Webhook Event'`
  - `verbose_name_plural`: `'Webhook Events'`
- Fields:
  - `subscription`: `ForeignKey` -> `WebhookSubscription` (on_delete=models.CASCADE, related_name='events')
  - `event_id`: `CharField` (max_length=255)
  - `resource_id`: `CharField` (max_length=255)
  - `event_category`: `CharField` (max_length=50, choices=EVENT_CATEGORIES, default='OTHER')
  - `event_type`: `CharField` (max_length=50)
  - `payload`: `JSONField` (blank=True, null=True)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default='received')
  - `error_message`: `TextField` (blank=True)
  - `retry_count`: `IntegerField` (default=0)
  - `received_at`: `DateTimeField`
  - `processed_at`: `DateTimeField` (null=True, blank=True)

### Model: `WebhookSubscription`

- Meta:
  - `ordering`: `['-created_at']`
  - `verbose_name`: `'Webhook Subscription'`
  - `verbose_name_plural`: `'Webhook Subscriptions'`
- Fields:
  - `tenant`: `ForeignKey` -> `XeroTenant` (on_delete=models.CASCADE, related_name='webhook_subscriptions')
  - `webhook_key`: `CharField` (max_length=255)
  - `status`: `CharField` (max_length=20, choices=STATUS_CHOICES, default='pending')
  - `event_types`: `JSONField` (default=list, blank=True)
  - `created_at`: `DateTimeField`
  - `updated_at`: `DateTimeField`
  - `last_event_at`: `DateTimeField` (null=True, blank=True)
  - `events_received`: `IntegerField` (default=0)
  - `events_processed`: `IntegerField` (default=0)
  - `events_failed`: `IntegerField` (default=0)
