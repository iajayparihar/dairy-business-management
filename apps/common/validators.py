from django.core.exceptions import ValidationError


def validate_same_business(instance, *related_objects):
    """
    Validate that related business-owned records belong
    to the same business as the current instance.
    """
    business_id = getattr(instance, "business_id", None)

    if business_id is None:
        return

    for related in related_objects:
        if related is None:
            continue

        related_business_id = getattr(
            related,
            "business_id",
            None,
        )

        if related_business_id != business_id:
            raise ValidationError(
                "Related records must belong to the same business."
            )