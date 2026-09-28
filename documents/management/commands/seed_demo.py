from __future__ import annotations

import os
from argparse import ArgumentParser
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from documents.models import Vendor

# Fictional vendor master data. Northwind is stored under its long legal name and
# without a tax ID so the demo shows fuzzy matching ("Northwind Traders Ltd.").
DEMO_VENDORS = (
    {"name": "Brightline Software GmbH", "tax_id": "DE811234567"},
    {"name": "Northwind Traders Limited", "tax_id": ""},
    {"name": "Kestrel Freight Logistics B.V.", "tax_id": "NL853746291B01"},
)


class Command(BaseCommand):
    help = "Create demo vendors, an optional demo login and (optionally) process sample_data/."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--with-samples", action="store_true", help="Also process the files in sample_data/."
        )
        parser.add_argument("--username", default=os.environ.get("DEMO_USERNAME", "demo"))
        parser.add_argument(
            "--password",
            default=os.environ.get("DEMO_USER_PASSWORD", ""),
            help="Create/update a superuser with this password (default: $DEMO_USER_PASSWORD).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        for data in DEMO_VENDORS:
            vendor, created = Vendor.objects.get_or_create(
                name=data["name"], defaults={"tax_id": data["tax_id"]}
            )
            self.stdout.write(f"{'created' if created else 'exists '} vendor {vendor.name}")

        if options["password"]:
            user_model = get_user_model()
            user, _ = user_model.objects.get_or_create(username=options["username"])
            user.is_staff = user.is_superuser = True
            user.set_password(options["password"])
            user.save()
            self.stdout.write(f"demo login ready: username '{user.get_username()}'")
        else:
            self.stdout.write("no DEMO_USER_PASSWORD set - skipping demo user")

        if options["with_samples"]:
            folder = str(settings.BASE_DIR / "sample_data")
            call_command("process_folder", folder, stdout=getattr(self.stdout, "_out", self.stdout))
