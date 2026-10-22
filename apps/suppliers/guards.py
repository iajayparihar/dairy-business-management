def supplier_has_records(supplier):
    """True when deleting or renaming the supplier would orphan milk, purchases or payments."""
    from apps.payments.models import SupplierPayment
    from apps.purchases.models import MilkCollection, SupplierPurchase

    return (
        MilkCollection.all_objects.filter(supplier=supplier).exists()
        or SupplierPurchase.all_objects.filter(supplier=supplier).exists()
        or SupplierPayment.objects.filter(supplier=supplier).exists()
    )
