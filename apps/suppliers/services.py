from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import BusinessMembership
from apps.suppliers.models import Supplier, SupplierMilkRate


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