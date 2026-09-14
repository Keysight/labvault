"""
Create a LabTopology from the photonic OCS site JSON (static OCS triplets).

Example:
  python manage.py populate_ocs_lab_topology --config resources/ocs_photonic_site.json
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from connect.lab_topology_ocs_json import build_lab_topology_from_ocs_json


class Command(BaseCommand):
    help = "Populate a LabTopology from ocs_photonic_site_*.json (OCS + Ares + Arista, links from fixed_mapping)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--config",
            type=str,
            default="resources/ocs_photonic_site.json",
            help="Path to the site JSON file",
        )
        parser.add_argument(
            "--name",
            type=str,
            default="",
            help="Lab topology name (default: from JSON label)",
        )

    def handle(self, *args, **options):
        path = Path(options["config"])
        if not path.is_file():
            raise CommandError(f"Config not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        name = (options.get("name") or "").strip() or j.get("label") or path.stem
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).order_by("id").first()
        try:
            topo = build_lab_topology_from_ocs_json(str(path), name, user, description="")
        except Exception as e:
            raise CommandError(str(e)) from e
        n_nodes = topo.nodes.count()
        n_links = topo.link_edges.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Created lab topology id={topo.id} name={topo.name!r} — {n_nodes} nodes, {n_links} links"
            )
        )
        miss = [n.label for n in topo.nodes.filter(device__isnull=True)[:20]]
        if miss:
            self.stdout.write(
                self.style.WARNING(
                    "Nodes without Device FK (run import_ocs_site_config first): " + ", ".join(miss)
                )
            )
