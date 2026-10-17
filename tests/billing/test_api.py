"""Customer billing API: contract, statuses, roles and tenant isolation."""
from datetime import date
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.models import CustomerBill, CustomerPayment
from tests.billing.base import BillFlow, ist


class ApiBase(BillFlow):
    def setUp(self):
        super().setUp()
        self.client_ = APIClient()

    def api(self, user=None):
        self.client_.force_authenticate(user or self.owner)
        return self.client_

    def url(self, name, business=None, **kwargs):
        return reverse(
            f"billing:{name}",
            kwargs={"business_id": (business or self.business).pk, **kwargs},
        )

    def cust_url(self, name, customer=None, **kw):
        return self.url(name, customer_id=(customer or self.customer).pk, **kw)

    def post_payment(self, body, user=None, customer=None):
        return self.api(user).post(
            self.cust_url("payment-list-create", customer), body, format="json",
        )


class PaymentApiTests(ApiBase, TestCase):
    def test_create_replay_conflict_and_response_shape(self):
        sep = self.make_bill(status="FINALIZED", total="900")

        created = self.post_payment({"amount": "1000", "client_ref": "r1"})

        self.assertEqual(created.status_code, 201, created.data)
        data = created.data
        self.assertRegex(data["payment_number"], r"^PAY-C001-\d{6}-1$")
        self.assertEqual(data["amount"], "1000.00")            # money is a string
        self.assertEqual(data["advance_remaining"], "100.00")
        self.assertEqual(data["allocations"][0]["bill_id"], str(sep.pk))
        self.assertEqual(data["allocations"][0]["amount"], "900.00")

        again = self.post_payment({"amount": "1000", "client_ref": "r1"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["id"], data["id"])
        different = self.post_payment({"amount": "999", "client_ref": "r1"})
        self.assertEqual(different.status_code, 409)
        self.assertEqual(CustomerPayment.objects.count(), 1)

    def test_rejected_and_unknown_fields(self):
        cases = [
            {"amount": "10", "payment_number": "MINE-1"},   # server numbers it
            {"amount": "10", "allocation": []},             # typo for allocations
            {"amount": "0"},
            {"amount": "1.005"},
            {"amount": "10", "payment_method": "BARTER"},
            {"amount": "10", "paid_at": "2026-09-01T10:00:00"},   # no timezone
            {"amount": "10", "paid_at": "2099-01-01T10:00:00+05:30"},  # future
            {"amount": "10", "allocations": [{"bill_id": str(uuid4()), "amount": "5"}]},
            {},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post_payment(body).status_code, 400)
        self.assertEqual(CustomerPayment.objects.count(), 0)

    def test_empty_allocations_keep_everything_as_advance(self):
        self.make_bill(status="FINALIZED", total="900")

        response = self.post_payment({"amount": "500", "allocations": []})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocations"], [])
        self.assertEqual(response.data["advance_remaining"], "500.00")

    def test_list_detail_reverse_and_allocate(self):
        sep = self.make_bill(status="FINALIZED", total="900")
        pid = self.post_payment({"amount": "500", "allocations": []}).data["id"]
        listing = self.api().get(self.cust_url("payment-list-create"))
        self.assertEqual(listing.data["count"], 1)

        allocate = self.api().post(
            self.url("payment-allocate", payment_id=pid),
            {"bill_id": str(sep.pk), "amount": "400"}, format="json",
        )
        self.assertEqual(allocate.status_code, 201, allocate.data)
        self.assertEqual(allocate.data["payment"]["amounts"]["unallocated"], "100.00")
        retry = self.api().post(
            self.url("payment-allocate", payment_id=pid),
            {"bill_id": str(sep.pk), "amount": "400"}, format="json",
        )
        self.assertEqual(retry.status_code, 200)

        reverse_ = self.api().post(
            self.url("payment-reverse", payment_id=pid),
            {"amount": "200", "reason": "bounced", "client_ref": "rv"}, format="json",
        )
        self.assertEqual(reverse_.status_code, 201, reverse_.data)
        self.assertEqual(reverse_.data["reversal"]["from_advance"], "100.00")
        self.assertEqual(
            reverse_.data["reversal"]["from_allocations"][0]["amount"], "100.00",
        )
        replay = self.api().post(
            self.url("payment-reverse", payment_id=pid),
            {"amount": "200", "reason": "bounced", "client_ref": "rv"}, format="json",
        )
        self.assertEqual(replay.status_code, 200)
        clash = self.api().post(
            self.url("payment-reverse", payment_id=pid),
            {"amount": "300", "reason": "bounced", "client_ref": "rv"}, format="json",
        )
        self.assertEqual(clash.status_code, 409)
        too_much = self.api().post(
            self.url("payment-reverse", payment_id=pid),
            {"amount": "9999", "reason": "x"}, format="json",
        )
        self.assertEqual(too_much.status_code, 400)
        no_reason = self.api().post(
            self.url("payment-reverse", payment_id=pid), {"amount": "1"}, format="json",
        )
        self.assertEqual(no_reason.status_code, 400)

        detail = self.api().get(self.url("payment-detail", payment_id=pid))
        self.assertEqual(detail.data["amounts"]["net"], "300.00")
        self.assertEqual(len(detail.data["reversals"]), 1)

    def test_allocation_errors_are_400(self):
        pid = self.post_payment({"amount": "50"}).data["id"]
        response = self.api().post(
            self.url("payment-allocate", payment_id=pid),
            {"bill_id": str(uuid4()), "amount": "10"}, format="json",
        )
        self.assertEqual(response.status_code, 400)


class BalanceAndStatementApiTests(ApiBase, TestCase):
    def test_balance_and_paginated_statement(self):
        self.make_bill(status="FINALIZED", total="900")
        self.post_payment({"amount": "1000"})
        self.deliver(self.cow, "1", ist(2026, 10, 2))

        balance = self.api().get(self.cust_url("customer-balance")).data
        self.assertEqual(balance["net_balance"], "-100.00")
        self.assertEqual(balance["available_credit"], "100.00")
        self.assertEqual(balance["unbilled_amount"], "60.00")
        self.assertEqual(balance["unbilled_deliveries"], 1)

        statement = self.api().get(
            self.cust_url("customer-statement"), {"page_size": 1},
        ).data
        self.assertEqual(statement["pagination"]["count"], 2)
        self.assertEqual(statement["pagination"]["num_pages"], 2)
        self.assertEqual(len(statement["transactions"]), 1)
        self.assertEqual(statement["closing_balance"], "-100.00")
        self.assertEqual(statement["customer_name"], "Ravi")

    def test_bad_dates_are_400(self):
        response = self.api().get(
            self.cust_url("customer-statement"), {"start_date": "nope"},
        )
        self.assertEqual(response.status_code, 400)


class BillApiTests(ApiBase, TestCase):
    PERIOD = {"period_start": "2026-09-01", "period_end": "2026-09-30"}

    def create_draft(self, customer=None):
        return self.api().post(
            self.cust_url("customer-bill-create", customer), self.PERIOD, format="json",
        )

    def test_draft_finalize_void_flow(self):
        self.september()
        created = self.create_draft()
        self.assertEqual(created.status_code, 201, created.data)
        draft = created.data
        self.assertTrue(draft["live"])
        self.assertEqual(draft["total_amount"], "1240.00")
        self.assertEqual(draft["totals"]["cow_litres"], "6.000")
        self.assertEqual(draft["totals"]["buffalo_litres"], "1.000")
        self.assertEqual(draft["totals"]["other_amount"], "800.00")
        self.assertEqual(len(draft["lines"]), 5)
        self.assertEqual(self.create_draft().status_code, 200)       # same draft

        stale = self.api().post(
            self.url("bill-finalize", bill_id=draft["id"]),
            {"expected_total": "1.00"}, format="json",
        )
        self.assertEqual(stale.status_code, 409)

        done = self.api().post(
            self.url("bill-finalize", bill_id=draft["id"]),
            {"expected_total": "1240.00"}, format="json",
        )
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data["status"], "FINALIZED")
        self.assertFalse(done.data["live"])
        self.assertEqual(done.data["account"]["balance_after"], "1240.00")
        self.assertEqual(done.data["account"]["payment_status"], "UNPAID")
        self.assertEqual(done.data["header"]["customer"]["name"], "Ravi")
        self.assertEqual(self.create_draft().status_code, 409)         # already billed

        voided = self.api().post(
            self.url("bill-void", bill_id=draft["id"]),
            {"reason": "wrong delivery"}, format="json",
        )
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["bill"]["status"], "VOID")
        self.assertEqual(voided.data["replacement"]["status"], "DRAFT")
        self.assertEqual(voided.data["replacement"]["replaces"], draft["id"])

        again = self.api().post(
            self.url("bill-void", bill_id=draft["id"]), {"reason": "again"}, format="json",
        )
        self.assertEqual(again.status_code, 400)
        no_reason = self.api().post(
            self.url("bill-void", bill_id=voided.data["replacement"]["id"]),
            {}, format="json",
        )
        self.assertEqual(no_reason.status_code, 400)

    def test_nothing_to_bill_and_bad_periods_are_400(self):
        self.assertEqual(self.create_draft().status_code, 400)
        bad = self.api().post(
            self.cust_url("customer-bill-create"),
            {"period_start": "2026-09-30", "period_end": "2026-09-01"}, format="json",
        )
        self.assertEqual(bad.status_code, 400)
        extra = self.api().post(
            self.cust_url("customer-bill-create"), {**self.PERIOD, "x": 1}, format="json",
        )
        self.assertEqual(extra.status_code, 400)

    def test_bulk_generate_and_list_filters(self):
        self.september(self.customer)
        self.september(self.customer2)

        generated = self.api().post(self.url("bill-generate"), self.PERIOD, format="json")
        self.assertEqual(generated.status_code, 200, generated.data)
        self.assertEqual(len(generated.data["created"]), 2)
        stranger = self.api().post(
            self.url("bill-generate"),
            {**self.PERIOD, "customer_ids": [str(uuid4())]}, format="json",
        )
        self.assertEqual(
            stranger.data["skipped"][0]["reason"], "CUSTOMER_NOT_FOUND",
        )

        bill_id = generated.data["created"][0]["id"]
        self.api().post(self.url("bill-finalize", bill_id=bill_id), {}, format="json")
        self.pay(  # pays the finalized one in part
            "100", customer=self.customer if generated.data["created"][0]["customer_id"] == str(self.customer.pk) else self.customer2,
        )

        everything = self.api().get(self.url("bill-list")).data
        self.assertEqual(everything["count"], 2)
        unpaid = self.api().get(self.url("bill-list"), {"unpaid": "true"}).data
        self.assertEqual(unpaid["count"], 1)
        self.assertEqual(unpaid["results"][0]["payment_status"], "PARTIAL")
        self.assertEqual(unpaid["results"][0]["paid_amount"], "100.00")
        drafts = self.api().get(self.url("bill-list"), {"status": "DRAFT"}).data
        self.assertEqual(drafts["count"], 1)
        mine = self.api().get(
            self.url("bill-list"), {"customer": str(self.customer.pk)},
        ).data
        self.assertEqual(mine["count"], 1)
        self.assertEqual(
            self.api().get(self.url("bill-list"), {"customer": "nope"}).status_code, 400,
        )
        outside = self.api().get(
            self.url("bill-list"), {"start_date": "2026-10-01"},
        ).data
        self.assertEqual(outside["count"], 0)

    def test_opening_balance_endpoint(self):
        url = self.cust_url("opening-balance")
        body = {"amount": "1200", "as_of_date": "2026-08-31"}

        first = self.api().post(url, body, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["kind"], "BILL")
        self.assertEqual(self.api().post(url, body, format="json").status_code, 200)
        self.assertEqual(
            self.api().post(url, {**body, "amount": "1300"}, format="json").status_code, 409,
        )
        advance = self.api().post(
            self.cust_url("opening-balance", self.customer2),
            {"amount": "-300", "as_of_date": "2026-08-31"}, format="json",
        )
        self.assertEqual(advance.status_code, 201)
        self.assertEqual(advance.data["kind"], "ADVANCE")
        self.assertEqual(
            self.api().post(url, {"amount": "0", "as_of_date": "2026-08-31"}, format="json").status_code,
            400,
        )


class AccessTests(ApiBase, TestCase):
    def endpoints(self, bill, payment):
        pid, bid = str(payment.pk), str(bill.pk)
        return [
            ("get", self.cust_url("payment-list-create"), None),
            ("post", self.cust_url("payment-list-create"), {"amount": "1"}),
            ("get", self.cust_url("customer-balance"), None),
            ("get", self.cust_url("customer-statement"), None),
            ("post", self.cust_url("customer-bill-create"), {"period_start": "2026-09-01", "period_end": "2026-09-30"}),
            ("post", self.cust_url("opening-balance"), {"amount": "1", "as_of_date": "2026-08-31"}),
            ("get", self.url("payment-detail", payment_id=pid), None),
            ("post", self.url("payment-reverse", payment_id=pid), {"reason": "x"}),
            ("post", self.url("payment-allocate", payment_id=pid), {"bill_id": bid, "amount": "1"}),
            ("get", self.url("bill-list"), None),
            ("post", self.url("bill-generate"), {"period_start": "2026-09-01", "period_end": "2026-09-30"}),
            ("get", self.url("bill-detail", bill_id=bid), None),
            ("post", self.url("bill-finalize", bill_id=bid), {}),
            ("post", self.url("bill-void", bill_id=bid), {"reason": "xyz"}),
        ]

    def test_staff_and_outsiders_are_forbidden_everywhere(self):
        bill = self.make_bill(status="FINALIZED", total="10")
        payment = self.make_payment()
        for user in (self.staff, self.outsider):
            for method, url, body in self.endpoints(bill, payment):
                with self.subTest(user=user.username, url=url):
                    response = getattr(self.api(user), method)(url, body, format="json")
                    self.assertEqual(response.status_code, 403)
        anonymous = APIClient().get(self.url("bill-list"))
        self.assertIn(anonymous.status_code, (401, 403))

    def test_manager_is_allowed(self):
        self.assertEqual(self.api(self.manager).get(self.url("bill-list")).status_code, 200)

    def test_another_business_ids_are_404(self):
        bill = self.make_bill(status="FINALIZED", total="10")
        payment = self.make_payment()
        other = self.other_business
        calls = [
            ("get", self.url("bill-detail", business=other, bill_id=bill.pk)),
            ("post", self.url("bill-finalize", business=other, bill_id=bill.pk)),
            ("post", self.url("bill-void", business=other, bill_id=bill.pk)),
            ("get", self.url("payment-detail", business=other, payment_id=payment.pk)),
            ("post", self.url("payment-reverse", business=other, payment_id=payment.pk)),
            ("get", self.url("customer-balance", business=other, customer_id=self.customer.pk)),
            ("get", self.url("customer-statement", business=other, customer_id=self.customer.pk)),
            ("post", self.url("payment-list-create", business=other, customer_id=self.customer.pk)),
        ]
        for method, url in calls:
            with self.subTest(url=url):
                response = getattr(self.api(self.other_owner), method)(url, {}, format="json")
                self.assertEqual(response.status_code, 404)
        self.assertEqual(CustomerBill.objects.get(pk=bill.pk).status, "FINALIZED")

    def test_a_deleted_customer_is_404(self):
        from apps.customers.models import Customer
        Customer.objects.filter(pk=self.customer2.pk).update(is_deleted=True)
        response = self.api().get(self.cust_url("customer-balance", self.customer2))
        self.assertEqual(response.status_code, 404)

    def test_an_inactive_customer_still_has_a_ledger(self):
        from apps.customers.models import Customer
        Customer.objects.filter(pk=self.customer2.pk).update(is_active=False)
        response = self.api().get(self.cust_url("customer-balance", self.customer2))
        self.assertEqual(response.status_code, 200)
