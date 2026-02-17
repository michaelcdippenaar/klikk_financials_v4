from django.db import models
from django_pandas.managers import DataFrameManager
import datetime
import pandas as pd
import logging
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount, XeroContacts, XeroTracking
from apps.xero.xero_metadata.utils import fiscal_year_to_financial_year, fiscal_month_to_financial_period

logger = logging.getLogger(__name__)


class XeroTrailBalanceManager(DataFrameManager):
    def consolidate_journals(self, organisation, journals, last_update_date=None):
        """
        Consolidate journals into trail balance.
        
        Args:
            organisation: XeroTenant instance
            journals: QuerySet or list of journal data from get_account_balances()
            last_update_date: Optional datetime for incremental updates. If provided,
                            only deletes and rebuilds affected periods instead of all data.
        """
        # For incremental updates, only delete affected periods
        if last_update_date:
            print('Start Incremental Consolidation for Trail Balance')
            # Get affected year/month combinations from new journals
            affected_periods = set()
            for data in journals:
                affected_periods.add((data['year'], data['month']))
            
            # Delete only affected periods
            for year, month in affected_periods:
                deleted_count = self.filter(
                    organisation=organisation,
                    year=year,
                    month=month
                ).delete()[0]
                if deleted_count > 0:
                    logger.info(f"Deleted {deleted_count} trail balance records for {year}-{month:02d}")
        else:
            # Full rebuild - delete all
            self.filter(organisation=organisation).delete()
            print('Start Full Consolidation for Trail Balance Creation')
        
        # Pre-fetch all related objects into dictionaries for O(1) lookup
        accounts_dict = {
            acc.account_id: acc for acc in XeroAccount.objects.filter(
                organisation=organisation
            ).only('account_id', 'code', 'name', 'type', 'grouping', 'business_unit_id')
        }
        # Key by both raw and str() so lookup works whether aggregation returns string or UUID
        contacts_dict = {}
        for c in XeroContacts.objects.filter(organisation=organisation).only('contacts_id', 'name'):
            contacts_dict[c.contacts_id] = c
            contacts_dict[str(c.contacts_id)] = c
        trackings_dict = {
            t.id: t for t in XeroTracking.objects.filter(
                organisation=organisation
            ).only('id', 'option')
        }
        
        print(f'[CONSOLIDATE] Pre-fetched {len(accounts_dict)} accounts, {len(contacts_dict)} contacts, {len(trackings_dict)} trackings')
        logger.info(f"Pre-fetched {len(accounts_dict)} accounts, {len(contacts_dict)} contacts, {len(trackings_dict)} trackings")
        
        lst = []
        skipped_accounts = 0
        skipped_zero_amounts = 0

        for data in journals:
            logger.debug(f"Processing journal data: {data}")
            
            # Use dictionary lookups (contact_id_value from get_account_balances; normalize to str for lookup)
            contact = None
            contact_id = data.get('contact_id_value') or data.get('contact')
            if contact_id:
                contact = contacts_dict.get(contact_id) or contacts_dict.get(str(contact_id).strip())
                if not contact:
                    logger.warning(f"Contact {contact_id!r} not found in contacts_dict (len={len(contacts_dict)}), setting to None")

            account = accounts_dict.get(data['account'])
            if not account:
                skipped_accounts += 1
                logger.warning(f"Skipping trail balance entry: Account {data['account']} not found")
                continue

            # Handle tracking1 and tracking2 using dictionary lookups
            tracking1 = None
            if data.get('tracking1'):
                tracking1 = trackings_dict.get(data['tracking1'])

            tracking2 = None
            if data.get('tracking2'):
                tracking2 = trackings_dict.get(data['tracking2'])

            date = datetime.datetime(data['year'], data['month'], 1)
            fin_year = fiscal_year_to_financial_year(data['year'], data['month'], 6)
            fin_period = fiscal_month_to_financial_period(data['month'], 6)
            
            if data['amount'] != 0:
                lst.append(self.model(
                    organisation=organisation,
                    account=account,
                    date=date,
                    year=data['year'],
                    month=data['month'],
                    fin_year=fin_year,
                    fin_period=fin_period,
                    contact=contact,
                    tracking1=tracking1,
                    tracking2=tracking2,
                    amount=data['amount']
                ))
            else:
                skipped_zero_amounts += 1
        
        print(f'[CONSOLIDATE] Processed {len(journals)} journal aggregates: {len(lst)} to create, {skipped_accounts} skipped (account not found), {skipped_zero_amounts} skipped (zero amount)')
        logger.info(f"Processed {len(journals)} journal aggregates: {len(lst)} to create, {skipped_accounts} skipped (account not found), {skipped_zero_amounts} skipped (zero amount)")
        
        if not lst:
            logger.warning("No trail balance records to create!")
            print('[CONSOLIDATE] WARNING: No trail balance records to create!')
            return self.filter(organisation=organisation)
        
        # Batch bulk_create in chunks to avoid memory issues with very large datasets
        batch_size = 5000
        total_created = 0
        for i in range(0, len(lst), batch_size):
            batch = lst[i:i + batch_size]
            created = self.bulk_create(batch, ignore_conflicts=True)
            total_created += len(created)
            print(f'[CONSOLIDATE] Created batch {i // batch_size + 1}: {len(created)} records (total: {total_created}/{len(lst)})')
        
        logger.info(f"Created {total_created} trail balance records in {len(lst) // batch_size + 1} batches")
        print(f'[CONSOLIDATE] Successfully created {total_created} trail balance records')
        
        return self.filter(organisation=organisation)


class XeroTrailBalance(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='trail_balances')
    account = models.ForeignKey(XeroAccount, on_delete=models.CASCADE, related_name='trail_balances')
    date = models.DateField(blank=True, null=True)
    year = models.IntegerField()
    month = models.IntegerField()
    fin_year = models.IntegerField()
    fin_period = models.IntegerField(blank=True, null=True)
    contact = models.ForeignKey(XeroContacts, on_delete=models.DO_NOTHING, null=True, blank=True,
                                related_name='trail_balances')
    tracking1 = models.ForeignKey(XeroTracking, on_delete=models.DO_NOTHING, related_name='trail_balances_track1',
                                  blank=True,
                                  null=True)
    tracking2 = models.ForeignKey(XeroTracking, on_delete=models.DO_NOTHING, related_name='trail_balances_track2',
                                  blank=True,
                                  null=True)
    amount = models.DecimalField(max_digits=30, decimal_places=2)
    balance_to_date = models.DecimalField(max_digits=30, decimal_places=2, null=True, blank=True,
                                          help_text="Balance to date for P&L accounts (REVENUE/EXPENSE) - cumulative sum of all previous months up to and including current month")

    objects = XeroTrailBalanceManager()

    class Meta:
        ordering = ['organisation', 'account', 'year', 'month', 'contact']
        indexes = [
            models.Index(fields=['organisation', 'year', 'month'], name='tb_org_ym_idx'),
            models.Index(fields=['organisation', 'account', 'year', 'month'], name='tb_org_acc_ym_idx'),
            models.Index(fields=['account', 'contact'], name='tb_acc_contact_idx'),
            models.Index(fields=['organisation', 'account', 'contact'], name='tb_org_acc_ct_idx'),
            models.Index(fields=['year', 'month'], name='tb_ym_idx'),
        ]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.year} {self.month} {self.account} {self.contact} {self.amount}'


class XeroPnlByTracking(models.Model):
    """
    Stores Xero P&L report values per tracking option, per account, per month.
    Used to compare Xero's official P&L (filtered by tracking) against the
    constructed trail balance.
    """
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='pnl_by_tracking')
    tracking = models.ForeignKey(XeroTracking, on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='pnl_by_tracking',
                                 help_text="Tracking option. NULL means unfiltered/overall P&L.")
    account = models.ForeignKey(XeroAccount, on_delete=models.CASCADE, related_name='pnl_by_tracking')
    year = models.IntegerField()
    month = models.IntegerField()
    xero_amount = models.DecimalField(max_digits=30, decimal_places=2, default=0)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('organisation', 'tracking', 'account', 'year', 'month')]
        indexes = [
            models.Index(fields=['organisation', 'tracking', 'account', 'year', 'month'],
                         name='pnl_trk_org_trk_acc_ym_idx'),
        ]

    def __str__(self):
        trk = self.tracking.option if self.tracking else 'Overall'
        return f'{self.organisation.tenant_name}: {trk} / {self.account.code} {self.year}-{self.month:02d} = {self.xero_amount}'


class XeroBalanceSheetManager(DataFrameManager):
    def consolidate_balance_sheet(self, organisation):
        self.filter(organisation=organisation).delete()
        tb = XeroTrailBalance.objects.filter(organisation=organisation)
        # Explicitly order by account and contact to match DISTINCT ON
        qs = tb.order_by('account', 'contact').values("account", "contact").distinct("account", "contact")
        lst = []

        for q in qs:
            item = tb.filter(**q).order_by('date')
            if not item.exists():
                continue
            start_date = item.first().date
            end_date = datetime.datetime.now()
            daterange = pd.date_range(start_date, end_date, freq='MS')
            balance = 0

            contact = item.first().contact
            account = item.first().account

            for single_date in daterange:
                year = single_date.year
                month = single_date.month
                amount = 0
                instance = item.filter(date=single_date).first()
                if instance:
                    amount = instance.amount
                balance = amount + balance

                if balance != 0:
                    lst.append(self.model(
                        organisation=organisation,
                        year=year,
                        month=month,
                        account=account,
                        date=single_date,
                        contact=contact,
                        amount=amount,
                        balance=balance
                    ))
        self.bulk_create(lst)
        return self.filter(organisation=organisation)


class XeroBalanceSheet(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='balance_sheets')
    date = models.DateField(blank=True, null=True)
    year = models.IntegerField(blank=True, null=True)
    month = models.IntegerField(blank=True, null=True)
    account = models.ForeignKey(XeroAccount, on_delete=models.CASCADE, related_name='balance_sheets_accounts',
                                to_field='account_id')
    contact = models.ForeignKey(XeroContacts, on_delete=models.DO_NOTHING, null=True, blank=True,
                                related_name='balance_sheets')
    amount = models.DecimalField(max_digits=30, decimal_places=2)
    balance = models.DecimalField(max_digits=30, decimal_places=2)

    objects = XeroBalanceSheetManager()

    class Meta:
        unique_together = [('organisation', 'account', 'contact', 'date')]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.date} {self.account} {self.contact} {self.amount} {self.balance}'
