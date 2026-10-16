from django.db import migrations


def backfill_full_reversal_unwinds(apps, schema_editor):
    """
    Until now a reversal neutralised every allocation of its payment at
    read time. Make that explicit: one unwind row per existing allocation,
    for the allocation's full amount. No existing row is changed and no
    money is recomputed; effective allocation is 0 exactly as before.
    """
    Reversal = apps.get_model("payments", "SupplierPaymentReversal")
    Allocation = apps.get_model("payments", "SupplierPaymentAllocation")
    Unwind = apps.get_model("payments", "SupplierPaymentAllocationReversal")

    rows = [
        Unwind(
            business_id=reversal.business_id,
            reversal=reversal,
            allocation=allocation,
            amount=allocation.amount,
        )
        for reversal in Reversal.objects.all().iterator()
        for allocation in Allocation.objects.filter(payment_id=reversal.payment_id)
    ]
    Unwind.objects.bulk_create(rows, batch_size=500)


def drop_unwinds(apps, schema_editor):
    apps.get_model("payments", "SupplierPaymentAllocationReversal").objects.all().delete()


class Migration(migrations.Migration):
    # Separate from 0004 on purpose: Postgres refuses schema changes (the
    # deferred index creation) on a table with pending FK trigger events.

    dependencies = [
        ("payments", "0004_partial_payment_reversals"),
    ]

    operations = [
        migrations.RunPython(backfill_full_reversal_unwinds, drop_unwinds),
    ]
