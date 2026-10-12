
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.inventory.models import StockMovement
from apps.products.models import Product
from apps.suppliers.models import Supplier, SupplierMilkRate
from apps.purchases.models import MilkCollection, SupplierPurchase, PurchaseLine
from apps.suppliers.services import calculate_supplier_milk_amount
from apps.businesses.services import require_business_membership

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

        if unit_price < 0 or unit_price.as_tuple().exponent < -4:
            raise ValidationError(
                {
                    "unit_price": (
                        "Price must be non-negative "
                        "with at most 4 decimal places."
                    )
                }
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



@transaction.atomic
def create_milk_collection(
    *,
    business_id,
    supplier_id,
    collection_number,
    milk_type,
    collected_at,
    litres,
    fat,
    user,
):
    require_business_membership(
        user=user,
        business_id=business_id,
        allowed_roles=["OWNER", "MANAGER"],
    )

    if not timezone.is_aware(collected_at):
        raise ValidationError({
            "collected_at": "Collection time must include a timezone."
        })

    if milk_type not in MilkCollection.MilkType.values:
        raise ValidationError({"milk_type": "Invalid milk type."})

    collection_number = (collection_number or "").strip()

    if not collection_number:
        raise ValidationError({
            "collection_number": "Collection number is required."
        })

    # Prefix leaves room for the purchase number's 50-character limit.
    purchase_number = f"MILK-{collection_number}"

    if len(purchase_number) > 50:
        raise ValidationError({
            "collection_number": "Collection number is too long."
        })

    def decimal_value(value, field, minimum, max_places):
        from decimal import Decimal, InvalidOperation

        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({field: "Enter a valid number."})

        if not number.is_finite() or number < minimum:
            raise ValidationError({field: "Invalid value."})

        if number.as_tuple().exponent < -max_places:
            raise ValidationError({
                field: f"Maximum {max_places} decimal places allowed."
            })

        return number

    litres = decimal_value(litres, "litres", Decimal("0.001"), 3)
    fat = decimal_value(fat, "fat", Decimal("0"), 2)

    # Serialize collection creation within the business.
    # The unique database constraint remains the final safeguard.
    business = Business.objects.select_for_update().get(
        pk=business_id
    )

    supplier = Supplier.objects.filter(
        pk=supplier_id,
        business_id=business.id,
        is_active=True,
        is_deleted=False,
    ).first()

    if supplier is None:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    if MilkCollection.objects.filter(
        business=business,
        collection_number=collection_number,
    ).exists():
        raise ValidationError({
            "collection_number": "This collection number already exists."
        })

    if SupplierPurchase.objects.filter(
        business=business,
        purchase_number=purchase_number,
    ).exists():
        raise ValidationError({
            "collection_number": "A purchase with this number already exists."
        })

    calculation = calculate_supplier_milk_amount(
        business_id=business.id,
        supplier_id=supplier.id,
        milk_type=milk_type,
        collected_at=collected_at,
        litres=litres,
        fat=fat,
    )

    rate_record = calculation["rate_record"]
    amount = calculation["amount"]

    # Milk collection creates a payable, not a stock-tracked
    # product purchase. Therefore no PurchaseLine is created.
    purchase = SupplierPurchase.objects.create(
        business=business,
        supplier=supplier,
        purchase_number=purchase_number,
        purchased_at=collected_at,
        status=SupplierPurchase.Status.RECEIVED,
        notes=f"Milk collection {collection_number}",
        total_amount=amount,
        created_by=user,
        received_at=timezone.now(),
    )

    collection = MilkCollection.objects.create(
        business=business,
        supplier=supplier,
        rate_record=rate_record,
        collection_number=collection_number,
        milk_type=milk_type,
        collected_at=collected_at,
        litres=litres,
        fat=fat,
        applied_rate=rate_record.rate,
        amount=amount,
        purchase=purchase,
        created_by=user,
    )

    return collection