import logging

from django.conf import settings

from apps.xero.xero_auth.models import XeroClientCredentials, XeroTenantToken
from apps.xero.xero_core.exceptions import DailyLimitReached, TenantReauthRequired
from apps.xero.xero_sync.models import XeroLastUpdate


logger = logging.getLogger(__name__)


PROCESS_DEFINITIONS = {
    'metadata': {
        'display_name': 'Metadata',
        'required': True,
        'prerequisites': (),
    },
    'transaction-journal-sync': {
        'display_name': 'Transaction and journal sync',
        'required': True,
        'prerequisites': ('metadata',),
    },
    'invoice-sync': {
        'display_name': 'Invoice sync',
        'required': True,
        'prerequisites': ('metadata',),
    },
    'process-journals': {
        'display_name': 'Process journals',
        'required': True,
        'prerequisites': ('transaction-journal-sync',),
    },
    'trail-balance': {
        'display_name': 'Trial Balance',
        'required': True,
        'prerequisites': ('process-journals',),
    },
    'documents': {
        'display_name': 'Documents',
        'required': False,
        'prerequisites': ('transaction-journal-sync',),
    },
    # Aged figures are bucketed from invoices held locally, so the real
    # prerequisite is the invoice sync, not metadata. Declaring metadata let
    # both stages run against an empty invoice table and report a confident
    # zero.
    'aged-payables': {
        'display_name': 'Aged payables',
        'required': False,
        'prerequisites': ('invoice-sync',),
    },
    'aged-receivables': {
        'display_name': 'Aged receivables',
        'required': False,
        'prerequisites': ('invoice-sync',),
    },
    'standard-sync': {
        'display_name': 'Standard sync',
        'required': False,
        'prerequisites': ('durable-worker',),
    },
}


LAST_UPDATE_ENDPOINTS = {
    'metadata': ('accounts', 'contacts', 'tracking_categories'),
    'transaction-journal-sync': (
        'bank_transactions', 'payments', 'credit_notes', 'prepayments',
        'overpayments', 'bank_transfers', 'manual_journals', 'journals',
    ),
    'invoice-sync': ('invoice_store',),
    'process-journals': ('process_journals',),
    'trail-balance': ('trail_balance',),
}


class ProcessCommandError(Exception):
    def __init__(self, code, message, *, retryable=False, blocked=False):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.blocked = blocked


def provision_definition_defaults(entity):
    from apps.web_api_v2.models import IngestSourceJobDefinition

    created = 0
    for key, definition in PROCESS_DEFINITIONS.items():
        if key == 'standard-sync':
            continue
        _, was_created = IngestSourceJobDefinition.objects.get_or_create(
            entity=entity,
            key=key,
            defaults={
                'source_family': IngestSourceJobDefinition.SourceFamily.XERO,
                'label': definition['display_name'],
                'required': definition['required'],
                'configuration_state': IngestSourceJobDefinition.ConfigurationState.CONFIGURED,
                'supported_operations': [key],
                'read_capabilities': ['VIEW_STATUS'],
            },
        )
        created += int(was_created)
    return created


def has_tenant_credentials(entity_id):
    if XeroTenantToken.objects.filter(tenant_id=entity_id, credentials__active=True).exists():
        return True
    for credential in XeroClientCredentials.objects.filter(active=True).only('tenant_tokens'):
        if str(entity_id) in (credential.tenant_tokens or {}):
            return True
    return False


def _has_last_update(entity_id, process_key):
    endpoints = LAST_UPDATE_ENDPOINTS.get(process_key, ())
    return bool(endpoints) and XeroLastUpdate.objects.filter(
        organisation_id=entity_id,
        end_point__in=endpoints,
        date__isnull=False,
    ).exists()


def prerequisite_status(entity, process_key):
    checks = []
    if entity.reauth_required:
        checks.append({
            'code': 'XERO_REAUTHORIZATION_REQUIRED',
            'satisfied': False,
            'message': 'The Xero connection must be re-authorized.',
        })
    elif process_key != 'standard-sync':
        checks.append({
            'code': 'XERO_CONNECTION_CONFIGURED',
            'satisfied': has_tenant_credentials(entity.pk),
            'message': 'An active Xero connection is required.',
        })

    for prerequisite in PROCESS_DEFINITIONS[process_key]['prerequisites']:
        if prerequisite == 'durable-worker':
            # A worker now exists (manage.py run_ingest_worker), so this is no
            # longer unconditionally unsatisfied. It stays a real check: the
            # prerequisite is satisfied only where the deployment actually runs
            # one, which the setting declares.
            available = getattr(settings, 'WEB_API_V2_INGEST_WORKER_ENABLED', False)
            checks.append({
                'code': 'DURABLE_WORKER_REQUIRED',
                'satisfied': bool(available),
                'message': (
                    'A durable background worker executes this process.'
                    if available else
                    'Standard sync is unavailable until a durable background worker '
                    'can execute beyond the HTTP request timeout.'
                ),
            })
        else:
            checks.append({
                'code': f'{prerequisite.upper().replace("-", "_")}_SUCCEEDED',
                'satisfied': _has_last_update(entity.pk, prerequisite),
                'message': f'{PROCESS_DEFINITIONS[prerequisite]["display_name"]} must succeed first.',
            })
    return checks


def first_blocking_prerequisite(entity, process_key):
    return next((item for item in prerequisite_status(entity, process_key) if not item['satisfied']), None)


def _credentials_user(entity_id):
    from apps.xero.xero_data.services import _get_credentials_for_tenant

    return _get_credentials_for_tenant(entity_id).user


def _safe_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:120]
    return None


def _safe_mapping(payload):
    result = {}
    for key, value in (payload or {}).items():
        if isinstance(value, (list, tuple)):
            safe_values = [_safe_scalar(item) for item in value[:50]]
            result[str(key)[:64]] = [item for item in safe_values if item is not None]
        elif isinstance(value, dict):
            result[str(key)[:64]] = _safe_mapping(value)
        else:
            safe = _safe_scalar(value)
            if safe is not None:
                result[str(key)[:64]] = safe
    return result


def normalize_result(process_key, result):
    result = result or {}
    stats = result.get('stats') if isinstance(result.get('stats'), dict) else result
    safe = _safe_mapping(stats)
    record_keys = ('records', 'created', 'updated', 'skipped', 'failed', 'errors',
                   'processed', 'synced', 'journals_processed', 'invoice_count')
    records = {
        key: int(safe[key])
        for key in record_keys
        if key in safe and isinstance(safe[key], (int, float))
    }
    periods = stats.get('affected_periods', [])
    normalized_periods = []
    for period in periods:
        if isinstance(period, str) and len(period) == 7:
            normalized_periods.append(period)
        elif isinstance(period, (list, tuple)) and len(period) == 2:
            try:
                normalized_periods.append(f'{int(period[0]):04d}-{int(period[1]):02d}')
            except (TypeError, ValueError):
                pass
    return {
        'records': records,
        'output': {'processKey': process_key, 'stats': safe},
        'periods': normalized_periods,
    }


# Dependency order, so a stage never runs before the data it needs. Documents
# and the aged reports come last: they are the heaviest Xero consumers and the
# least urgent, so a budget or rate limit stops them rather than the ledger.
STANDARD_SYNC_SEQUENCE = (
    'metadata',
    'transaction-journal-sync',
    'invoice-sync',
    'process-journals',
    'trail-balance',
    'aged-payables',
    'aged-receivables',
    'documents',
)


def run_standard_sync(entity):
    """Run every stage in dependency order, stopping at the first failure.

    Stopping matters: continuing past a failed transaction sync would build a
    trial balance on incomplete journals and report success. A partial result
    is returned either way, so the caller can see how far it got.
    """
    aggregate = {}
    periods = []
    for index, process_key in enumerate(STANDARD_SYNC_SEQUENCE):
        try:
            outcome = execute_process(process_key, entity)
        except ProcessCommandError as exc:
            raise ProcessCommandError(
                exc.code,
                f'{PROCESS_DEFINITIONS[process_key]["display_name"]}: {exc.safe_message}',
                retryable=exc.retryable,
                blocked=exc.blocked,
            ) from None
        for key, value in (outcome.get('records') or {}).items():
            aggregate[key] = aggregate.get(key, 0) + value
        for period in outcome.get('periods') or []:
            if period not in periods:
                periods.append(period)
        logger.info(
            'v2_standard_sync_stage entity=%s stage=%s (%s/%s) complete',
            entity.pk, process_key, index + 1, len(STANDARD_SYNC_SEQUENCE),
        )
    return {
        'records': aggregate,
        'output': {'stagesCompleted': len(STANDARD_SYNC_SEQUENCE)},
        'periods': periods,
    }


def _aged_result(process_key, label, stats):
    """Judge an aged sweep instead of reporting whatever it returns as success.

    On 28 Aug 2026 a Standard sync reported `succeeded` with 274 errors and no
    rows written, because these two stages were the only ones whose result was
    never checked. Every other stage already tests `success` or `errors`.
    """
    errors = int(stats.get('errors') or 0)
    if errors:
        processed = int(stats.get('contacts_processed') or 0)
        raise ProcessCommandError(
            'PROCESS_FAILED',
            f'{label} failed for {errors} of {processed} contacts.',
            retryable=True,
        )
    # Stopping at a budget is a legitimate partial result, not a failure, but it
    # must not be presented as a completed sweep.
    stopped = stats.get('stopped_early')
    if stopped in {'daily-limit', 'headroom-floor'}:
        raise ProcessCommandError(
            'XERO_DAILY_LIMIT_REACHED',
            f'{label} stopped to preserve the remaining Xero daily allowance.',
            retryable=True,
            blocked=True,
        )
    if stopped == 'max-api-calls':
        raise ProcessCommandError(
            'PROCESS_INCOMPLETE',
            f'{label} reached its API-call budget after '
            f'{stats.get("contacts_processed", 0)} of {stats.get("contact_count", 0)} contacts.',
            retryable=True,
        )
    return normalize_result(process_key, stats)


def execute_process(process_key, entity):
    """Execute one allowlisted synchronous process using reviewed Python services."""
    try:
        if process_key == 'standard-sync':
            # Reached only from the worker, and only once the deployment has
            # declared a worker. The prerequisite check above is the gate.
            if not getattr(settings, 'WEB_API_V2_INGEST_WORKER_ENABLED', False):
                raise ProcessCommandError(
                    'TEMPORARILY_UNAVAILABLE',
                    'Standard sync requires a durable background worker.',
                    retryable=True,
                    blocked=True,
                )
            return run_standard_sync(entity)
        if process_key == 'metadata':
            from apps.xero.xero_metadata.services import update_metadata
            result = update_metadata(entity.pk, user=_credentials_user(entity.pk))
            if not result.get('success'):
                raise ProcessCommandError('PROCESS_FAILED', 'Metadata sync failed.', retryable=True)
            return normalize_result(process_key, result)
        if process_key == 'transaction-journal-sync':
            from apps.xero.xero_data.services import update_financial_data
            result = update_financial_data(entity.pk, user=_credentials_user(entity.pk), load_all=False)
            if not result.get('success'):
                raise ProcessCommandError('PROCESS_FAILED', 'Transaction sync failed.', retryable=True)
            return normalize_result(process_key, result)
        if process_key == 'invoice-sync':
            from apps.xero.xero_data.invoices_service import sync_xero_invoices
            result = sync_xero_invoices(
                entity,
                full=False,
                max_api_calls=settings.WEB_API_V2_INGEST_MAX_XERO_CALLS,
            )
            if result.get('errors'):
                raise ProcessCommandError('PROCESS_FAILED', 'Invoice sync failed.', retryable=True)
            return normalize_result(process_key, result)
        if process_key == 'process-journals':
            from apps.xero.xero_cube.services import process_journals
            result = process_journals(entity.pk, force_reprocess=False)
            XeroLastUpdate.objects.update_or_create_timestamp('process_journals', entity)
            return normalize_result(process_key, result)
        if process_key == 'trail-balance':
            from apps.xero.xero_cube.services import process_xero_data
            result = process_xero_data(
                entity.pk,
                rebuild_trail_balance=False,
                exclude_manual_journals=False,
                calculate_pnl_ytd=True,
            )
            return normalize_result(process_key, result)
        if process_key == 'documents':
            from apps.xero.xero_data.document_sync import sync_documents_for_tenant
            result = sync_documents_for_tenant(
                entity.pk,
                user=_credentials_user(entity.pk),
                max_api_calls=settings.WEB_API_V2_INGEST_MAX_XERO_CALLS,
            )
            if not result.get('success'):
                raise ProcessCommandError('PROCESS_FAILED', 'Document sync failed.', retryable=True)
            return normalize_result(process_key, result)
        # Both aged stages are computed from invoices already held, so they
        # cost no Xero API calls. Xero has no bulk aged endpoint, and the
        # per-contact sweep they replace called every contact in the ledger to
        # discover that only a couple of dozen carry a balance.
        if process_key == 'aged-payables':
            from apps.xero.xero_data.aged_from_invoices import sync_aged_payables_from_invoices
            return _aged_result(process_key, 'Aged payables',
                                sync_aged_payables_from_invoices(entity))
        if process_key == 'aged-receivables':
            from apps.xero.xero_data.aged_from_invoices import sync_aged_receivables_from_invoices
            return _aged_result(process_key, 'Aged receivables',
                                sync_aged_receivables_from_invoices(entity))
        raise ProcessCommandError('UNKNOWN_PROCESS', 'The requested process is not allowlisted.')
    except TenantReauthRequired:
        raise ProcessCommandError(
            'XERO_REAUTHORIZATION_REQUIRED',
            'The Xero connection must be re-authorized.',
            blocked=True,
        ) from None
    except DailyLimitReached:
        raise ProcessCommandError(
            'XERO_DAILY_LIMIT_REACHED',
            'The Xero daily API limit has been reached.',
            retryable=True,
            blocked=True,
        ) from None
