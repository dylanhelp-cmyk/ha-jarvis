"""Bounded state and numeric attributes from the exposure-scoped Assist context."""
from collections import Counter
import json
import math
import re

import yaml


MAX_ENTITIES = 24
NUMERIC_ATTRIBUTES = {
    "brightness": (0, 255), "brightness_pct": (0, 100),
    "temperature": (-273.15, 10000), "current_temperature": (-273.15, 10000),
    "humidity": (0, 100), "current_position": (0, 100),
    "percentage": (0, 100), "volume_level": (0, 1),
}


def number(value, lower, upper):
    if type(value) not in (str, int, float) or isinstance(value, str) and len(value) > 24:
        return None
    try:
        numeric = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or not lower <= numeric <= upper:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and bool(value.strip())


def matches_name(value, expected):
    return _text(value, 256) and _text(expected, 256) and expected.casefold() in {
        value.casefold(), *(alias.casefold() for alias in value.split(", "))}


def context_rows(text, arguments):
    """Require every returned row to agree with the requested filters."""
    try:
        result = json.loads(text)
        payload = result.get("result") if isinstance(result, dict) else None
        if not isinstance(payload, str):
            return None
        header, _, body = payload.partition("\n")
        if not header.startswith("Live Context:"):
            return None
        rows = yaml.safe_load(body)
    except (TypeError, ValueError, yaml.YAMLError, RecursionError):
        return None
    if not isinstance(rows, list) or not 0 < len(rows) <= 512:
        return None
    if arguments.keys() - {"name", "area", "domain"}:
        return None
    domains = arguments.get("domain")
    if isinstance(domains, str):
        domains = [domains]
    if "domain" in arguments and (not isinstance(domains, list) or not 0 < len(domains) <= 32
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z_]+", item.strip().lower()) for item in domains)):
        return None
    domains = {item.strip().lower() for item in domains} if domains is not None else None
    for key in ("name", "area"):
        if key in arguments and not _text(arguments[key], 128):
            return None
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        names, domain, state = (row.get(key) for key in ("names", "domain", "state"))
        if (not _text(names, 256) or not _text(state, 64) or not isinstance(domain, str)
                or not re.fullmatch(r"[a-z_]+", domain) or domains is not None and domain not in domains
                or "name" in arguments and not matches_name(names, arguments["name"])
                or "area" in arguments and not matches_name(row.get("areas"), arguments["area"])):
            return None
        attributes = {}
        if state not in ("unknown", "unavailable") and isinstance(source := row.get("attributes"), dict):
            for key, bounds in NUMERIC_ATTRIBUTES.items():
                if key in source and (value := number(source[key], *bounds)) is not None:
                    attributes[key] = value
            for key in ("temperature_unit", "unit_of_measurement"):
                if _text(source.get(key), 24):
                    attributes[key] = source[key]
        if domain == "light" and "brightness" in attributes:
            raw = attributes["brightness"]
            percent = max(round(raw / 255 * 100), 1) if raw else 0
            if type(raw) is not int or attributes.get("brightness_pct", percent) != percent:
                return None
            attributes["brightness_pct"] = percent
        clean.append({"names": names, "domain": domain, "state": state, "attributes": attributes})
    return clean


def unique_context(text, arguments):
    rows = context_rows(text, arguments)
    return rows[0] if rows is not None and len(rows) == 1 else None


def state_result(text, arguments):
    rows = context_rows(text, arguments)
    if rows is None or "name" in arguments and len(rows) != 1:
        return None
    if len(rows) == 1 and "name" in arguments:
        row = rows[0]
        return {"outcome": "verified", "state": row["state"],
                **({"attributes": row["attributes"]} if row["attributes"] else {})}
    counts = Counter(row["state"] for row in rows)
    return {"outcome": "verified", "matched_entities": len(rows),
            **({"state_counts": dict(counts)} if len(counts) <= MAX_ENTITIES else {}),
            "truncated": len(rows) > MAX_ENTITIES,
            "entities": [{"name": row["names"].split(", ")[0], "domain": row["domain"],
                          "state": row["state"], **({"attributes": row["attributes"]} if row["attributes"] else {})}
                         for row in rows[:MAX_ENTITIES]]}
