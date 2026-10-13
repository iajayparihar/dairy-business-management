from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.payments.statements import get_supplier_account_statement


class SupplierStatementValidationTests(SimpleTestCase):

    def test_invalid_date_format_is_rejected(self):
        with self.assertRaises(ValidationError):
            get_supplier_account_statement(
                business_id="business-id",
                supplier_id="supplier-id",
                start_date="not-a-date",
                end_date="2026-10-31",
            )

    def test_start_date_after_end_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            get_supplier_account_statement(
                business_id="business-id",
                supplier_id="supplier-id",
                start_date=date(2026, 10, 20),
                end_date=date(2026, 10, 10),
            )