from __future__ import annotations


class GraphQLReadOnlyError(ValueError):
    pass


def _tokens(document: str):
    i = 0
    length = len(document)

    while i < length:
        char = document[i]

        if char.isspace() or char == ",":
            i += 1
            continue

        if char == "#":
            newline = document.find("\n", i + 1)
            i = length if newline < 0 else newline + 1
            continue

        if document.startswith('"""', i):
            i += 3
            while i < length:
                if document.startswith('\\"""', i):
                    i += 4
                    continue
                if document.startswith('"""', i):
                    i += 3
                    break
                i += 1
            continue

        if char == '"':
            i += 1
            escaped = False
            while i < length:
                current = document[i]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    i += 1
                    break
                i += 1
            continue

        if char.isalpha() or char == "_":
            start = i
            i += 1
            while i < length and (document[i].isalnum() or document[i] == "_"):
                i += 1
            yield "NAME", document[start:i]
            continue

        yield "PUNCT", char
        i += 1


def graphql_tokens(
    document: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(_tokens(document))


def operation_kinds(document: str) -> tuple[str, ...]:
    operations: list[str] = []
    brace_depth = 0
    definition_start = True

    for token_type, value in _tokens(document):
        if value == "{":
            if brace_depth == 0 and definition_start:
                operations.append("query")
                definition_start = False
            brace_depth += 1
            continue

        if value == "}":
            if brace_depth > 0:
                brace_depth -= 1
            if brace_depth == 0:
                definition_start = True
            continue

        if brace_depth != 0:
            continue

        if definition_start and token_type == "NAME":
            if value in {"query", "mutation", "subscription"}:
                operations.append(value)
            definition_start = False

    return tuple(operations)


def assert_read_only_graphql(document: str) -> None:
    operations = operation_kinds(document)

    if "mutation" in operations:
        raise GraphQLReadOnlyError(
            "GraphQL-Mutationen sind in HERO-005 gesperrt."
        )

    if "subscription" in operations:
        raise GraphQLReadOnlyError(
            "GraphQL-Subscriptions sind in HERO-005 nicht freigegeben."
        )
