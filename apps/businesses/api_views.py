from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import Http404
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.businesses import services
from apps.businesses.api_serializers import (
    StaffCreateSerializer,
    StaffReadSerializer,
    StaffUpdateSerializer,
)
from apps.businesses.models import Business, BusinessMembership
from apps.businesses.permissions import IsBusinessMember
from apps.common.pagination import StandardPagination
from apps.common.serializers import RejectUnknownFieldsMixin

Role = BusinessMembership.Role


def raise_api_validation_error(exc):
    detail = getattr(exc, "message_dict", None)
    if detail is None:
        detail = exc.messages
    raise ValidationError(detail)


class StaffAPIView(APIView):
    """Staff management: OWNER / MANAGER only, reads included."""

    permission_classes = [IsAuthenticated, IsBusinessMember]
    required_business_roles = [Role.OWNER, Role.MANAGER]

    def _read(self, membership):
        return StaffReadSerializer(membership, context={"request": self.request}).data

    def _run(self, fn, **kwargs):
        try:
            return fn(
                business=self.request.business,
                actor_membership=self.request.business_membership,
                **kwargs,
            )
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except BusinessMembership.DoesNotExist:
            raise Http404
        except DjangoPermissionDenied as exc:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(str(exc))


class StaffListCreateAPIView(StaffAPIView, generics.ListAPIView):
    serializer_class = StaffReadSerializer
    pagination_class = StandardPagination
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_manage"

    def get_throttles(self):  # only creating is throttled; listing is cheap
        return super().get_throttles() if self.request.method == "POST" else []

    def get_queryset(self):
        params = self.request.query_params
        qs = services.staff_for_business(self.request.business)
        active = params.get("is_active", "").lower()
        if active in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        elif active:
            raise ValidationError({"is_active": "Use true or false."})
        role = params.get("role", "")
        if role:
            if role not in Role.values:
                raise ValidationError({"role": "Unknown role."})
            qs = qs.filter(role=role)
        term = params.get("search", "").strip()
        if term:
            qs = qs.filter(
                Q(user__username__icontains=term) | Q(user__first_name__icontains=term)
                | Q(user__last_name__icontains=term) | Q(user__phone_number__icontains=term)
            )
        return qs

    def post(self, request, *args, **kwargs):
        s = StaffCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        membership, code, expires_at = self._run(services.create_staff, **s.validated_data)
        body = self._read(membership)
        body.update(setup_code=code, code_expires_at=expires_at)
        return Response(body, status=status.HTTP_201_CREATED)


class StaffDetailAPIView(StaffAPIView):
    def patch(self, request, business_id, membership_id):
        s = StaffUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        membership = self._run(
            services.update_staff, actor_user=request.user,
            membership_id=membership_id, **s.validated_data,
        )
        return Response(self._read(membership))


class StaffResetCodeAPIView(StaffAPIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_manage"

    def post(self, request, business_id, membership_id):
        code, expires_at = self._run(
            services.issue_staff_reset_code, actor_user=request.user,
            membership_id=membership_id,
        )
        return Response({"code": code, "expires_at": expires_at})


class BusinessDeleteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    confirm_name = serializers.CharField(max_length=200)


class BusinessAPIView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "shop_manage"

    def _run(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except DjangoValidationError as exc:
            raise_api_validation_error(exc)
        except Business.DoesNotExist:
            raise Http404


class BusinessDetailAPIView(BusinessAPIView):
    """DELETE = soft delete the shop. Owner only; the owner types the shop name to confirm."""

    permission_classes = [IsAuthenticated, IsBusinessMember]
    required_business_roles = [Role.OWNER]

    def delete(self, request, business_id):
        s = BusinessDeleteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        self._run(
            services.delete_business, business=request.business, actor=request.user,
            confirm_name=s.validated_data["confirm_name"],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class BusinessRestoreAPIView(BusinessAPIView):
    """Owner brings a deleted shop back. A deleted shop is unreachable through IsBusinessMember."""

    permission_classes = [IsAuthenticated]

    def post(self, request, business_id):
        business = self._run(
            services.restore_business, business_id=business_id, actor=request.user
        )
        return Response({"business_id": business.pk, "business_name": business.name})
