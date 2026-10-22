from django.db import migrations

from apps.users.phone import normalize_phone


def normalize(apps, schema_editor):
    """+91 E.164. Unparseable or duplicate numbers are blanked (first user keeps a duplicate)."""
    User = apps.get_model("users", "User")
    seen = set()
    for user in User.objects.exclude(phone_number="").order_by("id"):
        try:
            new = normalize_phone(user.phone_number)
        except ValueError:
            new = ""
        if new in seen:
            new = ""
        if new:
            seen.add(new)
        if new != user.phone_number:
            print(f"  users.User {user.pk}: phone {user.phone_number!r} -> {new!r}")
            user.phone_number = new
            user.save(update_fields=["phone_number"])


class Migration(migrations.Migration):
    dependencies = [("users", "0003_pwotp_constraints")]
    # Normalized numbers stay valid as free text, so there is nothing to undo.
    operations = [migrations.RunPython(normalize, migrations.RunPython.noop)]
