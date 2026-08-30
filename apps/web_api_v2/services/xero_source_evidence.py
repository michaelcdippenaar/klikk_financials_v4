"""How much source data an entity actually holds, per Xero pipeline stage.

The pipeline read reports the V2 ingest RUN ledger: what V2 has executed. In
production that ledger is empty, because the nightly cron is the legacy
pipeline and writes no V2 run records. The workbench was therefore truthful and
useless — every stage "Not run" while the entity held 178k trial-balance rows.

This measures the other half: what data is present. The 2026-08-22 KF-V2LIVE
postmortem requires the two be kept in different typed fields and UI labels, so
a source count is never read as a successful V2 run. Nothing here touches the
run ledger, and nothing here calls a provider.

Some sources are reference data with no period at all — an entity has accounts,
not accounts-for-March. Rather than hide those behind NOT_APPLICABLE, they are
reported with period_scoped=False so the UI can label an entity-wide count
honestly instead of implying it belongs to the selected months.
"""

from dataclasses import dataclass
from datetime import date

from django.db.models import Max


@dataclass(frozen=True)
class SourceMeasurement:
    """One stage's source evidence. `count is None` means we could not measure."""

    label: str
    period_scoped: bool
    count: int | None = None
    latest_at: object | None = None
    reason: str | None = None


def _measure(model_path, entity_field, label, *, entity, date_field=None,
             starts_on=None, ends_on=None):
    """Aggregate one source table. Never loads rows; count and max only."""
    module_name, _, attribute = model_path.rpartition('.')
    try:
        module = __import__(module_name, fromlist=[attribute])
        model = getattr(module, attribute)
    except (ImportError, AttributeError):
        return SourceMeasurement(
            label=label, period_scoped=bool(date_field),
            reason='This source table is unavailable in this deployment.',
        )

    queryset = model.objects.filter(**{entity_field: entity})
    period_scoped = bool(date_field)
    if period_scoped:
        # A DateTimeField compared against a bare date is naive, and Django
        # then interprets it outside the active timezone — silently moving the
        # window edges. Compare on the local date instead.
        field = model._meta.get_field(date_field)
        lookup = f'{date_field}__date' if field.get_internal_type() == 'DateTimeField' else date_field
        queryset = queryset.filter(**{
            f'{lookup}__gte': starts_on, f'{lookup}__lte': ends_on,
        })

    aggregate = queryset.aggregate(latest=Max(date_field)) if period_scoped else {}
    return SourceMeasurement(
        label=label,
        period_scoped=period_scoped,
        count=queryset.count(),
        latest_at=aggregate.get('latest'),
    )


# Stage -> the table that stage is responsible for filling. Reference data has
# no date column, so it is reported entity-wide rather than pretended into a
# period.
_SOURCES = {
    'METADATA': ('apps.xero.xero_metadata.models.XeroAccount', 'organisation', 'accounts', None),
    'TRANSACTIONS_JOURNALS': ('apps.xero.xero_data.models.XeroJournals', 'organisation', 'journal lines', 'date'),
    'INVOICES': ('apps.xero.xero_data.models.XeroInvoice', 'organisation', 'invoices', 'date'),
    'PROCESS_JOURNAL_LINES': ('apps.xero.xero_data.models.XeroJournalsSource', 'organisation', 'journal sources', None),
    'TRIAL_BALANCE': ('apps.xero.xero_cube.models.XeroTrailBalance', 'organisation', 'trial balance rows', 'date'),
    'DOCUMENTS': ('apps.xero.xero_data.models.XeroDocument', 'organisation', 'documents', 'created_at'),
    'AGED_PAYABLES': ('apps.xero.xero_data.models.AgedPayable', 'tenant', 'aged payable rows', 'report_date'),
    'AGED_RECEIVABLES': ('apps.xero.xero_data.models.AgedReceivable', 'tenant', 'aged receivable rows', 'report_date'),
}


def is_period_scoped(stage_key):
    """Whether this stage's evidence belongs to a period at all.

    A stage is period-scoped exactly when its source rows carry a date to scope
    BY. Metadata (accounts, contacts, tracking categories) and processed journal
    sources do not: they describe the organisation, not a month. This is the one
    classification — run evidence reads it too, so the pipeline cannot decide a
    stage is period-scoped for one kind of evidence and not the other.
    """
    source = _SOURCES.get(str(stage_key))
    return bool(source and source[3])


def measure_stage_source(stage_key, *, entity, starts_on: date, ends_on: date):
    """Source evidence for one stage, scoped to the entity and resolved window."""
    source = _SOURCES.get(str(stage_key))
    if source is None:
        return None
    model_path, entity_field, label, date_field = source
    return _measure(
        model_path, entity_field, label,
        entity=entity, date_field=date_field, starts_on=starts_on, ends_on=ends_on,
    )
