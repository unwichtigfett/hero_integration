# ERPNext, Helpdesk and CRM: how they work together

**(decided)** marks decisions the owner has already taken.

## 1. Principles

1. **One owner per record.** Exactly one system creates and changes each business
   record. All other systems hold references, never copies.
2. **HERO is the operational source; ERPNext is the shared master copy.** HERO creates
   and changes customers, contacts, addresses, tasks and appointments. The import copies
   them one-way into ERPNext. Projects come from HERO **or are created directly in
   ERPNext** **(decided)**. Writing back to HERO comes later and has no priority
   **(decided)**.
3. **Helpdesk and CRM reference ERPNext.** Tickets, leads and deals carry
   `erp_customer` and `erp_project` links. The apps create no HD Customer or CRM
   Organization copies. Their own ERPNext mirrors stay disabled **(decided, option A)**.
4. **Contacts are shared natively.** Helpdesk and CRM both use the Frappe `Contact`
   that the HERO import maintains.
5. **Fail closed, notify.** The import holds or blocks records it cannot import
   unambiguously and notifies the administrators **(decided)**.

## 2. Who owns what

| Object | Owner (create/change) | ERPNext DocType | Helpdesk | CRM |
|---|---|---|---|---|
| Customer | HERO | Customer | `erp_customer` link | `erp_customer` link |
| Contact person | HERO (primary contact); Helpdesk/CRM may add contacts for new inquiries | Contact | `contact` (native) | `contacts` (native) |
| Address | HERO | Address | via Customer | via Customer |
| Project | HERO (imported) **or** ERPNext (created there) | Project (+ trade, business number for HERO projects) | `erp_project` link | `erp_project` link |
| Task / appointment | HERO | Task / Event | – | – |
| Inquiry / lead | CRM (later also HERO `KONT-` requests) | – | – | CRM Lead / Deal |
| Support case, grid-operator request | Helpdesk | – | HD Ticket | – |
| Employee, department, team | HRMS / ERPNext | Employee, Department | HD Team (members only) | – |
| User and role | System administrator | User, Role Profile | Agent roles | Sales roles |

Consequences:
- **Never create Customers by hand in ERPNext.** A manual Customer later meets its HERO
  namesake. The import then reports it as ambiguous instead of linking it.
- **Projects may be created in ERPNext.** Such a project has no HERO binding. The import
  never changes it, and the deletion guard and the purge leave it alone. Its creator sets
  its department. Never create the same project in HERO as well. The import never merges
  records. It reports a HERO project with the same number or title as a conflict or as
  ambiguous.

## 3. Flows

### 3.1 New inquiry → customer → project
1. An inquiry arrives by phone, e-mail or web form and becomes a **CRM Lead**. Its
   contact is a Frappe Contact.
2. CRM handles qualification (deal, offer status).
3. When the deal is won, the office creates the customer **in HERO**. It creates the
   project either in HERO or directly in ERPNext, never in both.
4. The next import creates the ERPNext Customer and Project. The deal then gets its
   `erp_customer` / `erp_project` links. A chosen project fills the customer automatically.
5. *Later:* HERO contact requests (`KONT-…`) can become CRM Leads automatically. Today
   the import brings in their tasks without a project **(decided)**.

### 3.2 Support / service / complaint
1. An e-mail to the support address creates an **HD Ticket**. Helpdesk finds or creates
   the Contact by e-mail.
2. If exactly one ERPNext Customer belongs to that contact, `erp_customer` fills
   automatically. Otherwise an agent selects it.
3. The agent selects `erp_project`. The customer then follows from the project. The
   system refuses a mismatch.
4. The ticket goes to the HD Team of the trade (PV, Elektro, Verwaltung/Netzbetreiber).

### 3.3 Grid-operator requests (Netzbetreiber)
Applications to the grid operator are project-bound correspondence with an external
party. They become HD Tickets of type **Netzanmeldung** in the team **Netzbetreiber**,
always with `erp_project`. The operator's e-mails thread into the ticket. The project
shows its open requests. HERO still owns the project status itself.

### 3.4 Execution in the field
Tasks and appointments come from HERO. Electricians and roofers see their assigned
Tasks/Events. They record time in Timesheets (HRMS) against the Project. Mapping HERO
users to ERPNext users and employees is an open step (section 6).

## 4. Matching and identity rules

- **HERO identity** = source instance + HERO ID, stored in HERO Integration Link
  (unique `source_key`). Business numbers name new records (`2222`, `PV-3333`).
- **Contact → Customer** works only through the Contact's Dynamic Link to a Customer,
  and only when that link is unique. An e-mail address shared by contacts of different
  customers never selects one.
- **Project → Customer** always wins over a guess. Save refuses a conflicting pair.
- **Deleted in HERO**: ERPNext deletes nothing. The import skips records of deleted HERO
  projects **(decided)**.

## 5. Storage and provision (hardened)

| Topic | State |
|---|---|
| Binding lookup on every save/delete | Indexed (`target_doctype`, `target_name`) |
| Deleting a HERO-bound record outside the integration | Refused; only the purge and a review rejection may delete |
| Removing all imported data | `hero_integration.services.purge.purge_hero_data` (dry run by default, phrase + "Allow deletion" + no active run; referenced records are kept) |
| Held records | Pending Review with Approve/Reject only **(decided)** |
| Reporting of not imported records | One notice per run, one per held record |
| Helpdesk/CRM mirrors | Save refuses enabling ERPNext HD Settings or CRM's "create customer on status change" |
| Read access for other apps | Agent/Agent Manager read Customer/Project/Contact; Sales User/Manager read Project |
| Development data | Anonymized package, kept outside the repository |
| Backups | Cron runs `bench --site all backup` every 6 h. Configure retention so a pre-import backup survives, e.g. `backup_limit` 30 and `keep_backups_for_hours` 720 (~7 days of 6-hourly backups). Archive pre-import backups manually outside the rotation. **Open**: encryption and an off-site copy |
| Secrets | HERO token and passwords are encrypted in `__Auth`. Back up the site `encryption_key` separately; restored backups cannot decrypt them without it |

## 6. Open steps (in recommended order)

1. **Backups**: encrypted backups (`encrypt_backup`; store the key outside the server),
   an off-site copy on a separate host, longer retention, and a separate copy of
   `encryption_key`. Run restore tests in a CI environment that is rebuilt and
   discarded anyway.
2. **Department per project**: on import, set `Project.department` from the trade (PV,
   ELEK, …). Team leaders then see only their trade through User Permissions.
3. **HERO user ↔ employee mapping**, so task assignments from HERO reach the right
   ERPNext user.
4. **Helpdesk teams and ticket types** for PV, Elektro, Verwaltung and Netzbetreiber,
   with assignment rules.
5. **Telephony**: the installed app supports only Exotel/Twilio. sipgate needs its own
   adapter that links calls to the Contact and the `erp_customer`.
6. **CRM leads from HERO `KONT-`** contact requests (owner decision required).
7. **Write-back to HERO**: later **(decided)**.
