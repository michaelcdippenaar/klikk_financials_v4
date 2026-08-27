"""
Unit tests to validate Trial Balance parsing and UUID-based account matching.

These tests focus on:
- Ensuring the parser always links report lines to accounts by UUID first
- Verifying that missing accounts detection in validation uses account UUIDs
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.xero.xero_core.models import XeroTenant
from apps.xero.xero_metadata.models import XeroAccount
from apps.xero.xero_cube.models import XeroTrailBalance
from apps.xero.xero_validation.models import (
    XeroTrailBalanceReport,
    XeroTrailBalanceReportLine,
)
from apps.xero.xero_validation.helpers.trial_balance_parser import (
    parse_trial_balance_dict,
    parse_trial_balance_report,
)
from apps.xero.xero_metadata.utils import (
    fiscal_month_to_financial_period,
    fiscal_year_to_financial_year,
)
from apps.xero.xero_validation.helpers.service_helpers import convert_decimals_to_strings
from apps.xero.xero_validation.services import validate_balance_sheet_accounts


class TrialBalanceUUIDMatchingTests(TestCase):
    """
    Tests that Trial Balance parsing and validation always use UUID
    (Xero account_id) as the primary key for matching accounts.
    """

    def setUp(self):
        # Create a single tenant for all tests
        self.tenant = XeroTenant.objects.create(
            tenant_id="test-tenant-id",
            tenant_name="Test Tenant",
        )

    def _build_sample_trial_balance_json(self):
        """
        Build a minimal Trial Balance JSON payload similar to Xero's docs:
        https://developer.xero.com/documentation/api/accounting/reports#trial-balance
        """
        return {
            "Reports": [
                {
                    "ReportID": "TrialBalance",
                    "ReportName": "Trial Balance",
                    "ReportType": "TrialBalance",
                    "ReportDate": "21 February 2011",
                    "Rows": [
                        {
                            "RowType": "Header",
                            "Cells": [
                                {"Value": "Account"},
                                {"Value": "Debit"},
                                {"Value": "Credit"},
                                {"Value": "YTD Debit"},
                                {"Value": "YTD Credit"},
                            ],
                        },
                        {
                            "RowType": "Section",
                            "Title": "Revenue",
                            "Rows": [
                                {
                                    "RowType": "Row",
                                    "Cells": [
                                        {
                                            "Value": "Interest Income (270)",
                                            "Attributes": [
                                                {
                                                    "Value": "e9482110-7245-4a76-bfe2-14500495a076",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "",
                                            "Attributes": [
                                                {
                                                    "Value": "e9482110-7245-4a76-bfe2-14500495a076",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "0.00",
                                            "Attributes": [
                                                {
                                                    "Value": "e9482110-7245-4a76-bfe2-14500495a076",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "",
                                            "Attributes": [
                                                {
                                                    "Value": "e9482110-7245-4a76-bfe2-14500495a076",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "500.00",
                                            "Attributes": [
                                                {
                                                    "Value": "e9482110-7245-4a76-bfe2-14500495a076",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                    ],
                                },
                                {
                                    "RowType": "Row",
                                    "Cells": [
                                        {
                                            "Value": "Sales (200)",
                                            "Attributes": [
                                                {
                                                    "Value": "5040915e-8ce7-4177-8d08-fde416232f18",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "",
                                            "Attributes": [
                                                {
                                                    "Value": "5040915e-8ce7-4177-8d08-fde416232f18",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "12180.25",
                                            "Attributes": [
                                                {
                                                    "Value": "5040915e-8ce7-4177-8d08-fde416232f18",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "",
                                            "Attributes": [
                                                {
                                                    "Value": "5040915e-8ce7-4177-8d08-fde416232f18",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                        {
                                            "Value": "20775.53",
                                            "Attributes": [
                                                {
                                                    "Value": "5040915e-8ce7-4177-8d08-fde416232f18",
                                                    "Id": "account",
                                                }
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ]
        }

    def test_parse_uses_uuid_even_if_code_from_name(self):
        """
        Given TB JSON where account_id (UUID) is present but the code is only
        embedded in the Account Name (e.g. 'Interest Income (270)'), ensure
        the parser links the line to the correct XeroAccount by UUID even if
        the DB code is different.
        """
        tb_json = self._build_sample_trial_balance_json()

        # Xero account UUID from JSON
        account_uuid = "e9482110-7245-4a76-bfe2-14500495a076"
        # Use a different code in DB to make sure UUID is what links them
        db_code = "DIFF-270"

        # Create DB account with UUID and a different code
        account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id=account_uuid,
            code=db_code,
            name="Interest Income",
            type="REVENUE",
        )

        # Run parser on raw TB JSON
        parsed_rows = parse_trial_balance_dict(tb_json)
        # Should find one row for Interest Income
        interest_rows = [
            r for r in parsed_rows if r.get("account_id_uuid") == account_uuid
        ]
        self.assertEqual(len(interest_rows), 1)
        self.assertEqual(interest_rows[0]["account_code"], "270")

        # Create report and parse into report lines
        report = XeroTrailBalanceReport.objects.create(
            organisation=self.tenant,
            # XeroTenant has no created_at, so the old expression resolved to
            # None and tripped the NOT NULL on report_date. The sample payload
            # is an August-2011 trial balance, which is the period the
            # XeroTrailBalance rows below are written into.
            report_date=date(2011, 8, 31),
            report_type="TrialBalance",
            raw_data=tb_json,
            # parse_trial_balance_dict returns Decimals; JSONField cannot store
            # them. services/imports.py runs this same conversion before saving.
            parsed_json=convert_decimals_to_strings(parsed_rows),
        )

        stats = parse_trial_balance_report(report)
        self.assertGreater(stats["lines_created"], 0)

        # Fetch the created report line for this account
        line = XeroTrailBalanceReportLine.objects.get(
            report=report, account_code="270"
        )

        # The line must be linked to our DB account via UUID,
        # even though its parsed account_code is "270" and the DB code is different.
        # XeroAccount's primary key IS account_id (the Xero UUID) — there is no
        # surrogate .id — so the FK column on the line holds that UUID.
        self.assertEqual(line.account_id, account.pk)
        self.assertEqual(line.account.account_id, account_uuid)
        self.assertEqual(line.account.code, db_code)

    def test_validation_missing_in_xero_uses_uuid(self):
        """
        If a DB account has a non-zero balance but no corresponding UUID
        in the parsed TB data, validation should flag it as missing_in_xero
        using the account UUID.
        """
        # The account that IS in the TB JSON. Each test method is isolated, so
        # it has to be created here too — the sample payload references it by
        # UUID and the trail-balance row below links to it.
        present_uuid = "e9482110-7245-4a76-bfe2-14500495a076"
        present_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id=present_uuid,
            code="270",
            name="Interest Income",
            type="REVENUE",
        )

        # Create a DB account that is NOT present in the TB JSON
        missing_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        missing_account = XeroAccount.objects.create(
            organisation=self.tenant,
            account_id=missing_uuid,
            # XeroAccount.code is varchar(10) (Xero's own limit); "MISSING-999"
            # is 11 chars and never round-trips from Xero.
            code="MISS-999",
            name="Missing In Xero Account",
            type="LIABILITY",
            # validate_balance_sheet_accounts selects balance-sheet accounts by
            # `grouping`, not `type`. An account synced from Xero always carries
            # both; a fixture that sets only `type` is invisible to validation.
            grouping="LIABILITY",
        )

        # Create a small TB JSON that does NOT include this UUID
        tb_json = self._build_sample_trial_balance_json()
        parsed_rows = parse_trial_balance_dict(tb_json)

        # Create report + lines from parsed data
        report = XeroTrailBalanceReport.objects.create(
            organisation=self.tenant,
            # XeroTenant has no created_at, so the old expression resolved to
            # None and tripped the NOT NULL on report_date. The sample payload
            # is an August-2011 trial balance, which is the period the
            # XeroTrailBalance rows below are written into.
            report_date=date(2011, 8, 31),
            report_type="TrialBalance",
            raw_data=tb_json,
            # parse_trial_balance_dict returns Decimals; JSONField cannot store
            # them. services/imports.py runs this same conversion before saving.
            parsed_json=convert_decimals_to_strings(parsed_rows),
        )
        parse_trial_balance_report(report)

        # Create trail balance entries:
        # - one for Interest Income (present in TB)
        # - one for the missing account (only in DB)
        # Use year/month values that match validate_balance_sheet_accounts logic.
        # fin_year / fin_period are NOT NULL and are derived from the tenant's
        # fiscal calendar in production (xero_cube.services), so derive them the
        # same way here rather than hard-coding.
        fy_start = self.tenant.get_fiscal_year_start_month()
        fin_year = fiscal_year_to_financial_year(2011, 8, fy_start)
        fin_period = fiscal_month_to_financial_period(8, fy_start)

        XeroTrailBalance.objects.create(
            organisation=self.tenant,
            account=present_account,
            year=2011,
            month=8,
            fin_year=fin_year,
            fin_period=fin_period,
            amount=Decimal("500.00"),
        )

        XeroTrailBalance.objects.create(
            organisation=self.tenant,
            account=missing_account,
            year=2011,
            month=8,
            fin_year=fin_year,
            fin_period=fin_period,
            amount=Decimal("123.45"),
        )

        # Run validation
        result = validate_balance_sheet_accounts(
            tenant_id=self.tenant.tenant_id,
            report_id=report.id,
            tolerance=Decimal("0.01"),
        )

        self.assertTrue(result["success"])
        validations = result["validations"]

        # There should be an entry for the missing account by UUID.
        # validate_balance_sheet_accounts keys every validation entry on
        # 'account_id', which IS the Xero account UUID (XeroAccount's pk) —
        # that is the point this test pins. There is no 'db_account_uuid' key
        # anywhere in the service or its consumers.
        missing_entries = [
            v
            for v in validations
            if v["account_id"] == missing_uuid
            and v["status"] == "missing_in_xero"
        ]
        self.assertEqual(len(missing_entries), 1)
        entry = missing_entries[0]
        self.assertEqual(entry["account_id"], missing_uuid)
        self.assertEqual(entry["xero_value"], "0")
        self.assertEqual(entry["account_code"], missing_account.code)
        self.assertEqual(entry["account_name"], missing_account.name)


