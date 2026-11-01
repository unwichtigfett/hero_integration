"""Rename imported HERO projects to their HERO ID ``<trade>-<display_id>-H``.

Owner decision 2026-11-01: every HERO project ID must be taken in Frappe before
productive work starts, and HERO IDs end in -H.  Older imports still carry
``PROJ-…`` IDs.  The trade is read from HERO (``measure``, read-only); project
titles are never used to guess it.  Only rows the business-number preview marks
as safe are renamed; Frappe's rename moves every reference.

    bench --site <site> execute hero_integration.services.project_rename.rename_hero_projects
    bench --site <site> execute hero_integration.services.project_rename.rename_hero_projects \
        --kwargs "{'dry_run': 0, 'confirm': 'RENAME HERO PROJECTS'}"
"""

from __future__ import annotations

from collections import Counter

import frappe

CONFIRMATION = "RENAME HERO PROJECTS"
COMMIT_EVERY = 50


def hero_project_trades(client, links) -> dict[str, dict[str, str]]:
    """{source_instance: {source_id: trade_code}} from HERO's measure, 50 IDs per read-only query."""
    from hero_integration.client.graphql import HeroClientError
    from hero_integration.services.project_naming import ProjectTitleValidationError, project_trade

    trades: dict[str, dict[str, str]] = {}
    ids = sorted({int(link.source_id) for link in links if str(link.source_id).isdigit()})
    instance_of = {str(link.source_id): link.source_instance or "default" for link in links}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        document = "query HeroProjectTrades { " + " ".join(
            f"p{value}: project_match(project_match_id: {value}) {{ id measure {{ short name }} }}" for value in chunk
        ) + " }"
        try:
            data = client.query(document, {})
        except HeroClientError as error:
            data = getattr(error, "data", None) or {}
        data = data.get("data", data) if isinstance(data, dict) else {}
        for value in chunk:
            row = data.get(f"p{value}") if isinstance(data, dict) else None
            if not isinstance(row, dict) or row.get("id") != value:
                continue
            try:
                code, _name = project_trade({"measure": row.get("measure")})
            except ProjectTitleValidationError:
                continue
            trades.setdefault(instance_of[str(value)], {})[str(value)] = code
    return trades


def rename_hero_projects(dry_run: int | bool = 1, confirm: str = "", client=None) -> dict:
    """Rename safe HERO projects to their -H ID; returns counts (no names)."""
    frappe.only_for("System Manager")
    dry_run = bool(int(dry_run))
    if not dry_run and confirm != CONFIRMATION:
        frappe.throw(f'Renaming requires confirm="{CONFIRMATION}".')
    if frappe.get_all("HERO Integration Run", filters={"state": ["in", ["Queued", "Running"]]}, pluck="name"):
        frappe.throw("A HERO run is active; wait for it to finish.")
    from hero_integration.services.business_number_preview import preview_rows
    from hero_integration.services.guards import hero_project_naming

    links = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "source_type": "project",
                                                             "target_doctype": "Project"},
                           fields=["source_id", "source_instance"], limit_page_length=0)
    trades = hero_project_trades(client or _client(), links)
    rows = [row for row in preview_rows(trades) if row["doctype"] == "Project"]
    todo = [row for row in rows if row["safe_rename"] and row["proposed_name"] and row["proposed_name"] != row["current_name"]]
    report = {"dry_run": dry_run, "projects": len(rows), "trades_read": sum(len(v) for v in trades.values()),
              "already_named": sum(1 for row in rows if row["proposed_name"] == row["current_name"]),
              "to_rename": len(todo),
              "not_safe": dict(Counter(row["reason"][:80] for row in rows
                                       if not row["safe_rename"] and row["proposed_name"] != row["current_name"]))}
    if dry_run:
        return report
    renamed = failed = 0
    for row in todo:
        frappe.db.savepoint("hero_project_rename")
        try:
            with hero_project_naming():
                frappe.rename_doc("Project", row["current_name"], row["proposed_name"], rebuild_search=False,
                                  show_alert=False)
            renamed += 1
        except Exception:
            frappe.db.rollback(save_point="hero_project_rename")
            failed += 1
        if (renamed + failed) % COMMIT_EVERY == 0:
            frappe.db.commit()
    frappe.db.commit()
    report.update(renamed=renamed, failed=failed)
    return report


def _client():
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    return HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)
