"""
Xero cube views - data processing and summary endpoints.
"""
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_data.models import XeroJournals
from apps.xero.xero_cube.models import XeroTrailBalance, XeroBalanceSheet, XeroPnlByTracking
from apps.xero.xero_cube.services import process_xero_data, import_pnl_by_tracking


class XeroProcessDataView(APIView):
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def post(self, request):
        """
        Process Xero data (journals, trail balance, etc.).
        
        Expected payload:
        {
            "tenant_id": "string",
            "rebuild_trail_balance": false,  // Optional: If true, force full rebuild of trail balance and ignore existing data
            "exclude_manual_journals": false  // Optional: If true, only build trail balance from regular journals (exclude manual journals)
        }
        """
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        rebuild_trail_balance = request.data.get('rebuild_trail_balance', False)
        exclude_manual_journals = request.data.get('exclude_manual_journals', False)
        
        try:
            # Use the service function for consistency with scheduled tasks
            result = process_xero_data(tenant_id, rebuild_trail_balance=rebuild_trail_balance, exclude_manual_journals=exclude_manual_journals)
            
            return Response({
                "message": result['message'],
                "stats": result['stats']
            })
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({"error": f"Processing failed: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": f"Unexpected error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class XeroDataSummaryView(APIView):
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        """Get a summary of data for a tenant."""
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
            summary = {
                'tenant_id': tenant_id,
                'tenant_name': tenant.tenant_name,
                'accounts_count': XeroAccount.objects.filter(organisation=tenant).count(),
                'journals_count': XeroJournals.objects.filter(organisation=tenant).count(),
                'trail_balance_count': XeroTrailBalance.objects.filter(organisation=tenant).count(),
                'balance_sheet_count': XeroBalanceSheet.objects.filter(organisation=tenant).count(),
            }
            return Response(summary)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)


class XeroTrailBalanceListView(APIView):
    """
    List trail balance records with optional filters.
    Query params: tenant_id (required), contact_id (optional), tracking1_id (optional), tracking2_id (optional),
                  year, month, account_id, limit (default 5000, max 100000).
    """
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        qs = XeroTrailBalance.objects.filter(organisation=tenant).select_related(
            'account', 'contact', 'tracking1', 'tracking2'
        ).order_by('year', 'month', 'account__code', 'contact__name')

        # Optional filters
        contact_name = request.query_params.get('contact_name')
        if contact_name:
            qs = qs.filter(contact__name__icontains=contact_name)
        contact_id = request.query_params.get('contact_id')
        if contact_id:
            qs = qs.filter(contact_id=contact_id)
        tracking1_id = request.query_params.get('tracking1_id')
        if tracking1_id:
            try:
                qs = qs.filter(tracking1_id=int(tracking1_id))
            except ValueError:
                pass
        tracking2_id = request.query_params.get('tracking2_id')
        if tracking2_id:
            try:
                qs = qs.filter(tracking2_id=int(tracking2_id))
            except ValueError:
                pass
        year = request.query_params.get('year')
        if year:
            try:
                qs = qs.filter(year=int(year))
            except ValueError:
                pass
        month = request.query_params.get('month')
        if month:
            try:
                qs = qs.filter(month=int(month))
            except ValueError:
                pass
        account_id = request.query_params.get('account_id')
        if account_id:
            qs = qs.filter(account_id=account_id)

        limit = request.query_params.get('limit', '5000')
        try:
            limit = min(int(limit), 100000)
        except ValueError:
            limit = 5000
        qs = qs[:limit]

        # Pre-fetch Xero P&L by tracking for fast lookup
        pnl_lookup = {}
        pnl_qs = XeroPnlByTracking.objects.filter(organisation=tenant)
        for p in pnl_qs:
            key = (p.tracking_id, p.account_id, p.year, p.month)
            pnl_lookup[key] = p.xero_amount

        def serialize_tb(rec):
            xero_pnl = pnl_lookup.get(
                (rec.tracking1_id, rec.account_id, rec.year, rec.month)
            )
            return {
                'id': rec.id,
                'year': rec.year,
                'month': rec.month,
                'account_id': rec.account_id,
                'account_code': rec.account.code if rec.account else None,
                'account_name': rec.account.name if rec.account else None,
                'contact_id': rec.contact_id,
                'contact_name': rec.contact.name if rec.contact else None,
                'tracking1': rec.tracking1.option if rec.tracking1 else None,
                'tracking2': rec.tracking2.option if rec.tracking2 else None,
                'amount': str(rec.amount),
                'balance_to_date': str(rec.balance_to_date) if rec.balance_to_date is not None else None,
                'xero_pnl': str(xero_pnl) if xero_pnl is not None else None,
            }

        data = [serialize_tb(r) for r in qs]
        return Response({
            'tenant_id': tenant_id,
            'count': len(data),
            'results': data,
        })


class XeroLineItemsListView(APIView):
    """
    List journal lines (line-item level data) with optional filters.
    Each record is one line from invoices, bank transactions, credit notes, or manual journals.
    Query params: tenant_id (required), contact_id (optional), tracking1_id (optional), tracking2_id (optional),
                  account_id, year, month, date_from (YYYY-MM-DD), date_to (YYYY-MM-DD), limit (default 5000, max 100000).
    """
    permission_classes = [AllowAny]  # TODO: Change to IsAuthenticated for production

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        qs = XeroJournals.objects.filter(organisation=tenant).select_related(
            'account', 'contact', 'tracking1', 'tracking2', 'transaction_source'
        ).order_by('date', 'journal_number', 'id')

        # Optional filters
        contact_id = request.query_params.get('contact_id')
        if contact_id:
            from django.db.models import Q
            qs = qs.filter(
                Q(contact_id=contact_id) | Q(transaction_source__contact_id=contact_id)
            )
        tracking1_id = request.query_params.get('tracking1_id')
        if tracking1_id:
            try:
                qs = qs.filter(tracking1_id=int(tracking1_id))
            except ValueError:
                pass
        tracking2_id = request.query_params.get('tracking2_id')
        if tracking2_id:
            try:
                qs = qs.filter(tracking2_id=int(tracking2_id))
            except ValueError:
                pass
        account_id = request.query_params.get('account_id')
        if account_id:
            qs = qs.filter(account_id=account_id)
        year = request.query_params.get('year')
        if year:
            try:
                from django.db.models.functions import ExtractYear
                qs = qs.filter(date__year=int(year))
            except ValueError:
                pass
        month = request.query_params.get('month')
        if month:
            try:
                qs = qs.filter(date__month=int(month))
            except ValueError:
                pass
        date_from = request.query_params.get('date_from')
        if date_from:
            try:
                from datetime import datetime, time
                dt = datetime.strptime(date_from, '%Y-%m-%d')
                qs = qs.filter(date__gte=datetime.combine(dt.date(), time.min))
            except ValueError:
                pass
        date_to = request.query_params.get('date_to')
        if date_to:
            try:
                from datetime import datetime, time
                dt = datetime.strptime(date_to, '%Y-%m-%d')
                qs = qs.filter(date__lte=datetime.combine(dt.date(), time.max))
            except ValueError:
                pass

        limit = request.query_params.get('limit', '5000')
        try:
            limit = min(int(limit), 100000)
        except ValueError:
            limit = 5000
        total_count = qs.count()
        qs = qs[:limit]

        def serialize_line(rec):
            return {
                'id': rec.id,
                'journal_id': rec.journal_id,
                'journal_number': rec.journal_number,
                'journal_type': rec.journal_type,
                'date': rec.date.isoformat() if rec.date else None,
                'account_id': rec.account_id,
                'account_code': rec.account.code if rec.account else None,
                'account_name': rec.account.name if rec.account else None,
                'contact_id': rec.contact_id or (rec.transaction_source.contact_id if rec.transaction_source else None),
                'contact_name': rec.contact.name if rec.contact else (rec.transaction_source.contact.name if rec.transaction_source and rec.transaction_source.contact else None),
                'tracking1_id': rec.tracking1_id,
                'tracking1': rec.tracking1.option if rec.tracking1 else None,
                'tracking2_id': rec.tracking2_id,
                'tracking2': rec.tracking2.option if rec.tracking2 else None,
                'description': rec.description,
                'reference': rec.reference,
                'amount': str(rec.amount),
                'tax_amount': str(rec.tax_amount),
                'transaction_source_type': rec.transaction_source.transaction_source if rec.transaction_source else None,
            }

        data = [serialize_line(r) for r in qs]
        returned = len(data)
        remaining = max(0, total_count - returned)
        return Response({
            'tenant_id': tenant_id,
            'count': returned,
            'total_count': total_count,
            'remaining': remaining,
            'results': data,
        })


class ImportPnlByTrackingView(APIView):
    """
    Pull Xero P&L report for every tracking category option and store
    per-account/month values for comparison with the trail balance.

    POST body:
        tenant_id (required)
        from_date (optional, YYYY-MM-DD, default 12 months ago)
        to_date   (optional, YYYY-MM-DD, default today)
    """
    permission_classes = [AllowAny]  # TODO: IsAuthenticated for production

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        from_date = request.data.get('from_date')
        to_date = request.data.get('to_date')

        try:
            result = import_pnl_by_tracking(
                tenant_id=tenant_id,
                from_date=from_date,
                to_date=to_date,
                user=request.user if request.user.is_authenticated else None,
            )
            return Response(result)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": f"Failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PnlSummaryByTrackingView(APIView):
    """
    P&L summary grouped by tracking1 + year + month.
    Returns Income total, Expense total, and Net P&L from both the DB trail balance
    and the imported Xero P&L, plus differences.

    Query params:
        tenant_id (required)
        year (optional)
        month (optional)
    """
    permission_classes = [AllowAny]  # TODO: IsAuthenticated for production

    INCOME_TYPES = {'REVENUE', 'OTHERINCOME'}
    EXPENSE_TYPES = {'EXPENSE', 'DIRECTCOSTS', 'OVERHEADS'}

    def get(self, request):
        from django.db.models import Sum, Case, When, DecimalField, Value, Q

        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        pnl_types = self.INCOME_TYPES | self.EXPENSE_TYPES

        # ---------- DB Trail Balance aggregation ----------
        tb_qs = XeroTrailBalance.objects.filter(
            organisation=tenant,
            account__type__in=pnl_types,
        )
        year = request.query_params.get('year')
        month = request.query_params.get('month')
        if year:
            try:
                tb_qs = tb_qs.filter(year=int(year))
            except ValueError:
                pass
        if month:
            try:
                tb_qs = tb_qs.filter(month=int(month))
            except ValueError:
                pass

        db_agg = (
            tb_qs
            .values('tracking1', 'tracking1__option', 'year', 'month')
            .annotate(
                db_income=Sum(
                    Case(
                        When(account__type__in=self.INCOME_TYPES, then='amount'),
                        default=Value(0),
                        output_field=DecimalField(),
                    )
                ),
                db_expense=Sum(
                    Case(
                        When(account__type__in=self.EXPENSE_TYPES, then='amount'),
                        default=Value(0),
                        output_field=DecimalField(),
                    )
                ),
            )
            .order_by('year', 'month', 'tracking1__option')
        )

        # ---------- Xero P&L aggregation ----------
        xero_qs = XeroPnlByTracking.objects.filter(
            organisation=tenant,
            account__type__in=pnl_types,
        )
        if year:
            try:
                xero_qs = xero_qs.filter(year=int(year))
            except ValueError:
                pass
        if month:
            try:
                xero_qs = xero_qs.filter(month=int(month))
            except ValueError:
                pass

        xero_agg = (
            xero_qs
            .values('tracking', 'year', 'month')
            .annotate(
                xero_income=Sum(
                    Case(
                        When(account__type__in=self.INCOME_TYPES, then='xero_amount'),
                        default=Value(0),
                        output_field=DecimalField(),
                    )
                ),
                xero_expense=Sum(
                    Case(
                        When(account__type__in=self.EXPENSE_TYPES, then='xero_amount'),
                        default=Value(0),
                        output_field=DecimalField(),
                    )
                ),
            )
        )

        # Build Xero lookup: (tracking_id, year, month) -> {xero_income, xero_expense}
        xero_lookup = {}
        for x in xero_agg:
            key = (x['tracking'], x['year'], x['month'])
            xero_lookup[key] = {
                'xero_income': float(x['xero_income'] or 0),
                'xero_expense': float(x['xero_expense'] or 0),
            }

        # Build result rows
        results = []
        for row in db_agg:
            db_income = float(row['db_income'] or 0)
            db_expense = float(row['db_expense'] or 0)
            db_pnl = round(db_income + db_expense, 2)

            xero = xero_lookup.get((row['tracking1'], row['year'], row['month']), {})
            xero_income = xero.get('xero_income')
            xero_expense = xero.get('xero_expense')

            xero_pnl = None
            income_diff = None
            expense_diff = None
            pnl_diff = None
            if xero_income is not None:
                xero_pnl = round(xero_income + xero_expense, 2)
                income_diff = round(xero_income - db_income, 2)
                expense_diff = round(xero_expense - db_expense, 2)
                pnl_diff = round(xero_pnl - db_pnl, 2)

            results.append({
                'tracking1': row['tracking1__option'],
                'year': row['year'],
                'month': row['month'],
                'db_income': round(db_income, 2),
                'db_expense': round(db_expense, 2),
                'db_pnl': db_pnl,
                'xero_income': xero_income,
                'xero_expense': xero_expense,
                'xero_pnl': xero_pnl,
                'income_diff': income_diff,
                'expense_diff': expense_diff,
                'pnl_diff': pnl_diff,
            })

        return Response({
            'tenant_id': tenant_id,
            'count': len(results),
            'results': results,
        })


class AccountBalanceSummaryView(APIView):
    """
    Per-account summary comparing DB trail balance vs Xero P&L.
    Grouped into Income Statement and Balance Sheet sections.

    FIX NOTES:
    - DB trail balance is filtered to ONLY the same year/month combos present
      in the Xero P&L data (avoids comparing 8 years of DB vs 12 months of Xero).
    - Xero P&L is filtered to ONLY the 'Profit Center' tracking category
      (which matches tracking1 in the trail balance) to avoid cross-category
      double counting across Profit Center / Room / Custom Tracking.

    Query params:
        tenant_id (required)
        year (optional) - filter to specific year
        month (optional) - filter to specific month (1-12)
    """
    permission_classes = [AllowAny]

    INCOME_TYPES = {'REVENUE', 'OTHERINCOME'}
    EXPENSE_TYPES = {'EXPENSE', 'DIRECTCOSTS', 'OVERHEADS'}
    IS_TYPES = INCOME_TYPES | EXPENSE_TYPES
    BS_TYPES = {
        'BANK', 'CURRENT', 'FIXED', 'INVENTORY', 'NONCURRENT',
        'CURRLIAB', 'TERMLIAB', 'LIABILITY', 'EQUITY',
    }

    def get(self, request):
        from django.db.models import Sum, Q
        from apps.xero.xero_metadata.models import XeroTracking

        tenant_id = request.query_params.get('tenant_id')
        filter_year = request.query_params.get('year')
        filter_month = request.query_params.get('month')
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        except XeroTenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        # ------------------------------------------------------------------
        # 1. Determine common year/month range from Xero P&L data
        # ------------------------------------------------------------------
        pnl_months = set(
            XeroPnlByTracking.objects.filter(organisation=tenant)
            .values_list('year', 'month')
            .distinct()
        )
        has_xero = len(pnl_months) > 0

        # Apply optional year/month filter (intersect with available PnL months)
        if filter_year or filter_month:
            filtered = set()
            for y, m in pnl_months:
                if filter_year and int(filter_year) != y:
                    continue
                if filter_month and int(filter_month) != m:
                    continue
                filtered.add((y, m))
            pnl_months = filtered
            has_xero = len(pnl_months) > 0

        # Build Q filter for the common months
        month_q = Q()
        for y, m in pnl_months:
            month_q |= Q(year=y, month=m)

        # ------------------------------------------------------------------
        # 2. Xero P&L: use UNFILTERED records (tracking=NULL) for comparison
        #    These represent the overall P&L without any tracking filter,
        #    which is what should match our DB trail balance totals.
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # 3. DB trail balance per account (filtered to matching months)
        # ------------------------------------------------------------------
        tb_base = XeroTrailBalance.objects.filter(organisation=tenant)
        if has_xero:
            tb_filtered = tb_base.filter(month_q)
        else:
            tb_filtered = tb_base

        db_agg = (
            tb_filtered
            .values('account__account_id', 'account__code', 'account__name', 'account__type')
            .annotate(db_total=Sum('amount'))
            .order_by('account__code')
        )

        # Also get all-time totals for BS accounts
        db_alltime = {}
        for row in (
            tb_base
            .values('account__account_id')
            .annotate(db_total=Sum('amount'))
        ):
            db_alltime[row['account__account_id']] = float(row['db_total'] or 0)

        # ------------------------------------------------------------------
        # 4. Xero P&L per account (UNFILTERED / tracking=NULL, same months)
        # ------------------------------------------------------------------
        xero_agg = {}
        if has_xero:
            pnl_qs = XeroPnlByTracking.objects.filter(
                organisation=tenant,
                tracking__isnull=True,  # Use the overall/unfiltered P&L
            )
            # Apply same month filter as DB
            pnl_qs = pnl_qs.filter(month_q)
            for row in (
                pnl_qs
                .values('account__account_id')
                .annotate(xero_total=Sum('xero_amount'))
            ):
                xero_agg[row['account__account_id']] = float(row['xero_total'] or 0)

            # If no unfiltered data exists yet, fall back to Profit Center tracking
            if not xero_agg:
                pc_ids = set(
                    XeroTracking.objects.filter(organisation=tenant, name='Profit Center')
                    .values_list('id', flat=True)
                )
                pnl_qs = XeroPnlByTracking.objects.filter(
                    organisation=tenant,
                    tracking_id__in=pc_ids,
                ).filter(month_q)
                for row in (
                    pnl_qs
                    .values('account__account_id')
                    .annotate(xero_total=Sum('xero_amount'))
                ):
                    xero_agg[row['account__account_id']] = float(row['xero_total'] or 0)

        # ------------------------------------------------------------------
        # 5. Build account rows
        # ------------------------------------------------------------------
        income_statement = []
        balance_sheet = []
        is_in_balance = 0
        is_out_of_balance = 0
        bs_count = 0
        tolerance = 0.01

        for row in db_agg:
            acct_type = row['account__type'] or ''
            acct_id = row['account__account_id']
            db_total = float(row['db_total'] or 0)
            xero_total = xero_agg.get(acct_id)

            entry = {
                'account_code': row['account__code'] or '',
                'account_name': row['account__name'] or '',
                'account_type': acct_type,
                'db_total': round(db_total, 2),
                'xero_total': round(xero_total, 2) if xero_total is not None else None,
                'diff': None,
                'in_balance': None,
            }

            if acct_type in self.IS_TYPES:
                if xero_total is not None:
                    # Sign convention:
                    # Revenue in DB = negative (credit), in Xero P&L = positive
                    # Expense in DB = positive (debit), in Xero P&L = positive
                    if acct_type in self.INCOME_TYPES:
                        diff = round(db_total + xero_total, 2)  # should be ~0
                    else:
                        diff = round(db_total - xero_total, 2)  # should be ~0
                    entry['diff'] = diff
                    entry['in_balance'] = abs(diff) < tolerance
                    if entry['in_balance']:
                        is_in_balance += 1
                    else:
                        is_out_of_balance += 1
                income_statement.append(entry)
            else:
                # For BS accounts use all-time total
                entry['db_total'] = round(db_alltime.get(acct_id, 0), 2)
                bs_count += 1
                balance_sheet.append(entry)

        # Sort: out-of-balance first, then by account code
        income_statement.sort(key=lambda x: (x['in_balance'] is True, x['account_code']))
        balance_sheet.sort(key=lambda x: x['account_code'])

        # Format date range info for frontend
        if pnl_months:
            sorted_months = sorted(pnl_months)
            date_range = f"{sorted_months[0][0]}-{sorted_months[0][1]:02d} to {sorted_months[-1][0]}-{sorted_months[-1][1]:02d}"
        else:
            date_range = "N/A"

        # Diagnostic info
        tb_all_months = set(
            tb_base.values_list('year', 'month').distinct()
        )
        tracking_categories = sorted(set(
            XeroTracking.objects.filter(organisation=tenant)
            .values_list('name', flat=True)
        ))

        return Response({
            'tenant_id': tenant_id,
            'diagnostics': {
                'xero_pnl_months': len(pnl_months),
                'db_months_total': len(tb_all_months),
                'db_months_compared': len(pnl_months),
                'date_range': date_range,
                'tracking_categories': tracking_categories,
                'xero_pnl_filtered_to': 'Overall (unfiltered)' if XeroPnlByTracking.objects.filter(
                    organisation=tenant, tracking__isnull=True).exists() else 'Profit Center (fallback)',
                'journal_types': {
                    'transaction': XeroTrailBalance.objects.filter(organisation=tenant).count(),
                },
            },
            'income_statement': {
                'accounts': income_statement,
                'total': len(income_statement),
                'in_balance': is_in_balance,
                'out_of_balance': is_out_of_balance,
                'no_xero_data': len(income_statement) - is_in_balance - is_out_of_balance,
            },
            'balance_sheet': {
                'accounts': balance_sheet,
                'total': bs_count,
            },
        })
