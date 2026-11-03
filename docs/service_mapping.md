# Service mapping and migration

Services are non-stock ERPNext Items in the existing `Services` Item Group.
The manual Synchronization page area **Services** queues `Service Migration`.
New Full Migration runs process services after articles so material source
links can already exist. A stored per-run step list preserves old full-run
checkpoints. No automatic polling or outbound mutation is added.

## Configurable scalar mapping

`HeroServices` is a Query adapter v2 operation for `supply_services`, paged
with first/offset ordered by `id` (100 records per page). Its approved own
mapping is required for execution; default rules only provide an editable draft. Set records_path to
`supply_services`, identity_path to `id`, and bind first/offset to
`$page_size`/`$offset`; select every configured field and required guards
`id`, `company_id`, `is_deleted`, `modified`. Use the normal saved read Query,
validated snapshot, personal draft, publish/activate and read approval flow.

| Source | Target |
| --- | --- |
| id | source-scoped Integration Link (type supply_service) |
| nr | Item.item_code; occupied or changed codes require explicit review |
| name | Item.item_name |
| description | Item.description, sanitized HTML |
| unit_type | Item.stock_uom; Std reuses Hour; other units use their exact name |
| manufacturer | Item.brand |
| ean | Item.barcodes with uniqueness protection |
| net_price_per_unit | Standard Selling Item Price via Service Price |
| is_deleted | Item.disabled |
| internal_identifier | Internal Identifier |
| quantity | Default Quantity; null is preserved, never a stock quantity |
| time_minutes | Calculated Time (Minutes); never an actual Timesheet |
| is_fixed_net_price | Fixed Price observation, no new pricing-rule enforcement |
| vat_percent | Source Tax Rate and an existing matching Item Tax Template |
| created / modified | Source Created / Source Modified observations |
| company_id | source observation/consistency guard; local company uses ERPNext defaults |

The existing company resolver chooses ERPNext Global Defaults or the sole
Company. Tax selection requires exactly one enabled template in that company,
with one tax row matching the source percentage. Missing or ambiguous templates
block the record. No tax accounts or templates are guessed or created.
The price-list currency is required and provides the target currency; the
source service has no currency field. The price-list currency must match the
ERPNext company currency; no implicit exchange conversion is performed. `Service Price` preserves zero and rejects
negative/nonfinite input. Only Standard Selling rules are supported. Existing
local Item Prices and changed managed prices require review.

Scalar observations and the calculation table use app-owned internal fieldnames;
visible labels have no HERO prefix. Standard fields are reused for names,
numbers, units, brand, barcodes, prices and taxes.

## Composition and labor

A fixed read-only detail Query fetches the same IDs using `service_ids`, with
inline fragments for `Documents_SupplyProduct` and `WageGroup`. The query is
validated against the latest validated snapshot of the configured source.
The exact ID set, company_id and modified value must match the paged read;
otherwise no record is written. Source mapping approval does not redefine
this fixed detail contract.

Each source position becomes a read-only `HERO Service Calculation` child row:
component type, source ID, description, source unit, quantity, unit price and
planned time. Unmapped selected metadata is retained as Additional Source Data,
without copying those typed values a second time.

Material references reuse only synchronized article links from the same source.
Missing links remain explicitly Unresolved in the table. No duplicate materials
are created by name; a later run can resolve newly imported articles. This
calculation does not create stock movements, bundles or manufacturing orders.

Labor groups have their own source links (type WageGroup) to Activity Type.
Hourly billing rates are stored natively and per-row minute quantities stay
minutes. Existing unbound activity types, local edits or inconsistent group
billing rates require review. `wage_cost_price` retains its raw source value in
Source Cost Rate; its unit is unproven, so Activity Type.costing_rate is not
inferred. Markup and planned minutes are retained per row. There are no payroll,
Employee Activity Cost or actual-time bookings.

## Files and execution safety

Associated files are downloaded through the existing guarded HERO downloader
(host/path allowlist, no redirects, size bounds), then attached privately to
Item through Frappe's transactional File lifecycle. Unchanged attachments are
reused after ownership/hash checks; new versions retain the old attachment.
Source deletion never deletes a local file. No public download URLs are stored
as public Item images. Local attachment edits block replacement.

Run the normal app migration to install the child DocType, observation fields
and run-action metadata before starting this area. Existing conflicting custom
field definitions abort schema installation. The runtime only checks schema;
it never installs fields as a side effect of pressing Start.

Mapped Item fields, child rows, barcodes, taxes, Activity Types and prices have
baselines. Local edits are checked even when HERO has not changed. A failed
record rolls back its Item, masters, price, bindings and file writes together;
its exact offset remains retryable. Successful records and checkpoints commit
together. Cancellation and time budgets are checked per record. Source binding,
company, schema or mapping drift block resume. Repeated/out-of-order IDs block
instead of looping indefinitely. Source rejection is respected.

Tests use synthetic records and fake persistence; optional schema validation
uses a local reference schema. They do not establish a deployed schema or a
real site transaction. No live migration is authorized by these tests.
