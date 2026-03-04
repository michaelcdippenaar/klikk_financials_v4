"""
Transaction Processor - Converts Xero transactions to journal entries.

Trail balance is built from these transaction types (all are used):
- Invoices: sales (ACCREC) and bills / purchase invoices (ACCPAY)
- Bank Transactions (SPEND, RECEIVE)
- Payments (linked to invoices/bills)
- Credit Notes (ACCRECCREDIT, ACCPAYCREDIT)
- Prepayments
- Overpayments
- Manual Journals (loaded separately via Manual Journals API)

Contact: taken from each transaction's Contact (invoices, bank, credit notes,
prepayments, overpayments, payments). Stored on XeroTransactionSource and
flowed into journal aggregation so trail balance is per contact.

Tracking: taken from line-level Tracking on invoices, bank transactions, and
credit notes. Trail balance is aggregated per tracking1 and tracking2.
"""
import datetime
import logging
import re

from django.conf import settings
from decimal import Decimal, ROUND_HALF_UP
from datetime import timezone as dt_timezone
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def round_amount(amount):
    """
    Round amount to 2 decimal places using ROUND_HALF_UP.
    This ensures consistency with Xero's rounding.
    """
    if amount is None:
        return Decimal('0')
    return Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class TransactionProcessor:
    """
    Converts Xero transactions to journal entries.
    
    Usage:
        processor = TransactionProcessor(organisation)
        processor.process_all_transactions()
    """
    
    # System account types for different transactions
    # These are typically set in Xero's system settings
    ACCOUNT_TYPES = {
        'ACCOUNTS_RECEIVABLE': 'CURRENT',  # Type for AR account
        'ACCOUNTS_PAYABLE': 'CURRLIAB',    # Type for AP account
        'BANK': 'BANK',
    }
    
    def __init__(self, organisation):
        self.organisation = organisation
        self._accounts_by_code = None
        self._accounts_by_id = None
        self._accounts_by_type = None
        self._contacts_dict = None
        self._trackings_dict = None
        
    def _load_lookups(self):
        """Load all lookup dictionaries for efficient processing."""
        from apps.xero.xero_metadata.models import XeroAccount, XeroContacts, XeroTracking
        
        # Load accounts indexed by code, id, and type
        accounts = XeroAccount.objects.filter(organisation=self.organisation)
        self._accounts_by_code = {acc.code: acc for acc in accounts if acc.code}
        self._accounts_by_id = {acc.account_id: acc for acc in accounts}
        self._accounts_by_type = {}
        for acc in accounts:
            if acc.type not in self._accounts_by_type:
                self._accounts_by_type[acc.type] = []
            self._accounts_by_type[acc.type].append(acc)
        
        # Load contacts
        self._contacts_dict = {
            c.contacts_id: c for c in XeroContacts.objects.filter(organisation=self.organisation)
        }
        
        # Load tracking options
        self._trackings_dict = {
            t.option_id: t for t in XeroTracking.objects.filter(organisation=self.organisation)
        }
        
        logger.info(f"Loaded {len(self._accounts_by_code)} accounts, "
                   f"{len(self._contacts_dict)} contacts, "
                   f"{len(self._trackings_dict)} tracking options")
    
    def _get_system_account(self, account_type):
        """
        Get the system account for a given type (AR, AP, etc.).
        
        This method uses a hierarchical lookup:
        1. First, try to find by Xero's reporting code (most accurate)
        2. Fall back to account type matching
        
        Args:
            account_type: One of 'ACCOUNTS_RECEIVABLE', 'ACCOUNTS_PAYABLE', 'BANK', 'TAX_COLLECTED', 'TAX_PAID'
        
        Returns:
            XeroAccount or None
        """
        from apps.xero.xero_metadata.models import XeroAccount
        
        # Reporting code mappings for system accounts
        REPORTING_CODES = {
            'ACCOUNTS_RECEIVABLE': ['ASS.CUR.REC.TRA', 'ASS.CUR.REC'],
            'ACCOUNTS_PAYABLE': ['LIA.CUR.PAY.TRA', 'LIA.CUR.PAY'],
            'TAX_COLLECTED': ['LIA.CUR.TAX.GST', 'LIA.CUR.TAX'],
            'TAX_PAID': ['ASS.CUR.TAX.GST', 'ASS.CUR.TAX'],
        }
        
        # Try reporting code lookup first
        if account_type in REPORTING_CODES:
            for code in REPORTING_CODES[account_type]:
                account = XeroAccount.objects.filter(
                    organisation=self.organisation,
                    reporting_code__startswith=code
                ).first()
                if account:
                    return account
        
        # Fall back to type-based lookup
        xero_type = self.ACCOUNT_TYPES.get(account_type)
        if not xero_type or xero_type not in self._accounts_by_type:
            return None
        
        accounts = self._accounts_by_type.get(xero_type, [])
        return accounts[0] if accounts else None
    
    def _get_tax_account(self, transaction_type):
        """
        Get the appropriate tax account based on transaction type.
        
        Args:
            transaction_type: 'ACCREC', 'ACCPAY', 'SPEND', 'RECEIVE'
        
        Returns:
            XeroAccount or None
        """
        if transaction_type in ['ACCREC', 'RECEIVE']:
            # Output tax / GST collected
            return self._get_system_account('TAX_COLLECTED')
        else:  # ACCPAY, SPEND
            # Input tax / GST paid
            return self._get_system_account('TAX_PAID')
    
    def _parse_date(self, date_value):
        """
        Parse a date value from Xero API response.
        
        Handles:
        - .NET DateTime format: /Date(milliseconds)/
        - ISO format: 2024-01-15T00:00:00
        - Numeric timestamps
        
        Returns:
            datetime object or None
        """
        if not date_value:
            return None
            
        if isinstance(date_value, datetime.datetime):
            return date_value
        
        if isinstance(date_value, str):
            # Check for .NET DateTime format
            dotnet_pattern = r'/Date\((\d+)([+-]\d+)?\)/'
            match = re.match(dotnet_pattern, date_value)
            if match:
                try:
                    milliseconds = int(match.group(1))
                    return datetime.datetime.fromtimestamp(milliseconds / 1000.0, tz=dt_timezone.utc)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid .NET DateTime format: {date_value}, error: {e}")
                    return None
            
            # Try ISO format
            try:
                return datetime.datetime.fromisoformat(date_value.replace('Z', '+00:00'))
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid date string: {date_value}, error: {e}")
                return None
        
        if isinstance(date_value, (int, float)):
            try:
                timestamp = float(date_value)
                # If timestamp > Jan 1, 2000 in milliseconds, treat as milliseconds
                if timestamp > 946684800000:
                    return datetime.datetime.fromtimestamp(timestamp / 1000.0, tz=dt_timezone.utc)
                else:
                    return datetime.datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid timestamp: {date_value}, error: {e}")
                return None
        
        return None
    
    def _extract_tracking(self, line_item):
        """
        Extract tracking category IDs from a line item.
        
        Uses category_slot from XeroTracking (set from GET TrackingCategories array order).
        Slot 1 = first category, slot 2 = second. Fallback to index when category_slot is None.
        
        Handles multiple Xero tracking data formats:
        - Journals API: {"TrackingOptionID": "...", "Name": "...", "Option": "..."}
        - Invoices/BankTransactions API: {"TrackingCategoryID": "...", "Name": "...", "Option": "..."}
        
        Returns:
            tuple: (tracking1_id, tracking2_id) - database IDs, not Xero option IDs
        """
        tracking1_id = None
        tracking2_id = None
        
        tracking_data = line_item.get('Tracking', [])
        for index, tracking in enumerate(tracking_data):
            # Try direct option_id lookup first (Journals API format)
            option_id = (
                tracking.get('TrackingOptionID') or 
                tracking.get('OptionID') or 
                tracking.get('Option', {}).get('TrackingOptionID') if isinstance(tracking.get('Option'), dict) else None
            )
            
            tracking_obj = None
            if option_id:
                tracking_obj = self._trackings_dict.get(option_id)
            
            # Fallback: match by Option name (Invoices/BankTransactions API format)
            # These only have TrackingCategoryID (the category, not the option) + Option (name string)
            if tracking_obj is None and tracking.get('Option'):
                option_name = tracking['Option']
                category_name = tracking.get('Name', '')
                for tk_option_id, tk_obj in self._trackings_dict.items():
                    if tk_obj.option == option_name and (not category_name or tk_obj.name == category_name):
                        tracking_obj = tk_obj
                        break

            # Auto-create: if the tracking option exists on the transaction but
            # not in our metadata, create it so the data isn't silently dropped.
            if tracking_obj is None and tracking.get('Option') and tracking.get('Name'):
                tracking_obj = self._auto_create_tracking(tracking)

            if tracking_obj:
                # Prefer TrackingCategoryID (stable across renames); fallback to category_slot/index
                slot = self.organisation.get_tracking_slot(tracking_obj.tracking_category_id)
                if slot is None and tracking_obj.category_slot:
                    slot = tracking_obj.category_slot if tracking_obj.category_slot <= 2 else 2
                if slot is None:
                    slot = (index + 1) if index < 2 else 2
                if slot == 1:
                    tracking1_id = tracking_obj.id
                elif slot >= 2:
                    tracking2_id = tracking_obj.id
        
        return tracking1_id, tracking2_id
    
    def _auto_create_tracking(self, tracking_dict):
        """
        Auto-create a XeroTracking record when an invoice/transaction references
        a tracking option that doesn't exist in our metadata table. This prevents
        silent data loss when Xero options are added/renamed between metadata syncs.
        """
        from apps.xero.xero_metadata.models import XeroTracking
        import uuid

        option_name = tracking_dict.get('Option', '')
        category_name = tracking_dict.get('Name', '')
        category_id = tracking_dict.get('TrackingCategoryID', '')
        # Use TrackingOptionID if present, otherwise generate a synthetic one
        option_id = (
            tracking_dict.get('TrackingOptionID')
            or tracking_dict.get('OptionID')
            or f"auto_{uuid.uuid4().hex[:16]}"
        )

        slot = self.organisation.get_tracking_slot(category_id)

        try:
            obj, created = XeroTracking.objects.get_or_create(
                organisation=self.organisation,
                option_id=option_id,
                defaults={
                    'name': category_name,
                    'option': option_name,
                    'tracking_category_id': category_id or None,
                    'category_slot': slot,
                    'collection': tracking_dict,
                },
            )
            self._trackings_dict[option_id] = obj
            if created:
                logger.info(
                    f"Auto-created tracking: [{category_name}] {option_name} "
                    f"(option_id={option_id}, slot={slot})"
                )
            return obj
        except Exception as exc:
            logger.warning(f"Failed to auto-create tracking [{category_name}] {option_name}: {exc}")
            return None

    def _resolve_contact(self, txn_data):
        """Resolve a XeroContacts instance from a transaction's Contact dict."""
        contact_id = (txn_data or {}).get('Contact', {}).get('ContactID')
        if contact_id and self._contacts_dict:
            return self._contacts_dict.get(contact_id)
        return None

    def _create_journal_entry(self, transaction_source, account, amount, date,
                              description='', reference='', tracking1_id=None,
                              tracking2_id=None, journal_type='transaction', tax_amount=None,
                              contact=None):
        """
        Create a journal entry dict for later bulk creation.
        
        Args:
            transaction_source: XeroTransactionSource instance
            account: XeroAccount instance
            amount: Decimal amount (positive for debit, negative for credit)
            date: datetime of transaction
            description: Line description
            reference: Transaction reference
            tracking1_id: First tracking category DB ID
            tracking2_id: Second tracking category DB ID
            journal_type: Type of journal entry
            tax_amount: Optional tax amount for this line (default 0)
        
        Returns:
            dict: Journal entry data for bulk creation
        """
        if not account:
            logger.warning(f"Cannot create journal entry: account is None")
            return None
            
        # Use a line-index counter to ensure unique journal_ids when the same
        # account appears multiple times on one transaction (e.g. multi-line bills).
        # The counter is stored on the transaction_source to track line items.
        if not hasattr(transaction_source, '_line_counter'):
            transaction_source._line_counter = 0
        transaction_source._line_counter += 1
        line_idx = transaction_source._line_counter

        return {
            'organisation': self.organisation,
            'journal_id': f"{transaction_source.transactions_id}_{account.account_id}_{line_idx}",
            'journal_number': abs(hash(transaction_source.transactions_id)) % 1000000,
            'journal_type': journal_type,
            'account': account,
            'transaction_source': transaction_source,
            'journal_source': None,  # No journal source for transaction-based entries
            'date': date,
            'description': description[:500] if description else '',
            'reference': reference[:500] if reference else '',
            'amount': amount,
            'tax_amount': round_amount(tax_amount) if tax_amount is not None else Decimal('0'),
            'tracking1_id': tracking1_id,
            'tracking2_id': tracking2_id,
            'contact': contact,
        }
    
    def process_invoice(self, transaction_source):
        """
        Process an invoice to journal entries.
        
        Invoice types:
        - ACCREC: Sales invoice (Debit AR, Credit Revenue, Credit Tax Collected)
        - ACCPAY: Purchase invoice/Bill (Debit Expense, Debit Tax Paid, Credit AP)
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        invoice = transaction_source.collection
        
        if not invoice:
            return entries
        
        invoice_type = invoice.get('Type', '')
        date = self._parse_date(invoice.get('Date') or invoice.get('DateString'))
        reference = invoice.get('InvoiceNumber', '')
        contact_name = invoice.get('Contact', {}).get('Name', '')
        contact_obj = self._resolve_contact(invoice)
        
        # Skip voided, deleted, and draft invoices
        # DRAFT invoices are not yet approved and don't appear in Xero P&L
        # DELETED and VOIDED invoices are cancelled and don't appear in P&L
        status = invoice.get('Status', '')
        if status in ('VOIDED', 'DELETED', 'DRAFT'):
            logger.info(f"Skipping {status} invoice {transaction_source.transactions_id}")
            return entries
        
        if not date:
            logger.warning(f"Skipping invoice {transaction_source.transactions_id}: no valid date")
            return entries
        
        # Process line items
        total_amount = Decimal('0')
        total_tax = Decimal('0')
        line_amount_types = invoice.get('LineAmountTypes', 'Exclusive')
        
        for line in invoice.get('LineItems', []):
            account_code = line.get('AccountCode')
            if not account_code:
                continue
            
            account = self._accounts_by_code.get(account_code)
            if not account:
                logger.warning(f"Account code {account_code} not found for invoice line")
                continue
            
            line_amount = round_amount(line.get('LineAmount', 0))
            tax_amount = round_amount(line.get('TaxAmount', 0))
            description = line.get('Description', '')
            tracking1_id, tracking2_id = self._extract_tracking(line)
            
            # When LineAmountTypes is "Inclusive", LineAmount includes tax.
            # Extract the net (ex-tax) amount for the P&L entry, since
            # Xero P&L reports always show amounts exclusive of tax.
            net_amount = line_amount
            if line_amount_types == 'Inclusive':
                net_amount = line_amount - tax_amount
            
            # For ACCREC (sales): Revenue is CREDIT (negative)
            # For ACCPAY (purchases): Expense is DEBIT (positive)
            if invoice_type == 'ACCREC':
                amount = -net_amount  # Credit to revenue
            else:  # ACCPAY
                amount = net_amount   # Debit to expense
            
            entry = self._create_journal_entry(
                transaction_source=transaction_source,
                account=account,
                amount=round_amount(amount),
                date=date,
                description=f"{contact_name}: {description}" if contact_name else description,
                reference=reference,
                tracking1_id=tracking1_id,
                tracking2_id=tracking2_id,
                journal_type='transaction',
                tax_amount=tax_amount,
                contact=contact_obj,
            )
            if entry:
                entries.append(entry)
            
            total_amount += net_amount
            total_tax += tax_amount
        
        # Fallback: Xero API sometimes omits TaxAmount on LineItems but provides invoice-level TotalTax
        if total_tax == Decimal('0'):
            invoice_total_tax = round_amount(invoice.get('TotalTax', 0))
            if invoice_total_tax != Decimal('0'):
                total_tax = invoice_total_tax
                # Allocate total_tax to revenue/expense entries proportionally by net amount
                if total_amount > 0 and entries:
                    for e in entries:
                        e['tax_amount'] = round_amount(total_tax * (abs(e['amount']) / total_amount))
        
        # Add tax entry if there's tax
        if total_tax != Decimal('0'):
            tax_account = self._get_tax_account(invoice_type)
            if tax_account:
                # For ACCREC: Credit Tax Collected (negative)
                # For ACCPAY: Debit Tax Paid (positive)
                if invoice_type == 'ACCREC':
                    tax_entry_amount = -total_tax  # Credit tax collected
                else:  # ACCPAY
                    tax_entry_amount = total_tax   # Debit tax paid
                
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=tax_account,
                    amount=round_amount(tax_entry_amount),
                    date=date,
                    description=f"Tax - {reference}",
                    reference=reference,
                    journal_type='transaction',
                    tax_amount=abs(total_tax),
                )
                if entry:
                    entries.append(entry)
        
        # Add receivables/payables entry
        # For ACCREC: Debit AR (positive)
        # For ACCPAY: Credit AP (negative)
        if invoice_type == 'ACCREC':
            ar_account = self._get_system_account('ACCOUNTS_RECEIVABLE')
            if ar_account:
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ar_account,
                    amount=round_amount(total_amount + total_tax),  # Debit AR
                    date=date,
                    description=f"Invoice {reference} - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                )
                if entry:
                    entries.append(entry)
        else:  # ACCPAY
            ap_account = self._get_system_account('ACCOUNTS_PAYABLE')
            if ap_account:
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ap_account,
                    amount=round_amount(-(total_amount + total_tax)),  # Credit AP
                    date=date,
                    description=f"Bill {reference} - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                )
                if entry:
                    entries.append(entry)
        
        return entries
    
    def process_bank_transaction(self, transaction_source):
        """
        Process a bank transaction to journal entries.
        
        Transaction types:
        - SPEND / SPEND-OVERPAYMENT / SPEND-PREPAYMENT: Credit Bank, Debit Expense
        - RECEIVE / RECEIVE-OVERPAYMENT / RECEIVE-PREPAYMENT: Debit Bank, Credit Revenue
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        bank_txn = transaction_source.collection
        
        if not bank_txn:
            return entries
        
        txn_type = bank_txn.get('Type', '')
        is_spend = txn_type.startswith('SPEND')
        date = self._parse_date(bank_txn.get('Date') or bank_txn.get('DateString'))
        reference = bank_txn.get('Reference', '')
        contact_name = bank_txn.get('Contact', {}).get('Name', '')
        contact_obj = self._resolve_contact(bank_txn)
        
        # Skip deleted or voided bank transactions
        status = bank_txn.get('Status', '')
        if status in ('DELETED', 'VOIDED'):
            logger.info(f"Skipping {status} bank transaction {transaction_source.transactions_id}")
            return entries
        
        # Get bank account
        bank_account_id = bank_txn.get('BankAccount', {}).get('AccountID')
        bank_account = self._accounts_by_id.get(bank_account_id) if bank_account_id else None
        
        if not date:
            logger.warning(f"Skipping bank transaction {transaction_source.transactions_id}: no valid date")
            return entries
        
        # Process line items
        total_amount = Decimal('0')
        total_tax = Decimal('0')
        line_amount_types = bank_txn.get('LineAmountTypes', 'Exclusive')
        
        for line in bank_txn.get('LineItems', []):
            account_code = line.get('AccountCode')
            if not account_code:
                continue
            
            account = self._accounts_by_code.get(account_code)
            if not account:
                logger.warning(f"Account code {account_code} not found for bank transaction line")
                continue
            
            line_amount = round_amount(line.get('LineAmount', 0))
            tax_amount = round_amount(line.get('TaxAmount', 0))
            description = line.get('Description', '')
            tracking1_id, tracking2_id = self._extract_tracking(line)
            
            # When LineAmountTypes is "Inclusive", LineAmount includes tax.
            # Extract the net (ex-tax) amount for the P&L entry.
            net_amount = line_amount
            if line_amount_types == 'Inclusive':
                net_amount = line_amount - tax_amount
            
            # SPEND*: Expense is DEBIT (positive)
            # RECEIVE*: Revenue is CREDIT (negative)
            if is_spend:
                amount = net_amount
            else:
                amount = -net_amount
            
            entry = self._create_journal_entry(
                transaction_source=transaction_source,
                account=account,
                amount=round_amount(amount),
                date=date,
                description=f"{contact_name}: {description}" if contact_name else description,
                reference=reference,
                tracking1_id=tracking1_id,
                tracking2_id=tracking2_id,
                journal_type='transaction',
                tax_amount=tax_amount,
                contact=contact_obj,
            )
            if entry:
                entries.append(entry)
            
            total_amount += net_amount
            total_tax += tax_amount
        
        # Fallback: use bank transaction-level TotalTax when line-level TaxAmount is missing
        if total_tax == Decimal('0'):
            txn_total_tax = round_amount(bank_txn.get('TotalTax', 0))
            if txn_total_tax != Decimal('0'):
                total_tax = txn_total_tax
                if total_amount > 0 and entries:
                    for e in entries:
                        e['tax_amount'] = round_amount(total_tax * (abs(e['amount']) / total_amount))
        
        # Add tax entry if there's tax
        if total_tax != Decimal('0'):
            tax_account = self._get_tax_account(txn_type)
            if tax_account:
                if is_spend:
                    tax_entry_amount = total_tax
                else:
                    tax_entry_amount = -total_tax
                
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=tax_account,
                    amount=round_amount(tax_entry_amount),
                    date=date,
                    description=f"Tax - {reference}",
                    reference=reference,
                    journal_type='transaction',
                    tax_amount=abs(total_tax),
                )
                if entry:
                    entries.append(entry)
        
        # Add bank account entry (total including tax)
        if bank_account:
            if is_spend:
                bank_amount = -(total_amount + total_tax)
            else:
                bank_amount = total_amount + total_tax
            
            entry = self._create_journal_entry(
                transaction_source=transaction_source,
                account=bank_account,
                amount=round_amount(bank_amount),
                date=date,
                description=f"Bank {txn_type.lower()} - {contact_name}",
                reference=reference,
                journal_type='transaction'
            )
            if entry:
                entries.append(entry)
        
        return entries
    
    def process_payment(self, transaction_source):
        """
        Process a payment to journal entries.
        
        Payments move money between AR/AP and Bank accounts.
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        payment = transaction_source.collection
        
        if not payment:
            return entries
        
        date = self._parse_date(payment.get('Date') or payment.get('DateString'))
        reference = payment.get('Reference', '') or payment.get('PaymentID', '')[:20]
        
        # Skip voided/deleted payments
        status = payment.get('Status', '')
        if status in ['DELETED', 'VOIDED']:
            return entries
        
        if not date:
            logger.warning(f"Skipping payment {transaction_source.transactions_id}: no valid date")
            return entries
        
        amount = round_amount(payment.get('Amount', 0))
        if amount == 0:
            return entries
        
        # Get the bank account
        bank_account_id = payment.get('Account', {}).get('AccountID')
        bank_account = self._accounts_by_id.get(bank_account_id) if bank_account_id else None
        
        # Determine if this is a payment received (ACCREC) or payment made (ACCPAY)
        invoice = payment.get('Invoice', {})
        invoice_type = invoice.get('Type', '')
        
        if invoice_type == 'ACCREC':
            # Payment received: Debit Bank, Credit AR
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=round_amount(amount),  # Debit bank
                    date=date,
                    description=f"Payment received - Invoice {invoice.get('InvoiceNumber', '')}",
                    reference=reference,
                    journal_type='transaction'
                ))
            
            ar_account = self._get_system_account('ACCOUNTS_RECEIVABLE')
            if ar_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ar_account,
                    amount=round_amount(-amount),  # Credit AR
                    date=date,
                    description=f"Payment received - Invoice {invoice.get('InvoiceNumber', '')}",
                    reference=reference,
                    journal_type='transaction'
                ))
        
        elif invoice_type == 'ACCPAY':
            # Payment made: Credit Bank, Debit AP
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=round_amount(-amount),  # Credit bank
                    date=date,
                    description=f"Payment made - Bill {invoice.get('InvoiceNumber', '')}",
                    reference=reference,
                    journal_type='transaction'
                ))
            
            ap_account = self._get_system_account('ACCOUNTS_PAYABLE')
            if ap_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ap_account,
                    amount=round_amount(amount),  # Debit AP
                    date=date,
                    description=f"Payment made - Bill {invoice.get('InvoiceNumber', '')}",
                    reference=reference,
                    journal_type='transaction'
                ))
        
        return [e for e in entries if e]
    
    def process_credit_note(self, transaction_source):
        """
        Process a credit note to journal entries.
        
        Credit notes are essentially reverse invoices:
        - ACCRECCREDIT: Credit AR, Debit Revenue, Debit Tax Collected
        - ACCPAYCREDIT: Debit AP, Credit Expense, Credit Tax Paid
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        credit_note = transaction_source.collection
        
        if not credit_note:
            return entries
        
        cn_type = credit_note.get('Type', '')
        date = self._parse_date(credit_note.get('Date') or credit_note.get('DateString'))
        reference = credit_note.get('CreditNoteNumber', '')
        contact_name = credit_note.get('Contact', {}).get('Name', '')
        contact_obj = self._resolve_contact(credit_note)
        
        # Skip voided, deleted, and draft credit notes
        status = credit_note.get('Status', '')
        if status in ('VOIDED', 'DELETED', 'DRAFT'):
            logger.info(f"Skipping {status} credit note {transaction_source.transactions_id}")
            return entries
        
        if not date:
            logger.warning(f"Skipping credit note {transaction_source.transactions_id}: no valid date")
            return entries
        
        # Process line items (opposite of invoice)
        total_amount = Decimal('0')
        total_tax = Decimal('0')
        line_amount_types = credit_note.get('LineAmountTypes', 'Exclusive')
        
        for line in credit_note.get('LineItems', []):
            account_code = line.get('AccountCode')
            if not account_code:
                continue
            
            account = self._accounts_by_code.get(account_code)
            if not account:
                continue
            
            line_amount = round_amount(line.get('LineAmount', 0))
            tax_amount = round_amount(line.get('TaxAmount', 0))
            description = line.get('Description', '')
            tracking1_id, tracking2_id = self._extract_tracking(line)
            
            # When LineAmountTypes is "Inclusive", LineAmount includes tax.
            # Extract the net (ex-tax) amount for the P&L entry.
            net_amount = line_amount
            if line_amount_types == 'Inclusive':
                net_amount = line_amount - tax_amount
            
            # Opposite of invoice:
            # ACCRECCREDIT: Debit Revenue (positive)
            # ACCPAYCREDIT: Credit Expense (negative)
            if cn_type == 'ACCRECCREDIT':
                amount = net_amount   # Debit to revenue (reversal)
            else:  # ACCPAYCREDIT
                amount = -net_amount  # Credit to expense (reversal)
            
            entry = self._create_journal_entry(
                transaction_source=transaction_source,
                account=account,
                amount=round_amount(amount),
                date=date,
                description=f"Credit Note: {contact_name}: {description}" if contact_name else description,
                reference=reference,
                tracking1_id=tracking1_id,
                tracking2_id=tracking2_id,
                journal_type='transaction',
                tax_amount=tax_amount,
                contact=contact_obj,
            )
            if entry:
                entries.append(entry)
            
            total_amount += net_amount
            total_tax += tax_amount
        
        # Fallback: use credit note-level TotalTax when line-level TaxAmount is missing
        if total_tax == Decimal('0'):
            cn_total_tax = round_amount(credit_note.get('TotalTax', 0))
            if cn_total_tax != Decimal('0'):
                total_tax = cn_total_tax
                if total_amount > 0 and entries:
                    for e in entries:
                        e['tax_amount'] = round_amount(total_tax * (abs(e['amount']) / total_amount))
        
        # Add tax entry if there's tax (opposite of invoice)
        if total_tax != Decimal('0'):
            tax_account = self._get_tax_account(cn_type.replace('CREDIT', ''))
            if tax_account:
                # Opposite of invoice tax:
                # ACCRECCREDIT: Debit Tax Collected (positive)
                # ACCPAYCREDIT: Credit Tax Paid (negative)
                if cn_type == 'ACCRECCREDIT':
                    tax_entry_amount = total_tax   # Debit tax collected (reversal)
                else:  # ACCPAYCREDIT
                    tax_entry_amount = -total_tax  # Credit tax paid (reversal)
                
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=tax_account,
                    amount=round_amount(tax_entry_amount),
                    date=date,
                    description=f"Tax - Credit Note {reference}",
                    reference=reference,
                    journal_type='transaction',
                    tax_amount=abs(total_tax),
                )
                if entry:
                    entries.append(entry)
        
        # Add AR/AP entry (opposite of invoice, including tax)
        if cn_type == 'ACCRECCREDIT':
            ar_account = self._get_system_account('ACCOUNTS_RECEIVABLE')
            if ar_account:
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ar_account,
                    amount=round_amount(-(total_amount + total_tax)),  # Credit AR (reversal)
                    date=date,
                    description=f"Credit Note {reference} - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                )
                if entry:
                    entries.append(entry)
        else:  # ACCPAYCREDIT
            ap_account = self._get_system_account('ACCOUNTS_PAYABLE')
            if ap_account:
                entry = self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=ap_account,
                    amount=round_amount(total_amount + total_tax),  # Debit AP (reversal)
                    date=date,
                    description=f"Credit Note {reference} - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                )
                if entry:
                    entries.append(entry)
        
        return entries
    
    def process_prepayment(self, transaction_source):
        """
        Process a prepayment to journal entries.
        
        Prepayments are payments received/made before invoice.
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        prepayment = transaction_source.collection
        
        if not prepayment:
            return entries
        
        pp_type = prepayment.get('Type', '')
        date = self._parse_date(prepayment.get('Date') or prepayment.get('DateString'))
        reference = prepayment.get('Reference', '')
        contact_name = prepayment.get('Contact', {}).get('Name', '')
        contact_obj = self._resolve_contact(prepayment)
        
        # Skip voided prepayments
        status = prepayment.get('Status', '')
        if status == 'VOIDED':
            return entries
        
        if not date:
            return entries
        
        # Get bank account
        bank_account_id = prepayment.get('BankAccount', {}).get('AccountID')
        bank_account = self._accounts_by_id.get(bank_account_id) if bank_account_id else None
        
        total_amount = round_amount(prepayment.get('Total', 0))
        
        if pp_type == 'RECEIVE-PREPAYMENT':
            # Prepayment received: Debit Bank, Credit Prepayment liability
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=total_amount,
                    date=date,
                    description=f"Prepayment received - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                ))
        elif pp_type == 'SPEND-PREPAYMENT':
            # Prepayment made: Credit Bank, Debit Prepayment asset
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=-total_amount,
                    date=date,
                    description=f"Prepayment made - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                ))
        
        return [e for e in entries if e]
    
    def process_overpayment(self, transaction_source):
        """
        Process an overpayment to journal entries.
        
        Overpayments occur when payment exceeds invoice amount.
        
        Returns:
            list: Journal entry dicts
        """
        entries = []
        overpayment = transaction_source.collection
        
        if not overpayment:
            return entries
        
        op_type = overpayment.get('Type', '')
        date = self._parse_date(overpayment.get('Date') or overpayment.get('DateString'))
        reference = overpayment.get('Reference', '')
        contact_name = overpayment.get('Contact', {}).get('Name', '')
        contact_obj = self._resolve_contact(overpayment)
        
        # Skip voided overpayments
        status = overpayment.get('Status', '')
        if status == 'VOIDED':
            return entries
        
        if not date:
            return entries
        
        # Get bank account
        bank_account_id = overpayment.get('BankAccount', {}).get('AccountID')
        bank_account = self._accounts_by_id.get(bank_account_id) if bank_account_id else None
        
        total_amount = round_amount(overpayment.get('Total', 0))
        
        if op_type == 'RECEIVE-OVERPAYMENT':
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=round_amount(total_amount),
                    date=date,
                    description=f"Overpayment received - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                ))
        elif op_type == 'SPEND-OVERPAYMENT':
            if bank_account:
                entries.append(self._create_journal_entry(
                    transaction_source=transaction_source,
                    account=bank_account,
                    amount=round_amount(-total_amount),
                    date=date,
                    description=f"Overpayment made - {contact_name}",
                    reference=reference,
                    journal_type='transaction'
                ))
        
        return [e for e in entries if e]
    
    @transaction.atomic
    def process_all_transactions(self, clear_existing=False, touched_transaction_ids=None):
        """
        Process all transactions for the organisation to journal entries.

        Args:
            clear_existing: If True, delete existing transaction-based journals (full) or
                only journals for touched transactions (incremental)
            touched_transaction_ids: Optional set of transaction IDs updated in fetch.
                If provided and non-empty, incremental mode: only delete and reprocess those.
                If None or empty, full rebuild: clear all and reprocess everything.

        Returns:
            dict: Processing stats
        """
        from apps.xero.xero_data.models import XeroTransactionSource, XeroJournals
        
        incremental = (
            touched_transaction_ids is not None
            and len(touched_transaction_ids) > 0
        )
        if touched_transaction_ids is not None and len(touched_transaction_ids) == 0:
            # Incremental fetch returned no modifications - nothing to do
            return {
                'invoices_processed': 0,
                'bank_transactions_processed': 0,
                'payments_processed': 0,
                'credit_notes_processed': 0,
                'prepayments_processed': 0,
                'overpayments_processed': 0,
                'journal_entries_created': 0,
                'errors': [],
            }
        if incremental:
            pass  # Incremental mode: reprocessing touched transactions
        else:
            pass  # Full rebuild: processing all transactions
        
        # Load all lookups
        self._load_lookups()
        
        # Clear journals: either all (full rebuild) or only for touched transactions (incremental)
        if clear_existing:
            if incremental:
                deleted = XeroJournals.objects.filter(
                    organisation=self.organisation,
                    journal_type='transaction',
                    transaction_source__transactions_id__in=touched_transaction_ids,
                ).delete()
            else:
                deleted = XeroJournals.objects.filter(
                    organisation=self.organisation,
                    journal_type='transaction'
                ).delete()
        
        stats = {
            'invoices_processed': 0,
            'bank_transactions_processed': 0,
            'payments_processed': 0,
            'credit_notes_processed': 0,
            'prepayments_processed': 0,
            'overpayments_processed': 0,
            'journal_entries_created': 0,
            'errors': [],
        }
        
        all_entries = []
        
        # Get transactions: all or only touched (incremental)
        transactions = XeroTransactionSource.objects.filter(organisation=self.organisation)
        if incremental:
            transactions = transactions.filter(transactions_id__in=touched_transaction_ids)
        
        # Process by transaction type
        processor_map = {
            'Invoice': (self.process_invoice, 'invoices_processed'),
            'BankTransaction': (self.process_bank_transaction, 'bank_transactions_processed'),
            'Payment': (self.process_payment, 'payments_processed'),
            'CreditNote': (self.process_credit_note, 'credit_notes_processed'),
            'Prepayment': (self.process_prepayment, 'prepayments_processed'),
            'Overpayment': (self.process_overpayment, 'overpayments_processed'),
        }
        
        for txn in transactions:
            processor_info = processor_map.get(txn.transaction_source)
            if not processor_info:
                continue
            
            processor_func, stat_key = processor_info
            
            try:
                entries = processor_func(txn)
                all_entries.extend(entries)
                stats[stat_key] += 1
            except Exception as e:
                error_msg = f"Error processing {txn.transaction_source} {txn.transactions_id}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                stats['errors'].append(error_msg)
        
        # Bulk create journal entries
        if all_entries:
            # When we cleared existing (full or incremental), all entries are new - skip DB check
            if clear_existing:
                new_entries = all_entries
            else:
                existing_ids = set(XeroJournals.objects.filter(
                    organisation=self.organisation,
                    journal_id__in=[e['journal_id'] for e in all_entries]
                ).values_list('journal_id', flat=True))
                new_entries = [e for e in all_entries if e['journal_id'] not in existing_ids]
            
            if new_entries:
                journals_to_create = []
                for entry in new_entries:
                    journal = XeroJournals(
                        organisation=entry['organisation'],
                        journal_id=entry['journal_id'],
                        journal_number=entry['journal_number'],
                        journal_type=entry['journal_type'],
                        account=entry['account'],
                        transaction_source=entry['transaction_source'],
                        journal_source=entry['journal_source'],
                        date=entry['date'],
                        description=entry['description'],
                        reference=entry['reference'],
                        amount=entry['amount'],
                        tax_amount=entry['tax_amount'],
                    )
                    if entry['tracking1_id']:
                        journal.tracking1_id = entry['tracking1_id']
                    if entry['tracking2_id']:
                        journal.tracking2_id = entry['tracking2_id']
                    if entry.get('contact'):
                        journal.contact = entry['contact']
                    journals_to_create.append(journal)
                
                # Batch create
                batch_size = 5000
                for i in range(0, len(journals_to_create), batch_size):
                    batch = journals_to_create[i:i + batch_size]
                    XeroJournals.objects.bulk_create(batch, ignore_conflicts=True)
                
                stats['journal_entries_created'] = len(new_entries)
        
        if settings.DEBUG:
            print(
                "[Sync] Transactions updated: invoices=%d, bank_transactions=%d, payments=%d, "
                "credit_notes=%d, prepayments=%d, overpayments=%d | journal_entries=%d | errors=%d"
                % (
                    stats['invoices_processed'],
                    stats['bank_transactions_processed'],
                    stats['payments_processed'],
                    stats['credit_notes_processed'],
                    stats['prepayments_processed'],
                    stats['overpayments_processed'],
                    stats['journal_entries_created'],
                    len(stats['errors']),
                )
            )

        return stats


def process_transactions_to_journals(organisation, touched_transaction_ids=None):
    """
    Convenience function to process all transactions to journal entries.

    Args:
        organisation: XeroTenant instance
        touched_transaction_ids: Optional set of transaction IDs that were updated in the fetch.
            If provided and non-empty, only those transactions are reprocessed (incremental).
            If None or empty, does full rebuild (clear all and reprocess everything).

    Returns:
        dict: Processing stats
    """
    processor = TransactionProcessor(organisation)
    return processor.process_all_transactions(
        clear_existing=True,
        touched_transaction_ids=touched_transaction_ids,
    )
