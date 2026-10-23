from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .graphql_query_composer import GraphQLQueryComposerModel


class GraphQLSelectionSetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphQLSelectionPathSegment:
    """One stable protocol field in a requested selection path."""

    protocol_node_id: str
    field_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_node_id, str) or not self.protocol_node_id:
            raise GraphQLSelectionSetError(
                "Selection protocol node ID must be a non-empty string."
            )
        if not isinstance(self.field_name, str) or not self.field_name:
            raise GraphQLSelectionSetError(
                "Selection field name must be a non-empty string."
            )


@dataclass(frozen=True, slots=True)
class GraphQLSelectionSegment:
    """Immutable merged selection node; protocol identity is authoritative."""

    protocol_node_id: str
    field_name: str
    children: tuple[GraphQLSelectionSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphQLSelectionTree:
    """Canonical stable-node selection tree shared by all callers."""

    roots: tuple[GraphQLSelectionSegment, ...] = ()

    @property
    def node_count(self) -> int:
        count = 0
        pending = list(self.roots)
        while pending:
            node = pending.pop()
            count += 1
            pending.extend(node.children)
        return count

    @property
    def depth(self) -> int:
        maximum = 0
        pending = [(node, 1) for node in self.roots]
        while pending:
            node, depth = pending.pop()
            maximum = max(maximum, depth)
            pending.extend((child, depth + 1) for child in node.children)
        return maximum

    def render(self) -> str:
        """Render this canonical tree as a deterministic GraphQL selection."""
        if not self.roots:
            return ""
        lines = ["{"]
        _render_segments(self.roots, lines, indent=1)
        lines.append("}")
        return "\n".join(lines)

    def merge(self, other: GraphQLSelectionTree) -> GraphQLSelectionTree:
        """Return the canonical stable-node union of two selection trees."""

        if not isinstance(other, GraphQLSelectionTree):
            raise TypeError("other must be a GraphQLSelectionTree.")
        return _build_tree(
            (*_tree_paths(self), *_tree_paths(other)),
        )

    def render_debug(self) -> str:
        """Backward-compatible diagnostic alias for the canonical renderer."""

        return self.render()


@dataclass(slots=True)
class _SelectionNode:
    protocol_node_id: str
    field_name: str
    children: dict[str, _SelectionNode] = field(default_factory=dict)


def build_stable_selection_tree(
    selected_paths: Iterable[tuple[GraphQLSelectionPathSegment, ...]],
) -> GraphQLSelectionTree:
    """Merge stable protocol paths with the canonical selection algorithm."""

    normalized: list[tuple[GraphQLSelectionPathSegment, ...]] = []
    for raw_path in selected_paths:
        if isinstance(raw_path, (str, bytes)):
            raise GraphQLSelectionSetError(
                "Stable selection paths must contain path-segment tuples."
            )
        path = tuple(raw_path)
        if not path:
            raise GraphQLSelectionSetError(
                "Stable selection path must not be empty."
            )
        if not all(isinstance(item, GraphQLSelectionPathSegment) for item in path):
            raise GraphQLSelectionSetError(
                "Stable selection paths contain invalid segments."
            )
        normalized.append(path)
    return _build_tree(normalized)


def build_selection_set(
    model: GraphQLQueryComposerModel,
    selected_paths: tuple[str, ...] | list[str],
) -> str:
    normalized_paths = _normalize_paths(model, selected_paths)
    paths = tuple(
        tuple(
            GraphQLSelectionPathSegment(
                protocol_node_id=f"legacy-field:{segment}",
                field_name=segment,
            )
            for segment in path
        )
        for path in normalized_paths
    )
    return _build_tree(paths).render()


def build_field_selection_suffix(
    model: GraphQLQueryComposerModel,
    selected_paths: tuple[str, ...] | list[str],
) -> str:
    selection_set = build_selection_set(model, selected_paths)
    if not selection_set:
        return ""
    return f" {selection_set}"


def _build_tree(
    paths: Iterable[tuple[GraphQLSelectionPathSegment, ...]],
) -> GraphQLSelectionTree:
    """The sole recursive-selection merge/dedup authority."""

    roots: dict[str, _SelectionNode] = {}
    for path in paths:
        current = roots
        for segment in path:
            existing = current.get(segment.protocol_node_id)
            if existing is None:
                existing = _SelectionNode(
                    protocol_node_id=segment.protocol_node_id,
                    field_name=segment.field_name,
                )
                current[segment.protocol_node_id] = existing
            elif existing.field_name != segment.field_name:
                raise GraphQLSelectionSetError(
                    "One protocol node ID resolved to conflicting field names."
                )
            current = existing.children
    return GraphQLSelectionTree(roots=_freeze_children(roots))


def _tree_paths(
    tree: GraphQLSelectionTree,
) -> tuple[tuple[GraphQLSelectionPathSegment, ...], ...]:
    """Expose immutable leaf paths to the sole canonical merge authority."""

    paths: list[tuple[GraphQLSelectionPathSegment, ...]] = []
    pending = [(root, ()) for root in reversed(tree.roots)]
    while pending:
        node, parent_path = pending.pop()
        path = (
            *parent_path,
            GraphQLSelectionPathSegment(
                protocol_node_id=node.protocol_node_id,
                field_name=node.field_name,
            ),
        )
        if node.children:
            pending.extend(
                (child, path) for child in reversed(node.children)
            )
        else:
            paths.append(path)
    return tuple(paths)


def _freeze_children(
    children: dict[str, _SelectionNode],
) -> tuple[GraphQLSelectionSegment, ...]:
    return tuple(
        GraphQLSelectionSegment(
            protocol_node_id=node.protocol_node_id,
            field_name=node.field_name,
            children=_freeze_children(node.children),
        )
        for node in (children[node_id] for node_id in sorted(children))
    )


def _normalize_paths(
    model: GraphQLQueryComposerModel,
    selected_paths: tuple[str, ...] | list[str],
) -> tuple[tuple[str, ...], ...]:
    known = {selection.path for selection in model.selectable_fields}
    normalized: set[tuple[str, ...]] = set()
    for raw_path in selected_paths:
        if not isinstance(raw_path, str):
            raise GraphQLSelectionSetError("Selection paths must be strings.")
        path = tuple(part for part in raw_path.strip().split(".") if part)
        if not path:
            raise GraphQLSelectionSetError("Selection path must not be empty.")
        if path not in known:
            raise GraphQLSelectionSetError(
                "Selection path is not available in the composer model: "
                f"{raw_path!r}."
            )
        normalized.add(path)
    return tuple(sorted(normalized, key=lambda item: (len(item), item)))


def _render_segments(
    segments: tuple[GraphQLSelectionSegment, ...],
    lines: list[str],
    *,
    indent: int,
) -> None:
    prefix = "  " * indent
    for segment in segments:
        if not segment.children:
            lines.append(f"{prefix}{segment.field_name}")
            continue
        lines.append(f"{prefix}{segment.field_name} {{")
        _render_segments(segment.children, lines, indent=indent + 1)
        lines.append(f"{prefix}}}")


__all__ = [
    "GraphQLSelectionPathSegment",
    "GraphQLSelectionSegment",
    "GraphQLSelectionSetError",
    "GraphQLSelectionTree",
    "build_field_selection_suffix",
    "build_selection_set",
    "build_stable_selection_tree",
]
