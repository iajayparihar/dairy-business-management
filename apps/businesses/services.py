from django.db import transaction
from django.core.exceptions import PermissionDenied
from apps.businesses.models import Business, BusinessMembership
from django.core.exceptions import ValidationError
from django.db import transaction


@transaction.atomic
def create_business(*, name, slug, owner, **business_fields):
    """
    Create a business and assign its initial owner atomically.

    If either database operation fails, both operations are rolled back.
    """
    business = Business.objects.create(
        name=name,
        slug=slug,
        **business_fields,
    )

    BusinessMembership.objects.create(
        business=business,
        user=owner,
        role=BusinessMembership.Role.OWNER,
        is_active=True,
    )

    return business


@transaction.atomic
def update_membership(*, membership_id, role=None, is_active=None):
    """
    Update a membership while protecting the last active owner.

    All membership role and activation changes should use this service.
    """
    # Read the membership to identify the business whose row we must lock.
    membership_ref = BusinessMembership.objects.only(
        "business_id"
    ).get(pk=membership_id)

    # Serialize ownership changes for this business. NO KEY UPDATE still
    # serializes this service but, unlike a plain FOR UPDATE, does not block
    # every insert that references the business (ARCHITECTURE lock rules).
    Business.objects.select_for_update(no_key=True).get(
        pk=membership_ref.business_id
    )

    # Reload and lock the membership after locking the business.
    membership = BusinessMembership.objects.select_for_update().get(
        pk=membership_id,
        business_id=membership_ref.business_id,
    )

    new_role = membership.role if role is None else role
    new_is_active = (
        membership.is_active if is_active is None else is_active
    )

    # Is this update removing an active owner?
    removing_owner = (
        membership.role == BusinessMembership.Role.OWNER
        and membership.is_active
        and (
            new_role != BusinessMembership.Role.OWNER
            or not new_is_active
        )
    )

    if removing_owner:
        active_owner_count = BusinessMembership.objects.filter(
            business_id=membership.business_id,
            role=BusinessMembership.Role.OWNER,
            is_active=True,
        ).count()

        if active_owner_count <= 1:
            raise ValidationError(
                "The last active owner cannot be deactivated "
                "or demoted. Assign another active owner first."
            )

    membership.role = new_role
    membership.is_active = new_is_active
    membership.save(update_fields=["role", "is_active"])

    return membership


def require_business_membership(*, user, business_id, allowed_roles=None):
    """
    Validate active membership in an active business.

    If allowed_roles is provided, the member must also have
    one of those roles.
    """
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied("You do not have access to this business.")

    membership = (
        BusinessMembership.objects
        .select_related("business")
        .filter(
            business_id=business_id,
            user=user,
            is_active=True,
            business__is_active=True,
        )
        .first()
    )

    if membership is None:
        raise PermissionDenied("You do not have access to this business.")

    if allowed_roles is not None and membership.role not in allowed_roles:
        raise PermissionDenied(
            "Your role does not permit this operation."
        )

    return membership

