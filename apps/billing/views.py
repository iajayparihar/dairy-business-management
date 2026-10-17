"""Customer billing API. Money data: OWNER/MANAGER only, reads included."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing import api_serializers as ser
from apps.billing import bill_services, payment_services, selectors
from apps.billing.bill_services import BillConflict
from apps.billing.documents import bill_document, bill_header, money, payment_status
from apps.billing.models import (
    CustomerBill,
    CustomerPayment,
    CustomerPaymentAllocationReversal,
)
from apps.billing.pdf import render_bill_pdf
from apps.billing.statements import (
    get_customer_account_statement,
    get_customer_balance,
)
from apps.businesses.models import BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.customers.models import Customer
from apps.payments.services import ClientRefConflict
from apps.purchases.api_views import raise_api_validation_error


def fail(exc):
    """409 for conflicts, 400 for everything else."""
    if isinstance(exc, (ClientRefConflict, BillConflict)):
        body = (
            exc.message_dict if hasattr(exc, "error_dict")
            else {"detail": exc.messages}
        )
        return Response(body, status=status.HTTP_409_CONFLICT)
    raise_api_validation_error(exc)


class BillingView(APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]

    def get_permissions(self):
        self.required_business_roles = [
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ]
        return super().get_permissions()

    def customer(self, customer_id):
        # Inactive customers still have a ledger; deleted ones are gone.
        return get_object_or_404(
            Customer.objects.filter(business_id=self.request.business.id),
            pk=customer_id,
        )

    def bill(self, bill_id):
        return get_object_or_404(
            CustomerBill.objects.filter(business_id=self.request.business.id),
            pk=bill_id,
        )

    def payment(self, payment_id):
        return get_object_or_404(
            CustomerPayment.objects.filter(business_id=self.request.business.id),
            pk=payment_id,
        )

    def page(self, rows):
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, self.request)
        return paginator, page


# -- payments ---------------------------------------------------------------

def payment_detail(payment):
    reversals = list(payment.reversals.all())
    unwinds = {}
    for unwind in (
        CustomerPaymentAllocationReversal.objects
        .filter(reversal__payment=payment)
        .select_related("allocation__bill")
        .order_by("created_at", "pk")
    ):
        unwinds.setdefault(unwind.reversal_id, []).append(unwind)

    allocations = (
        payment.allocations.select_related("bill")
        .prefetch_related("unwinds")
        .order_by("created_at", "pk")
    )
    return {
        **ser.serialize_payment(payment),
        "amounts": ser.jsonable_money(selectors.get_payment_amounts(payment)),
        "allocations": [ser.serialize_allocation(a) for a in allocations],
        "reversals": [
            ser.serialize_reversal(r, unwinds.get(r.pk, [])) for r in reversals
        ],
    }


class CustomerPaymentListCreateView(BillingView):
    def get(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        rows = CustomerPayment.objects.filter(
            business_id=request.business.id, customer=customer,
        ).order_by("-paid_at", "-created_at")
        paginator, page = self.page(rows)
        return paginator.get_paginated_response(
            [ser.serialize_payment(p) for p in page]
        )

    def post(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        serializer = ser.PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        allocations = data.get("allocations")

        try:
            payment = payment_services.record_customer_payment(
                business_id=request.business.id,
                customer_id=customer.pk,
                amount=data["amount"],
                payment_method=data["payment_method"],
                paid_at=data.get("paid_at"),
                reference_number=data["reference_number"],
                notes=data["notes"],
                client_ref=data.get("client_ref"),
                allocations=(
                    None if allocations is None
                    else [dict(a) for a in allocations]
                ),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        body = payment_detail(payment)
        body["advance_remaining"] = body["amounts"]["unallocated"]
        return Response(
            body,
            status=status.HTTP_200_OK if payment.replayed else status.HTTP_201_CREATED,
        )


class CustomerPaymentDetailView(BillingView):
    def get(self, request, business_id, payment_id):
        return Response(payment_detail(self.payment(payment_id)))


class CustomerPaymentReverseView(BillingView):
    def post(self, request, business_id, payment_id):
        payment = self.payment(payment_id)
        serializer = ser.PaymentReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reversal = payment_services.reverse_customer_payment(
                business_id=request.business.id,
                payment_id=payment.pk,
                amount=data.get("amount"),
                reason=data["reason"],
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        payment.refresh_from_db()
        return Response(
            {
                "reversal": ser.serialize_reversal(reversal, reversal.unwound),
                "payment": payment_detail(payment),
            },
            status=status.HTTP_200_OK if reversal.replayed else status.HTTP_201_CREATED,
        )


class CustomerPaymentAllocateView(BillingView):
    def post(self, request, business_id, payment_id):
        payment = self.payment(payment_id)
        serializer = ser.PaymentAllocateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            allocation = payment_services.allocate_customer_advance(
                business_id=request.business.id,
                payment_id=payment.pk,
                bill_id=data["bill_id"],
                amount=data["amount"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        allocation.bill = CustomerBill.objects.get(pk=allocation.bill_id)
        return Response(
            {
                "allocation": ser.serialize_allocation(allocation),
                "payment": payment_detail(payment),
            },
            status=status.HTTP_200_OK if allocation.replayed else status.HTTP_201_CREATED,
        )


# -- balance and statement --------------------------------------------------

class CustomerBalanceView(BillingView):
    def get(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        balance = get_customer_balance(
            business_id=request.business.id, customer_id=customer.pk,
        )
        return Response({
            "customer_id": str(customer.pk), **ser.jsonable_money(balance),
        })


class CustomerStatementView(BillingView):
    """`transactions` is one page; each row's running balance spans the period."""

    def get(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        try:
            statement = get_customer_account_statement(
                business_id=request.business.id,
                customer_id=customer.pk,
                start_date=request.query_params.get("start_date"),
                end_date=request.query_params.get("end_date"),
            )
        except DjangoValidationError as exc:
            return fail(exc)

        paginator, page = self.page(statement["transactions"])
        statement["transactions"] = page
        statement["pagination"] = {
            "count": paginator.page.paginator.count,
            "page": paginator.page.number,
            "page_size": paginator.get_page_size(request),
            "num_pages": paginator.page.paginator.num_pages,
        }
        return Response(ser.jsonable_money(statement))


# -- bills ------------------------------------------------------------------

def bill_row(bill):
    """List row: header plus paid / due from the paid_amount annotation."""
    row = bill_header(bill)
    finalized = bill.status == CustomerBill.Status.FINALIZED
    paid = bill.paid_amount if finalized else None
    row.update({
        "customer_name": bill.customer.name,
        "customer_code": bill.customer.customer_code,
        "paid_amount": money(paid),
        "due_amount": money(bill.total_amount - paid) if finalized else None,
        "payment_status": payment_status(bill, paid) if finalized else None,
    })
    return row


class BillListView(BillingView):
    def get(self, request, business_id):
        params = request.query_params
        try:
            bills = selectors.with_paid_amount(
                CustomerBill.objects.filter(business_id=request.business.id)
                .select_related("customer")
            )
            for key, field in (
                ("customer", "customer_id"), ("status", "status"),
                ("bill_type", "bill_type"),
            ):
                if params.get(key):
                    bills = bills.filter(**{field: params[key]})
            if params.get("start_date"):
                bills = bills.filter(period_end__gte=params["start_date"])
            if params.get("end_date"):
                bills = bills.filter(period_start__lte=params["end_date"])
            if params.get("unpaid", "").lower() in ("1", "true", "yes"):
                bills = bills.filter(status="FINALIZED", due_amount__gt=0)
            bills = bills.order_by("-period_start", "-created_at", "pk")
            paginator, page = self.page(bills)
            rows = [bill_row(b) for b in page]
        except (DjangoValidationError, ValueError):
            return fail(DjangoValidationError({"filters": "Invalid filter value."}))
        return paginator.get_paginated_response(rows)


class BillDetailView(BillingView):
    def get(self, request, business_id, bill_id):
        return Response(bill_document(self.bill(bill_id)))


class CustomerBillCreateView(BillingView):
    def post(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        serializer = ser.PeriodSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            bill = bill_services.generate_draft_bill(
                business_id=request.business.id,
                customer_id=customer.pk,
                period_start=data["period_start"],
                period_end=data["period_end"],
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        return Response(
            bill_document(bill),
            status=status.HTTP_201_CREATED if bill.created else status.HTTP_200_OK,
        )


class BillGenerateView(BillingView):
    def post(self, request, business_id):
        serializer = ser.BulkGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = bill_services.generate_drafts(
                business_id=request.business.id,
                period_start=data["period_start"],
                period_end=data["period_end"],
                customer_ids=data.get("customer_ids"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        return Response({
            "created": [bill_header(b) for b in result["created"]],
            "existing": [bill_header(b) for b in result["existing"]],
            "skipped": [
                {"customer_id": str(s["customer_id"]), "reason": s["reason"]}
                for s in result["skipped"]
            ],
        })


class BillFinalizeView(BillingView):
    def post(self, request, business_id, bill_id):
        bill = self.bill(bill_id)
        serializer = ser.FinalizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            done = bill_services.finalize_bill(
                business_id=request.business.id,
                bill_id=bill.pk,
                expected_total=serializer.validated_data.get("expected_total"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        document = bill_document(done)
        document["credit_applied"] = money(
            sum((a.amount for a in done.credit_applied), 0)
        )
        document["warnings"] = getattr(done, "warnings", document["warnings"])
        return Response(document)


class BillVoidView(BillingView):
    def post(self, request, business_id, bill_id):
        bill = self.bill(bill_id)
        serializer = ser.VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            voided, replacement = bill_services.void_bill(
                business_id=request.business.id,
                bill_id=bill.pk,
                reason=data["reason"],
                create_replacement=data.get("create_replacement"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        return Response({
            "bill": bill_document(voided),
            "replacement": bill_header(replacement) if replacement else None,
        })


class OpeningBalanceView(BillingView):
    def post(self, request, business_id, customer_id):
        customer = self.customer(customer_id)
        serializer = ser.OpeningBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = bill_services.record_opening_balance(
                business_id=request.business.id,
                customer_id=customer.pk,
                amount=data["amount"],
                as_of_date=data["as_of_date"],
                client_ref=data.get("client_ref"),
                user=request.user,
            )
        except DjangoValidationError as exc:
            return fail(exc)

        body = {
            "kind": result.kind,
            "bill": bill_header(result.bill) if result.bill else None,
            "payment": (
                ser.serialize_payment(result.payment) if result.payment else None
            ),
        }
        return Response(
            body,
            status=status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED,
        )


class BillPdfView(BillingView):
    """application/pdf for a FINALIZED or VOID bill; a draft has no PDF yet."""

    def get(self, request, business_id, bill_id):
        bill = self.bill(bill_id)
        if bill.status == CustomerBill.Status.DRAFT:
            return Response(
                {"detail": "Only a finalized or void bill has a PDF."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        response = HttpResponse(render_bill_pdf(bill), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{bill.bill_number}.pdf"'
        response["Cache-Control"] = "no-store"
        return response
