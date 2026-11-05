# Configurable Products, Documents and Pictures mappings

Products, Documents and Pictures use Query adapter version `2`, with the same
personal draft, publication, activation and saved-read approval flow as other
native adapters. Their target operations are `HeroProducts`, `HeroDocuments`
and `HeroPictures`. A mapping alone does not authorize execution.

## Configurable fields and fixed behavior

| Operation | Saved Query root / records path | Configurable targets |
| --- | --- | --- |
| `HeroProducts` | `supply_product_versions` | `Item.item_name`, `description`, `item_group`, `stock_uom`, `brand`, `disabled`, `barcodes`, `min_order_qty`, `lead_time_days`; `Item Price.price_list_rate`; app-owned `Item.custom_hero_*` fields |
| `HeroDocuments` | `FileUpload_FileUploads.edges.node` | `File.file_name` and app-owned `File.custom_hero_*` fields |
| `HeroPictures` | `FileUpload_FileUploads.edges.node` | Same File targets as Documents |

Field selection, source paths, supported transformations and price-list rules
are configurable. Protected fields remain unavailable. Every mapping needs
exactly one **Record Name** rule: `Item.item_name` or `File.file_name`.

Identity is fixed. Products bind `product_id` using `Nonempty String ID`;
files bind `id` using `Positive Int To Text`. Each mapping needs exactly one
**Source Identity** rule to `HERO Integration Link.source_id`. It cannot replace
this binding with another source field or relation.

Deletion handling, source binding, local conflict protection, master-record
creation, file downloading and storage, and attachment to the Project/Customer
remain server code. A field rule does not replace those policies. In particular,
Pictures accepts only category `images`; Documents accepts every other category.
Both use the file connection, even though attachment details are fetched by the
fixed `file_uploads(uuids)` detail query.

## Editable templates and execution approval

Default rules are templates for ordinary personal mapping drafts. They are not
active mappings and are never executed as an implicit built-in fallback. The mapping
list displays saved definitions and personal drafts. A direct migration requires
an approved own mapping and its saved read query for the intended source.
Missing or revoked approval blocks execution, including resume.

The product template uses `nr` for Item Code, `base_data.name` and
`base_data.description`, native category/unit/manufacturer/supplier fields,
minimum quantity, delivery time, deletion status and separate buying/selling
prices. These rules can be edited through the normal draft flow. An article
without a description keeps an empty description.

The HERO article number is the Item's identity (Item Code = document name). When
it is already used by an ERPNext Item without this article's link, nothing is
written: the article appears under Synchronization → Conflicts. Approve links that
existing Item to the HERO article; decline keeps them apart and the article is not
imported. Declined articles are skipped by later runs.
Documents and Pictures use `filename` for the File name. Both include their
fixed identity rules. Publishing or activating a mapping without its read
approval does not authorize an import.

## Create an own mapping

1. In **API & Schema**, save a read-only Query against a validated snapshot of
   the intended source. Select `supply_product_versions` for Products, or
   `FileUpload_FileUploads` for Documents/Pictures. Declare bounded `first` and
   `offset` variables, for example `$first: Int!` and `$offset: Int!`, and pass
   them to the root. Use the saved schema's supported arguments and field types.
2. Select every configured source field and all fixed guard fields. Products
   require `product_id` and `is_deleted`. Files require `id`, `uuid`, `category`,
   `downloadUrl`, `size`, `modified`, `created`, plus the configured name field.
   For files select `edges { node { ... } }`. Paths within individual rules are
   relative to a node, e.g. `filename`, rather than `edges.node.filename`.
3. Create a Query mapping and open its personal draft. Set `target_operation`
   to the intended operation, `records_path` to `supply_product_versions` or
   **`FileUpload_FileUploads.edges.node`**, and `identity_path` to `product_id`
   or `id`. Bind variables as `{"first": "$page_size", "offset": "$offset"}`.
   Choose an explicit bounded `page_size` (template defaults: 500 products,
   200 files). Root aliases require the matching records-path prefix.
4. Under **Configure Query adapter**, select version `2` and the saved
   `read_service`. Add field rules with the source and target pickers, including
   the fixed Source Identity and required Record Name rules. Configure each
   rule's source type and transformation. The editor offers coded HERO
   transformation suggestions and an explicit **Price list** choice for
   `Item Price`. The server validates every selection again.
5. Save and publish/activate the draft, then approve the saved read service
   against that exact mapping revision, Query, snapshot and source. Only the
   resulting approved read authorizes execution. Changes to the
   Query or mapping require validation and approval again before synchronization.

Example mapping settings (replace rule IDs and service name with saved ones):

```json
{
  "target_operation": "HeroProducts",
  "records_path": "supply_product_versions",
  "identity_path": "product_id",
  "variables": {"first": "$page_size", "offset": "$offset"},
  "page_size": 500,
  "mapping_definition_ids": ["product-identity", "product-title"],
  "query_adapter": {"version": 2, "read_service": "YOUR-SAVED-READ"}
}
```

## Coded transformations and prices

| Transformation | Required target | Behavior |
| --- | --- | --- |
| `HERO Item Group` | `Item.item_group` | Catalog name below `HERO Artikel`; empty names use `Ohne Katalog` |
| `HERO Category Group` | `Item.item_group` | Category below `HERO Artikel`; without category the catalog, then `Ohne Katalog` |
| `HERO Supplier` | `Item.supplier_items` | Resolve/create the Supplier; one Item Supplier row with `base_data.supplier_id` as supplier part number; other rows stay |
| `HERO UOM` | `Item.stock_uom` | Resolve/create the UOM; empty units use `Stk` |
| `HERO Brand` | `Item.brand` | Resolve/create the manufacturer Brand |
| `HERO EAN Barcode` | `Item.barcodes` | Normalize an EAN and add one barcode row only when unique |
| `HERO Price` | `Item Price.price_list_rate` | Positive finite price rounded to two decimals |
| `HERO Default Sales Price` | `Item Price.price_list_rate` | Default sales price, otherwise `list_price` |

These targets require their coded HERO resolver; those resolvers cannot feed
other fields and accept no transformation options. Other supported fields can
use regular compatible transformations, such as `Trim` or `Safe HTML`.

Every Item Price rule must set `relation` to exactly `Standard Buying` or
`Standard Selling`. Each price list may have only one rule. Other targets must
leave `relation` empty. The template buying rule maps `base_price` using
`HERO Price`; its selling rule maps `sales_prices` using
`HERO Default Sales Price`. The latter requires the saved Query to select
`sales_prices.net_price_per_unit`,
`sales_prices.supply_sales_price.is_default` and `list_price` for its fallback.
Invalid, zero, negative or nonfinite prices produce no price value. Existing
price conflict protection remains active for configurable mappings.

See [general configurable mappings](configurable_mappings_1.0.md) for the shared
Query approval and transformation-options flow. Offline tests verify source
contracts and synthetic behavior; they do not establish deployment or a live
HERO import.

See [service mapping and migration](service_mapping.md) for Services, calculation rows, tax handling and private attachments.

## Employees

`HeroEmployees` also uses Query adapter v2 and the same draft/approval workflow.
The saved schema determines whether the collection is `partners` or
`company.partners`. Read the complete list without pagination arguments; select
`id`, `user_id` and `user.id` as identity guards plus all configured fields.
Rules are relative to each partner. The server rejects oversized or duplicate
inventories and blocks resume after a mapping change.

The editable template maps native Employee names, email, mobile and status,
User login email, telephone and language, and source role/account/audit
observations. `Employee Status` translates source status values; an explicit
compatible value map can replace it. `Employee Language` resolves an installed
language. Source roles never grant local permissions.

Account identity and existing privileges remain protected. New accounts are
active, receive only the Employee role and no welcome email. Company is the
configured ERPNext company (or sole Company); joining date is 2026-01-01.
Birth date and gender are optional. Local edits require review. Visible field
labels use ordinary business names without an integration prefix.
