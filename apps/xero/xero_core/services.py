"""
Core Xero API client services.
"""
import datetime
import logging
import requests
from django.utils import timezone
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient, Configuration
from xero_python.api_client.oauth2 import OAuth2Token
from xero_python.api_client.serializer import serialize

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_auth.models import XeroClientCredentials, XeroAuthSettings, XeroTenantToken

logger = logging.getLogger(__name__)

# Cache for XeroAuthSettings to avoid repeated database queries
_auth_settings_cache = None


class TenantTokenData:
    """Simple class to hold tenant token data, mimicking XeroTenantToken interface."""
    def __init__(self, credentials, tenant_id, token_data):
        self.credentials = credentials
        self.tenant_id = tenant_id
        self.token = token_data.get('token', {})
        self.refresh_token = token_data.get('refresh_token', '')
        expires_at_str = token_data.get('expires_at', '')
        if expires_at_str:
            if isinstance(expires_at_str, str):
                from dateutil import parser
                self.expires_at = parser.parse(expires_at_str)
            else:
                self.expires_at = expires_at_str
        else:
            self.expires_at = None
        self.connected_at = token_data.get('connected_at')
        self.pk = None  # No primary key, stored in JSON
        self.tenant = None  # Will be set if needed
    
    def save(self):
        """Save token data back to credentials.tenant_tokens."""
        from django.utils import timezone
        self.credentials.set_tenant_token_data(
            tenant_id=self.tenant_id,
            token_data=self.token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at
        )
    
    def refresh_from_db(self):
        """Reload token data from credentials."""
        token_data = self.credentials.get_tenant_token_data(self.tenant_id)
        if token_data:
            self.token = token_data.get('token', {})
            self.refresh_token = token_data.get('refresh_token', '')
            expires_at_str = token_data.get('expires_at', '')
            if expires_at_str:
                if isinstance(expires_at_str, str):
                    from dateutil import parser
                    self.expires_at = parser.parse(expires_at_str)
                else:
                    self.expires_at = expires_at_str
            else:
                self.expires_at = None


def serialize_model(model):
    """
    Serialize a Xero API response object into a Python dictionary.

    Args:
        model: Xero API response object (e.g., Accounts, Journals).

    Returns:
        dict: Serialized data as a Python dictionary.
    """
    try:
        serialized = serialize(model)
        logger.debug(f"Serialized model: {type(model)} to dict")
        return serialized
    except Exception as e:
        logger.error(f"Failed to serialize model {type(model)}: {str(e)}")
        raise


class XeroApiClient:
    def __init__(self, user, tenant_id=None):
        self.user = user
        self.tenant_id = tenant_id
        self.credentials = XeroClientCredentials.objects.get(user=self.user, active=True)
        self.api_client = ApiClient(
            Configuration(
                debug=False,
                oauth2_token=OAuth2Token(
                    client_id=self.credentials.client_id,
                    client_secret=self.credentials.client_secret
                )
            ),
            pool_threads=1,
        )
        self.tenant_token = None
        if tenant_id:
            self.tenant_token = self.get_tenant_token()
            self.configure_api_client(self.tenant_token)

    def get_tenant_token(self):
        """Get tenant token data from credentials.tenant_tokens or XeroTenantToken model."""
        # Reload credentials to ensure we have latest data
        self.credentials.refresh_from_db()
        
        # First, try to get token data from JSONField
        token_data = self.credentials.get_tenant_token_data(self.tenant_id)
        
        # If not found in JSONField, check the XeroTenantToken model (backward compatibility)
        if not token_data:
            try:
                tenant = XeroTenant.objects.get(tenant_id=self.tenant_id)
                tenant_token_model = XeroTenantToken.objects.get(
                    tenant=tenant,
                    credentials=self.credentials
                )
                # Migrate token from model to JSONField for future use
                logger.info(f"Migrating token for tenant {self.tenant_id} from model to JSONField")
                expires_at = tenant_token_model.expires_at
                connected_at = tenant_token_model.connected_at
                self.credentials.set_tenant_token_data(
                    tenant_id=self.tenant_id,
                    token_data=tenant_token_model.token,
                    refresh_token=tenant_token_model.refresh_token,
                    expires_at=expires_at,
                    connected_at=connected_at
                )
                # Reload to get the migrated data
                self.credentials.refresh_from_db()
                token_data = self.credentials.get_tenant_token_data(self.tenant_id)
            except (XeroTenant.DoesNotExist, XeroTenantToken.DoesNotExist):
                pass  # Token not found in model either
        
        if not token_data:
            raise ValueError(
                f"No token found for tenant {self.tenant_id}. "
                f"Please re-authenticate this tenant through the Xero authorization flow."
            )
        
        # Create TenantTokenData object
        tenant_token = TenantTokenData(self.credentials, self.tenant_id, token_data)
        
        # Get tenant object for reference
        try:
            tenant_token.tenant = XeroTenant.objects.get(tenant_id=self.tenant_id)
        except XeroTenant.DoesNotExist:
            pass  # Tenant might not exist yet
        
        # Check if refresh token exists
        if not tenant_token.refresh_token:
            raise ValueError(
                f"No refresh token available for tenant {self.tenant_id}. "
                f"Please re-authenticate this tenant through the Xero authorization flow."
            )
        
        # Refresh token if expired during initialization
        try:
            self.refresh_token_if_expired(tenant_token)
        except ValueError as e:
            # Re-raise with context
            raise
        except Exception as e:
            raise ValueError(
                f"Failed to refresh token for tenant {self.tenant_id}: {str(e)}. "
                f"Please re-authenticate this tenant."
            ) from e
        
        # Reload from database to get updated token if it was refreshed
        tenant_token.refresh_from_db()
        return tenant_token

    def configure_api_client(self, tenant_token):
        # Store reference to self for use in closures
        api_client_instance = self
        
        @self.api_client.oauth2_token_getter
        def obtain_xero_oauth2_token():
            if not api_client_instance.tenant_token:
                raise ValueError("Tenant token not configured. Initialize XeroApiClient with tenant_id.")
            
            # Check if this is a temporary token (not saved to DB) - used during OAuth callback
            # Temporary tokens have tenant_id='temp' or don't have a primary key
            is_temp_token = (
                not hasattr(api_client_instance.tenant_token, 'pk') or 
                api_client_instance.tenant_token.pk is None or
                (hasattr(api_client_instance.tenant_token, 'tenant') and 
                 api_client_instance.tenant_token.tenant.tenant_id == 'temp')
            )
            
            if is_temp_token:
                # For temporary tokens, just return the token directly without DB operations
                return api_client_instance.tenant_token.token
            
            # For saved tokens, check expiration and refresh if needed
            current_time = timezone.now()
            expires_at = api_client_instance.tenant_token.expires_at
            
            # Only reload from DB and refresh if token is expired or expiring soon
            if expires_at and expires_at <= current_time + datetime.timedelta(seconds=30):
                try:
                    api_client_instance.tenant_token.refresh_from_db()
                    # Check again after reload (in case it was refreshed by another process)
                    if api_client_instance.tenant_token.expires_at <= current_time + datetime.timedelta(seconds=30):
                        api_client_instance.refresh_token_if_expired(api_client_instance.tenant_token)
                        # Reload after refresh to get updated token
                        api_client_instance.tenant_token.refresh_from_db()
                except Exception as e:
                    # If refresh_from_db fails (e.g., token was deleted), log and continue with in-memory token
                    logger.warning(f"Could not refresh token from DB: {str(e)}, using in-memory token")
            
            return api_client_instance.tenant_token.token

        @self.api_client.oauth2_token_saver
        def store_xero_oauth2_token(token):
            tenant_token.token = token
            tenant_token.refresh_token = token.get('refresh_token', tenant_token.refresh_token)
            tenant_token.expires_at = timezone.now() + datetime.timedelta(seconds=token.get('expires_in', 1800))
            tenant_token.save()

    def refresh_token_if_expired(self, tenant_token):
        """Check if token is expired and refresh it if needed."""
        # Add a small buffer (30 seconds) to refresh before actual expiration
        if tenant_token.expires_at and tenant_token.expires_at <= timezone.now() + datetime.timedelta(seconds=30):
            logger.info(f"Token expired or expiring soon (expires_at: {tenant_token.expires_at}), refreshing...")
            # Use cached auth settings to avoid repeated database queries
            global _auth_settings_cache
            if _auth_settings_cache is None:
                _auth_settings_cache = XeroAuthSettings.objects.first()
            auth_settings = _auth_settings_cache
            
            if not auth_settings:
                raise ValueError("XeroAuthSettings not configured in the database")
            credentials = tenant_token.credentials
            refresh_url = auth_settings.refresh_url
            # Use Basic authentication like the callback does
            import base64
            tokenb4 = f"{credentials.client_id}:{credentials.client_secret}"
            basic_token = base64.urlsafe_b64encode(tokenb4.encode()).decode()
            headers = {
                'Authorization': f"Basic {basic_token}",
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            data = {
                "grant_type": "refresh_token",
                "refresh_token": tenant_token.refresh_token
            }
            try:
                response = requests.post(refresh_url, headers=headers, data=data)
                response.raise_for_status()
                new_token = response.json()
                # Update the specific tenant token
                tenant_token.token = new_token
                tenant_token.refresh_token = new_token.get('refresh_token', tenant_token.refresh_token)
                tenant_token.expires_at = timezone.now() + datetime.timedelta(seconds=new_token.get('expires_in', 1800))
                tenant_token.save()
                tenant_id_display = tenant_token.tenant_id if hasattr(tenant_token, 'tenant_id') else (tenant_token.tenant.tenant_id if tenant_token.tenant else 'unknown')
                logger.info(f"Successfully refreshed token for tenant {tenant_id_display}")
            except requests.HTTPError as e:
                # Log detailed error information
                tenant_id_display = tenant_token.tenant_id if hasattr(tenant_token, 'tenant_id') else (tenant_token.tenant.tenant_id if tenant_token.tenant else 'unknown')
                error_details = {
                    'status_code': e.response.status_code if e.response else None,
                    'url': refresh_url,
                    'tenant_id': tenant_id_display,
                    'error': str(e)
                }
                try:
                    if e.response:
                        error_details['response_body'] = e.response.text[:500]  # Limit response body length
                        error_details['response_json'] = e.response.json() if e.response.headers.get('content-type', '').startswith('application/json') else None
                except:
                    pass  # Ignore errors parsing response
                
                logger.error(
                    f"Failed to refresh token for tenant {tenant_id_display}: "
                    f"HTTP {error_details['status_code']} - {error_details.get('response_body', str(e))}",
                    extra=error_details
                )
                
                # Raise a more descriptive error
                error_msg = f"Token refresh failed for tenant {tenant_id_display}"
                if error_details.get('response_json') and 'error' in error_details['response_json']:
                    error_msg += f": {error_details['response_json']['error']}"
                    if 'error_description' in error_details['response_json']:
                        error_msg += f" - {error_details['response_json']['error_description']}"
                elif error_details.get('response_body'):
                    error_msg += f" (HTTP {error_details['status_code']}): {error_details['response_body'][:200]}"
                else:
                    error_msg += f" (HTTP {error_details['status_code']})"
                
                raise ValueError(error_msg) from e
            except requests.RequestException as e:
                tenant_id_display = tenant_token.tenant_id if hasattr(tenant_token, 'tenant_id') else (tenant_token.tenant.tenant_id if tenant_token.tenant else 'unknown')
                logger.error(
                    f"Failed to refresh token for tenant {tenant_id_display}: {str(e)}",
                    exc_info=True
                )
                raise ValueError(f"Token refresh request failed for tenant {tenant_id_display}: {str(e)}") from e


class XeroAccountingApi:
    def __init__(self, api_client, tenant_id):
        from apps.xero.xero_metadata.models import XeroAccount, XeroTracking, XeroContacts
        from apps.xero.xero_data.models import XeroTransactionSource, XeroJournalsSource
        from apps.xero.xero_sync.models import XeroLastUpdate
        
        self.tenant_id = tenant_id
        self.api_client = AccountingApi(api_client.api_client)
        self.organisation = XeroTenant.objects.get(tenant_id=tenant_id)

    def accounts(self):
        from apps.xero.xero_metadata.models import XeroAccount
        
        class Accounts:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                accounts_obj = self.api_client.get_accounts(self.parent.tenant_id)
                response = serialize_model(accounts_obj)['Accounts']
                XeroAccount.objects.create_accounts(self.organisation, response)

        return Accounts(self)

    def tracking_categories(self):
        from apps.xero.xero_metadata.models import XeroTracking
        
        class TrackingCategories:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                print('Updating Tracking Categories')
                tracking_obj = self.api_client.get_tracking_categories(self.parent.tenant_id, include_archived='True')
                response = serialize_model(tracking_obj)['TrackingCategories']
                print(response)
                XeroTracking.objects.create_tracking_categories_from_xero(self.organisation, response)

        return TrackingCategories(self)

    def contacts(self):
        from apps.xero.xero_metadata.models import XeroContacts
        
        class Contacts:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                contacts_obj = self.api_client.get_contacts(self.parent.tenant_id)
                response = serialize_model(contacts_obj)['Contacts']
                XeroContacts.objects.create_contacts_from_xero(self.organisation, response)

        return Contacts(self)

    def bank_transactions(self):
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class BankTransactions:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                modified_since = XeroLastUpdate.objects.get_utc_date_time('bank_transactions', self.organisation)
                page = 1
                page_size = 100
                total_written = 0
                while True:
                    kwargs = dict(page=page, page_size=page_size)
                    if modified_since:
                        kwargs['if_modified_since'] = modified_since
                    bank_trans_obj = self.api_client.get_bank_transactions(
                        self.parent.tenant_id,
                        **kwargs
                    )
                    page_data = serialize_model(bank_trans_obj)
                    items = page_data.get('BankTransactions') or []
                    if items:
                        XeroTransactionSource.objects.create_bank_transaction_from_xero(
                            self.organisation, items
                        )
                        total_written += len(items)
                        logger.info(f"[BANK_TRANSACTIONS] Page {page}: wrote {len(items)} to DB (total so far: {total_written})")
                    if not items or len(items) < page_size:
                        break
                    page += 1
                XeroLastUpdate.objects.update_or_create_timestamp('bank_transactions', self.organisation)
                logger.info(f"[BANK_TRANSACTIONS] Completed: {total_written} bank transactions written to DB")

        return BankTransactions(self)

    def invoices(self):
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class Invoices:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                modified_since = XeroLastUpdate.objects.get_utc_date_time('invoices', self.organisation)
                page = 1
                page_size = 100
                total_written = 0
                while True:
                    kwargs = dict(page=page, page_size=page_size)
                    if modified_since:
                        kwargs['if_modified_since'] = modified_since
                    invoices_obj = self.api_client.get_invoices(
                        self.parent.tenant_id,
                        **kwargs
                    )
                    page_data = serialize_model(invoices_obj)
                    items = page_data.get('Invoices') or []
                    if items:
                        XeroTransactionSource.objects.create_invoices_from_xero(
                            self.organisation, items
                        )
                        total_written += len(items)
                        logger.info(f"[INVOICES] Page {page}: wrote {len(items)} to DB (total so far: {total_written})")
                    if not items or len(items) < page_size:
                        break
                    page += 1
                XeroLastUpdate.objects.update_or_create_timestamp('invoices', self.organisation)
                logger.info(f"[INVOICES] Completed: {total_written} invoices written to DB")

        return Invoices(self)

    def payments(self):
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class Payments:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                payments_obj = self.api_client.get_payments(self.parent.tenant_id)
                response = serialize_model(payments_obj)['Payments']
                XeroTransactionSource.objects.create_payments_from_xero(self.organisation, response)

        return Payments(self)

    def credit_notes(self):
        """Get credit notes from Xero API (paged; each page written to DB immediately)."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class CreditNotes:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                modified_since = XeroLastUpdate.objects.get_utc_date_time('credit_notes', self.organisation)
                logger.info(f"[CREDIT_NOTES] Fetching credit notes for tenant {self.organisation.tenant_id}")
                page = 1
                page_size = 100
                total_written = 0
                while True:
                    kwargs = dict(page=page, page_size=page_size)
                    if modified_since:
                        kwargs['if_modified_since'] = modified_since
                    credit_notes_obj = self.api_client.get_credit_notes(
                        self.parent.tenant_id,
                        **kwargs
                    )
                    page_data = serialize_model(credit_notes_obj)
                    items = page_data.get('CreditNotes') or []
                    if items:
                        XeroTransactionSource.objects.create_credit_notes_from_xero(
                            self.organisation, items
                        )
                        total_written += len(items)
                        logger.info(f"[CREDIT_NOTES] Page {page}: wrote {len(items)} to DB (total so far: {total_written})")
                    if not items or len(items) < page_size:
                        break
                    page += 1
                XeroLastUpdate.objects.update_or_create_timestamp('credit_notes', self.organisation)
                logger.info(f"[CREDIT_NOTES] Completed: {total_written} credit notes written to DB")

        return CreditNotes(self)

    def prepayments(self):
        """Get prepayments from Xero API (paged; each page written to DB immediately)."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class Prepayments:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                modified_since = XeroLastUpdate.objects.get_utc_date_time('prepayments', self.organisation)
                logger.info(f"[PREPAYMENTS] Fetching prepayments for tenant {self.organisation.tenant_id}")
                page = 1
                page_size = 100
                total_written = 0
                while True:
                    kwargs = dict(page=page, page_size=page_size)
                    if modified_since:
                        kwargs['if_modified_since'] = modified_since
                    prepayments_obj = self.api_client.get_prepayments(
                        self.parent.tenant_id,
                        **kwargs
                    )
                    page_data = serialize_model(prepayments_obj)
                    items = page_data.get('Prepayments') or []
                    if items:
                        XeroTransactionSource.objects.create_prepayments_from_xero(
                            self.organisation, items
                        )
                        total_written += len(items)
                        logger.info(f"[PREPAYMENTS] Page {page}: wrote {len(items)} to DB (total so far: {total_written})")
                    if not items or len(items) < page_size:
                        break
                    page += 1
                XeroLastUpdate.objects.update_or_create_timestamp('prepayments', self.organisation)
                logger.info(f"[PREPAYMENTS] Completed: {total_written} prepayments written to DB")

        return Prepayments(self)

    def overpayments(self):
        """Get overpayments from Xero API (paged; each page written to DB immediately)."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class Overpayments:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                modified_since = XeroLastUpdate.objects.get_utc_date_time('overpayments', self.organisation)
                logger.info(f"[OVERPAYMENTS] Fetching overpayments for tenant {self.organisation.tenant_id}")
                page = 1
                page_size = 100
                total_written = 0
                while True:
                    kwargs = dict(page=page, page_size=page_size)
                    if modified_since:
                        kwargs['if_modified_since'] = modified_since
                    overpayments_obj = self.api_client.get_overpayments(
                        self.parent.tenant_id,
                        **kwargs
                    )
                    page_data = serialize_model(overpayments_obj)
                    items = page_data.get('Overpayments') or []
                    if items:
                        XeroTransactionSource.objects.create_overpayments_from_xero(
                            self.organisation, items
                        )
                        total_written += len(items)
                        logger.info(f"[OVERPAYMENTS] Page {page}: wrote {len(items)} to DB (total so far: {total_written})")
                    if not items or len(items) < page_size:
                        break
                    page += 1
                XeroLastUpdate.objects.update_or_create_timestamp('overpayments', self.organisation)
                logger.info(f"[OVERPAYMENTS] Completed: {total_written} overpayments written to DB")

        return Overpayments(self)

    def purchase_orders(self):
        """Get purchase orders from Xero API."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class PurchaseOrders:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                print(f"[PURCHASE_ORDERS] Fetching purchase orders for tenant {self.organisation.tenant_id}")
                purchase_orders_obj = self.api_client.get_purchase_orders(self.parent.tenant_id)
                response = serialize_model(purchase_orders_obj)['PurchaseOrders']
                print(f"[PURCHASE_ORDERS] Retrieved {len(response)} purchase orders")
                XeroTransactionSource.objects.create_purchase_orders_from_xero(self.organisation, response)
                logger.info(f"Successfully updated purchase orders for tenant {self.organisation.tenant_id}")

        return PurchaseOrders(self)

    def bank_transfers(self):
        """Get bank transfers from Xero API."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class BankTransfers:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                print(f"[BANK_TRANSFERS] Fetching bank transfers for tenant {self.organisation.tenant_id}")
                bank_transfers_obj = self.api_client.get_bank_transfers(self.parent.tenant_id)
                response = serialize_model(bank_transfers_obj)['BankTransfers']
                print(f"[BANK_TRANSFERS] Retrieved {len(response)} bank transfers")
                XeroTransactionSource.objects.create_bank_transfers_from_xero(self.organisation, response)
                logger.info(f"Successfully updated bank transfers for tenant {self.organisation.tenant_id}")

        return BankTransfers(self)

    def expense_claims(self):
        """Get expense claims from Xero API."""
        from apps.xero.xero_data.models import XeroTransactionSource
        
        class ExpenseClaims:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                print(f"[EXPENSE_CLAIMS] Fetching expense claims for tenant {self.organisation.tenant_id}")
                expense_claims_obj = self.api_client.get_expense_claims(self.parent.tenant_id)
                response = serialize_model(expense_claims_obj)['ExpenseClaims']
                print(f"[EXPENSE_CLAIMS] Retrieved {len(response)} expense claims")
                XeroTransactionSource.objects.create_expense_claims_from_xero(self.organisation, response)
                logger.info(f"Successfully updated expense claims for tenant {self.organisation.tenant_id}")

        return ExpenseClaims(self)

    def manual_journals(self, load_all=False):
        """
        Get manual journals from Xero API.
        
        Args:
            load_all: If True, ignore last update timestamp and load all journals. Default False.
        """
        from apps.xero.xero_data.models import XeroJournalsSource
        from apps.xero.xero_sync.models import XeroLastUpdate
        
        class ManualJournals:
            def __init__(self, parent, load_all):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation
                self.load_all = load_all

            def get(self):
                from apps.xero.xero_core.services import serialize_model
                from apps.xero.xero_sync.models import XeroLastUpdate
                
                try:
                    journals_to_process = []
                    journal_ids_to_fetch = []
                    
                    # If load_all is True, ignore last update timestamp (set to None)
                    # Otherwise, use incremental updates based on last update time
                    if self.load_all:
                        modified_since = None
                        print(f"[MANUAL_JOURNALS] Loading ALL manual journals (ignoring last update timestamp)")
                    else:
                        modified_since = XeroLastUpdate.objects.get_utc_date_time('manual_journals', self.organisation)
                        print(f"[MANUAL_JOURNALS] Loading manual journals modified since {modified_since}")
                    
                    print('Manual Journals Montified Since',modified_since)
                    # Manual journals - use page-based pagination
                    print(f"[MANUAL_JOURNALS] Fetching all manual journals (with pagination)")
                    page = 1
                    page_size = 100
                    journal_set = []
                    
                    # Loop through all pages
                    while True:
                        print(f"[MANUAL_JOURNALS] Fetching page {page} (page_size={page_size})")
                        # Only pass if_modified_since if it's not None (incremental update)
                        if modified_since:
                            journals_obj = self.api_client.get_manual_journals(
                                self.parent.tenant_id, 
                                if_modified_since=modified_since,
                                page=page,
                                page_size=page_size
                            )
                        else:
                            journals_obj = self.api_client.get_manual_journals(
                                self.parent.tenant_id,
                                page=page,
                                page_size=page_size
                            )

                        # DEBUG: print raw manual journal response to console (only for first page)
                        if page == 1:
                            try:
                                raw_data = serialize_model(journals_obj)
                                print(f"[MANUAL_JOURNALS] RAW manual journals response (top-level keys): {list(raw_data.keys())}")
                                manual_journals = raw_data.get('ManualJournals') or []
                                print(f"[MANUAL_JOURNALS] RAW manual journals count on page 1: {len(manual_journals)}")
                                if manual_journals:
                                    print(f"[MANUAL_JOURNALS] RAW first manual journal payload: {manual_journals[0]}")
                            except Exception as debug_e:
                                print(f"[MANUAL_JOURNALS] ERROR while printing raw manual journals response: {debug_e}")

                        page_data = serialize_model(journals_obj)
                        page_journals = page_data.get('ManualJournals') or []
                        
                        if not page_journals:
                            print(f"[MANUAL_JOURNALS] No more manual journals found. Final page={page}")
                            break
                        
                        print(f"[MANUAL_JOURNALS] Retrieved {len(page_journals)} manual journals on page {page}")
                        journal_set.extend(page_journals)
                        
                        # If we got fewer than page_size, this is the last page
                        if len(page_journals) < page_size:
                            print(f"[MANUAL_JOURNALS] Last page reached. Final page={page}")
                            break
                        
                        page += 1
                    
                    if journal_set:
                        print(f"[MANUAL_JOURNALS] Retrieved {len(journal_set)} manual journals")
                        # Debug: Print first journal structure if available
                        if journal_set and len(journal_set) > 0:
                            sample_keys = list(journal_set[0].keys())
                            print(f"[MANUAL_JOURNALS] Sample journal keys: {sample_keys}")
                        
                        for journal in journal_set:
                            # Try multiple possible ID field names
                            journal_id = (
                                journal.get('ManualJournalID') or 
                                journal.get('JournalID') or 
                                journal.get('ID')
                            )
                            if not journal_id:
                                logger.warning(f"Skipping journal: No ID found. Available keys: {list(journal.keys())}")
                                print(f"[MANUAL_JOURNALS] WARNING: Journal missing ID. Keys: {list(journal.keys())}")
                                continue
                            
                            # Generate journal number from ManualJournalID hash
                            journal_number = abs(hash(journal_id)) % 1000000
                            
                            journal_ids_to_fetch.append(journal_id)
                            journals_to_process.append({
                                'journal_id': journal_id,
                                'journal_number': journal_number,
                                'collection': journal,
                            })
                    else:
                        print(f"[MANUAL_JOURNALS] No manual journals found")
                    
                    print(f"[MANUAL_JOURNALS] Completed fetching all manual journals. Total: {len(journals_to_process)}")
                    
                    # Update timestamp immediately after API call succeeds, before database processing
                    XeroLastUpdate.objects.update_or_create_timestamp('manual_journals', self.organisation)
                    
                    # Bulk update/create journals using bulk operations
                    if journals_to_process:
                        # Fetch existing journals in one query (filter by journal_type)
                        existing_journals = {
                            j.journal_id: j for j in XeroJournalsSource.objects.filter(
                                organisation=self.organisation,
                                journal_id__in=journal_ids_to_fetch,
                                journal_type='manual_journal'
                            )
                        }
                        
                        to_create = []
                        to_update = []
                        
                        for journal_data in journals_to_process:
                            journal_id = journal_data['journal_id']
                            if journal_id in existing_journals:
                                # Update existing
                                existing = existing_journals[journal_id]
                                existing.journal_number = journal_data['journal_number']
                                existing.collection = journal_data['collection']
                                existing.journal_type = 'manual_journal'
                                existing.processed = False
                                to_update.append(existing)
                            else:
                                # Create new
                                to_create.append(XeroJournalsSource(
                                    organisation=self.organisation,
                                    journal_id=journal_id,
                                    journal_number=journal_data['journal_number'],
                                    journal_type='manual_journal',
                                    collection=journal_data['collection'],
                                    processed=False
                                ))
                        
                        # Bulk create and update
                        if to_create:
                            XeroJournalsSource.objects.bulk_create(to_create, ignore_conflicts=True)
                        if to_update:
                            XeroJournalsSource.objects.bulk_update(to_update, ['journal_number', 'journal_type', 'collection', 'processed'])
                    
                    # Only process the manual journals that were just fetched (incremental update)
                    XeroJournalsSource.objects.create_journals_from_xero(self.organisation, journal_ids=journal_ids_to_fetch if journals_to_process else None)
                    
                    logger.info(f"Successfully updated manual journals for tenant {self.organisation.tenant_id}")
                except Exception as e:
                    # Don't update timestamp on error - preserve last successful date
                    error_msg = str(e)
                    logger.error(f"Failed to update manual journals for tenant {self.organisation.tenant_id}: {error_msg}")
                    raise

        return ManualJournals(self, load_all)

    def profit_and_loss(self):
        """
        Get Profit and Loss report from Xero API.
        """
        class ProfitAndLoss:
            def __init__(self, parent):
                self.parent = parent
                self.api_client = parent.api_client
                self.organisation = parent.organisation

            def get(self, from_date, to_date, periods=11, timeframe='MONTH',
                    tracking_category_id=None, tracking_option_id=None,
                    tracking_category_id2=None, tracking_option_id2=None):
                """
                Get Profit and Loss report.
                
                Args:
                    from_date: Start date (YYYY-MM-DD format)
                    to_date: End date (YYYY-MM-DD format)
                    periods: Number of periods (default 11 for 12 months)
                    timeframe: MONTH, QUARTER, or YEAR
                    tracking_category_id: Filter by tracking category UUID
                    tracking_option_id: Filter by tracking option UUID
                    tracking_category_id2: Filter by second tracking category UUID
                    tracking_option_id2: Filter by second tracking option UUID
                
                Returns:
                    Serialized P&L report data
                """
                from apps.xero.xero_core.services import serialize_model
                kwargs = dict(
                    from_date=from_date,
                    to_date=to_date,
                    periods=periods,
                    timeframe=timeframe,
                )
                if tracking_category_id:
                    kwargs['tracking_category_id'] = tracking_category_id
                if tracking_option_id:
                    kwargs['tracking_option_id'] = tracking_option_id
                if tracking_category_id2:
                    kwargs['tracking_category_id2'] = tracking_category_id2
                if tracking_option_id2:
                    kwargs['tracking_option_id2'] = tracking_option_id2
                pnl_obj = self.api_client.get_report_profit_and_loss(
                    self.parent.tenant_id,
                    **kwargs
                )
                return serialize_model(pnl_obj)

        return ProfitAndLoss(self)
