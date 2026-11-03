# Configurable Products, Documents and Pictures mappings

Products, Documents and Pictures use Query adapter version `2`, with the same
personal draft, publication, activation and saved-read approval flow as other
native adapters. Their target operations are `HeroProducts`, `HeroDocuments`
and `HeroPictures`. A mapping alone does not authorize execution.

## Configurable fields and fixed behavior

| Operation | Saved Query root / records path | Configurable targets |
| --- | --- | --- |
| `HeroProducts` | `supply_product_versions` | `Item.item_name`, `description`, `item_group`, `stock_uom`, `brand`, `disabled`, `barcodes`; `Item Price.price_list_rate`; app-owned `Item.custom_hero_*` fields |
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

## Built-in mapping

**Mapping → Active Mappings** shows built-in rules read-only, including when a
schema snapshot filter is selected: these rules have no `schema_snapshot`.
Operation and field filters still apply. Built-in rows cannot open an editable
definition or become personal drafts.

Until an own approved mapping is available for the operation and source, the
runtime uses the built-in rules and a Query generated from them. Products use
`base_data.name`, `base_data.description`, `base_data.supply_catalog.name`,
`base_data.unit_type`, `base_data.manufacturer`, `base_data.ean`, `is_deleted`,
`base_price` and `sales_prices` (with `list_price` as the sales fallback).
Documents and Pictures use `filename` for the File name. Both include their
fixed identity rules. Publishing or activating a mapping without its read
approval does not replace this fallback.

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
   Choose an explicit bounded `page_size` (built-in defaults: 500 products,
   200 files). Root aliases require the matching records-path prefix.
4. Under **Configure Query adapter**, select version `2` and the saved
   `read_service`. Add field rules with the source and target pickers, including
   the fixed Source Identity and required Record Name rules. Configure each
   rule's source type and transformation. The editor offers coded HERO
   transformation suggestions and an explicit **Price list** choice for
   `Item Price`. The server validates every selection again.
5. Save and publish/activate the draft, then approve the saved read service
   against that exact mapping revision, Query, snapshot and source. Only the
   resulting approved read can replace the built-in mapping. Changes to the
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
leave `relation` empty. The built-in buying rule maps `base_price` using
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
