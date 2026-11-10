"""
Strict tool use limits, checked in code (no network, no anthropic import, so dev tools and tests
can both use it). The API's strict mode closes every object, requires every field, and caps the
number of optional and union-typed parameters across one schema; these counts approximate how it
counts them, and tools/probe_schemas.py confirms against the real API.
"""

# Strict tool use limits (see schema_multi.py): at most 24 optional parameters and 16
# union-typed parameters across the schema; every object closed and fully required; and no
# constraint keywords the API's strict mode does not support.
STRICT_MAX_OPTIONAL = 24
STRICT_MAX_UNION = 16
STRICT_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                      "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "pattern")


def _walk(schema: dict) -> tuple[list[str], dict]:
    problems: list[str] = []
    defs = schema.get("$defs", {})
    counts = {"optional": 0, "union": 0}
    seen: set[str] = set()

    def walk(node, path, stack):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in stack:
                problems.append(f"{path}: recursive reference to {name}")
            elif name not in seen:                  # each definition counts once
                seen.add(name)
                walk(defs.get(name, {}), f"{path}->{name}", stack | {name})
            return
        for key in STRICT_UNSUPPORTED:
            if key in node:
                problems.append(f"{path}: unsupported keyword {key}")
        if "anyOf" in node or "oneOf" in node or isinstance(node.get("type"), list):
            counts["union"] += 1
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties is not false")
            props = node.get("properties", {})
            required = set(node.get("required", []))
            counts["optional"] += len(set(props) - required)
            for name, sub in props.items():
                walk(sub, f"{path}.{name}", stack)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            sub = node.get(key)
            for i, item in enumerate(sub if isinstance(sub, list) else [sub] if sub else []):
                walk(item, f"{path}.{key}[{i}]" if isinstance(sub, list) else f"{path}.{key}", stack)

    walk(schema, "$", frozenset())
    return problems, counts


def strict_schema_counts(schema: dict) -> dict:
    """{'optional': n, 'union': n} as counted against the strict-mode limits."""
    return _walk(schema)[1]


def strict_schema_problems(schema: dict) -> list[str]:
    """Why a JSON schema would fail (or strain) strict tool use; [] when it complies."""
    problems, counts = _walk(schema)
    if counts["optional"] > STRICT_MAX_OPTIONAL:
        problems.append(f"{counts['optional']} optional parameters (limit {STRICT_MAX_OPTIONAL})")
    if counts["union"] > STRICT_MAX_UNION:
        problems.append(f"{counts['union']} union-typed parameters (limit {STRICT_MAX_UNION})")
    return problems
