from django.db import models
from django_pandas.managers import DataFrameManager
import datetime
import pandas as pd
import logging
from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount, XeroContacts, XeroTracking

logger = logging.getLogger(__name__)


class XeroTrailBalanceManager(DataFrameManager):
    def consolidate_journals(self, organisation, exclude_manual_journals=False,
                             affected_periods=None):
        """
        Consolidate journals into trail balance using a single SQL INSERT...SELECT.

        Args:
            organisation: XeroTenant instance
            exclude_manual_journals: If True, exclude manual journals from aggregation
            affected_periods: list of (year, month) tuples for incremental mode.
                If None, does a full rebuild (deletes all then re-inserts).
        """
        from django.db import connection

        tenant_id = organisation.tenant_id
        fiscal_start = organisation.get_fiscal_year_start_month()

        # --- Delete phase ---
        if affected_periods:
            print(f'[CONSOLIDATE] Incremental: rebuilding {len(affected_periods)} periods')
            for year, month in affected_periods:
                deleted = self.filter(organisation=organisation, year=year, month=month).delete()[0]
                if deleted:
                    logger.info(f"Deleted {deleted} trail balance records for {year}-{month:02d}")
        else:
            deleted = self.filter(organisation=organisation).delete()[0]
            print(f'[CONSOLIDATE] Full rebuild: deleted {deleted} existing records')

        # --- Build WHERE clause fragments ---
        params = [fiscal_start, fiscal_start, fiscal_start, fiscal_start, tenant_id]
        where_extra = ""

        if exclude_manual_journals:
            where_extra += " AND j.journal_type != 'manual_journal'"

        if affected_periods:
            period_clauses = []
            for year, month in affected_periods:
                period_clauses.append(
                    "(EXTRACT(YEAR FROM j.date)::int = %s AND EXTRACT(MONTH FROM j.date)::int = %s)"
                )
                params.extend([year, month])
            where_extra += " AND (" + " OR ".join(period_clauses) + ")"

        sql = f"""
            INSERT INTO xero_cube_xerotrailbalance
                (organisation_id, account_id, date, year, month,
                 fin_year, fin_period,
                 contact_id, tracking1_id, tracking2_id,
                 amount, tax_amount, balance_to_date)
            SELECT
                j.organisation_id,
                j.account_id,
                make_date(EXTRACT(YEAR FROM j.date)::int, EXTRACT(MONTH FROM j.date)::int, 1),
                EXTRACT(YEAR FROM j.date)::int,
                EXTRACT(MONTH FROM j.date)::int,
                CASE
                    WHEN EXTRACT(MONTH FROM j.date) >= %s
                        THEN EXTRACT(YEAR FROM j.date)::int
                    ELSE EXTRACT(YEAR FROM j.date)::int - 1
                END,
                CASE
                    WHEN EXTRACT(MONTH FROM j.date) >= %s
                        THEN EXTRACT(MONTH FROM j.date)::int - %s + 1
                    ELSE EXTRACT(MONTH FROM j.date)::int + (12 - %s) + 1
                END,
                COALESCE(j.contact_id, ts.contact_id),
                j.tracking1_id,
                j.tracking2_id,
                SUM(j.amount),
                SUM(j.tax_amount),
                NULL
            FROM xero_data_xerojournals j
            LEFT JOIN xero_data_xerotransactionsource ts
                ON j.transaction_source_id = ts.transactions_id
            WHERE j.organisation_id = %s
                {where_extra}
            GROUP BY
                j.organisation_id, j.account_id,
                EXTRACT(YEAR FROM j.date), EXTRACT(MONTH FROM j.date),
                COALESCE(j.contact_id, ts.contact_id),
                j.tracking1_id, j.tracking2_id
            HAVING SUM(j.amount) != 0
        """

        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            total_created = cursor.rowcount

        print(f'[CONSOLIDATE] Inserted {total_created} trail balance records (single SQL statement)')
        logger.info(f"Inserted {total_created} trail balance records via INSERT...SELECT")

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
    tax_amount = models.DecimalField(max_digits=30, decimal_places=2, default=0, blank=True,
                                     help_text="Sum of journal line tax_amount for this account/period/contact/tracking.")
    balance_to_date = models.DecimalField(max_digits=30, decimal_places=2, null=True, blank=True,
                                          help_text="Balance to date (YTD) for balance sheet accounts (ASSET, LIABILITY, EQUITY) - cumulative sum of all periods up to and including current month")

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
