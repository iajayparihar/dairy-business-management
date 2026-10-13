
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.businesses.models import Business
from apps.businesses.services import require_business_membership


class IsBusinessMember(BasePermission):
    """
    Enforce business-level access.

    Superusers may access any active business without membership.
    Normal users must have an active membership and satisfy the
    view's required role. Object access remains business-scoped.
    """

    message = "You do not have permission to access this business."

    def has_permission(self, request, view):
        business_id = (
            view.kwargs.get("business_id")
            or view.kwargs.get("business_pk")
        )

        if not business_id:
            return False

        user = request.user

        if (
            not user
            or not user.is_authenticated
            or not user.is_active
        ):
            return False

        if user.is_superuser:
            business = Business.objects.filter(
                pk=business_id,
                is_active=True,
            ).first()

            if business is None:
                return False

            request.business = business
            request.business_membership = None
            return True

        allowed_roles = getattr(
            view,
            "required_business_roles",
            None,
        )

        try:
            membership = require_business_membership(
                user=user,
                business_id=business_id,
                allowed_roles=allowed_roles,
            )
        except (PermissionDenied, ValueError, TypeError):
            return False

        request.business = membership.business
        request.business_membership = membership
        return True

    def has_object_permission(self, request, view, obj):
        business = getattr(request, "business", None)

        if business is None:
            return False

        object_business_id = getattr(obj, "business_id", None)

        if object_business_id is None:
            return False

        if request.user.is_superuser:
            return object_business_id == business.pk

        membership = getattr(
            request,
            "business_membership",
            None,
        )

        if membership is None:
            return False

        return object_business_id == membership.business_id
