from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.businesses.models import BusinessMembership
from apps.suppliers.models import Supplier, SupplierMilkRate
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


@transaction.atomic
def create_supplier_milk_rate(
    *,
    business_id,
    supplier_id,
    milk_type,
    rate,
    effective_from,
    user,
    effective_to=None,
):
    membership = BusinessMembership.objects.filter(
        business_id=business_id,
        user=user,
        is_active=True,
        role__in=[
            BusinessMembership.Role.OWNER,
            BusinessMembership.Role.MANAGER,
        ],
    ).first()

    if membership is None:
        raise ValidationError(
            "You do not have permission to configure supplier milk rates."
        )

    if timezone.is_naive(effective_from):
        raise ValidationError("effective_from must be timezone-aware.")

    if effective_to is not None:
        if timezone.is_naive(effective_to):
            raise ValidationError("effective_to must be timezone-aware.")

        if effective_to <= effective_from:
            raise ValidationError(
                "effective_to must be later than effective_from."
            )

    if milk_type not in SupplierMilkRate.MilkType.values:
        raise ValidationError("Unsupported milk type.")

    supplier = Supplier.objects.filter(
        id=supplier_id,
        business_id=business_id,
        is_active=True,
        is_deleted=False,
    ).first()

    if supplier is None:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    try:
        rate = SupplierMilkRate._meta.get_field("rate").clean(
            rate,
            None,
        )
    except ValidationError:
        raise

    if rate < 0:
        raise ValidationError("Rate cannot be negative.")

    return SupplierMilkRate.objects.create(
        business_id=business_id,
        supplier=supplier,
        milk_type=milk_type,
        rate=rate,
        effective_from=effective_from,
        effective_to=effective_to,
        created_by=user,
    )
    




def calculate_milk_amount(*, litres, fat, rate):
    try:
        litres = Decimal(str(litres))
        fat = Decimal(str(fat))
        rate = Decimal(str(rate))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Litres, fat, and rate must be valid numbers.")

    if not all(value.is_finite() for value in (litres, fat, rate)):
        raise ValidationError("Values must be finite numbers.")

    if litres <= 0:
        raise ValidationError("Litres must be greater than zero.")

    if fat < 0:
        raise ValidationError("Fat cannot be negative.")

    if rate < 0:
        raise ValidationError("Rate cannot be negative.")

    return (litres * fat * rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def resolve_supplier_milk_rate(
    *,
    business_id,
    supplier_id,
    milk_type,
    collected_at,
):
    if timezone.is_naive(collected_at):
        raise ValidationError("collected_at must be timezone-aware.")

    if milk_type not in SupplierMilkRate.MilkType.values:
        raise ValidationError("Unsupported milk type.")

    supplier_exists = Supplier.objects.filter(
        id=supplier_id,
        business_id=business_id,
        is_active=True,
        is_deleted=False,
    ).exists()

    if not supplier_exists:
        raise ValidationError(
            "Supplier does not exist, is inactive, or belongs to another business."
        )

    rate = SupplierMilkRate.objects.filter(
        business_id=business_id,
        supplier_id=supplier_id,
        milk_type=milk_type,
        effective_from__lte=collected_at,
    ).filter(
        Q(effective_to__isnull=True)
        | Q(effective_to__gt=collected_at)
    ).first()

    if rate is None:
        raise ValidationError(
            "No milk rate is configured for this supplier, milk type, and timestamp."
        )

    return rate


def calculate_supplier_milk_amount(
    *,
    business_id,
    supplier_id,
    milk_type,
    collected_at,
    litres,
    fat,
):
    rate_record = resolve_supplier_milk_rate(
        business_id=business_id,
        supplier_id=supplier_id,
        milk_type=milk_type,
        collected_at=collected_at,
    )

    amount = calculate_milk_amount(
        litres=litres,
        fat=fat,
        rate=rate_record.rate,
    )

    return {
        "rate_record": rate_record,
        "milk_type": rate_record.milk_type,
        "litres": Decimal(str(litres)),
        "fat": Decimal(str(fat)),
        "applied_rate": rate_record.rate,
        "amount": amount,
    }
