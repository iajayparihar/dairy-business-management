from apps.products.models import Product, ProductCategory, ProductPrice


def product_has_records(product):
    """Any delivery, sale, purchase, stock or production row points at this product."""
    for rel in Product._meta.related_objects:
        if rel.related_model is ProductPrice:
            continue  # a price list alone does not make a product undeletable
        if rel.related_model._base_manager.filter(**{rel.field.name: product}).exists():
            return True
    return False


def category_has_products(category):
    return Product.all_objects.filter(category=category).exists()
