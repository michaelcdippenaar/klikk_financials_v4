"""
Discrepancy analyzer for journal totals (e.g. reconciliation).

Compares totals by journal type (transaction vs manual_journal) and suggests
fixes. Useful when reconciling against another source (e.g. v3).

Usage:
    from apps.xero.xero_data.discrepancy_analyzer import DiscrepancyAnalyzer
    analyzer = DiscrepancyAnalyzer(tenant_id='your-tenant-id')
    analysis = analyzer.analyze()
"""
import logging
from decimal import Decimal, ROUND_HALF_UP
from django.db.models import Sum, Count
from collections import defaultdict

logger = logging.getLogger(__name__)


class DiscrepancyAnalyzer:
    """
    Analyzes discrepancies between journal pipelines and suggests fixes.
    """
    
    # Tolerance for rounding differences (2 decimal places)
    TOLERANCE = Decimal('0.01')
    
    def __init__(self, tenant_id):
        from apps.xero.xero_core.models import XeroTenant
        
        self.tenant = XeroTenant.objects.get(tenant_id=tenant_id)
        self._analysis_cache = None
    
    def analyze(self):
        """
        Perform comprehensive analysis of discrepancies.
        
        Returns:
            dict: Analysis results with categorized discrepancies
        """
        from apps.xero.xero_data.comparison_utils import compare_totals
        
        comparison = compare_totals(self.tenant.tenant_id)
        
        if 'error' in comparison:
            return comparison
        
        analysis = {
            'summary': comparison['summary'],
            'categorized_discrepancies': self._categorize_discrepancies(
                comparison.get('discrepancies', [])
            ),
            'old_only_analysis': self._analyze_old_only(
                comparison.get('old_only', [])
            ),
            'new_only_analysis': self._analyze_new_only(
                comparison.get('new_only', [])
            ),
        }
        
        self._analysis_cache = analysis
        return analysis
    
    def _categorize_discrepancies(self, discrepancies):
        """
        Categorize discrepancies by likely cause.
        """
        categories = {
            'rounding': [],
            'tax_related': [],
            'ar_ap': [],
            'bank': [],
            'large_difference': [],
            'unknown': []
        }
        
        for d in discrepancies:
            account_code = d.get('account_code', '')
            diff = abs(d.get('difference', Decimal('0')))
            account_type = d.get('type', '')
            
            # Check for rounding differences (< $0.10)
            if diff < Decimal('0.10'):
                categories['rounding'].append(d)
            # Check for tax-related accounts
            elif 'TAX' in account_code.upper() or 'GST' in account_code.upper():
                categories['tax_related'].append(d)
            # Check for AR/AP accounts
            elif account_code.startswith('1') and len(account_code) == 4:  # Typically 1100, 1200
                categories['ar_ap'].append(d)
            # Check for bank accounts
            elif account_code.startswith('1') and account_code.endswith('00'):
                categories['bank'].append(d)
            # Large differences
            elif diff > Decimal('1000'):
                categories['large_difference'].append(d)
            else:
                categories['unknown'].append(d)
        
        return categories
    
    def _analyze_old_only(self, old_only):
        """
        Analyze accounts that only exist in old pipeline.
        These are likely from journals that don't have corresponding transactions.
        """
        analysis = {
            'total_accounts': len(old_only),
            'total_amount': sum(d['total'] for d in old_only),
            'likely_causes': []
        }
        
        # Check for manual journals
        manual_journal_accounts = self._get_manual_journal_accounts()
        for item in old_only:
            if item['account_code'] in manual_journal_accounts:
                analysis['likely_causes'].append({
                    'account': item['account_code'],
                    'cause': 'manual_journal',
                    'suggestion': 'Manual journals are handled separately, ensure they are processed'
                })
        
        return analysis
    
    def _analyze_new_only(self, new_only):
        """
        Analyze accounts that only exist in new pipeline.
        These may be from transactions not captured in journals.
        """
        analysis = {
            'total_accounts': len(new_only),
            'total_amount': sum(d['total'] for d in new_only),
            'likely_causes': []
        }
        
        for item in new_only:
            analysis['likely_causes'].append({
                'account': item['account_code'],
                'cause': 'new_transaction',
                'suggestion': 'Check if this transaction type was not synced via journals'
            })
        
        return analysis
    
    def _get_manual_journal_accounts(self):
        """Get set of account codes used by manual journals."""
        from apps.xero.xero_data.models import XeroJournals
        
        return set(
            XeroJournals.objects.filter(
                organisation=self.tenant,
                journal_type='manual_journal'
            ).values_list('account__code', flat=True)
        )
    
    def suggest_fixes(self):
        """
        Suggest fixes based on analysis.
        
        Returns:
            list: List of suggested fixes with implementation details
        """
        if not self._analysis_cache:
            self.analyze()
        
        suggestions = []
        categories = self._analysis_cache.get('categorized_discrepancies', {})
        
        # Rounding fixes
        if categories.get('rounding'):
            suggestions.append({
                'category': 'rounding',
                'count': len(categories['rounding']),
                'description': 'Minor rounding differences detected',
                'fix': 'Apply ROUND_HALF_UP with 2 decimal places',
                'code_location': 'transaction_processor.py: _create_journal_entry()',
                'implementation': '''
# Add rounding to amount
amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
'''
            })
        
        # Tax-related fixes
        if categories.get('tax_related'):
            suggestions.append({
                'category': 'tax',
                'count': len(categories['tax_related']),
                'description': 'Tax account differences detected',
                'fix': 'Add separate tax line processing',
                'code_location': 'transaction_processor.py: process_invoice()',
                'implementation': '''
# Add tax line if TaxAmount is non-zero
if tax_amount and tax_amount != Decimal('0'):
    tax_account = self._get_tax_account(invoice_type)
    if tax_account:
        entries.append(self._create_journal_entry(
            transaction_source=transaction_source,
            account=tax_account,
            amount=tax_amount if invoice_type == 'ACCPAY' else -tax_amount,
            date=date,
            description=f"Tax - {reference}",
            reference=reference
        ))
'''
            })
        
        # AR/AP fixes
        if categories.get('ar_ap'):
            suggestions.append({
                'category': 'ar_ap',
                'count': len(categories['ar_ap']),
                'description': 'Accounts Receivable/Payable differences',
                'fix': 'Ensure correct system accounts are mapped',
                'code_location': 'transaction_processor.py: _get_system_account()',
                'implementation': '''
# Get system account from organisation settings
def _get_system_account(self, account_type):
    # First, try to get from organisation's account settings
    from apps.xero.xero_metadata.models import XeroAccount
    
    if account_type == 'ACCOUNTS_RECEIVABLE':
        # Look for the AR account by reporting code
        ar = XeroAccount.objects.filter(
            organisation=self.organisation,
            reporting_code='ASS.CUR.REC.TRA'
        ).first()
        if ar:
            return ar
    elif account_type == 'ACCOUNTS_PAYABLE':
        # Look for the AP account by reporting code
        ap = XeroAccount.objects.filter(
            organisation=self.organisation,
            reporting_code='LIA.CUR.PAY.TRA'
        ).first()
        if ap:
            return ap
    
    # Fall back to type-based lookup
    return self._accounts_by_type.get(self.ACCOUNT_TYPES.get(account_type), [None])[0]
'''
            })
        
        # Large difference investigation
        if categories.get('large_difference'):
            suggestions.append({
                'category': 'large_difference',
                'count': len(categories['large_difference']),
                'description': 'Large differences require manual investigation',
                'fix': 'Review specific transactions and journals',
                'code_location': 'Manual review required',
                'accounts': [d['account_code'] for d in categories['large_difference']]
            })
        
        return suggestions
    
    def apply_rounding_fix(self):
        """
        Apply rounding fixes to existing transaction-based journals.
        """
        from apps.xero.xero_data.models import XeroJournals
        
        journals = XeroJournals.objects.filter(
            organisation=self.tenant,
            journal_type='transaction'
        )
        
        updates = []
        for journal in journals:
            rounded = journal.amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if rounded != journal.amount:
                journal.amount = rounded
                updates.append(journal)
        
        if updates:
            XeroJournals.objects.bulk_update(updates, ['amount'])
            logger.info(f"Applied rounding fix to {len(updates)} journal entries")
        
        return len(updates)
    
    def investigate_account(self, account_code):
        """
        Deep investigation of a specific account's discrepancy.
        
        Args:
            account_code: Account code to investigate
        
        Returns:
            dict: Detailed investigation results
        """
        from apps.xero.xero_data.models import XeroJournals, XeroTransactionSource
        from apps.xero.xero_metadata.models import XeroAccount
        
        try:
            account = XeroAccount.objects.get(
                organisation=self.tenant,
                code=account_code
            )
        except XeroAccount.DoesNotExist:
            return {'error': f'Account {account_code} not found'}
        
        # Get journals from old pipeline
        old_journals = XeroJournals.objects.filter(
            organisation=self.tenant,
            account=account,
            journal_type__in=['journal', 'manual_journal']
        ).order_by('date')
        
        # Get journals from new pipeline
        new_journals = XeroJournals.objects.filter(
            organisation=self.tenant,
            account=account,
            journal_type='transaction'
        ).order_by('date')
        
        # Get transaction sources
        transactions = XeroTransactionSource.objects.filter(
            organisation=self.tenant,
            collection__LineItems__contains=[{'AccountCode': account_code}]
        )
        
        return {
            'account': {
                'code': account.code,
                'name': account.name,
                'type': account.type
            },
            'old_pipeline': {
                'count': old_journals.count(),
                'total': old_journals.aggregate(total=Sum('amount'))['total'] or Decimal('0'),
                'sample': list(old_journals[:5].values('journal_id', 'date', 'amount', 'description'))
            },
            'new_pipeline': {
                'count': new_journals.count(),
                'total': new_journals.aggregate(total=Sum('amount'))['total'] or Decimal('0'),
                'sample': list(new_journals[:5].values('journal_id', 'date', 'amount', 'description'))
            },
            'transaction_sources': {
                'count': transactions.count(),
                'types': list(transactions.values_list('transaction_source', flat=True).distinct())
            }
        }


def analyze_and_fix(tenant_id, auto_fix=False):
    """
    Convenience function to analyze discrepancies and optionally apply fixes.
    
    Args:
        tenant_id: Xero tenant ID
        auto_fix: If True, apply automated fixes
    
    Returns:
        dict: Analysis results and fix summary
    """
    analyzer = DiscrepancyAnalyzer(tenant_id)
    analysis = analyzer.analyze()
    suggestions = analyzer.suggest_fixes()
    
    result = {
        'analysis': analysis,
        'suggestions': suggestions,
        'fixes_applied': []
    }
    
    if auto_fix:
        # Apply rounding fix
        rounding_fixed = analyzer.apply_rounding_fix()
        if rounding_fixed:
            result['fixes_applied'].append({
                'type': 'rounding',
                'count': rounding_fixed
            })
    
    return result
