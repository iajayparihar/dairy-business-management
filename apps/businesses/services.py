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



# ---------------------------------------------------------------- staff management
# Owners manage MANAGER / STAFF, managers manage STAFF. OWNER rows and a person's own
# row are never touched here (ownership changes stay in admin), which also keeps the
# last-owner protection of update_membership() unreachable from the API.

def manageable_roles(actor_membership):
    """Roles the actor may create or change. A superuser (no membership) acts as owner."""
    role = actor_membership.role if actor_membership else BusinessMembership.Role.OWNER
    R = BusinessMembership.Role
    return {R.MANAGER, R.STAFF} if role == R.OWNER else {R.STAFF}


def staff_for_business(business):
    from django.db.models import Exists, OuterRef

    elsewhere = BusinessMembership.objects.filter(
        user=OuterRef("user"), is_active=True, business__is_active=True
    ).exclude(business=OuterRef("business"))
    return (
        BusinessMembership.objects.filter(business=business)
        .select_related("user")
        .annotate(other_shop=Exists(elsewhere))
        .order_by("user__username", "id")
    )


def _target(business, membership_id):
    return staff_for_business(business).get(pk=membership_id)


def _check_manageable(actor_user, actor_membership, target):
    if target.user_id == actor_user.pk:
        raise ValidationError("You cannot change your own access here.")
    if target.role not in manageable_roles(actor_membership):
        raise PermissionDenied("Your role does not allow managing this person.")


def reset_blocker(target):
    """Why an owner cannot issue a reset code for this member, or None."""
    user = target.user
    if not target.is_active or not user.is_active:
        return "This person is inactive."
    if user.is_staff or user.is_superuser:
        return "This account cannot be reset from the app."
    if target.other_shop:
        return (
            "This person also works in another shop, so only they can reset their "
            "password (by email)."
        )
    return None


@transaction.atomic
def create_staff(*, business, actor_membership, username, role, first_name="",
                 last_name="", phone_number="", email=""):
    """New login for this shop. No password is set: the person starts with a reset code."""
    from django.contrib.auth import get_user_model
    from django.db import IntegrityError
    from django.db.models import Q

    from apps.users import services as user_services

    if role not in manageable_roles(actor_membership):
        raise PermissionDenied("Your role cannot add this kind of user.")
    User = get_user_model()
    errors = {}
    # username / email / phone are all accepted as a login identifier, so no two
    # accounts may share a value across them (resolve_user needs exactly one match)
    if User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
        errors["username"] = ["This username is already used."]
    if email and User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
        errors["email"] = ["This email already has an account."]
    if phone_number and User.objects.filter(phone_number=phone_number).exists():
        errors["phone_number"] = ["This phone number already has an account."]
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, email=email, password=None,
                first_name=first_name, last_name=last_name, phone_number=phone_number,
            )
            membership = BusinessMembership.objects.create(
                business=business, user=user, role=role, is_active=True
            )
    except IntegrityError:  # lost a race on one of the unique constraints
        raise ValidationError("Username or phone number was just taken. Try again.")
    code, expires_at = user_services.issue_code_for_user(user)
    return _target(business, membership.pk), code, expires_at


def update_staff(*, business, actor_user, actor_membership, membership_id, role=None,
                 is_active=None):
    target = _target(business, membership_id)
    _check_manageable(actor_user, actor_membership, target)
    if role is not None and role not in manageable_roles(actor_membership):
        raise PermissionDenied("Your role cannot give this role.")
    update_membership(membership_id=membership_id, role=role, is_active=is_active)
    return _target(business, membership_id)


def issue_staff_reset_code(*, business, actor_user, actor_membership, membership_id):
    from apps.users import services as user_services

    target = _target(business, membership_id)
    _check_manageable(actor_user, actor_membership, target)
    blocker = reset_blocker(target)
    if blocker:
        raise ValidationError(blocker)
    code, expires_at = user_services.issue_code_for_user(target.user)
    return code, expires_at
