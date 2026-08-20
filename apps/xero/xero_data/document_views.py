"""
Xero document views - search + signed file streaming from the Postgres/disk mirror.

Everything here serves ONLY from the local mirror (XeroDocument rows + their files
on disk under XERO_DOCUMENTS_ROOT). No Xero API calls happen in this module.

The file streamer follows the same pattern as apps/audit/slip_view.py:

URL: /xero/data/documents/<id>/file/?s=<sig>   where sig = HMAC-SHA256(SECRET_KEY, id)[:32]
Links are unguessable and never expire so spreadsheet/console links keep working.

The streamer is DELIBERATELY unauthenticated and must stay that way: the console's
document modal loads it in a plain <img>/<iframe> and exported spreadsheets link to
it, neither of which can carry a Bearer token. The HMAC in ?s= is its only guard,
which is why the endpoint that hands out these links (XeroDocumentSearchView below)
requires authentication.
"""
import hashlib
import hmac
import logging
import mimetypes
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.xero.xero_data.models import XeroDocument, XeroInvoice, XeroJournals

logger = logging.getLogger(__name__)

AMOUNT_TOLERANCE = Decimal('0.01')


def xero_document_signature(document_id) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), str(document_id).encode(), hashlib.sha256
    ).hexdigest()[:32]


DEFAULT_VIEW_BASE = 'https://console.8-bit.space/backend'


def public_base_url(request=None) -> str:
    """
    The public origin + path prefix to hang signed document links off.

    Derived from the REQUEST (scheme + host) rather than hardcoded, so a link
    minted on staging/dev/localhost points back at the host the caller actually
    reached — the old behaviour handed every caller a production URL, which is a
    dead link anywhere but production and a silent cross-environment leak of the
    console's hostname.

    The PATH PREFIX cannot come from the request: nginx serves this API under
    ``/backend`` and strips that prefix before Django sees it, so
    ``request.path`` has no ``/backend`` in it and building the URL from the
    request alone would drop it and produce a 404 in production. The prefix
    therefore comes from ``X-Forwarded-Prefix`` when the proxy sets it, else
    from the configured ``SLIP_VIEW_BASE_URL`` — which is also the whole-URL
    fallback when there is no request (management commands, exports, tests).
    """
    configured = getattr(settings, 'SLIP_VIEW_BASE_URL', DEFAULT_VIEW_BASE)
    if request is None:
        return configured
    try:
        host = request.get_host()
    except Exception:  # DisallowedHost and friends — never 500 a link builder
        return configured
    if not host:
        return configured
    prefix = (request.META.get('HTTP_X_FORWARDED_PREFIX') or urlsplit(configured).path or '')
    return f'{request.scheme}://{host}{prefix.rstrip("/")}'


def xero_document_url(document_id, base: str | None = None, request=None) -> str:
    """Signed, absolute URL for ``xero_document_file_view``.

    ``base`` is an explicit override and wins over everything; otherwise the base
    is derived from ``request`` (see ``public_base_url``).
    """
    base = base or public_base_url(request)
    return f"{base}/xero/data/documents/{document_id}/file/?s={xero_document_signature(document_id)}"


class XeroDocumentSearchView(APIView):
    """
    Read-only document search over the local Xero document mirror.

    Query params (all optional; with none given, returns the most recent documents):
    - invoice_number: fragment of the linked invoice's number
    - amount: decimal; matches the linked invoice total OR any journal debit on the
      same transaction source, within +/- 0.01
    - q: fragment of invoice contact name, journal description, or document file name
    - date_from/date_to: YYYY-MM-DD, inclusive, against the linked invoice's date
    - tenant_id: exact tenant id
    - limit: default 20, max 100

    Each result carries a signed `view_url` served by xero_document_file_view below.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = XeroDocument.objects.select_related(
            'transaction_source', 'organisation'
        ).order_by('-id')

        tenant_id = (request.query_params.get('tenant_id') or '').strip()
        if tenant_id:
            qs = qs.filter(organisation__tenant_id=tenant_id)

        # invoice_number and date filters both constrain the same joined invoice,
        # so resolve them with a single XeroInvoice query into a GUID set.
        # XeroInvoice is not an FK from XeroDocument; the join key is
        # XeroTransactionSource.transactions_id == XeroInvoice.invoice_id.
        invoice_qs = XeroInvoice.objects.all()
        invoice_filtered = False

        invoice_number = (request.query_params.get('invoice_number') or '').strip()
        if invoice_number:
            invoice_qs = invoice_qs.filter(invoice_number__icontains=invoice_number)
            invoice_filtered = True

        date_from = parse_date(request.query_params.get('date_from') or '')
        if date_from:
            invoice_qs = invoice_qs.filter(date__gte=date_from)
            invoice_filtered = True

        date_to = parse_date(request.query_params.get('date_to') or '')
        if date_to:
            invoice_qs = invoice_qs.filter(date__lte=date_to)
            invoice_filtered = True

        if invoice_filtered:
            matching_guids = set(invoice_qs.values_list('invoice_id', flat=True))
            # An empty set is correct here: no invoice matched, so no documents match.
            qs = qs.filter(transaction_source__transactions_id__in=matching_guids)

        amount_param = (request.query_params.get('amount') or '').strip()
        if amount_param:
            try:
                amount = Decimal(amount_param)
            except (InvalidOperation, ValueError):
                return Response(
                    {'error': 'amount must be a decimal number'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            lo, hi = amount - AMOUNT_TOLERANCE, amount + AMOUNT_TOLERANCE
            amount_guids = set(
                XeroInvoice.objects.filter(total__gte=lo, total__lte=hi)
                .values_list('invoice_id', flat=True)
            )
            # XeroJournals.transaction_source uses to_field='transactions_id',
            # so transaction_source_id already holds the GUID string.
            amount_guids |= set(
                XeroJournals.objects.filter(debit__gte=lo, debit__lte=hi)
                .values_list('transaction_source_id', flat=True)
            )
            qs = qs.filter(transaction_source__transactions_id__in=amount_guids)

        q = (request.query_params.get('q') or '').strip()
        if q:
            q_guids = set(
                XeroInvoice.objects.filter(contact_name__icontains=q)
                .values_list('invoice_id', flat=True)
            )
            q_guids |= set(
                XeroJournals.objects.filter(description__icontains=q)
                .values_list('transaction_source_id', flat=True)
            )
            qs = qs.filter(
                Q(file_name__icontains=q)
                | Q(transaction_source__transactions_id__in=q_guids)
            )

        try:
            requested_limit = int(request.query_params.get('limit', 20))
        except (TypeError, ValueError):
            requested_limit = 20
        limit = min(max(requested_limit, 1), 100)

        page = list(qs[:limit])

        # Resolve the linked invoices for the page in one query.
        page_guids = {
            d.transaction_source.transactions_id for d in page if d.transaction_source
        }
        invoices_by_guid = {
            inv.invoice_id: inv
            for inv in XeroInvoice.objects.filter(invoice_id__in=page_guids)
        }

        results = []
        for d in page:
            guid = d.transaction_source.transactions_id if d.transaction_source else ''
            inv = invoices_by_guid.get(guid)
            results.append({
                'id': d.id,
                'file_name': d.file_name,
                'content_type': d.content_type,
                'invoice_number': inv.invoice_number if inv else None,
                'contact_name': inv.contact_name if inv else None,
                'date': inv.date.isoformat() if inv and inv.date else None,
                'total': str(inv.total) if inv and inv.total is not None else None,
                'transaction_source': (
                    d.transaction_source.transaction_source if d.transaction_source else ''
                ),
                'transaction_id': guid,
                'view_url': xero_document_url(d.id, request=request),
            })

        return Response({
            'count': len(results),
            'limit': limit,
            'results': results,
        })


@require_GET
def xero_document_file_view(request, document_id: int):
    # Signature check BEFORE any DB lookup so an invalid caller learns nothing
    # about which document ids exist.
    sig = request.GET.get('s', '')
    if not sig or not hmac.compare_digest(sig, xero_document_signature(document_id)):
        return HttpResponseForbidden("invalid signature")
    doc = XeroDocument.objects.filter(id=document_id).first()
    if not doc or not doc.file or not doc.file.name:
        return HttpResponseNotFound("document not found")
    try:
        with doc.file.open('rb') as fh:
            data = fh.read()
    except (FileNotFoundError, OSError, ValueError):
        logger.warning("Xero document %s has a DB row but no file on disk", document_id)
        return HttpResponseNotFound("document not found")
    ctype = (
        doc.content_type
        or mimetypes.guess_type(doc.file_name or '')[0]
        or "application/octet-stream"
    )
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = f'inline; filename="{doc.file_name}"'
    resp["Cache-Control"] = "private, max-age=3600"
    return resp
