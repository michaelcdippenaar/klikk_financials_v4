from django.db import models
import datetime
from datetime import timezone as dt_timezone
import logging
import re
from django.utils import timezone
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroContacts, XeroAccount, XeroTracking

logger = logging.getLogger(__name__)


def _resolve_tracking_slot(tracking_obj, organisation, fallback_idx=None):
    """Return 1 or 2 for tracking1/tracking2. Uses TrackingCategoryID (stable); fallback to category_slot/index."""
    slot = organisation.get_tracking_slot(tracking_obj.tracking_category_id)
    if slot is None and tracking_obj.category_slot:
        slot = min(tracking_obj.category_slot, 2)
    if slot is None and fallback_idx is not None:
        slot = 1 if fallback_idx == 0 else 2
    return slot


class XeroTransactionSourceModelManager(models.Manager):
    def _create_transactions_from_xero(self, organisation, xero_response, transaction_source_type, transaction_id_key):
        """Helper method to create transactions with bulk operations."""
        # Pre-fetch all contacts into a dictionary for O(1) lookup
        contacts_dict = {
            c.contacts_id: c for c in XeroContacts.objects.filter(organisation=organisation)
        }
        
        # Collect transaction IDs
        transaction_ids = []
        for r in xero_response:
            transaction_ids.append(r[transaction_id_key])
        
        # Fetch existing transactions in one query
        existing_transactions = {
            t.transactions_id: t for t in self.filter(
                organisation=organisation,
                transactions_id__in=transaction_ids
            )
        }
        
        to_create = []
        to_update = []
        
        for r in xero_response:
            transaction_id = r[transaction_id_key]
            contact = None
            if "Contact" in r:
                contact_id = r["Contact"].get("ContactID")
                contact = contacts_dict.get(contact_id)
            
            if transaction_id in existing_transactions:
                # Update existing
                existing = existing_transactions[transaction_id]
                existing.contact = contact
                existing.transaction_source = transaction_source_type
                existing.collection = r
                to_update.append(existing)
            else:
                # Create new
                to_create.append(XeroTransactionSource(
                    organisation=organisation,
                    transactions_id=transaction_id,
                    contact=contact,
                    transaction_source=transaction_source_type,
                    collection=r
                ))
        
        # Bulk create and update
        if to_create:
            self.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            self.bulk_update(to_update, ['contact', 'transaction_source', 'collection'])
        
        return self
    
    def create_bank_transaction_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'BankTransaction', 'BankTransactionID'
        )

    def create_invoices_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'Invoice', 'InvoiceID'
        )

    def create_payments_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'Payment', 'PaymentID'
        )

    def create_credit_notes_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'CreditNote', 'CreditNoteID'
        )

    def create_prepayments_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'Prepayment', 'PrepaymentID'
        )

    def create_overpayments_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'Overpayment', 'OverpaymentID'
        )

    def create_purchase_orders_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'PurchaseOrder', 'PurchaseOrderID'
        )

    def create_bank_transfers_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'BankTransfer', 'BankTransferID'
        )

    def create_expense_claims_from_xero(self, organisation, xero_response):
        return self._create_transactions_from_xero(
            organisation, xero_response, 'ExpenseClaim', 'ExpenseClaimID'
        )


class XeroTransactionSource(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='transaction_sources')
    transactions_id = models.CharField(max_length=51, unique=True)
    transaction_source = models.CharField(max_length=51)
    contact = models.ForeignKey(XeroContacts, on_delete=models.DO_NOTHING, null=True, blank=True,
                                related_name='transaction_sources')
    collection = models.JSONField(blank=True, null=True)

    objects = XeroTransactionSourceModelManager()

    class Meta:
        unique_together = [('organisation', 'transactions_id')]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.contact} - {self.transaction_source}'


class XeroJournalsSourceManager(models.Manager):
    def create_journals_from_xero(self, organisation, journal_ids=None, force_reprocess=False):
        """
        Process journals from XeroJournalsSource to XeroJournals.
        
        Args:
            organisation: The XeroTenant organisation
            journal_ids: Optional list of journal IDs to process. If None, processes all unprocessed journals.
                        This allows incremental updates to only process newly fetched journals.
            force_reprocess: If True, process all journal sources (including already processed) to fix tracking.
        """
        from apps.xero.xero_data.models import XeroTransactionSource, XeroJournals
        from apps.xero.xero_metadata.models import XeroAccount, XeroTracking, XeroContacts
        
        print('Creating Journals from Xero', organisation)
        # Pre-fetch all related data into dictionaries for O(1) lookup
        source_transactions_dict = {
            t.transactions_id: t for t in XeroTransactionSource.objects.filter(organisation=organisation)
        }
        
        # Filter source journals: force_reprocess processes all; otherwise only unprocessed
        if force_reprocess:
            source = XeroJournalsSource.objects.filter(organisation=organisation)
            if journal_ids:
                source = source.filter(journal_id__in=journal_ids)
        elif journal_ids:
            source = XeroJournalsSource.objects.filter(organisation=organisation, processed=False, journal_id__in=journal_ids)
        else:
            source = XeroJournalsSource.objects.filter(organisation=organisation, processed=False)
        if journal_ids:
            print(f"[PROCESS] Processing only newly fetched journals: {len(journal_ids)} journal IDs")
        elif force_reprocess:
            print(f"[PROCESS] Force reprocess: re-processing all journal sources to fix tracking assignment")
        else:
            print(f"[PROCESS] Processing all unprocessed journals")
        
        manual_count = source.filter(journal_type='manual_journal').count()
        regular_count = source.filter(journal_type='journal').count()
        print(f"[PROCESS] Journals to process: {manual_count} manual, {regular_count} regular, {source.count()} total")
        # Create accounts dict by ID for regular journals
        accounts_dict = {
            acc.account_id: acc for acc in XeroAccount.objects.filter(organisation=organisation)
        }
        # Create accounts dict by code for manual journals (they use AccountCode instead of AccountID)
        accounts_by_code_dict = {
            acc.code: acc for acc in XeroAccount.objects.filter(organisation=organisation) if acc.code
        }
        trackings_dict = {
            t.option_id: t for t in XeroTracking.objects.filter(organisation=organisation)
        }
        contacts_dict = {
            c.contacts_id: c for c in XeroContacts.objects.filter(organisation=organisation)
        }
        
        # Collect all journal line IDs to check existing
        all_journal_line_ids = []
        journal_data_list = []
        journals_to_mark_processed = []
        
        skipped_by_status = 0
        for j_obj in source:
            j = j_obj.collection
            # Debug: Print journal type to verify it's set correctly
            print(f"[PROCESS] Processing journal {j_obj.journal_id}, journal_type from source: {j_obj.journal_type}")
            is_manual_journal = j_obj.journal_type == 'manual_journal'
            if is_manual_journal:
                print(f"[PROCESS] Detected manual journal: {j_obj.journal_id}")
            
            # Skip non-active journals (VOIDED, DELETED, DRAFT)
            # Only POSTED manual journals and active regular journals should create journal entries
            journal_status = j.get('Status', '')
            if journal_status in ('VOIDED', 'DELETED', 'DRAFT'):
                print(f"[PROCESS] Skipping {journal_status} journal {j_obj.journal_id}")
                skipped_by_status += 1
                journals_to_mark_processed.append(j_obj)
                continue
            
            # Handle different field names for regular vs manual journals
            if is_manual_journal:
                reference = j.get("Narration", "")  # Manual journals use "Narration"
                journal_number = j.get('JournalNumber', 0)  # May not exist, use 0 or generate
                if journal_number == 0:
                    # Generate a journal number from ManualJournalID hash
                    journal_number = abs(hash(j_obj.journal_id)) % 1000000
                date_raw = j.get('Date')  # Manual journals use "Date" not "JournalDate"
            else:
                reference = j.get("Reference", "")  # Regular journals use "Reference"
                journal_number = j.get('JournalNumber', 0)
                date_raw = j.get('JournalDate')  # Regular journals use "JournalDate"
            
            source_transactions_obj = None
            if "SourceID" in j:
                source_id = j["SourceID"]
                source_transactions_obj = source_transactions_dict.get(source_id)

            # Parse date field (handles both JournalDate and Date)
            logger.info(f"Processing journal {j_obj.journal_id} (type: {j_obj.journal_type}), Date: {date_raw} (type: {type(date_raw)})")
            date = None
            
            if isinstance(date_raw, str):
                # Check for .NET DateTime format: /Date(milliseconds)/ or /Date(milliseconds+offset)/
                dotnet_pattern = r'/Date\((\d+)([+-]\d+)?\)/'
                match = re.match(dotnet_pattern, date_raw)
                if match:
                    try:
                        # .NET DateTime is milliseconds since Unix epoch
                        milliseconds = int(match.group(1))
                        date = datetime.datetime.fromtimestamp(milliseconds / 1000.0, tz=dt_timezone.utc)
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Skipping journal {j_obj.journal_id}: Invalid .NET DateTime format: {date_raw}, error: {str(e)}")
                        continue
                else:
                    # Try ISO format
                    try:
                        date = datetime.datetime.fromisoformat(date_raw.replace('Z', '+00:00'))
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Skipping journal {j_obj.journal_id}: Invalid date string: {date_raw}, error: {str(e)}")
                        continue
            elif isinstance(date_raw, (int, float)):
                try:
                    # Check if it's milliseconds (>= year 2000 timestamp in milliseconds)
                    timestamp = float(date_raw)
                    # If timestamp is > 946684800000 (Jan 1, 2000 in milliseconds), treat as milliseconds
                    if timestamp > 946684800000:
                        date = datetime.datetime.fromtimestamp(timestamp / 1000.0, tz=dt_timezone.utc)
                    else:
                        # Otherwise treat as seconds
                        date = datetime.datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"Skipping journal {j_obj.journal_id}: Invalid JournalDate timestamp: {date_raw}, error: {str(e)}")
                    continue
            else:
                logger.warning(
                    f"Skipping journal {j_obj.journal_id}: JournalDate is not a string or timestamp, got {type(date_raw)}: {date_raw}")
                continue

            # Process JournalLines - handle different structures for regular vs manual journals
            journal_lines = j.get('JournalLines', [])
            journal_lines_count = len(journal_lines) if journal_lines else 0
            print(f"[PROCESS] Journal {j_obj.journal_id} ({j_obj.journal_type}) has {journal_lines_count} journal lines")
            
            # Handle empty JournalLines array
            if not journal_lines or journal_lines_count == 0:
                if is_manual_journal:
                    print(f"[PROCESS] WARNING: Manual journal {j_obj.journal_id} has no JournalLines. Narration: {j.get('Narration', 'N/A')[:50]}")
                    logger.warning(f"Manual journal {j_obj.journal_id} has no JournalLines. Full data: {j}")
                else:
                    print(f"[PROCESS] WARNING: Regular journal {j_obj.journal_id} has no JournalLines. Reference: {j.get('Reference', 'N/A')[:50]}")
                    logger.warning(f"Regular journal {j_obj.journal_id} has no JournalLines. Full data: {j}")
                # Still mark as processed to avoid reprocessing empty journals
                journals_to_mark_processed.append(j_obj)
                continue
            
            for line_index, jl in enumerate(journal_lines):
                if is_manual_journal:
                    # Manual journals: Generate line ID, use AccountCode, LineAmount
                    print(f"[PROCESS] Processing manual journal line {line_index} for journal {j_obj.journal_id}")
                    line_id = f"{j_obj.journal_id}_{line_index}"  # Generate line ID
                    account_code = jl.get('AccountCode')
                    print(f"[PROCESS] Manual journal line {line_index}: AccountCode={account_code}, LineAmount={jl.get('LineAmount')}, Description={jl.get('Description', '')[:50]}")
                    
                    if not account_code:
                        error_msg = f"Skipping manual journal line {line_index}: No AccountCode found. Line data: {jl}"
                        logger.warning(error_msg)
                        print(f"[PROCESS] ERROR: {error_msg}")
                        continue
                    
                    # Look up account by code instead of ID
                    account_instance = accounts_by_code_dict.get(account_code)
                    
                    if not account_instance:
                        available_codes = list(accounts_by_code_dict.keys())[:10]  # Show first 10 for debugging
                        error_msg = f"Skipping manual journal line {line_index}: Account code '{account_code}' not found. Available codes (sample): {available_codes}"
                        logger.warning(error_msg)
                        print(f"[PROCESS] ERROR: {error_msg}")
                        continue
                    
                    print(f"[PROCESS] Found account for code '{account_code}': {account_instance.name} (ID: {account_instance.account_id})")
                    
                    amount = jl.get('LineAmount', 0)  # Manual journals use "LineAmount"
                    tax_amount = jl.get('TaxAmount', 0)
                    description = jl.get('Description', '')
                    
                    # Manual journals use "Tracking" array (different structure)
                    tracking_data = jl.get('Tracking', [])
                    print(f"[PROCESS] Manual journal line {line_index}: amount={amount}, tax_amount={tax_amount}, tracking_count={len(tracking_data)}")
                else:
                    # Regular journals: Use JournalLineID, AccountID, NetAmount
                    line_id = jl.get('JournalLineID')
                    if not line_id:
                        logger.warning(f"Skipping journal line: No JournalLineID found")
                        continue
                    
                    account_id = jl.get('AccountID')
                    if not account_id:
                        logger.warning(f"Skipping journal line {line_id}: No AccountID found")
                        continue
                    
                    account_instance = accounts_dict.get(account_id)
                    if not account_instance:
                        logger.warning(f"Skipping journal line {line_id}: Account {account_id} not found")
                        continue
                    
                    amount = jl.get('NetAmount', 0)  # Regular journals use "NetAmount"
                    tax_amount = jl.get('TaxAmount', 0)
                    description = jl.get('Description', '')
                    
                    # Regular journals use "TrackingCategories" array
                    tracking_data = jl.get('TrackingCategories', [])

                # Contact: for manual journals use journal-level Contact from API if present
                contact_instance = None
                if is_manual_journal and j.get('Contact'):
                    contact_id = j.get('Contact', {}).get('ContactID')
                    if contact_id:
                        contact_instance = contacts_dict.get(contact_id)
                
                all_journal_line_ids.append(line_id)
                journal_entry = {
                    'line_id': line_id,
                    'journal_number': journal_number,
                    'account': account_instance,
                    'date': date,
                    'description': description,
                    'reference': reference,
                    'amount': amount,
                    'tax_amount': tax_amount,
                    'journal_source': j_obj,
                    'transaction_source': source_transactions_obj,
                    'journal_type': j_obj.journal_type,  # Include journal type from source
                    'contact': contact_instance,
                    'tracking1_id': None,
                    'tracking2_id': None,
                }
                journal_data_list.append(journal_entry)
                
                if is_manual_journal:
                    print(f"[PROCESS] Added manual journal line to list: line_id={line_id}, journal_type={journal_entry['journal_type']}, amount={amount}")
                
                # Process tracking categories/tracking - use category_slot from Xero API order
                for idx, t in enumerate(tracking_data):
                    if is_manual_journal:
                        tracking_option_id = t.get('TrackingOptionID') or t.get('OptionID') or t.get('ID')
                    else:
                        tracking_option_id = t.get('TrackingOptionID')
                    if tracking_option_id:
                        tracking_obj = trackings_dict.get(tracking_option_id)
                        if tracking_obj:
                            slot = _resolve_tracking_slot(tracking_obj, organisation, idx)
                            if slot == 1:
                                journal_data_list[-1]['tracking1_id'] = tracking_obj.id
                            elif slot == 2:
                                journal_data_list[-1]['tracking2_id'] = tracking_obj.id
                
                # Inherit tracking from transaction source if journal line has no tracking
                # This handles cases where Xero's Journals API doesn't carry tracking from
                # the source document (e.g., invoice tracking not on bank transaction journals)
                if (journal_data_list[-1]['tracking1_id'] is None and 
                    source_transactions_obj is not None):
                    try:
                        txn_collection = source_transactions_obj.collection or {}
                        txn_line_items = txn_collection.get('LineItems', [])
                        # Match by AccountCode (account_instance.code)
                        acct_code = account_instance.code if account_instance else None
                        for txn_line in txn_line_items:
                            txn_tracking = txn_line.get('Tracking', [])
                            if txn_tracking and txn_line.get('AccountCode') == acct_code:
                                # Found matching line with tracking - use category_slot from API order
                                t_idx = 0
                                for tt in txn_tracking:
                                    option_name = tt.get('Option', '')
                                    for tk_id, tk_obj in trackings_dict.items():
                                        if tk_obj.option == option_name:
                                            slot = _resolve_tracking_slot(tk_obj, organisation, t_idx)
                                            if slot == 1:
                                                journal_data_list[-1]['tracking1_id'] = tk_obj.id
                                            elif slot == 2:
                                                journal_data_list[-1]['tracking2_id'] = tk_obj.id
                                            break
                                    t_idx += 1
                                break  # Use first matching line - exit txn_line loop
                        
                        # If still no tracking and there's only one line item with tracking, use it
                        if journal_data_list[-1]['tracking1_id'] is None:
                            for txn_line in txn_line_items:
                                txn_tracking = txn_line.get('Tracking', [])
                                if txn_tracking:
                                    t_idx = 0
                                    for tt in txn_tracking:
                                        option_name = tt.get('Option', '')
                                        for tk_id, tk_obj in trackings_dict.items():
                                            if tk_obj.option == option_name:
                                                slot = _resolve_tracking_slot(tk_obj, organisation, t_idx)
                                                if slot == 1:
                                                    journal_data_list[-1]['tracking1_id'] = tk_obj.id
                                                elif slot == 2:
                                                    journal_data_list[-1]['tracking2_id'] = tk_obj.id
                                                break
                                        t_idx += 1
                                    break  # Use first line with tracking
                    except Exception as e:
                        logger.warning(f"Error inheriting tracking from transaction source: {e}")
            
            # Debug: Print summary for this journal
            if is_manual_journal:
                lines_added = sum(1 for entry in journal_data_list if entry.get('journal_source') == j_obj)
                print(f"[PROCESS] Manual journal {j_obj.journal_id}: Added {lines_added} lines to processing list (out of {journal_lines_count} total lines)")
            
            journals_to_mark_processed.append(j_obj)

        # Debug: Print total summary before processing
        manual_journal_lines = sum(1 for entry in journal_data_list if entry.get('journal_type') == 'manual_journal')
        regular_journal_lines = sum(1 for entry in journal_data_list if entry.get('journal_type') == 'journal')
        print(f"[PROCESS] Total journal lines to process: {manual_journal_lines} manual, {regular_journal_lines} regular, {len(journal_data_list)} total")

        # Fetch existing journals in one query
        existing_journals = {
            j.journal_id: j for j in XeroJournals.objects.filter(
                organisation=organisation,
                journal_id__in=all_journal_line_ids
            )
        }
        
        to_create = []
        to_update = []
        
        for journal_data in journal_data_list:
            line_id = journal_data['line_id']
            journal_type_from_data = journal_data['journal_type']
            # Debug: Print journal type being set
            if journal_type_from_data == 'manual_journal':
                print(f"[PROCESS] Setting journal_type='manual_journal' for line_id={line_id}")
            
            if line_id in existing_journals:
                # Update existing
                existing = existing_journals[line_id]
                existing.journal_number = journal_data['journal_number']
                existing.account = journal_data['account']
                existing.date = journal_data['date']
                existing.description = journal_data['description']
                existing.reference = journal_data['reference']
                existing.amount = journal_data['amount']
                existing.tax_amount = journal_data['tax_amount']
                existing.journal_source = journal_data['journal_source']
                existing.transaction_source = journal_data['transaction_source']
                existing.journal_type = journal_type_from_data  # Update journal type
                if existing.journal_type != journal_type_from_data:
                    print(f"[PROCESS] WARNING: journal_type mismatch for {line_id}. Existing: {existing.journal_type}, Setting: {journal_type_from_data}")
                # Always set both to fix wrongly-assigned tracking (e.g. category2 in tracking1)
                existing.tracking1_id = journal_data['tracking1_id']
                existing.tracking2_id = journal_data['tracking2_id']
                if journal_data.get('contact') is not None:
                    existing.contact = journal_data['contact']
                to_update.append(existing)
            else:
                # Create new
                journal_obj = XeroJournals(
                    organisation=organisation,
                    journal_id=line_id,
                    journal_number=journal_data['journal_number'],
                    journal_type=journal_type_from_data,  # Include journal type
                    account=journal_data['account'],
                    date=journal_data['date'],
                    description=journal_data['description'],
                    reference=journal_data['reference'],
                    amount=journal_data['amount'],
                    tax_amount=journal_data['tax_amount'],
                    journal_source=journal_data['journal_source'],
                    transaction_source=journal_data['transaction_source'],
                )
                if journal_type_from_data == 'manual_journal':
                    print(f"[PROCESS] Creating new journal with journal_type='manual_journal' for line_id={line_id}")
                if journal_data['tracking1_id']:
                    journal_obj.tracking1_id = journal_data['tracking1_id']
                if journal_data['tracking2_id']:
                    journal_obj.tracking2_id = journal_data['tracking2_id']
                if journal_data.get('contact'):
                    journal_obj.contact = journal_data['contact']
                to_create.append(journal_obj)
                if journal_type_from_data == 'manual_journal':
                    print(f"[PROCESS] Creating new manual journal with journal_type='manual_journal' for line_id={line_id}, amount={journal_data['amount']}")
        
        # Debug: Print counts before bulk operations
        manual_to_create = sum(1 for j in to_create if j.journal_type == 'manual_journal')
        manual_to_update = sum(1 for j in to_update if j.journal_type == 'manual_journal')
        print(f"[PROCESS] Bulk operations: {manual_to_create} manual journals to create, {manual_to_update} manual journals to update")
        print(f"[PROCESS] Bulk operations: {len(to_create)} total to create, {len(to_update)} total to update")
        
        # Bulk create and update
        if to_create:
            print(f"[PROCESS] Bulk creating {len(to_create)} journal entries...")
            try:
                # Batch bulk_create to avoid database locks and timeouts with large datasets
                batch_size = 5000
                total_created = 0
                for i in range(0, len(to_create), batch_size):
                    batch = to_create[i:i + batch_size]
                    created = XeroJournals.objects.bulk_create(batch, ignore_conflicts=True)
                    total_created += len(created)
                    print(f"[PROCESS] Bulk created batch {i // batch_size + 1}: {len(created)} entries (total: {total_created}/{len(to_create)})")
                
                print(f"[PROCESS] Successfully bulk created {total_created} journal entries")
            except Exception as e:
                print(f"[PROCESS] ERROR during bulk_create: {str(e)}")
                logger.error(f"Failed to bulk create journals: {str(e)}", exc_info=True)
                raise
        
        if to_update:
            print(f"[PROCESS] Bulk updating {len(to_update)} journal entries...")
            try:
                # Batch bulk_update to avoid database locks and timeouts with large datasets
                batch_size = 5000
                total_updated = 0
                for i in range(0, len(to_update), batch_size):
                    batch = to_update[i:i + batch_size]
                    XeroJournals.objects.bulk_update(batch, [
                        'journal_number', 'journal_type', 'account', 'date', 'description', 'reference',
                        'amount', 'tax_amount', 'journal_source', 'transaction_source',
                        'contact', 'tracking1', 'tracking2'
                    ])
                    total_updated += len(batch)
                    print(f"[PROCESS] Bulk updated batch {i // batch_size + 1}: {len(batch)} entries (total: {total_updated}/{len(to_update)})")
                
                print(f"[PROCESS] Successfully bulk updated {total_updated} journal entries")
            except Exception as e:
                print(f"[PROCESS] ERROR during bulk_update: {str(e)}")
                logger.error(f"Failed to bulk update journals: {str(e)}", exc_info=True)
                raise
        
        # Mark journals as processed in bulk
        if journals_to_mark_processed:
            manual_processed = sum(1 for j in journals_to_mark_processed if j.journal_type == 'manual_journal')
            print(f"[PROCESS] Marking {manual_processed} manual journals and {len(journals_to_mark_processed) - manual_processed} regular journals as processed")
            XeroJournalsSource.objects.filter(
                id__in=[j.id for j in journals_to_mark_processed]
            ).update(processed=True)
            print(f"[PROCESS] Successfully marked {len(journals_to_mark_processed)} journals as processed")

        # Final summary
        result_queryset = XeroJournals.objects.filter(organisation=organisation, journal_id__in=all_journal_line_ids)
        manual_in_result = result_queryset.filter(journal_type='manual_journal').count()
        regular_in_result = result_queryset.filter(journal_type='journal').count()
        print(f"[PROCESS] Final result: {manual_in_result} manual journals, {regular_in_result} regular journals in database")
        
        return result_queryset


class XeroJournalsSource(models.Model):
    JOURNAL_TYPE_CHOICES = [
        ('journal', 'Journal'),
        ('manual_journal', 'Manual Journal'),
    ]
    
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='journals_sources')
    journal_id = models.CharField(max_length=51)
    journal_number = models.IntegerField()
    journal_type = models.CharField(max_length=20, choices=JOURNAL_TYPE_CHOICES, default='journal', help_text="Type of journal: regular journal or manual journal")
    collection = models.JSONField(blank=True, null=True)
    processed = models.BooleanField(default=False)

    objects = XeroJournalsSourceManager()

    class Meta:
        unique_together = [('organisation', 'journal_id', 'journal_type')]  # Include journal_type in unique constraint
        ordering = ['organisation', 'journal_number']
        indexes = [
            models.Index(fields=['organisation', 'processed'], name='jrnl_src_org_proc_idx'),
            models.Index(fields=['organisation', 'journal_number'], name='jrnl_src_org_num_idx'),
            models.Index(fields=['organisation', 'journal_type'], name='jrnl_src_org_type_idx'),
        ]

    def __str__(self):
        journal_date = self.collection.get("JournalDate", "N/A") if self.collection else "N/A"
        return f'{self.organisation.tenant_name}: {self.journal_type} {self.journal_number} - {journal_date}'


from django.db.models import Func


class Month(Func):
    function = 'EXTRACT'
    template = '%(function)s(MONTH from %(expressions)s)'
    output_field = models.IntegerField()


class Year(Func):
    function = 'EXTRACT'
    template = '%(function)s(YEAR from %(expressions)s)'
    output_field = models.IntegerField()


class XeroJournalsManager(models.Manager):
    def get_account_balances(self, organisation, date_from=None, exclude_manual_journals=False):
        """
        Aggregate journals by account, year, month, contact, and tracking categories.
        Used to build trail balance: per account, per contact, per tracking1/tracking2, per period.
        Contact comes from journal.contact (manual journals) or transaction_source.contact (invoices, bank, etc.).
        Tracking comes from journal line tracking (invoices, bank transactions, credit notes, manual journals).
        
        Args:
            organisation: XeroTenant instance
            date_from: Optional datetime to filter journals from this date onwards
            exclude_manual_journals: If True, exclude manual journals from aggregation
        
        Returns:
            QuerySet of aggregated journal data
        """
        from django.db.models import Sum, F
        from django.db.models.functions import Coalesce
        
        # Aggregate journals by account, year, month, contact, and tracking categories.
        # Contact: from direct contact (manual journals) or transaction_source (transaction lines).
        qs = self.filter(organisation=organisation)
        
        # Exclude manual journals if requested
        if exclude_manual_journals:
            qs = qs.exclude(journal_type='manual_journal')
        
        # Add date filter if provided (for incremental updates)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        
        # Use contact_id_value to avoid conflicting with model field 'contact'
        qs = qs.annotate(
            month=Month('date'),
            year=Year('date'),
            contact_id_value=Coalesce(F('contact_id'), F('transaction_source__contact_id')),
        ).values("account", "year", "month", "contact_id_value", "tracking1", "tracking2").order_by().annotate(
            amount=Sum("amount"),
        )
        return qs


class XeroJournals(models.Model):
    JOURNAL_TYPE_CHOICES = [
        ('journal', 'Journal'),
        ('manual_journal', 'Manual Journal'),
    ]
    
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='journals')
    journal_id = models.CharField(max_length=200)
    journal_number = models.IntegerField()
    journal_type = models.CharField(max_length=20, choices=JOURNAL_TYPE_CHOICES, default='journal', help_text="Type of journal: regular journal or manual journal")
    account = models.ForeignKey(XeroAccount, on_delete=models.CASCADE, related_name='journals', to_field='account_id')
    transaction_source = models.ForeignKey(
        XeroTransactionSource,
        on_delete=models.CASCADE,
        related_name='journals',
        to_field='transactions_id',
        blank=True,
        null=True
    )
    journal_source = models.ForeignKey(XeroJournalsSource, on_delete=models.CASCADE, related_name='journals',
                                       blank=True, null=True)
    contact = models.ForeignKey(
        XeroContacts,
        on_delete=models.DO_NOTHING,
        related_name='journals',
        to_field='contacts_id',
        blank=True,
        null=True,
        help_text='Contact/customer (set for manual journals from API; transaction lines get it via transaction_source)',
    )
    date = models.DateTimeField()
    tracking1 = models.ForeignKey(XeroTracking, on_delete=models.DO_NOTHING, related_name='journals_track1', blank=True,
                                  null=True)
    tracking2 = models.ForeignKey(XeroTracking, on_delete=models.DO_NOTHING, related_name='journals_track2', blank=True,
                                  null=True)
    description = models.TextField(blank=True)
    reference = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=30, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=30, decimal_places=2)

    objects = XeroJournalsManager()

    class Meta:
        unique_together = [('organisation', 'journal_id')]
        ordering = ['organisation', 'date', 'journal_number']
        indexes = [
            models.Index(fields=['organisation', 'date'], name='journals_org_date_idx'),
            models.Index(fields=['organisation', 'account'], name='journals_org_acc_idx'),
            models.Index(fields=['organisation', 'date', 'account'], name='journals_org_dt_acc_idx'),
            models.Index(fields=['date'], name='journals_date_idx'),
            models.Index(fields=['organisation', 'transaction_source'], name='journals_org_txn_idx'),
            models.Index(fields=['organisation', 'journal_type'], name='journals_org_type_idx'),
        ]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.journal_type} {self.date} {self.journal_number} {self.reference} {self.description}'
