
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.businesses.services import require_business_membership


class IsBusinessMember(BasePermission):
    """
    Require active membership in the business specified in the URL.

    Views may define required_business_roles to restrict access
    to particular membership roles.
    """

    message = "You do not have permission to access this business."

    def has_permission(self, request, view):
        business_id = (
            view.kwargs.get("business_id")
            or view.kwargs.get("business_pk")
        )

        # Business context must come from the URL, not an untrusted body.
        if not business_id:
            return False

        allowed_roles = getattr(
            view, "required_business_roles", None
        )

        try:
            membership = require_business_membership(
                user=request.user,
                business_id=business_id,
                allowed_roles=allowed_roles,
            )
        except (PermissionDenied, ValueError, TypeError):
            return False

        request.business_membership = membership
        request.business = membership.business
        return True

    def has_object_permission(self, request, view, obj):
        """
        Reject objects belonging to a different business.
        Querysets must still be tenant-scoped separately.
        """
        membership = getattr(
            request, "business_membership", None
        )

        if membership is None:
            return False

        object_business_id = getattr(obj, "business_id", None)

        if object_business_id is None:
            return False

        return object_business_id == membership.business_id
