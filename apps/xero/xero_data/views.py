"""
Xero data views - transaction and journal data update endpoints.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Q, Min, Max
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_data.services import update_financial_data
from apps.xero.xero_data.models import XeroJournals, XeroJournalsSource, XeroDocument, AgedPayable, AgedReceivable
from apps.xero.xero_metadata.models import XeroTracking
from apps.xero.xero_data.document_sync import sync_documents_for_tenant
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls
from apps.xero.xero_data.aged_reports_service import sync_aged_payables, sync_aged_receivables

logger = logging.getLogger(__name__)


def _fin_year_label(journal):
    """FY named for the year it ends in. Per tenant: each organisation carries
    its own fiscal_year_start_month (Klikk 7, Tremly 3, Dippenaar Family 1)."""
    if not journal.date:
        return ''
    org = journal.organisation
    start = (getattr(org, 'fiscal_year_start_month', None) or 1) if org else 1
    y, m = journal.date.year, journal.date.month
    return 'FY%d' % (y + 1 if (start > 1 and m >= start) else y)


class XeroJournalSearchView(APIView):
    """
    Read-only journal search for agent and reporting workflows.

    Query params:
    - q: text search across description, reference, contact, account code/name, tenant
    - amount: exact amount; matches debit/credit and signed amount
    - date_from/date_to: YYYY-MM-DD
    - tenant: tenant id or tenant name fragment
    - account: account code or account name fragment
    - contact: contact name fragment
    - reference: reference fragment
    - journal_type: 'journal' or 'transaction' (journals are mirrored under both)
    - description: description fragment
    - limit/offset: pagination, max limit 1000
    """
    # Locked down 2026-08-19: this endpoint exposes the full general ledger.
    # Excel add-in authenticates with a DRF token; the console sends its JWT.
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = XeroJournals.objects.select_related(
            'organisation',
            'account',
            'contact',
            'tracking1',
            'tracking2',
            'transaction_source',
            'transaction_source__contact',
        ).order_by('-date', '-journal_number', '-id')

        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(description__icontains=q)
                | Q(reference__icontains=q)
                | Q(contact__name__icontains=q)
                | Q(transaction_source__contact__name__icontains=q)
                | Q(account__code__icontains=q)
                | Q(account__name__icontains=q)
                | Q(organisation__tenant_name__icontains=q)
            )

        tenant = (request.query_params.get('tenant') or '').strip()
        if tenant:
            qs = qs.filter(
                Q(organisation__tenant_id__icontains=tenant)
                | Q(organisation__tenant_name__icontains=tenant)
            )

        account = (request.query_params.get('account') or '').strip()
        if account:
            qs = qs.filter(Q(account__code__icontains=account) | Q(account__name__icontains=account))

        contact = (request.query_params.get('contact') or '').strip()
        if contact:
            # journal-type rows carry no contact of their own; the supplier only
            # exists on the transaction source, so match either side.
            qs = qs.filter(
                Q(contact__name__icontains=contact)
                | Q(transaction_source__contact__name__icontains=contact)
            )

        reference = (request.query_params.get('reference') or '').strip()
        if reference:
            qs = qs.filter(reference__icontains=reference)

        description = (request.query_params.get('description') or '').strip()
        if description:
            qs = qs.filter(description__icontains=description)

        journal_type = (request.query_params.get('journal_type') or '').strip()
        if journal_type:
            qs = qs.filter(journal_type__iexact=journal_type)

        amount_param = (request.query_params.get('amount') or '').strip()
        if amount_param:
            try:
                amount = Decimal(amount_param)
                qs = qs.filter(
                    Q(amount=amount)
                    | Q(amount=-amount)
                    | Q(debit=amount)
                    | Q(credit=amount)
                    | Q(credit=-amount)
                )
            except (InvalidOperation, ValueError):
                return Response({'error': 'amount must be a decimal number'}, status=status.HTTP_400_BAD_REQUEST)

        date_from = parse_date(request.query_params.get('date_from') or '')
        if date_from:
            qs = qs.filter(date__date__gte=date_from)

        date_to = parse_date(request.query_params.get('date_to') or '')
        if date_to:
            qs = qs.filter(date__date__lte=date_to)

        try:
            requested_limit = int(request.query_params.get('limit', 100))
        except (TypeError, ValueError):
            requested_limit = 100
        limit = min(max(requested_limit, 1), 1000)

        try:
            offset = int(request.query_params.get('offset', 0))
        except (TypeError, ValueError):
            offset = 0
        offset = max(offset, 0)

        total_count = qs.count()
        page = qs[offset:offset + limit]

        results = []
        for journal in page:
            account_obj = journal.account
            contact_obj = journal.contact
            tracking1 = journal.tracking1
            tracking2 = journal.tracking2
            transaction_source = journal.transaction_source
            source_contact = transaction_source.contact if transaction_source else None
            # Xero hangs the supplier off the source document, not the journal line;
            # only 'transaction' rows carry a contact directly.
            supplier = contact_obj or source_contact
            results.append({
                'id': journal.id,
                'tenant_id': journal.organisation.tenant_id if journal.organisation else '',
                'tenant_name': journal.organisation.tenant_name if journal.organisation else '',
                'date': journal.date.date().isoformat() if journal.date else None,
                'journal_number': journal.journal_number,
                'journal_type': journal.journal_type,
                'fin_year': _fin_year_label(journal),
                'report': (
                    ('Income Statement' if account_obj.grouping in ('REVENUE', 'EXPENSE')
                     else 'Balance Sheet') if account_obj and account_obj.grouping else ''
                ),
                'account_class': account_obj.grouping if account_obj else '',
                'account_code': account_obj.code if account_obj else '',
                'account_name': account_obj.name if account_obj else '',
                'account_type': account_obj.type if account_obj else '',
                'amount': str(journal.amount),
                'debit': str(journal.debit),
                'credit': str(journal.credit),
                'tax_amount': str(journal.tax_amount),
                'contact_name': contact_obj.name if contact_obj else '',
                'supplier_name': supplier.name if supplier else '',
                'supplier_via': ('journal' if contact_obj else ('source' if source_contact else '')),
                'description': journal.description or '',
                'reference': journal.reference or '',
                'tracking1_category': tracking1.name if tracking1 else '',
                'tracking1': tracking1.option if tracking1 else '',
                'tracking2_category': tracking2.name if tracking2 else '',
                'tracking2': tracking2.option if tracking2 else '',
                'transaction_source_type': transaction_source.transaction_source if transaction_source else '',
                'transaction_source_id': transaction_source.transactions_id if transaction_source else '',
            })

        return Response({
            'count': total_count,
            'limit': limit,
            'offset': offset,
            'results': results,
        })


class XeroUpdateDataView(APIView):
    """
    API endpoint to update Xero transaction data (bank_transactions, invoices, payments, journals).
    This is separate from metadata updates (accounts, contacts, tracking categories).
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Update transaction data for a specific tenant.
        
        Expected payload:
        {
            "tenant_id": "string",
            "load_all": false  // Optional, default: false - If true, ignores last update timestamp and loads everything.
                              // If false, uses incremental updates based on last update timestamp.
        }
        """
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Get journal loading parameters
        load_all = request.data.get('load_all', False)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            # Use logged-in user if authenticated, otherwise pass None to let service find credentials with token
            user = request.user if request.user.is_authenticated else None
            
            # Transaction pipeline: transactions + Manual Journals only.
            if settings.DEBUG:
                print("[Sync] Updating data (transactions + Manual Journals)")
            result = update_financial_data(
                tenant_id,
                user=user,
                load_all=load_all,
            )

            # Log API calls for rate limit tracking
            api_calls = result.get('stats', {}).get('api_calls', 0)
            log_xero_api_calls('data', api_calls, tenant=tenant)

            if result['success']:
                return Response({
                    "message": result['message'],
                    "stats": result['stats']
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "message": result['message'],
                    "errors": result['errors'],
                    "stats": result['stats']
                }, status=status.HTTP_207_MULTI_STATUS)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({"error": f"Failed to update data: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroProcessJournalsView(APIView):
    """
    API endpoint to process journals from XeroJournalsSource to XeroJournals.
    This parses the raw journal data and creates individual journal line records.
    Handles both regular journals and manual journals.
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Process journals from XeroJournalsSource to XeroJournals.
        
        Expected payload:
        {
            "tenant_id": "string"
        }
        """
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            # Debug: Check all journals for this tenant
            all_journals_count = XeroJournalsSource.objects.filter(organisation=tenant).count()
            processed_count_db = XeroJournalsSource.objects.filter(organisation=tenant, processed=True).count()
            unprocessed_count = XeroJournalsSource.objects.filter(
                organisation=tenant,
                processed=False
            ).count()
            
            # Debug: Check by journal type
            unprocessed_manual = XeroJournalsSource.objects.filter(
                organisation=tenant,
                processed=False,
                journal_type='manual_journal'
            ).count()
            unprocessed_regular = XeroJournalsSource.objects.filter(
                organisation=tenant,
                processed=False,
                journal_type='journal'
            ).count()
            
            # Log debug information
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[PROCESS JOURNALS] Tenant {tenant_id}: Total={all_journals_count}, "
                       f"Processed={processed_count_db}, Unprocessed={unprocessed_count} "
                       f"(Manual={unprocessed_manual}, Regular={unprocessed_regular})")
            
            if unprocessed_count == 0:
                log_xero_api_calls('journals', 0, tenant=tenant)
                return Response({
                    "message": f"No unprocessed journals found for tenant {tenant_id}",
                    "journals_processed": 0,
                    "debug": {
                        "total_journals": all_journals_count,
                        "processed": processed_count_db,
                        "unprocessed": unprocessed_count,
                        "unprocessed_manual": unprocessed_manual,
                        "unprocessed_regular": unprocessed_regular
                    }
                }, status=status.HTTP_200_OK)

            # Process journals from XeroJournalsSource to XeroJournals
            result = XeroJournalsSource.objects.create_journals_from_xero(tenant)
            
            # Count processed journals
            processed_count = result.count()
            log_xero_api_calls('journals', 0, tenant=tenant)

            return Response({
                "message": f"Successfully processed {processed_count} journal lines for tenant {tenant_id}",
                "journals_processed": processed_count,
                "unprocessed_before": unprocessed_count
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response({
                "error": f"Failed to process journals: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroSyncDocumentsView(APIView):
    """
    Import documents (attachments) from Xero and link them to transactions.

    Requires Xero OAuth scope: accounting.attachments or accounting.attachments.read.
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Sync documents for a tenant.

        Payload:
        {
            "tenant_id": "string",
            "transaction_ids": ["id1", "id2"],  // optional; if omitted, syncs all supported transactions
            "types": ["Invoice", "CreditNote", "BankTransaction"]  // optional
        }
        """
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = request.user if request.user.is_authenticated else None
            result = sync_documents_for_tenant(
                tenant_id,
                user=user,
                transaction_ids=request.data.get('transaction_ids'),
                source_types=request.data.get('types'),
            )
            status_code = status.HTTP_200_OK if result['success'] else status.HTTP_207_MULTI_STATUS
            return Response(result, status=status_code)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroDocumentsByTransactionView(APIView):
    """List documents linked to a Xero transaction (by transaction ID, e.g. InvoiceID)."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request, transaction_id):
        qs = XeroDocument.objects.filter(
            transaction_source__transactions_id=transaction_id
        ).select_related('transaction_source', 'organisation').order_by('file_name')
        tenant_id = request.query_params.get('tenant_id')
        if tenant_id:
            qs = qs.filter(organisation__tenant_id=tenant_id)
        docs = qs
        data = [
            {
                'id': d.id,
                'file_name': d.file_name,
                'content_type': d.content_type,
                'url': request.build_absolute_uri(d.file.url) if d.file else None,
                'transaction_id': d.transaction_source.transactions_id,
                'transaction_source': d.transaction_source.transaction_source,
            }
            for d in docs
        ]
        return Response(data)


# ---------------------------------------------------------------------------
# Aged Reports — sync triggers + list views
# ---------------------------------------------------------------------------

class XeroSyncAgedPayablesView(APIView):
    """
    POST /xero/data/aged-payables/sync/

    Trigger a sync of Aged Payables By Contact from Xero into the local DB.

    Payload:  { "tenant_id": "<UUID>" }
    Response: { "created": N, "updated": N, "skipped": N, "errors": N,
                "contact_count": N, "completed_at": "<ISO>" }
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({'error': 'Tenant not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            result = sync_aged_payables(tenant)
            log_xero_api_calls('aged-payables', result.get('contact_count', 0), tenant=tenant)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            logger.exception('Aged payables sync failed for tenant %s', tenant_id)
            return Response({'error': f'Sync failed: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroSyncAgedReceivablesView(APIView):
    """
    POST /xero/data/aged-receivables/sync/

    Trigger a sync of Aged Receivables By Contact from Xero into the local DB.

    Payload:  { "tenant_id": "<UUID>" }
    Response: { "created": N, "updated": N, "skipped": N, "errors": N,
                "contact_count": N, "completed_at": "<ISO>" }
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({'error': 'Tenant not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            result = sync_aged_receivables(tenant)
            log_xero_api_calls('aged-receivables', result.get('contact_count', 0), tenant=tenant)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            logger.exception('Aged receivables sync failed for tenant %s', tenant_id)
            return Response({'error': f'Sync failed: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class _AgedReportPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 500


class XeroAgedPayablesListView(APIView):
    """
    GET /xero/data/aged-payables/?tenant_id=<UUID>&date=<YYYY-MM-DD>

    List AgedPayable rows. date filter is optional (returns all dates if omitted).
    Response: paginated list of { id, contact_id, contact_name, report_date,
              current, one_month, two_months, three_months, older, total, synced_at }
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        qs = AgedPayable.objects.filter(tenant_id=tenant_id).order_by('report_date', 'contact_name')
        date_filter = request.query_params.get('date')
        if date_filter:
            qs = qs.filter(report_date=date_filter)

        paginator = _AgedReportPagination()
        page = paginator.paginate_queryset(qs, request)
        data = [
            {
                'id': r.id,
                'contact_id': r.contact_id,
                'contact_name': r.contact_name,
                'report_date': r.report_date.isoformat(),
                'current': str(r.current),
                'one_month': str(r.one_month),
                'two_months': str(r.two_months),
                'three_months': str(r.three_months),
                'older': str(r.older),
                'total': str(r.total),
                'synced_at': r.synced_at.isoformat(),
            }
            for r in page
        ]
        return paginator.get_paginated_response(data)


class XeroAgedReceivablesListView(APIView):
    """
    GET /xero/data/aged-receivables/?tenant_id=<UUID>&date=<YYYY-MM-DD>

    List AgedReceivable rows.
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        qs = AgedReceivable.objects.filter(tenant_id=tenant_id).order_by('report_date', 'contact_name')
        date_filter = request.query_params.get('date')
        if date_filter:
            qs = qs.filter(report_date=date_filter)

        paginator = _AgedReportPagination()
        page = paginator.paginate_queryset(qs, request)
        data = [
            {
                'id': r.id,
                'contact_id': r.contact_id,
                'contact_name': r.contact_name,
                'report_date': r.report_date.isoformat(),
                'current': str(r.current),
                'one_month': str(r.one_month),
                'two_months': str(r.two_months),
                'three_months': str(r.three_months),
                'older': str(r.older),
                'total': str(r.total),
                'synced_at': r.synced_at.isoformat(),
            }
            for r in page
        ]
        return paginator.get_paginated_response(data)


# ============================================================================
# Xero Quotes — sync + list + detail
# ============================================================================

from apps.xero.xero_data.models import XeroQuote, XeroQuoteLineItem  # noqa: E402
from apps.xero.xero_data.quotes_service import sync_xero_quotes  # noqa: E402


class XeroSyncQuotesView(APIView):
    """
    POST /xero/data/quotes/sync/

    Payload: { "tenant_id": "<UUID>", "modified_since": "YYYY-MM-DD"?, "full": bool? }
    Response: { stats dict from sync_xero_quotes }
    """
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({'error': 'Tenant not found'},
                            status=status.HTTP_404_NOT_FOUND)

        modified_since = None
        ms = request.data.get('modified_since')
        if ms:
            from datetime import datetime as _dt
            try:
                modified_since = _dt.strptime(ms, '%Y-%m-%d')
            except ValueError:
                return Response({'error': 'modified_since must be YYYY-MM-DD'},
                                status=status.HTTP_400_BAD_REQUEST)

        full = bool(request.data.get('full', False))

        try:
            result = sync_xero_quotes(tenant, modified_since=modified_since, full=full)
            log_xero_api_calls('quotes', result.get('api_calls', 0), tenant=tenant)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception('Quotes sync failed for tenant %s', tenant_id)
            return Response({'error': f'Sync failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _serialize_quote(q: XeroQuote, include_lines=False):
    payload = {
        'id': q.id,
        'tenant_id': q.organisation_id,
        'quote_id': q.quote_id,
        'quote_number': q.quote_number,
        'reference': q.reference,
        'contact': {
            'contact_id': q.xero_contact_id,
            'name': q.contact_name,
        } if q.xero_contact_id or q.contact_name else None,
        'status': q.status,
        'date': q.date.isoformat() if q.date else None,
        'expiry_date': q.expiry_date.isoformat() if q.expiry_date else None,
        'currency_code': q.currency_code,
        'currency_rate': str(q.currency_rate),
        'sub_total': str(q.sub_total),
        'total_tax': str(q.total_tax),
        'total': str(q.total),
        'total_discount': str(q.total_discount) if q.total_discount is not None else None,
        'title': q.title,
        'summary': q.summary,
        'terms': q.terms,
        'line_amount_types': q.line_amount_types,
        'branding_theme_id': q.branding_theme_id,
        'updated_date_utc': q.updated_date_utc.isoformat() if q.updated_date_utc else None,
        'synced_at': q.synced_at.isoformat(),
    }
    if include_lines:
        payload['line_items'] = [{
            'id': li.id,
            'line_item_id': li.line_item_id,
            'description': li.description,
            'quantity': str(li.quantity),
            'unit_amount': str(li.unit_amount),
            'item_code': li.item_code,
            'account_code': li.account_code,
            'tax_type': li.tax_type,
            'tax_amount': str(li.tax_amount),
            'line_amount': str(li.line_amount),
            'discount_rate': str(li.discount_rate) if li.discount_rate is not None else None,
            'discount_amount': str(li.discount_amount) if li.discount_amount is not None else None,
            'tracking1': li.tracking1.option_name if li.tracking1_id else None,
            'tracking2': li.tracking2.option_name if li.tracking2_id else None,
            'position': li.position,
        } for li in q.line_items.all().order_by('position')]
    return payload


class XeroQuoteListView(APIView):
    """
    GET /xero/data/quotes/?tenant_id=&status=&contact_id=&date_from=&date_to=&q=&limit=&offset=

    Returns: { count, results: [...] }
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        qs = XeroQuote.objects.filter(organisation_id=tenant_id)

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        contact_id = request.query_params.get('contact_id')
        if contact_id:
            qs = qs.filter(xero_contact_id=contact_id)

        date_from = request.query_params.get('date_from')
        if date_from:
            d = parse_date(date_from)
            if d:
                qs = qs.filter(date__gte=d)
        date_to = request.query_params.get('date_to')
        if date_to:
            d = parse_date(date_to)
            if d:
                qs = qs.filter(date__lte=d)

        q_term = request.query_params.get('q')
        if q_term:
            qs = qs.filter(
                Q(quote_number__icontains=q_term) |
                Q(reference__icontains=q_term) |
                Q(title__icontains=q_term) |
                Q(contact_name__icontains=q_term)
            )

        total = qs.count()
        try:
            limit = min(int(request.query_params.get('limit', 100)), 1000)
        except ValueError:
            limit = 100
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        rows = qs.order_by('-date', '-updated_date_utc')[offset:offset + limit]
        return Response({
            'count': total,
            'limit': limit,
            'offset': offset,
            'results': [_serialize_quote(q, include_lines=False) for q in rows],
        }, status=status.HTTP_200_OK)


class XeroQuoteDetailView(APIView):
    """
    GET /xero/data/quotes/<quote_id>/

    quote_id matches the Xero QuoteID (UUID).
    Returns: full quote + line_items[].
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request, quote_id):
        tenant_id = request.query_params.get('tenant_id')
        qs = XeroQuote.objects.filter(quote_id=quote_id)
        if tenant_id:
            qs = qs.filter(organisation_id=tenant_id)
        quote = qs.first()
        if not quote:
            return Response({'error': 'Quote not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_quote(quote, include_lines=True),
                        status=status.HTTP_200_OK)


# ============================================================================
# Xero Invoices — sync + list + detail
# (Parallel to XeroTransactionSource; the JSON path stays as the TB source.)
# ============================================================================

from apps.xero.xero_data.models import XeroInvoice, XeroInvoiceLineItem  # noqa: E402
from apps.xero.xero_data.invoices_service import sync_xero_invoices  # noqa: E402


class XeroSyncInvoicesView(APIView):
    """POST /xero/data/invoices/sync/
    Payload: { tenant_id, modified_since?, type?, statuses?, full? }"""
    # SECURITY (2026-08-20): mutating Xero trigger. Was AllowAny on a publicly
    # routed host, so anyone on the internet could start a sync and burn the
    # 1,000-calls/day tenant budget. Console sends its JWT; MCP uses the service token.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({'error': 'Tenant not found'}, status=status.HTTP_404_NOT_FOUND)

        modified_since = None
        ms = request.data.get('modified_since')
        if ms:
            from datetime import datetime as _dt
            try:
                modified_since = _dt.strptime(ms, '%Y-%m-%d')
            except ValueError:
                return Response({'error': 'modified_since must be YYYY-MM-DD'},
                                status=status.HTTP_400_BAD_REQUEST)

        invoice_type = request.data.get('type')
        statuses = request.data.get('statuses')
        full = bool(request.data.get('full', False))

        try:
            result = sync_xero_invoices(
                tenant, modified_since=modified_since,
                statuses=statuses, invoice_type=invoice_type, full=full,
            )
            log_xero_api_calls('invoices', result.get('api_calls', 0), tenant=tenant)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception('Invoices sync failed for tenant %s', tenant_id)
            return Response({'error': f'Sync failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _serialize_invoice(inv: XeroInvoice, include_lines=False):
    payload = {
        'id': inv.id,
        'tenant_id': inv.organisation_id,
        'invoice_id': inv.invoice_id,
        'invoice_number': inv.invoice_number,
        'reference': inv.reference,
        'type': inv.type,
        'status': inv.status,
        'contact': {
            'contact_id': inv.xero_contact_id,
            'name': inv.contact_name,
        } if inv.xero_contact_id or inv.contact_name else None,
        'date': inv.date.isoformat() if inv.date else None,
        'due_date': inv.due_date.isoformat() if inv.due_date else None,
        'fully_paid_on_date': inv.fully_paid_on_date.isoformat() if inv.fully_paid_on_date else None,
        'currency_code': inv.currency_code,
        'currency_rate': str(inv.currency_rate),
        'sub_total': str(inv.sub_total),
        'total_tax': str(inv.total_tax),
        'total': str(inv.total),
        'amount_due': str(inv.amount_due),
        'amount_paid': str(inv.amount_paid),
        'amount_credited': str(inv.amount_credited),
        'line_amount_types': inv.line_amount_types,
        'sent_to_contact': inv.sent_to_contact,
        'is_discounted': inv.is_discounted,
        'has_attachments': inv.has_attachments,
        'updated_date_utc': inv.updated_date_utc.isoformat() if inv.updated_date_utc else None,
        'synced_at': inv.synced_at.isoformat(),
    }
    if include_lines:
        payload['line_items'] = [{
            'id': li.id,
            'line_item_id': li.line_item_id,
            'description': li.description,
            'quantity': str(li.quantity),
            'unit_amount': str(li.unit_amount),
            'item_code': li.item_code,
            'account_code': li.account_code,
            'tax_type': li.tax_type,
            'tax_amount': str(li.tax_amount),
            'line_amount': str(li.line_amount),
            'discount_rate': str(li.discount_rate) if li.discount_rate is not None else None,
            'discount_amount': str(li.discount_amount) if li.discount_amount is not None else None,
            'tracking1': li.tracking1.option if li.tracking1_id else None,
            'tracking2': li.tracking2.option if li.tracking2_id else None,
            'position': li.position,
        } for li in inv.line_items.all().order_by('position')]
    return payload


class XeroInvoiceListView(APIView):
    """GET /xero/data/invoices/
    Filters: tenant_id (required), type=ACCREC|ACCPAY, status, contact_id,
             date_from, date_to, due_date_from, due_date_to,
             min_amount_due (e.g. for "outstanding > 0"), q, limit, offset.
    """
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        qs = XeroInvoice.objects.filter(organisation_id=tenant_id)

        t = request.query_params.get('type')
        if t:
            qs = qs.filter(type=t.upper())

        s_filter = request.query_params.get('status')
        if s_filter:
            qs = qs.filter(status=s_filter.upper())

        contact_id = request.query_params.get('contact_id')
        if contact_id:
            qs = qs.filter(xero_contact_id=contact_id)

        for field, param in (('date__gte', 'date_from'),
                             ('date__lte', 'date_to'),
                             ('due_date__gte', 'due_date_from'),
                             ('due_date__lte', 'due_date_to')):
            v = request.query_params.get(param)
            if v:
                d = parse_date(v)
                if d:
                    qs = qs.filter(**{field: d})

        min_due = request.query_params.get('min_amount_due')
        if min_due:
            try:
                qs = qs.filter(amount_due__gte=Decimal(min_due))
            except (InvalidOperation, ValueError):
                pass

        q_term = request.query_params.get('q')
        if q_term:
            qs = qs.filter(
                Q(invoice_number__icontains=q_term) |
                Q(reference__icontains=q_term) |
                Q(contact_name__icontains=q_term)
            )

        total_rows = qs.count()
        try:
            limit = min(int(request.query_params.get('limit', 100)), 1000)
        except ValueError:
            limit = 100
        try:
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        rows = qs.order_by('-date', '-updated_date_utc')[offset:offset + limit]
        return Response({
            'count': total_rows,
            'limit': limit, 'offset': offset,
            'results': [_serialize_invoice(inv, include_lines=False) for inv in rows],
        }, status=status.HTTP_200_OK)


class XeroInvoiceDetailView(APIView):
    """GET /xero/data/invoices/<invoice_id>/ — full invoice + line items."""
    permission_classes = [IsAuthenticated]  # Gated 2026-08-20 (SECURITY-NOTE.md lockdown); was AllowAny

    def get(self, request, invoice_id):
        tenant_id = request.query_params.get('tenant_id')
        qs = XeroInvoice.objects.filter(invoice_id=invoice_id)
        if tenant_id:
            qs = qs.filter(organisation_id=tenant_id)
        inv = qs.first()
        if not inv:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_invoice(inv, include_lines=True),
                        status=status.HTTP_200_OK)


class XeroJournalFilterOptionsView(APIView):
    """
    Dropdown fodder for the Excel add-in: the tenants and accounts that actually
    appear in the journal table, plus the distinct journal types.

    Derived from the journals themselves rather than the metadata tables, so the
    lists never offer a filter that returns nothing.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenants = (
            XeroJournals.objects
            .values('organisation__tenant_id', 'organisation__tenant_name')
            .distinct()
            .order_by('organisation__tenant_name')
        )
        accounts = (
            XeroJournals.objects
            .values('account__code', 'account__name', 'account__type')
            .distinct()
            .order_by('account__code')
        )
        journal_types = (
            XeroJournals.objects
            .values_list('journal_type', flat=True)
            .distinct()
            .order_by('journal_type')
        )
        # Suppliers reachable from a journal line — directly, or via the source
        # document, which is the only place 'journal' rows carry a contact.
        contacts = sorted({
            name for name in (
                list(XeroJournals.objects
                     .exclude(contact__isnull=True)
                     .values_list('contact__name', flat=True).distinct())
                + list(XeroJournals.objects
                       .exclude(transaction_source__contact__isnull=True)
                       .values_list('transaction_source__contact__name', flat=True).distinct())
            ) if name
        })
        tracking_categories = sorted({
            name for name in XeroTracking.objects
            .values_list('name', flat=True).distinct() if name
        })
        date_range = XeroJournals.objects.aggregate(
            first=Min('date'), last=Max('date')
        )

        return Response({
            'tenants': [
                {'tenant_id': t['organisation__tenant_id'], 'tenant_name': t['organisation__tenant_name']}
                for t in tenants if t['organisation__tenant_id']
            ],
            'accounts': [
                {'code': a['account__code'], 'name': a['account__name'], 'type': a['account__type']}
                for a in accounts if a['account__code']
            ],
            'journal_types': [jt for jt in journal_types if jt],
            'contacts': contacts,
            'tracking_categories': tracking_categories,
            'date_from': date_range['first'].date().isoformat() if date_range['first'] else None,
            'date_to': date_range['last'].date().isoformat() if date_range['last'] else None,
        })


class XeroCreateDraftInvoiceView(APIView):
    """POST /xero/data/invoices/create-draft/ — the ONE Xero write path.

    Creates a single DRAFT invoice (ACCREC or ACCPAY), pre-logged to
    audit.xero_writes with MC's authorising instruction quoted verbatim.
    DRAFT only: nothing touches a ledger until a human approves it in Xero.
    See invoice_write_service for the rules this enforces.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.xero.xero_core.exceptions import DailyLimitReached, TenantReauthRequired
        from apps.xero.xero_data.invoice_write_service import (
            InvoiceValidationError, create_draft_invoice,
        )
        try:
            result = create_draft_invoice(request.data if isinstance(request.data, dict) else {})
        except InvoiceValidationError as exc:
            return Response({'error': 'validation', 'problems': exc.problems, **exc.extra},
                            status=status.HTTP_400_BAD_REQUEST)
        except TenantReauthRequired as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        except DailyLimitReached as exc:
            return Response({'error': str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except Exception as exc:  # SDK ApiException etc. — already audit-logged by the service
            return Response({'error': f'Xero write failed: {exc}'},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response(result, status=status.HTTP_201_CREATED)
