"""
Journal / trail balance comparison tests.

Compares totals by journal type (transaction vs manual_journal) for
reconciliation (e.g. against v3).

Usage:
    python manage.py test apps.xero.xero_data.tests.test_journal_migration

For reconciliation against another environment:
    from apps.xero.xero_data.tests.test_journal_migration import compare_pipelines
    compare_pipelines(tenant_id='your-tenant-id')
"""
import logging
from decimal import Decimal
from django.test import TestCase
from django.db.models import Sum, F
from django.utils import timezone
import datetime

logger = logging.getLogger(__name__)


class JournalMigrationComparisonTest(TestCase):
    """
    Compare totals by journal type (transaction vs manual_journal) for reconciliation.
    """
    
    @classmethod
    def setUpClass(cls):
        """Set up test class."""
        super().setUpClass()
        # Import here to avoid circular imports
        from apps.xero.tests.factories import (
            UserFactory, XeroTenantFactory, XeroClientCredentialsFactory
        )
        
        cls.user = UserFactory()
        cls.tenant = XeroTenantFactory()
        cls.credentials = XeroClientCredentialsFactory(user=cls.user)
    
    def setUp(self):
        """Set up test data."""
        from apps.xero.xero_metadata.models import XeroAccount, XeroContacts, XeroTracking
        
        # Create test accounts
        self.revenue_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id='acc-revenue-001',
            code='4000',
            name='Sales Revenue',
            type='REVENUE',
            grouping='REVENUE'
        )
        
        self.expense_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id='acc-expense-001',
            code='5000',
            name='Cost of Sales',
            type='EXPENSE',
            grouping='EXPENSE'
        )
        
        self.ar_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id='acc-ar-001',
            code='1100',
            name='Accounts Receivable',
            type='CURRENT',
            grouping='ASSET'
        )
        
        self.bank_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id='acc-bank-001',
            code='1000',
            name='Bank Account',
            type='BANK',
            grouping='ASSET'
        )
        
        # Create test contact
        self.contact = XeroContacts.objects.create(
            organisation=self.tenant,
            contacts_id='contact-001',
            name='Test Customer'
        )
        
        # Create test tracking
        self.tracking = XeroTracking.objects.create(
            organisation=self.tenant,
            option_id='tracking-001',
            name='Department',
            option='Sales'
        )
    
    def test_empty_database_comparison(self):
        """Both pipelines should return empty results for empty database."""
        from apps.xero.xero_data.models import XeroJournals
        from apps.xero.xero_cube.models import XeroTrailBalance
        
        # Get old pipeline totals (from journals)
        old_totals = XeroJournals.objects.filter(
            organisation=self.tenant
        ).values('account').annotate(total=Sum('amount'))
        
        # Get new pipeline totals (from transactions)
        new_totals = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='transaction'
        ).values('account').annotate(total=Sum('amount'))
        
        self.assertEqual(len(old_totals), 0)
        self.assertEqual(len(new_totals), 0)
    
    def test_journal_entry_creation(self):
        """Test creating journal entries from both sources."""
        from apps.xero.xero_data.models import (
            XeroJournals, XeroJournalsSource, XeroTransactionSource
        )
        
        # Create a journal from the old pipeline
        journal_source = XeroJournalsSource.objects.create(
            organisation=self.tenant,
            journal_id='journal-001',
            journal_number=1,
            journal_type='journal',
            collection={
                'JournalID': 'journal-001',
                'JournalNumber': 1,
                'JournalDate': '2024-01-15T00:00:00',
                'JournalLines': [
                    {
                        'JournalLineID': 'line-001',
                        'AccountID': self.revenue_account.account_id,
                        'NetAmount': -1000.00,
                        'TaxAmount': 0
                    },
                    {
                        'JournalLineID': 'line-002',
                        'AccountID': self.ar_account.account_id,
                        'NetAmount': 1000.00,
                        'TaxAmount': 0
                    }
                ]
            },
            processed=False
        )
        
        # Process the journal
        XeroJournalsSource.objects.create_journals_from_xero(self.tenant)
        
        # Verify journals were created
        journals = XeroJournals.objects.filter(organisation=self.tenant)
        self.assertEqual(journals.count(), 2)
        
        # Verify debit/credit balance
        total = journals.aggregate(total=Sum('amount'))['total']
        self.assertEqual(total, Decimal('0'))
    
    def test_invoice_to_journal_conversion(self):
        """Test converting an invoice to journal entries using transaction processor."""
        from apps.xero.xero_data.models import XeroTransactionSource
        from apps.xero.xero_data.transaction_processor import TransactionProcessor
        
        # Create a transaction source (invoice)
        invoice = XeroTransactionSource.objects.create(
            organisation=self.tenant,
            transactions_id='invoice-001',
            transaction_source='Invoice',
            contact=self.contact,
            collection={
                'InvoiceID': 'invoice-001',
                'Type': 'ACCREC',
                'Date': '2024-01-15T00:00:00',
                'InvoiceNumber': 'INV-001',
                'Contact': {'Name': 'Test Customer'},
                'LineItems': [
                    {
                        'AccountCode': '4000',
                        'LineAmount': 1000.00,
                        'TaxAmount': 0,
                        'Description': 'Test sale',
                        'Tracking': []
                    }
                ]
            }
        )
        
        # Process using transaction processor
        processor = TransactionProcessor(self.tenant)
        entries = processor.process_invoice(invoice)
        
        # Should create 2 entries: revenue (credit) and AR (debit)
        self.assertEqual(len(entries), 2)
        
        # Verify amounts balance to zero
        total = sum(e['amount'] for e in entries)
        self.assertEqual(total, Decimal('0'))
    
    def test_trail_balance_totals_match(self):
        """Account totals should match between old and new pipelines."""
        from apps.xero.xero_data.models import XeroJournals
        
        # Create matching entries from both pipelines
        # Old pipeline entry
        XeroJournals.objects.create(
            organisation=self.tenant,
            journal_id='old-journal-001',
            journal_number=1,
            journal_type='journal',
            account=self.revenue_account,
            date=timezone.now(),
            amount=Decimal('-1000.00'),
            tax_amount=Decimal('0'),
            description='Old pipeline entry'
        )
        
        # New pipeline entry (same amount)
        XeroJournals.objects.create(
            organisation=self.tenant,
            journal_id='new-txn-001',
            journal_number=2,
            journal_type='transaction',
            account=self.revenue_account,
            date=timezone.now(),
            amount=Decimal('-1000.00'),
            tax_amount=Decimal('0'),
            description='New pipeline entry'
        )
        
        # Get totals by pipeline type
        old_total = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='journal'
        ).aggregate(total=Sum('amount'))['total']
        
        new_total = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='transaction'
        ).aggregate(total=Sum('amount'))['total']
        
        self.assertEqual(old_total, new_total)
    
    def test_monthly_totals_match(self):
        """Monthly account totals should match."""
        from apps.xero.xero_data.models import XeroJournals
        
        jan_date = datetime.datetime(2024, 1, 15, tzinfo=timezone.utc)
        feb_date = datetime.datetime(2024, 2, 15, tzinfo=timezone.utc)
        
        # Create entries for both months, both pipelines
        for month_date in [jan_date, feb_date]:
            XeroJournals.objects.create(
                organisation=self.tenant,
                journal_id=f'old-{month_date.month}',
                journal_number=month_date.month,
                journal_type='journal',
                account=self.revenue_account,
                date=month_date,
                amount=Decimal('-500.00'),
                tax_amount=Decimal('0')
            )
            XeroJournals.objects.create(
                organisation=self.tenant,
                journal_id=f'new-{month_date.month}',
                journal_number=month_date.month + 100,
                journal_type='transaction',
                account=self.revenue_account,
                date=month_date,
                amount=Decimal('-500.00'),
                tax_amount=Decimal('0')
            )
        
        # Compare monthly totals
        old_monthly = get_monthly_totals(self.tenant, 'journal')
        new_monthly = get_monthly_totals(self.tenant, 'transaction')
        
        for key in old_monthly:
            self.assertIn(key, new_monthly)
            self.assertEqual(old_monthly[key], new_monthly[key])
    
    def test_tracking_breakdown_matches(self):
        """Tracking category breakdown should match."""
        from apps.xero.xero_data.models import XeroJournals
        
        # Create entries with tracking
        XeroJournals.objects.create(
            organisation=self.tenant,
            journal_id='old-tracking-001',
            journal_number=1,
            journal_type='journal',
            account=self.revenue_account,
            date=timezone.now(),
            amount=Decimal('-1000.00'),
            tax_amount=Decimal('0'),
            tracking1=self.tracking
        )
        
        XeroJournals.objects.create(
            organisation=self.tenant,
            journal_id='new-tracking-001',
            journal_number=2,
            journal_type='transaction',
            account=self.revenue_account,
            date=timezone.now(),
            amount=Decimal('-1000.00'),
            tax_amount=Decimal('0'),
            tracking1=self.tracking
        )
        
        # Get totals by tracking
        old_tracking = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='journal',
            tracking1=self.tracking
        ).aggregate(total=Sum('amount'))['total']
        
        new_tracking = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='transaction',
            tracking1=self.tracking
        ).aggregate(total=Sum('amount'))['total']
        
        self.assertEqual(old_tracking, new_tracking)


def get_monthly_totals(organisation, journal_type):
    """
    Get monthly totals by account for a given journal type.
    
    Returns:
        dict: {(account_id, year, month): total}
    """
    from apps.xero.xero_data.models import XeroJournals, Month, Year
    
    totals = XeroJournals.objects.filter(
        organisation=organisation,
        journal_type=journal_type
    ).annotate(
        month=Month('date'),
        year=Year('date')
    ).values('account', 'year', 'month').annotate(
        total=Sum('amount')
    )
    
    return {
        (t['account'], t['year'], t['month']): t['total']
        for t in totals
    }


def compare_trail_balances(organisation):
    """
    Run both pipelines and compare results.
    
    Args:
        organisation: XeroTenant instance
    
    Returns:
        dict: Comparison results with any discrepancies
    """
    from apps.xero.xero_cube.models import XeroTrailBalance
    from apps.xero.xero_data.models import XeroJournals
    
    # Get totals from journals-based pipeline (all non-transaction journals)
    old_tb = XeroJournals.objects.filter(
        organisation=organisation
    ).exclude(
        journal_type='transaction'
    ).values('account__code', 'account__name').annotate(
        total=Sum('amount')
    ).order_by('account__code')
    
    # Get totals from transaction-based pipeline
    new_tb = XeroJournals.objects.filter(
        organisation=organisation,
        journal_type='transaction'
    ).values('account__code', 'account__name').annotate(
        total=Sum('amount')
    ).order_by('account__code')
    
    # Convert to dicts
    old_dict = {r['account__code']: r['total'] for r in old_tb}
    new_dict = {r['account__code']: r['total'] for r in new_tb}
    
    # Compare
    all_accounts = set(old_dict.keys()) | set(new_dict.keys())
    
    discrepancies = []
    matching = []
    
    for account_code in sorted(all_accounts):
        old_total = old_dict.get(account_code, Decimal('0'))
        new_total = new_dict.get(account_code, Decimal('0'))
        
        if old_total != new_total:
            discrepancies.append({
                'account_code': account_code,
                'old_total': old_total,
                'new_total': new_total,
                'difference': old_total - new_total
            })
        else:
            matching.append({
                'account_code': account_code,
                'total': old_total
            })
    
    return {
        'organisation': organisation.tenant_name,
        'matching_accounts': len(matching),
        'discrepancy_count': len(discrepancies),
        'discrepancies': discrepancies,
        'old_total_count': len(old_dict),
        'new_total_count': len(new_dict),
    }


def compare_monthly_totals(organisation):
    """
    Compare monthly totals between pipelines.
    
    Args:
        organisation: XeroTenant instance
    
    Returns:
        dict: Monthly comparison with discrepancies
    """
    from apps.xero.xero_data.models import XeroJournals, Month, Year
    
    # Old pipeline monthly totals
    old_monthly = XeroJournals.objects.filter(
        organisation=organisation
    ).exclude(
        journal_type='transaction'
    ).annotate(
        month=Month('date'),
        year=Year('date')
    ).values('account__code', 'year', 'month').annotate(
        total=Sum('amount')
    )
    
    # New pipeline monthly totals
    new_monthly = XeroJournals.objects.filter(
        organisation=organisation,
        journal_type='transaction'
    ).annotate(
        month=Month('date'),
        year=Year('date')
    ).values('account__code', 'year', 'month').annotate(
        total=Sum('amount')
    )
    
    # Convert to dicts
    old_dict = {
        (r['account__code'], r['year'], r['month']): r['total']
        for r in old_monthly
    }
    new_dict = {
        (r['account__code'], r['year'], r['month']): r['total']
        for r in new_monthly
    }
    
    # Compare
    all_keys = set(old_dict.keys()) | set(new_dict.keys())
    
    discrepancies = []
    
    for key in sorted(all_keys):
        old_total = old_dict.get(key, Decimal('0'))
        new_total = new_dict.get(key, Decimal('0'))
        
        if old_total != new_total:
            discrepancies.append({
                'account_code': key[0],
                'year': key[1],
                'month': key[2],
                'old_total': old_total,
                'new_total': new_total,
                'difference': old_total - new_total
            })
    
    return {
        'organisation': organisation.tenant_name,
        'discrepancy_count': len(discrepancies),
        'discrepancies': discrepancies,
    }


def compare_pipelines(tenant_id):
    """
    Compare both pipelines for a specific tenant.
    
    This function:
    1. Fetches all transactions
    2. Processes them through the transaction processor
    3. Compares totals with existing journal-based data
    
    Args:
        tenant_id: Xero tenant ID
    
    Returns:
        dict: Full comparison results
    """
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_data.services import update_xero_transactions
    from apps.xero.xero_data.transaction_processor import process_transactions_to_journals
    
    print(f"[COMPARE] Starting pipeline comparison for tenant {tenant_id}")
    
    try:
        organisation = XeroTenant.objects.get(tenant_id=tenant_id)
    except XeroTenant.DoesNotExist:
        return {'error': f'Tenant {tenant_id} not found'}
    
    print(f"[COMPARE] Tenant: {organisation.tenant_name}")
    
    # Step 1: Fetch transactions (if needed)
    print("[COMPARE] Step 1: Fetching transactions...")
    try:
        result = update_xero_transactions(tenant_id)
        print(f"[COMPARE] Transactions fetched: {result.get('stats', {})}")
    except Exception as e:
        print(f"[COMPARE] Warning: Could not fetch transactions: {e}")
    
    # Step 2: Process transactions to journals
    print("[COMPARE] Step 2: Processing transactions to journal entries...")
    try:
        stats = process_transactions_to_journals(organisation)
        print(f"[COMPARE] Processing stats: {stats}")
    except Exception as e:
        print(f"[COMPARE] Error processing transactions: {e}")
        return {'error': str(e)}
    
    # Step 3: Compare totals
    print("[COMPARE] Step 3: Comparing totals...")
    comparison = compare_trail_balances(organisation)
    
    print(f"[COMPARE] Results:")
    print(f"  - Matching accounts: {comparison['matching_accounts']}")
    print(f"  - Discrepancies: {comparison['discrepancy_count']}")
    
    if comparison['discrepancies']:
        print("[COMPARE] Discrepancy details:")
        for d in comparison['discrepancies'][:10]:  # Show first 10
            print(f"    Account {d['account_code']}: "
                  f"Old={d['old_total']}, New={d['new_total']}, "
                  f"Diff={d['difference']}")
    
    # Monthly comparison
    monthly_comparison = compare_monthly_totals(organisation)
    comparison['monthly_discrepancies'] = monthly_comparison['discrepancies']
    
    return comparison


def run_full_comparison():
    """
    Run comparison for all tenants.
    
    Returns:
        list: Results for each tenant
    """
    from apps.xero.xero_core.models import XeroTenant
    
    results = []
    tenants = XeroTenant.objects.all()
    
    print(f"[COMPARE] Running comparison for {tenants.count()} tenants")
    
    for tenant in tenants:
        try:
            result = compare_pipelines(tenant.tenant_id)
            results.append({
                'tenant': tenant.tenant_name,
                'tenant_id': tenant.tenant_id,
                **result
            })
        except Exception as e:
            results.append({
                'tenant': tenant.tenant_name,
                'tenant_id': tenant.tenant_id,
                'error': str(e)
            })
    
    return results
