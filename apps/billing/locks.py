from apps.customers.models import Customer


def lock_customer(business_id, customer_id):
    """
    The first lock of every writer of a customer's deliveries, bills or
    payments (lock order: route run -> customer -> bill -> delivery ->
    payment -> product).

    NO KEY UPDATE serializes that one customer without blocking the FK
    inserts that reference the row (deliveries, bills, payments).
    Returns None for an unknown or soft-deleted customer; an inactive one is
    still returned, because a customer who left can still be billed and pay.
    """
    return (
        Customer.objects.select_for_update(no_key=True)
        .filter(pk=customer_id, business_id=business_id)
        .first()
    )
