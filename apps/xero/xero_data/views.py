"""
Xero data views - transaction and journal data update endpoints.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.pagination import PageNumberPagination

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_data.services import update_financial_data
from apps.xero.xero_data.models import XeroJournals, XeroJournalsSource, XeroDocument, AgedPayable, AgedReceivable
from apps.xero.xero_data.document_sync import sync_documents_for_tenant
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls
from apps.xero.xero_data.aged_reports_service import sync_aged_payables, sync_aged_receivables

logger = logging.getLogger(__name__)


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
    - description: description fragment
    - limit/offset: pagination, max limit 1000
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = XeroJournals.objects.select_related(
            'organisation',
            'account',
            'contact',
            'tracking1',
            'tracking2',
            'transaction_source',
        ).order_by('-date', '-journal_number', '-id')

        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(description__icontains=q)
                | Q(reference__icontains=q)
                | Q(contact__name__icontains=q)
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
            qs = qs.filter(contact__name__icontains=contact)

        reference = (request.query_params.get('reference') or '').strip()
        if reference:
            qs = qs.filter(reference__icontains=reference)

        description = (request.query_params.get('description') or '').strip()
        if description:
            qs = qs.filter(description__icontains=description)

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
            results.append({
                'id': journal.id,
                'tenant_id': journal.organisation.tenant_id if journal.organisation else '',
                'tenant_name': journal.organisation.tenant_name if journal.organisation else '',
                'date': journal.date.date().isoformat() if journal.date else None,
                'journal_number': journal.journal_number,
                'journal_type': journal.journal_type,
                'account_code': account_obj.code if account_obj else '',
                'account_name': account_obj.name if account_obj else '',
                'account_type': account_obj.type if account_obj else '',
                'amount': str(journal.amount),
                'debit': str(journal.debit),
                'credit': str(journal.credit),
                'tax_amount': str(journal.tax_amount),
                'contact_name': contact_obj.name if contact_obj else '',
                'description': journal.description or '',
                'reference': journal.reference or '',
                'tracking1': tracking1.option if tracking1 else '',
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
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

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
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

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
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

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
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

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
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]

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
