from django.db import models
from django_pandas.managers import DataFrameManager
from apps.xero.xero_core.models import XeroTenant
import logging

logger = logging.getLogger(__name__)


class XeroBusinessUnits(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='business_units')
    division_code = models.CharField(max_length=1, blank=True, null=True)
    business_unit_code = models.CharField(max_length=1)
    division_description = models.CharField(max_length=100, blank=True, null=True)
    business_unit_description = models.CharField(max_length=100)

    class Meta:
        unique_together = [('organisation', 'business_unit_code', 'division_code')]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.business_unit_code}{self.division_code} - {self.business_unit_description} {self.division_description}'


class XeroAccountManager(DataFrameManager):
    def create_accounts(self, organisation, response):
        from apps.xero.xero_metadata.models import XeroBusinessUnits
        
        # Pre-fetch all business units into a dictionary for O(1) lookup
        business_units = XeroBusinessUnits.objects.filter(organisation=organisation)
        bu_dict = {}
        for bu in business_units:
            key = (bu.business_unit_code, bu.division_code)
            bu_dict[key] = bu
        
        # Fetch existing accounts in one query
        account_ids = [r['AccountID'] for r in response]
        existing_accounts = {
            acc.account_id: acc for acc in self.filter(
                organisation=organisation,
                account_id__in=account_ids
            )
        }
        
        to_create = []
        to_update = []
        
        for r in response:
            code = r.get('Code', '')
            bu = None
            if code and len(code) >= 2:
                bu_key = (code[:1], code[1:2])
                bu = bu_dict.get(bu_key)
            
            account_id = r['AccountID']
            account_data = {
                'organisation': organisation,
                'business_unit': bu,
                'account_id': account_id,
                'grouping': r.get('Class', ''),
                'code': code,
                'name': r.get('Name', ''),
                'reporting_code': r.get('ReportingCode', ''),
                'reporting_code_name': r.get('ReportingCodeName', ''),
                'type': r.get('Type', ''),
                'collection': r,
            }
            
            if account_id in existing_accounts:
                # Update existing account
                existing = existing_accounts[account_id]
                for key, value in account_data.items():
                    if key != 'account_id':  # Don't update primary key
                        setattr(existing, key, value)
                to_update.append(existing)
            else:
                # Create new account
                to_create.append(XeroAccount(**account_data))
        
        # Bulk create and update
        if to_create:
            self.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            self.bulk_update(to_update, [
                'organisation', 'business_unit', 'grouping', 'code', 'name',
                'reporting_code', 'reporting_code_name', 'type', 'collection'
            ])


class XeroAccount(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='accounts')
    account_id = models.CharField(primary_key=True, max_length=40, unique=True)
    business_unit = models.ForeignKey(XeroBusinessUnits, on_delete=models.DO_NOTHING, null=True, blank=True)
    reporting_code = models.TextField(blank=True)
    reporting_code_name = models.TextField(blank=True)
    bank_account_number = models.CharField(max_length=40, blank=True, null=True)
    grouping = models.CharField(max_length=30, blank=True)
    code = models.CharField(max_length=10, blank=True)
    name = models.CharField(max_length=150, blank=True)
    type = models.CharField(max_length=30, blank=True)
    collection = models.JSONField(blank=True, null=True)
    attr_entry_type = models.CharField(max_length=30, blank=True, null=True)
    attr_occurrence = models.CharField(max_length=30, blank=True, null=True)

    objects = XeroAccountManager()

    class Meta:
        unique_together = [('organisation', 'account_id')]
        ordering = ['organisation', 'code']
        indexes = [
            models.Index(fields=['organisation', 'code'], name='acc_org_code_idx'),
            models.Index(fields=['organisation', 'type'], name='acc_org_type_idx'),
            models.Index(fields=['organisation', 'business_unit'], name='acc_org_bu_idx'),
        ]

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.code} {self.type} {self.name}'


class XeroTrackingModelManager(models.Manager):
    def create_tracking_categories_from_xero(self, organisation, xero_response):
        # Collect all tracking options first
        tracking_options = []
        option_ids = []
        
        # Store first two category IDs on tenant (Xero allows max 2 active; stable even if names change)
        category_ids = []
        for idx, tc in enumerate(xero_response):
            slot = idx + 1  # 1-based: first = slot 1, second = slot 2
            tracking_category_id = tc.get('TrackingCategoryID')
            if idx < 2 and tracking_category_id:
                category_ids.append((idx + 1, tracking_category_id))
            tracking_category_name = tc.get('Name', 'Unnamed Category')
            for option in tc.get('Options', []):
                tracking_option_id = option.get('TrackingOptionID')
                option_ids.append(tracking_option_id)
                tracking_options.append({
                    'option_id': tracking_option_id,
                    'name': tracking_category_name,
                    'option': option.get('Name', 'Unnamed Option'),
                    'collection': option,
                    'tracking_category_id': tracking_category_id,
                    'category_slot': slot,  # fallback for legacy
                })
        
        # Fetch existing trackings in one query
        existing_trackings = {
            t.option_id: t for t in self.filter(
                organisation=organisation,
                option_id__in=option_ids
            )
        }
        
        to_create = []
        to_update = []
        
        for opt_data in tracking_options:
            option_id = opt_data['option_id']
            if option_id in existing_trackings:
                existing = existing_trackings[option_id]
                existing.name = opt_data['name']
                existing.option = opt_data['option']
                existing.collection = opt_data['collection']
                existing.tracking_category_id = opt_data.get('tracking_category_id')
                existing.category_slot = opt_data.get('category_slot')
                to_update.append(existing)
            else:
                to_create.append(XeroTracking(
                    organisation=organisation,
                    option_id=option_id,
                    name=opt_data['name'],
                    option=opt_data['option'],
                    collection=opt_data['collection'],
                    tracking_category_id=opt_data.get('tracking_category_id'),
                    category_slot=opt_data.get('category_slot'),
                ))
        
        if to_create:
            self.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            self.bulk_update(to_update, ['name', 'option', 'collection', 'tracking_category_id', 'category_slot'])
        
        # Update tenant's category ID mapping (first two only; stable across renames)
        if category_ids:
            for slot, cid in category_ids:
                if slot == 1:
                    organisation.tracking_category_1_id = cid
                elif slot == 2:
                    organisation.tracking_category_2_id = cid
            organisation.save(update_fields=['tracking_category_1_id', 'tracking_category_2_id'])
        
        logger.info(
            f"Updated {self.filter(organisation=organisation).count()} tracking categories for {organisation.tenant_id}")
        return self


class XeroTracking(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='tracking')
    option_id = models.TextField(max_length=1024)
    name = models.TextField(max_length=1024, blank=True, null=True)
    option = models.TextField(max_length=1024, blank=True, null=True)
    collection = models.JSONField(blank=True, null=True)
    tracking_category_id = models.CharField(
        max_length=64, blank=True, null=True,
        help_text='Xero TrackingCategoryID - stable identifier, survives category renames.'
    )
    category_slot = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Deprecated: use org tracking_category_1_id/2_id + tracking_category_id. Kept for fallback.'
    )

    objects = XeroTrackingModelManager()

    class Meta:
        unique_together = [('organisation', 'option_id')]
        ordering = ['organisation', 'option']

    def __str__(self):
        return f'{self.id} - {self.organisation.tenant_name}: {self.name} {self.option}'


class XeroContactsModelManager(models.Manager):
    def create_contacts_from_xero(self, organisation, xero_response):
        # Fetch existing contacts in one query
        contact_ids = [r['ContactID'] for r in xero_response]
        existing_contacts = {
            c.contacts_id: c for c in self.filter(
                organisation=organisation,
                contacts_id__in=contact_ids
            )
        }
        
        to_create = []
        to_update = []
        
        for r in xero_response:
            contact_id = r['ContactID']
            name = r.get('Name', '')
            
            if contact_id in existing_contacts:
                # Update existing
                existing = existing_contacts[contact_id]
                existing.name = name
                existing.collection = r
                to_update.append(existing)
            else:
                # Create new
                to_create.append(XeroContacts(
                    organisation=organisation,
                    contacts_id=contact_id,
                    name=name,
                    collection=r
                ))
        
        # Bulk create and update
        if to_create:
            self.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            self.bulk_update(to_update, ['name', 'collection'])
        
        return self


class XeroContacts(models.Model):
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='contacts')
    contacts_id = models.CharField(max_length=55, unique=True, primary_key=True)
    name = models.TextField()
    collection = models.JSONField(blank=True, null=True)

    objects = XeroContactsModelManager()

    class Meta:
        unique_together = [('organisation', 'contacts_id')]
        ordering = ['organisation', 'name']

    def __str__(self):
        return f'{self.organisation.tenant_name}: {self.name}'
