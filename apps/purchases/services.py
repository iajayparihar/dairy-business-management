
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.businesses.services import require_business_membership
from apps.inventory.models import StockMovement
from apps.products.models import Product
from apps.purchases.models import PurchaseLine, SupplierPurchase
from apps.suppliers.models import Supplier


ALLOWED_ROLES = ["OWNER", "MANAGER"]


def _validate_membership(*, user, business_id):
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=ALLOWED_ROLES,
    )


def _decimal(value, field_name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Enter a valid number."})

    if not number.is_finite():
        raise ValidationError({field_name: "Enter a finite number."})

    return number


def _validate_lines(lines):
    if not lines:
        raise ValidationError("A purchase must contain at least one product.")

    validated = []

    for item in lines:
        product_id = item.get("product_id")
        quantity = _decimal(item.get("quantity"), "quantity")
        unit_price = _decimal(item.get("unit_price"), "unit_price")

        if quantity <= 0 or quantity.as_tuple().exponent < -3:
            raise ValidationError(
                {"quantity": "Quantity must be positive with at most 3 decimals."}
            )

        if unit_price < 0 or unit_price.as_tuple().exponent < -2:
            raise ValidationError(
                {"unit_price": "Price must be non-negative with at most 2 decimals."}
            )

        validated.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": unit_price,
            }
        )

    return validated


def _calculate_total(purchase):
    total = sum(
        (line.line_total for line in purchase.lines.all()),
        Decimal("0.00"),
    )
    purchase.total_amount = total.quantize(Decimal("0.01"))
    purchase.save(update_fields=["total_amount", "updated_at"])


@transaction.atomic
def create_purchase(
    *,
    business_id,
    supplier_id,
    purchase_number,
    purchased_at,
    lines,
    user,
    notes="",
):
    _validate_membership(user=user, business_id=business_id)
    validated_lines = _validate_lines(lines)

    if not timezone.is_aware(purchased_at):
        raise ValidationError(
            {"purchased_at": "Purchase date and time must include a timezone."}
        )

    business = Business.objects.get(pk=business_id)
    supplier = Supplier.objects.get(
        pk=supplier_id,
        business=business,
        is_active=True,
    )

    purchase = SupplierPurchase.objects.create(
        business=business,
        supplier=supplier,
        purchase_number=purchase_number,
        purchased_at=purchased_at,
        notes=notes,
        created_by=user,
    )

    for item in validated_lines:
        product = Product.objects.get(
            pk=item["product_id"],
            business=business,
            is_active=True,
        )

        PurchaseLine.objects.create(
            purchase=purchase,
            business=business,
            product=product,
            quantity=item["quantity"],
            unit_price=item["unit_price"],
        )

    _calculate_total(purchase)
    return purchase


@transaction.atomic
def update_draft_purchase(*, purchase_id, user, supplier_id=None,
                          purchased_at=None, lines=None, notes=None):
    purchase = SupplierPurchase.objects.select_for_update().get(
        pk=purchase_id
    )

    _validate_membership(
        user=user,
        business_id=purchase.business_id,
    )

    if purchase.status != SupplierPurchase.Status.DRAFT:
        raise ValidationError("Only draft purchases can be edited.")

    if supplier_id is not None:
        purchase.supplier = Supplier.objects.get(
            pk=supplier_id,
            business=purchase.business,
            is_active=True,
        )

    if purchased_at is not None:
        if not timezone.is_aware(purchased_at):
            raise ValidationError(
                {"purchased_at": "Purchase date and time must include a timezone."}
            )
        purchase.purchased_at = purchased_at

    if notes is not None:
        purchase.notes = notes

    purchase.save()

    if lines is not None:
        validated_lines = _validate_lines(lines)

        for old_line in purchase.lines.all():
            old_line.soft_delete(user=user)

        for item in validated_lines:
            product = Product.objects.get(
                pk=item["product_id"],
                business=purchase.business,
                is_active=True,
            )

            PurchaseLine.objects.create(
                purchase=purchase,
                business=purchase.business,
                product=product,
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )

    _calculate_total(purchase)
    return purchase


@transaction.atomic
def receive_purchase(*, purchase_id, user):
    purchase = SupplierPurchase.objects.select_for_update().get(
        pk=purchase_id
    )

    _validate_membership(
        user=user,
        business_id=purchase.business_id,
    )

    if purchase.status != SupplierPurchase.Status.DRAFT:
        raise ValidationError("Only draft purchases can be received.")

    lines = list(
        purchase.lines.select_related("product").order_by("pk")
    )

    if not lines:
        raise ValidationError("Cannot receive a purchase without product lines.")

    received_at = timezone.now()

    for line in lines:
        if line.business_id != purchase.business_id:
            raise ValidationError("Purchase line belongs to another business.")

        if line.product.business_id != purchase.business_id:
            raise ValidationError("Product belongs to another business.")

        # Inventory is tracked only for products with track_stock enabled.
        if not line.product.track_stock:
            continue

        StockMovement.objects.create(
            business=purchase.business,
            product=line.product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=line.quantity,
            occurred_at=received_at,
            reason=f"Received from purchase {purchase.purchase_number}",
            created_by=user,
            purchase_line=line,
        )

    purchase.status = SupplierPurchase.Status.RECEIVED
    purchase.received_at = received_at
    purchase.save(update_fields=["status", "received_at", "updated_at"])

    return purchase
