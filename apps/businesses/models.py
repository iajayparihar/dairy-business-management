
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from apps.common.models import SoftDeleteModel

slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message="Use lowercase letters, numbers, and single hyphens only.",
)


class Business(SoftDeleteModel):
    """
    Represents an independent dairy business in the SaaS platform.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(max_length=200)

    slug = models.SlugField(
        max_length=100,
        unique=True,
        validators=[slug_validator],
    )

    phone = models.CharField(max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")

    timezone = models.CharField(
        max_length=64,
        default="Asia/Kolkata",
    )

    currency = models.CharField(
        max_length=3,
        default="INR",
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses_business"
        ordering = ["name"]
        indexes = [
            models.Index(
                fields=["is_active", "name"],
                name="biz_active_name_idx",
            ),
        ]

    def __str__(self):
        return self.name


class BusinessMembership(SoftDeleteModel):
    """
    Connects a user to a business and defines their business-level role.
    """

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        MANAGER = "MANAGER", "Manager"
        STAFF = "STAFF", "Staff"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name="memberships",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="business_memberships",
    )

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.STAFF,
    )

    is_active = models.BooleanField(default=True)

    joined_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses_membership"
        ordering = ["business__name", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "user"],
                name="uniq_business_user_membership",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "is_active"],
                name="bizmem_user_active_idx",
            ),
            models.Index(
                fields=["business", "is_active", "role"],
                name="bizmem_biz_active_role_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.user} — {self.business} "
            f"({self.get_role_display()})"
        )
