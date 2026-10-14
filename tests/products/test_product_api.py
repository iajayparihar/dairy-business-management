
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, BusinessMembership
from apps.businesses.services import create_business
from apps.products.models import Product, ProductCategory

User = get_user_model()


class ProductAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.owner = User.objects.create_user(
            username="api_owner",
            password="test-password-123",
        )
        self.staff = User.objects.create_user(
            username="api_staff",
            password="test-password-123",
        )
        self.outsider = User.objects.create_user(
            username="api_outsider",
            password="test-password-123",
        )

        self.business_a = create_business(
            name="API Dairy A",
            slug="api-dairy-a",
            owner=self.owner,
        )
        self.business_b = Business.objects.create(
            name="API Dairy B",
            slug="api-dairy-b",
        )

        BusinessMembership.objects.create(
            business=self.business_a,
            user=self.staff,
            role=BusinessMembership.Role.STAFF,
            is_active=True,
        )

        self.category_a = ProductCategory.objects.create(
            business=self.business_a,
            name="Milk",
        )
        self.product_a = Product.objects.create(
            business=self.business_a,
            category=self.category_a,
            sku="MILK-001",
            name="Cow Milk",
            unit=Product.Unit.LITRE,
        )
        self.product_b = Product.objects.create(
            business=self.business_b,
            sku="MILK-001",
            name="Buffalo Milk",
            unit=Product.Unit.LITRE,
        )

        self.product_list_url = reverse(
            "products:product-list-create",
            kwargs={"business_id": self.business_a.pk},
        )
        self.product_detail_url = reverse(
            "products:product-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.product_a.pk,
            },
        )
        
        self.category_b = ProductCategory.objects.create(
            business=self.business_b,
            name="Milk",
        )
        
        self.category_list_url = reverse(
            "products:category-list-create",
            kwargs={"business_id": self.business_a.pk},
        )


    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(self.product_list_url)

        self.assertEqual(response.status_code, 401)

    def test_owner_can_list_own_products(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(self.product_list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            str(response.data[0]["id"]),
            str(self.product_a.pk),
        )

    def test_outsider_cannot_list_products(self):
        self.client.force_authenticate(user=self.outsider)

        response = self.client.get(self.product_list_url)

        self.assertEqual(response.status_code, 403)

    def test_foreign_product_is_not_retrievable(self):
        self.client.force_authenticate(user=self.owner)

        foreign_url = reverse(
            "products:product-detail",
            kwargs={
                "business_id": self.business_a.pk,
                "pk": self.product_b.pk,
            },
        )

        response = self.client.get(foreign_url)

        self.assertEqual(response.status_code, 404)

    def test_staff_cannot_create_products(self):
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            self.product_list_url,
            {
                "sku": "MILK-002",
                "name": "New Milk",
                "unit": Product.Unit.LITRE,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_owner_can_create_product(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.product_list_url,
            {
                "sku": "MILK-002",
                "name": "New Milk",
                "unit": Product.Unit.LITRE,
                "category": str(self.category_a.pk),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            Product.objects.filter(
                business=self.business_a,
                sku="MILK-002",
            ).exists()
        )

    def test_product_delete_is_soft_delete(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.delete(self.product_detail_url)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            Product.all_objects.filter(
                pk=self.product_a.pk,
                is_deleted=True,
            ).exists()
        )



    def test_owner_can_create_category(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            self.category_list_url,
            {
                "name": "Curd",
                "description": "Fresh dairy curd",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ProductCategory.objects.filter(
                business=self.business_a,
                name="Curd",
            ).exists()
        )

    def test_staff_cannot_create_category(self):
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            self.category_list_url,
            {"name": "Curd"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_owner_can_update_own_product(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.patch(
            self.product_detail_url,
            {"name": "Updated Cow Milk"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.name, "Updated Cow Milk")

    def test_product_cannot_be_updated_with_foreign_category(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.patch(
            self.product_detail_url,
            {"category": str(self.category_b.pk)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

        self.product_a.refresh_from_db()
        self.assertEqual(self.product_a.category, self.category_a)
