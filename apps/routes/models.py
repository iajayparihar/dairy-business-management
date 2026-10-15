import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.businesses.models import Business, BusinessMembership
from apps.common.models import SoftDeleteModel
from apps.common.validators import validate_same_business
from apps.customers.models import Customer
from apps.products.models import Product


def _uuid_pk():
    return models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


class Route(SoftDeleteModel):
    """A named, reusable delivery route; assigned_to is the usual delivery boy."""

    id = _uuid_pk()
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="routes")
    name = models.CharField(max_length=100)
    assigned_to = models.ForeignKey(
        BusinessMembership, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "routes_route"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "name"],
                condition=Q(is_deleted=False),
                name="uniq_route_business_name",
            ),
        ]

    def __str__(self):
        return self.name


class RouteCustomer(models.Model):
    """A customer's place on a route. A customer is on one route at a time."""

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="stops")
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="route_stops")
    sequence = models.PositiveIntegerField()

    class Meta:
        db_table = "routes_route_customer"
        ordering = ["route", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["customer"], name="uniq_route_customer"),
            models.UniqueConstraint(fields=["route", "sequence"], name="uniq_route_sequence"),
        ]

    def clean(self):
        validate_same_business(self, self.route, self.customer)


class StandingOrder(models.Model):
    """What a customer normally receives; the one-tap default on the route."""

    id = _uuid_pk()
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="standing_orders")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    shift = models.CharField(max_length=10, choices=[("MORNING", "Morning"), ("EVENING", "Evening")], default="MORNING")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0.001)])
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "routes_standing_order"
        constraints = [
            models.UniqueConstraint(fields=["customer", "product", "shift"], name="uniq_standing_order"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="standing_order_qty_positive"),
        ]

    def clean(self):
        validate_same_business(self, self.customer, self.product)


class RouteRun(models.Model):
    """One route on one date and shift: the unit that is closed at the end."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    id = _uuid_pk()
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="route_runs")
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="runs")
    run_date = models.DateField()
    shift = models.CharField(max_length=10, choices=[("MORNING", "Morning"), ("EVENING", "Evening")], default="MORNING")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(
        BusinessMembership, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reopen_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "routes_run"
        ordering = ["-run_date", "route__name"]
        constraints = [
            models.UniqueConstraint(fields=["route", "run_date", "shift"], name="uniq_run_route_date_shift"),
        ]
        indexes = [models.Index(fields=["business", "run_date"], name="run_biz_date_idx")]


class RouteVisit(models.Model):
    """Expected visit of one customer in a run, and how it ended."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DELIVERED = "DELIVERED", "Delivered"
        NOT_DELIVERED = "NOT_DELIVERED", "Not delivered"
        ABSENT = "ABSENT", "Customer absent"
        SKIPPED = "SKIPPED", "Skipped"

    id = _uuid_pk()
    run = models.ForeignKey(RouteRun, on_delete=models.CASCADE, related_name="visits")
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="visits")
    sequence = models.PositiveIntegerField()
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True, default="")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    recorded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "routes_visit"
        ordering = ["run", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["run", "customer"], name="uniq_visit_run_customer"),
        ]
