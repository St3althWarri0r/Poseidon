"""Robust JSON-object extraction for weak-model output (reuses the #2 discipline:
never crash on malformed output — degrade)."""
from __future__ import annotations

import json
from typing import Any


def first_json_obj(text: str) -> dict[str, Any]:
    """The first balanced {...} object in ``text`` as a dict, or {} on failure.
    Weak models wrap JSON in prose/markdown fences; tolerate that."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else {}
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return {}
