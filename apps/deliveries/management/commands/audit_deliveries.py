from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    """
    Read-only audit to run BEFORE applying deliveries.0004.

    Finds rows that would violate uniq_delivery_per_shift
    (business, customer, product, delivery_date, shift where status is
    DELIVERED). Works on the pre-migration schema, where every existing
    row becomes shift=MORNING, status=DELIVERED. Soft-deleted rows are
    included on purpose: the constraint covers them. Exit status 1 means
    the migration would fail. Nothing is written.
    """

    help = "Report duplicate deliveries that would violate uniq_delivery_per_shift."

    def handle(self, *args, **options):
        table = "deliveries_delivery"
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cols = {
                c.name for c in connection.introspection.get_table_description(cur, table)
            }
            # delivery_date exists since 0001; status/shift only after 0004.
            shift = "shift" if "shift" in cols else "'MORNING'"
            where = "status = 'DELIVERED'" if "status" in cols else "TRUE"
            date = "delivery_date" if "delivery_date" in cols else "delivered_at::date"

            cur.execute(f"SELECT count(*) FROM {table}")
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT business_id, customer_id, product_id, {date}, {shift},
                       count(*), array_agg(id ORDER BY id),
                       count(*) FILTER (WHERE is_deleted)
                FROM {table} WHERE {where}
                GROUP BY 1, 2, 3, 4, 5 HAVING count(*) > 1
                ORDER BY 6 DESC, 4
            """)
            dupes = cur.fetchall()
            refs = []
            if "client_ref" in cols:
                cur.execute(f"""
                    SELECT business_id, client_ref, count(*) FROM {table}
                    WHERE client_ref IS NOT NULL
                    GROUP BY 1, 2 HAVING count(*) > 1
                """)
                refs = cur.fetchall()

        self.stdout.write(
            f"Deliveries scanned: {total} "
            f"(schema {'post' if 'status' in cols else 'pre'}-0004)"
        )
        for b, c, p, d, s, n, ids, soft in dupes:
            self.stdout.write(self.style.ERROR(
                f"DUPLICATE x{n} ({soft} soft-deleted): business={b} customer={c} "
                f"product={p} date={d} shift={s} ids={[str(i) for i in ids]}"
            ))
        for b, ref, n in refs:
            self.stdout.write(self.style.ERROR(f"DUPLICATE client_ref x{n}: {b} {ref}"))

        if dupes or refs:
            raise CommandError(
                f"{len(dupes)} duplicate group(s), {len(refs)} duplicate "
                "client_ref(s). Resolve them before migrating."
            )
        self.stdout.write(self.style.SUCCESS("OK: no duplicates; safe to migrate."))
