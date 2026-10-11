
def scope_to_business(queryset, business):
    """
    Restrict a queryset to one business.

    `business` may be a Business instance or a business UUID/ID.
    Fails closed when no business is provided.
    """
    if business is None:
        raise ValueError("Business context is required.")

    business_id = getattr(business, "pk", business)

    if business_id is None:
        raise ValueError("A valid business ID is required.")

    return queryset.filter(business_id=business_id)
