"""
Drift backstop for the billing -> finance bridge. Checks two invariants
that must always hold by construction if the bridge ran correctly for
every issued invoice:

1. Every charge item on a non-cancelled invoice has exactly one bridged
   RevenueEntry (the bridge never silently drops an item).
2. The sum of those entries' `amount` equals the invoice's `total_net`
   (each entry's amount is a direct copy of its charge item's
   total_amount, and total_net is defined as the sum of the same
   charge items' total_amount at issue time).

Received-amount reconciliation (accounting for refund history) is
intentionally out of scope here: a REFUNDED entry keeps its
`amount_received` as the historical fact of what was collected before
the refund, so "current net collected" can't be re-derived from
the ledger alone without a separate adjustment trail. That is a known,
documented limitation of this first cut, not an oversight.

Run on a schedule (post-deploy, nightly cron) — a non-zero exit code is
the alert signal for external monitoring to pick up.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.core.money import quantize2
from apps.finance.models import RevenueEntry


class Command(BaseCommand):
    help = (
        "Verify every non-cancelled invoice's charge items are correctly "
        "bridged to the finance ledger."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--clinic",
            help="Limit the check to one clinic's external_id (default: every clinic).",
        )

    def handle(self, *args, **options):
        # Function-local import — apps.finance never imports apps.billing
        # at module level, even in
        # tooling that isn't itself part of the import cycle the rule
        # exists to avoid.
        from apps.billing.models import Invoice, InvoiceStatus

        invoices = (
            Invoice.objects.filter(deleted=False)
            .exclude(status=InvoiceStatus.CANCELLED)
            .select_related("clinic")
        )
        if options["clinic"]:
            invoices = invoices.filter(clinic__external_id=options["clinic"])

        missing_count = 0
        mismatched_count = 0

        for invoice in invoices.iterator():
            items = list(invoice.charge_items.filter(deleted=False))
            if not items:
                continue

            entries = {
                entry.charge_item_id: entry
                for entry in RevenueEntry.objects.filter(
                    charge_item_id__in=[item.pk for item in items], deleted=False
                )
            }

            item_total = Decimal("0")
            for item in items:
                entry = entries.get(item.pk)
                if entry is None:
                    missing_count += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"MISSING bridge entry: clinic={invoice.clinic.name!r} "
                            f"invoice={invoice.number or invoice.external_id} "
                            f"charge_item={item.external_id} ({item.title})"
                        )
                    )
                    continue
                item_total += entry.amount

            if entries and quantize2(item_total) != invoice.total_net:
                mismatched_count += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"AMOUNT DRIFT: clinic={invoice.clinic.name!r} "
                        f"invoice={invoice.number or invoice.external_id} "
                        f"ledger_total={quantize2(item_total)} "
                        f"invoice.total_net={invoice.total_net}"
                    )
                )

        if missing_count or mismatched_count:
            raise CommandError(
                f"Ledger drift detected: {missing_count} missing bridge entries, "
                f"{mismatched_count} amount mismatches."
            )

        clinic_scope = options["clinic"] or "all clinics"
        self.stdout.write(self.style.SUCCESS(f"No drift detected ({clinic_scope})."))
