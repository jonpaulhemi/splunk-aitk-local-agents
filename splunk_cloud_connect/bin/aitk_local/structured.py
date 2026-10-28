"""Structured output: the agent's final answer in the shape set on the agent.

AI Toolkit sends the agent's `structured_output` as {"fields": {name: field}},
where each field has a type (string, number, integer, boolean or object),
`required`, and optionally is_array, allowed_values, description, example,
min_length/max_length, minimum/maximum, precision and
array_min_items/array_max_items. It JSON-encodes a successful answer into the
result column itself.
"""

TOOL_NAME = "structured_output"


class Invalid(Exception):
    """The model's answer can't be used; the message says why, without the answer's values."""


def schema(structured_output):
    """The JSON Schema for AI Toolkit's `structured_output`."""
    fields = (structured_output or {}).get("fields") or {}
    return {
        "title": TOOL_NAME,
        "description": "Return the final answer in this shape.",
        "type": "object",
        "properties": {name: _property(field) for name, field in fields.items()},
        "required": [name for name, field in fields.items() if field.get("required")],
    }


def _property(field):
    item = {"type": field["type"]}
    if field.get("allowed_values") is not None:
        item["enum"] = field["allowed_values"]
    for ours, theirs in (("min_length", "minLength"), ("max_length", "maxLength"),
                         ("minimum", "minimum"), ("maximum", "maximum")):
        if ours in field:
            item[theirs] = field[ours]
    hints = [field["description"]] if field.get("description") else []
    if "precision" in field:
        hints.append(f"Round to {field['precision']} decimal places.")
    if not field.get("is_array"):
        return {**item, **_described(hints, field)}
    array = {"type": "array", "items": item}
    for ours, theirs in (("array_min_items", "minItems"), ("array_max_items", "maxItems")):
        if ours in field:
            array[theirs] = field[ours]
    return {**array, **_described(hints, field)}


def _described(hints, field):
    extra = {"description": " ".join(hints)} if hints else {}
    if "example" in field:
        extra["examples"] = [field["example"]]
    return extra


async def answer(model, messages, json_schema):
    """Ask `model` for the final answer of the conversation in `messages` in
    `json_schema`'s shape, and check it."""
    from aitk_local import llm

    result = await llm.structured(model, json_schema).ainvoke(messages)
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        raise Invalid("The model didn't return the agent's structured output")
    _check(parsed, json_schema)
    return parsed


def _check(value, json_schema):
    import jsonschema

    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(json_schema).iter_errors(value))
    if error is None:
        return
    # The error's own message quotes the offending value, so name the field and the rule only.
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in error.instance]
        problem = f"'{missing[0]}' is missing"
    else:
        field = str(error.absolute_path[0]) if error.absolute_path else "answer"
        problem = f"'{field}' " + {"enum": "isn't one of the allowed values",
                                   "type": "has the wrong type"}.get(error.validator, f"fails {error.validator}")
    raise Invalid(f"The model's answer didn't match the agent's structured output: {problem}")
