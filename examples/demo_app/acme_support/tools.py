"""Tool definitions for the support agent."""

_TOOL_NAMES = {
    "lookup_order",
    "update_address",
    "open_carrier_case",
    "create_return",
    "issue_partial_refund",
    "create_warranty_claim",
    "search_catalogue",
    "escalate_to_human",
}

_SCHEMAS = {
    "lookup_order": {"order_id": {"type": "string"}, "email": {"type": "string"}},
    "update_address": {"order_id": {"type": "string"}, "address": {"type": "string"}},
    "open_carrier_case": {"order_id": {"type": "string"}, "notes": {"type": "string"}},
    "create_return": {"order_id": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
    "issue_partial_refund": {"order_id": {"type": "string"}, "percent": {"type": "number"}},
    "create_warranty_claim": {"order_id": {"type": "string"}, "serial": {"type": "string"},
                              "fault": {"type": "string"}},
    "search_catalogue": {"query": {"type": "string"}},
    "escalate_to_human": {"reason": {"type": "string"}, "summary": {"type": "string"}},
}


def build_tools():
    # Iterating a set gives a different order in each process (hash randomisation), so the
    # serialized tool list — the very first bytes of every request — changes between workers.
    return [
        {
            "name": name,
            "description": f"Acme support tool: {name.replace('_', ' ')}.",
            "input_schema": {"type": "object", "properties": _SCHEMAS[name], "required": list(_SCHEMAS[name])},
        }
        for name in _TOOL_NAMES
    ]
