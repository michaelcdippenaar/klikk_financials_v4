"""
Xero data views - transaction and journal data update endpoints.
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials
from apps.xero.xero_data.services import update_financial_data
from apps.xero.xero_data.models import XeroJournalsSource, XeroDocument
from apps.xero.xero_data.document_sync import sync_documents_for_tenant
from apps.xero.xero_sync.api_call_logging import log_xero_api_calls

logger = logging.getLogger(__name__)


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
