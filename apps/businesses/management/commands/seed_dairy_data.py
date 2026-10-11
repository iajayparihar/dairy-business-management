
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.customers.models import Customer
from apps.deliveries.services import create_delivery
from apps.products.models import Product, ProductCategory, ProductPrice
from apps.suppliers.models import Supplier


User = get_user_model()


class Command(BaseCommand):
    help = "Create repeatable demo data for Abhigyan Dairy and Vij Dairy"

    @transaction.atomic
    def handle(self, *args, **options):
        if not connection.in_atomic_block:
            raise CommandError("Seed must run inside a transaction.")

        self.stdout.write("Creating dairy demo data...")

        # --------------------------------------------------
        # 1. USERS
        # --------------------------------------------------
        users = {}

        user_data = [
            ("abhigyan_owner", "Abhigyan", "Owner"),
            ("vij_owner", "Vij", "Owner"),
            ("staff1", "Staff", "One"),
            ("staff2", "Staff", "Two"),
            ("staff3", "Staff", "Three"),
            ("staff4", "Staff", "Four"),
            ("staff5", "Staff", "Five"),
        ]

        for username, first_name, last_name in user_data:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": first_name,
                    "last_name": last_name,
                    "is_active": True,
                },
            )

            if created:
                user.set_password("DairyTest@123")
                user.save(update_fields=["password"])

            users[username] = user

        # --------------------------------------------------
        # 2. BUSINESSES AND MEMBERSHIPS
        # --------------------------------------------------
        abhigyan = Business.objects.filter(
            slug="abhigyan-dairy"
        ).first()

        if abhigyan is None:
            abhigyan = create_business(
                name="Abhigyan Dairy",
                slug="abhigyan-dairy",
                owner=users["abhigyan_owner"],
            )

        vij = Business.objects.filter(slug="vij-dairy").first()

        if vij is None:
            vij = create_business(
                name="Vij Dairy",
                slug="vij-dairy",
                owner=users["vij_owner"],
            )

        businesses = {
            "abhigyan": abhigyan,
            "vij": vij,
        }

        memberships = [
            (abhigyan, users["abhigyan_owner"], "OWNER"),
            (abhigyan, users["staff1"], "STAFF"),
            (abhigyan, users["staff2"], "STAFF"),
            (abhigyan, users["staff3"], "STAFF"),
            (vij, users["vij_owner"], "OWNER"),
            (vij, users["staff4"], "STAFF"),
            (vij, users["staff5"], "STAFF"),
        ]

        for business, user, role in memberships:
            BusinessMembership.objects.get_or_create(
                business=business,
                user=user,
                defaults={
                    "role": role,
                    "is_active": True,
                },
            )

        # --------------------------------------------------
        # 3. SUPPLIERS
        # --------------------------------------------------
        supplier_data = {
            "abhigyan": [
                ("SUP-001", "Supplier One"),
                ("SUP-002", "Supplier Two"),
            ],
            "vij": [
                ("SUP-002", "Supplier Two"),
                ("SUP-003", "Supplier Three"),
            ],
        }

        for business_key, entries in supplier_data.items():
            for code, name in entries:
                Supplier.objects.get_or_create(
                    business=businesses[business_key],
                    supplier_code=code,
                    defaults={
                        "name": name,
                        "supplier_type": Supplier.SupplierType.FARMER,
                        "is_active": True,
                    },
                )

        # --------------------------------------------------
        # 4. CUSTOMERS
        # --------------------------------------------------
        customer_names = [
            ("CUST-001", "Ajay"),
            ("CUST-002", "Falu"),
            ("CUST-003", "Vij"),
            ("CUST-004", "Rahul"),
            ("CUST-005", "Nisha"),
        ]

        customers = {}

        for business_key, business in businesses.items():
            customers[business_key] = {}

            for code, name in customer_names:
                customer, _ = Customer.objects.get_or_create(
                    business=business,
                    customer_code=code,
                    defaults={
                        "name": name,
                        "is_active": True,
                    },
                )
                customers[business_key][name] = customer

        # --------------------------------------------------
        # 5. CATEGORIES, PRODUCTS AND SELLING PRICES
        # --------------------------------------------------
        product_data = {
            "abhigyan": [
                ("MILK", "Cow Milk", Product.Unit.LITRE, "60.00"),
                ("GHEE", "Cow Ghee", Product.Unit.KILOGRAM, "800.00"),
                ("DAHI", "Dahi", Product.Unit.KILOGRAM, "100.00"),
            ],
            "vij": [
                ("MILK", "Cow Milk", Product.Unit.LITRE, "70.00"),
                ("GHEE", "Cow Ghee", Product.Unit.KILOGRAM, "500.00"),
                ("DAHI", "Dahi", Product.Unit.KILOGRAM, "80.00"),
            ],
        }

        now = timezone.now()
        initial_effective_from = now - timedelta(days=30)
        scheduled_change = now + timedelta(days=7)

        products = {}

        for business_key, business in businesses.items():
            category, _ = ProductCategory.objects.get_or_create(
                business=business,
                name="Dairy Products",
                defaults={
                    "description": "Milk and dairy products",
                    "is_active": True,
                },
            )

            products[business_key] = {}

            for sku, name, unit, price in product_data[business_key]:
                product, _ = Product.objects.get_or_create(
                    business=business,
                    sku=sku,
                    defaults={
                        "name": name,
                        "category": category,
                        "unit": unit,
                        "is_active": True,
                        "track_stock": False,
                    },
                )
                products[business_key][sku] = product

                ProductPrice.objects.get_or_create(
                    business=business,
                    product=product,
                    price_type=ProductPrice.PriceType.SELLING,
                    effective_from=initial_effective_from,
                    defaults={
                        "unit_price": Decimal(price),
                        "effective_to": scheduled_change,
                        "created_by": users[
                            "abhigyan_owner"
                            if business_key == "abhigyan"
                            else "vij_owner"
                        ],
                    },
                )

                # Schedule a Milk price change seven days from now.
                if sku == "MILK":
                    future_price = (
                        Decimal(price) + Decimal("5.00")
                    )

                    ProductPrice.objects.get_or_create(
                        business=business,
                        product=product,
                        price_type=ProductPrice.PriceType.SELLING,
                        effective_from=scheduled_change,
                        defaults={
                            "unit_price": future_price,
                            "effective_to": None,
                            "created_by": users[
                                "abhigyan_owner"
                                if business_key == "abhigyan"
                                else "vij_owner"
                            ],
                        },
                    )

        # --------------------------------------------------
        # 6. SAMPLE DELIVERIES
        # --------------------------------------------------
        # Create only when the customer has no delivery yet
        # for this seed timestamp and product.
        delivery_time = now - timedelta(hours=1)

        sample_deliveries = [
            ("abhigyan", "Ajay", "MILK", "2.000"),
            ("abhigyan", "Falu", "MILK", "1.000"),
            ("abhigyan", "Rahul", "DAHI", "0.500"),
            ("vij", "Vij", "MILK", "2.000"),
            ("vij", "Nisha", "GHEE", "1.000"),
        ]

        for business_key, customer_name, sku, quantity in sample_deliveries:
            business = businesses[business_key]
            customer = customers[business_key][customer_name]
            product = products[business_key][sku]

            exists = business.deliveries.filter(
                customer=customer,
                product=product,
                delivered_at=delivery_time,
            ).exists()

            if not exists:
                create_delivery(
                    business_id=business.pk,
                    customer_id=customer.pk,
                    product_id=product.pk,
                    delivered_at=delivery_time,
                    quantity=Decimal(quantity),
                    user=users[
                        "abhigyan_owner"
                        if business_key == "abhigyan"
                        else "vij_owner"
                    ],
                    notes="Seed demo delivery",
                )

        self.stdout.write(
            self.style.SUCCESS(
                "Seed completed successfully!\n"
                "Businesses: Abhigyan Dairy, Vij Dairy\n"
                "Users: 2 owners and 5 staff\n"
                "Suppliers, customers, products and prices created\n"
                "Sample deliveries created\n"
                "Milk price change scheduled for 7 days from now"
            )
        )
