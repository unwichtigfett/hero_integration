"""Site-owned, verified HERO product IDs from an article usage export."""
import csv
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import re

import frappe


def load(source):
    config = (getattr(frappe, 'conf', None) or {}).get('hero_product_usage')
    if config is None:
        return None
    from hero_integration.client.settings import get_configuration
    current = get_configuration()
    digest = config['sha256']
    if (config['source_instance'] != source or current.source_instance != source
            or current.source_binding_revision != config['source_binding_revision']
            or not re.fullmatch(r'[a-f0-9]{64}', digest)):
        raise ValueError('Die Artikel-Positivliste gehört zu einer anderen HERO-Quellenkonfiguration.')
    data = Path(frappe.get_site_path('private', 'hero-product-usage', digest + '.csv')).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Prüfsumme der Artikel-Positivliste stimmt nicht.')
    reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), delimiter=';', strict=True)
    if next(reader, None) != ['product_id', 'Genutzt']:
        raise ValueError('Ungültiger Spaltenkopf der Artikel-Positivliste.')
    counts = {}
    for key, count in reader:
        if not key or key != key.strip() or key in counts or not re.fullmatch(r'[0-9]+', count) or int(count) <= 0:
            raise ValueError('Ungültiger oder doppelter Eintrag in der Artikel-Positivliste.')
        counts[key] = int(count)
    if not counts:
        raise ValueError('Leere Artikel-Positivliste.')
    return digest, counts


def page(client, read, keys, offset, size):
    from graphql import parse, print_ast
    from graphql.language.ast import ArgumentNode, FieldNode, ListValueNode, NameNode, OperationDefinitionNode, StringValueNode
    from hero_integration.services import direct_runtime

    wanted = keys[offset:offset + size]
    if not wanted:
        return []
    tree = parse(read.document)
    ops = [n for n in tree.definitions if isinstance(n, OperationDefinitionNode)]
    if len(ops) != 1 or ops[0].operation.value != 'query' or len(ops[0].selection_set.selections) != 1:
        raise ValueError('Artikelabfrage lässt sich nicht auf die Positivliste einschränken.')
    root = ops[0].selection_set.selections[0]
    if (not isinstance(root, FieldNode) or root.name.value != 'supply_product_versions'
            or any(a.name.value == 'product_ids' for a in root.arguments)):
        raise ValueError('Unvereinbarer Artikelfilter in der freigegebenen Abfrage.')
    root.arguments = (*root.arguments, ArgumentNode(name=NameNode(value='product_ids'),
        value=ListValueNode(values=tuple(StringValueNode(value=k) for k in wanted))))
    scoped = replace(read, document=print_ast(tree))
    rows = direct_runtime.fetch_page(client, scoped, 0, len(wanted) + 1)
    by_id = {r.get('product_id'): r for r in rows}
    if len(rows) != len(wanted) or set(by_id) != set(wanted):
        raise ValueError('HERO lieferte abweichende, fehlende oder doppelte Artikel-IDs für die Positivliste.')
    return [by_id[k] for k in wanted]
