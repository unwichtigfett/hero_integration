# ERPNext, Helpdesk and CRM: how they work together

Status: concept, 2026-11-01. Decisions already taken by the owner are marked **(decided)**.

## 1. Principles

1. **One owner per record type.** Every business object has exactly one system that
   creates and changes it. All other systems hold references, never copies.
2. **HERO is the operational source; ERPNext is the shared master copy.** Customers,
   contacts, addresses, projects, tasks and appointments are created and changed in HERO
   and imported one-way into ERPNext. Writing back to HERO comes later and is not a
   priority **(decided)**.
3. **Helpdesk and CRM reference ERPNext.** Tickets, leads and deals carry
   `erp_customer` and `erp_project` links. No HD Customer or CRM Organization copies are
   created, and the apps' own ERPNext mirrors stay disabled **(decided, option A)**.
4. **Contacts are shared natively.** Helpdesk and CRM both use the Frappe `Contact`
   that the HERO import maintains.
5. **Fail closed, notify.** Records that cannot be imported unambiguously are held or
   blocked, and the administrators are notified **(decided)**.

## 2. Who owns what

| Object | Owner (create/change) | ERPNext DocType | Helpdesk | CRM |
|---|---|---|---|---|
| Customer | HERO | Customer | `erp_customer` link | `erp_customer` link |
| Contact person | HERO (primary contact); Helpdesk/CRM may add contacts for new inquiries | Contact | `contact` (native) | `contacts` (native) |
| Address | HERO | Address | via Customer | via Customer |
| Project | HERO | Project (+ trade, business number) | `erp_project` link | `erp_project` link |
| Task / appointment | HERO | Task / Event | – | – |
| Inquiry / lead | CRM (later also HERO `KONT-` requests) | – | – | CRM Lead / Deal |
| Support case, grid-operator request | Helpdesk | – | HD Ticket | – |
| Employee, department, team | HRMS / ERPNext | Employee, Department | HD Team (members only) | – |
| User and role | System administrator | User, Role Profile | Agent roles | Sales roles |

Consequence: **nobody creates ERPNext Customers or Projects by hand.** A manually
created Customer would later meet its HERO namesake and be reported as ambiguous instead
of being linked.

## 3. Flows

### 3.1 New inquiry → customer → project
1. The inquiry arrives by phone, e-mail or web form and becomes a **CRM Lead**. Its
   contact is a Frappe Contact.
2. Qualification happens in CRM (deal, offer status).
3. When it is won, the office creates the customer and the project **in HERO**.
4. The next import creates the ERPNext Customer and Project. The deal is then linked
   with `erp_customer` / `erp_project`. A chosen project fills the customer automatically.
5. *Later:* HERO contact requests (`KONT-…`) can become CRM Leads automatically. Today
   their tasks are imported without a project **(decided)**.

### 3.2 Support / service / complaint
1. An e-mail to the support address creates an **HD Ticket**. Helpdesk finds or creates
   the Contact by e-mail.
2. `erp_customer` is filled automatically when exactly one ERPNext Customer belongs to
   that contact. Otherwise an agent selects it.
3. The agent selects `erp_project`. The customer then follows from the project, and a
   mismatch is refused.
4. The ticket is routed to the HD Team of the trade (PV, Elektro, Verwaltung/Netzbetreiber).
   See the company structure concept.

### 3.3 Grid-operator requests (Netzbetreiber)
Applications to the grid operator are project-bound correspondence with an external
party. They become HD Tickets of type **Netzanmeldung** in the team **Netzbetreiber**,
always with `erp_project`. The operator's e-mails thread into the ticket, and the
project shows its open requests. HERO remains the owner of the project status itself.

### 3.4 Execution in the field
Tasks and appointments come from HERO. Electricians and roofers see their assigned
Tasks/Events and record time in Timesheets (HRMS) against the Project. Mapping HERO
users to ERPNext users and employees is an open step (section 6).

## 4. Matching and identity rules

- **HERO identity** = source instance + HERO ID, stored in HERO Integration Link
  (unique `source_key`). Business numbers name new records (`2222`, `PV-3333`).
- **Contact → Customer** only through the Contact's Dynamic Link to a Customer, and only
  when it is unique. An e-mail address that belongs to several contacts of different
  customers never selects one.
- **Project → Customer** always wins over a guess. A conflicting pair is refused on save.
- **Deleted in HERO**: no deletion in ERPNext. Records of deleted HERO projects are not
  imported **(decided)**.

## 5. Storage and provision (hardened)

| Topic | State |
|---|---|
| Binding lookup on every save/delete | Indexed (`target_doctype`, `target_name`); before this it was a full table scan on each save |
| Deleting a HERO-bound record outside the integration | Refused; only the purge and a review rejection may delete |
| Removing all imported data | `hero_integration.services.purge.purge_hero_data` (dry run by default, phrase + "Allow deletion" + no active run; referenced records are kept) |
| Held records | Pending Review with Approve/Reject only **(decided)** |
| Reporting of not imported records | One notice per run, one per held record |
| Helpdesk/CRM mirrors | Enabling ERPNext HD Settings, or CRM's "create customer on status change", is refused on save |
| Read access for other apps | Agent/Agent Manager read Customer/Project/Contact; Sales User/Manager read Project |
| Development data | Anonymized package outside the repo (`/home/frappe/hero-dev-packages`) |
| Backups | **Open**: only 3 local backups, not encrypted, no off-site copy (see 6) |
| Secrets | HERO token and passwords encrypted in `__Auth`; the site `encryption_key` must be backed up separately, otherwise restored backups cannot decrypt them |

## 6. Open steps (in recommended order)

1. **Backups**: daily encrypted backups (`encrypt_backup`), off-site copy to ci-server
   (`/srv/backups/hero_integration`), longer retention, and a separate copy of
   `encryption_key`. A restore test in the ci-server CI pipeline is preferable, because that
   environment is rebuilt and discarded anyway.
2. **Department per project**: on import, set `Project.department` from the trade (PV,
   ELEK, …). Team leaders then see only their trade through User Permissions. See the
   structure concept.
3. **HERO user ↔ employee mapping**, so task assignments from HERO reach the right
   ERPNext user.
4. **Helpdesk teams and ticket types** for PV, Elektro, Verwaltung and Netzbetreiber,
   with assignment rules.
5. **Telephony**: the installed app supports Exotel/Twilio only. sipgate needs its own
   adapter that links calls to the Contact and the `erp_customer`.
6. **CRM leads from HERO `KONT-`** contact requests (owner decision required).
7. **Write-back to HERO**: later **(decided)**.
