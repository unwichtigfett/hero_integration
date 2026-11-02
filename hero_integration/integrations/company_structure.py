"""Company structure building blocks.

Create-only: existing departments, designations, role profiles, Helpdesk teams and
ticket types are never changed, and no personal data is created.  Run once after
the owner decisions:

    bench --site <site> execute hero_integration.integrations.company_structure.setup_company_structure
"""

from __future__ import annotations

import re

import frappe

DEPARTMENTS = ("Geschäftsführung", "Verwaltung", "Rechnungswesen", "Netzbetreiber", "Photovoltaik", "Elektro", "IT")
DESIGNATIONS = (
    "Geschäftsführer kaufmännisch", "Geschäftsführer technisch", "Teamleiter PV", "Teamleiter Elektro",
    "Verwaltungsmitarbeiter", "Sachbearbeiter Netzbetreiber", "Systemadministrator", "Elektriker", "Dachmonteur",
)
ROLE_PROFILES = {
    "Geschäftsführung": ("Accounts Manager", "Projects Manager", "HR Manager", "Sales Manager", "Purchase Manager",
                         "Stock Manager", "Agent Manager"),
    "Teamleitung": ("Projects User", "Agent", "Employee Self Service"),
    "Verwaltung": ("Accounts User", "Sales User", "Purchase User", "Projects User", "Agent"),
    "Systemadministration": ("System Manager", "Agent Manager"),
    "Monteur": ("Employee Self Service",),
}
HD_TEAMS = ("Verwaltung", "Netzbetreiber", "PV", "Elektro")
HD_TICKET_TYPES = {"Netzanmeldung": "Application or correspondence with the grid operator; always set the ERPNext Project."}
# HERO trade code -> department base name.  KLIM and SONS await an owner decision.
TRADE_DEPARTMENTS = {"PV": "Photovoltaik", "DS": "Photovoltaik", "ABS": "Photovoltaik", "ELEK": "Elektro"}
_TRADE_PREFIX = re.compile(r"^([A-Z]{2,5})-")


def setup_company_structure(company: str | None = None) -> dict:
    """Create the missing structure objects; returns what was created."""
    frappe.only_for("System Manager")
    company = company or frappe.db.get_single_value("Global Defaults", "default_company")
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(frappe._("A company with an abbreviation is required."))
    created = {"Department": [], "Designation": [], "Role Profile": [], "HD Ticket Type": []}
    root = "All Departments" if frappe.db.exists("Department", "All Departments") else None
    for base in DEPARTMENTS:
        if not frappe.db.exists("Department", f"{base} - {abbr}"):
            frappe.get_doc({"doctype": "Department", "department_name": base, "company": company,
                            "parent_department": root, "is_group": 0}).insert()
            created["Department"].append(base)
    for name in DESIGNATIONS:
        if not frappe.db.exists("Designation", name):
            frappe.get_doc({"doctype": "Designation", "designation_name": name}).insert()
            created["Designation"].append(name)
    for name, roles in ROLE_PROFILES.items():
        if not frappe.db.exists("Role Profile", name):
            profile = frappe.get_doc({"doctype": "Role Profile", "role_profile": name})
            for role in roles:
                if frappe.db.exists("Role", role):
                    profile.append("roles", {"role": role})
            profile.insert()
            created["Role Profile"].append(name)
    # HD Teams require members; they are created with ensure_hd_team once the users exist.
    if frappe.db.exists("DocType", "HD Ticket Type"):
        for name, description in HD_TICKET_TYPES.items():
            if not frappe.db.exists("HD Ticket Type", name):
                frappe.get_doc({"doctype": "HD Ticket Type", "name": name, "description": description}).insert()
                created["HD Ticket Type"].append(name)
    return created


def ensure_hd_team(name: str, users: list[str]) -> bool:
    """Create a Helpdesk team (one of HD_TEAMS) with its first members; never changes an existing team."""
    frappe.only_for("System Manager")
    if name not in HD_TEAMS or not users or frappe.db.exists("HD Team", name):
        return False
    team = frappe.get_doc({"doctype": "HD Team", "team_name": name})
    for user in users:
        team.append("users", {"user": user})
    team.insert()
    return True


def department_for_trade(trade_code: str | None, company: str | None) -> str | None:
    """The department of a HERO trade, if the mapping and the department exist."""
    base = TRADE_DEPARTMENTS.get((trade_code or "").upper())
    abbr = frappe.db.get_value("Company", company, "abbr") if company and base else None
    name = f"{base} - {abbr}" if abbr else None
    return name if name and frappe.db.exists("Department", name) else None


def _trade_of(project) -> str | None:
    """The HERO trade code from the title (``PV-1234 | …``) or the ID; older IDs are ``PROJ-…``."""
    from hero_integration.services.project_naming import TRADE_CATALOG

    for text in (project.project_name, project.name):
        match = _TRADE_PREFIX.match(text or "")
        if match and match.group(1) in TRADE_CATALOG:
            return match.group(1)
    return None


def backfill_project_departments(dry_run: int | bool = 1, fields: str = "department,project_type") -> dict:
    """Fill the department and the Project Type (Gewerk) of HERO projects where they are empty.

    Only Projects bound by a HERO Integration Link are touched; projects created in
    ERPNext keep what their author set.  Existing values are never overwritten.
    """
    frappe.only_for("System Manager")
    from hero_integration.integrations.project_form import project_type_for_trade

    dry_run = bool(int(dry_run))
    wanted = {field.strip() for field in str(fields).split(",") if field.strip()}
    bound = set(frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "target_doctype": "Project"},
                               pluck="target_name", limit_page_length=0))
    counts: dict[str, int] = {}
    projects = frappe.get_all("Project", fields=["name", "project_name", "company", "department", "project_type"],
                              limit_page_length=0)
    for project in projects:
        if project.name not in bound:
            counts["not_from_hero"] = counts.get("not_from_hero", 0) + 1
            continue
        trade = _trade_of(project)
        updates = {}
        if "department" in wanted and not project.department and (department := department_for_trade(trade, project.company)):
            updates["department"] = department
        if "project_type" in wanted and not project.project_type and (project_type := project_type_for_trade(trade)):
            updates["project_type"] = project_type
        for field, value in updates.items():
            counts[f"{field}: {value}"] = counts.get(f"{field}: {value}", 0) + 1
        if not updates:
            counts["unchanged"] = counts.get("unchanged", 0) + 1
        elif not dry_run:
            frappe.db.set_value("Project", project.name, updates, update_modified=False)
    if not dry_run:
        frappe.db.commit()
    return {"dry_run": dry_run, "projects": counts}
