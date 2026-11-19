"""
Editable view of a contact's raw lead payload (the JSON a lead vendor posted).

Unlike the read-only dossier (contacts.lead_dossier, also used for the AI's lead context),
this shows EVERY key, including ones with no value, and gives each value an input whose
name encodes its exact JSON path. On save only the values the user actually changed are
written back, converted to the original value's type (numbers stay numbers, yes/no stays
a boolean, {year, month, day} dates keep their shape...), so untouched data is never reformatted.
"""

import copy
import json
import re
from datetime import timezone as dt_timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .lead_dossier import (
    PAYLOAD_FIELD_KEYS, SECTION_TITLES, OVERVIEW_ICON, MAX_CARDS_PER_GROUP, _WRAPPER_KEYS, _ENTITY_PREFIX,
    _age, _date_from_parts, _entity_stem, _epoch_to_str, _group_title, _infer_title, _is_address,
    _is_birth_key, _iso_to_str, _ordered_items, _phone, _relation_badge, icon_for, label_for,
)

FIELD_PREFIX = "lead::"
# Identity keys lead each card (a person's name, a vehicle's year/make/model), then the dossier's order
_FIRST_KEYS = ["firstname", "lastname", "namesuffix", "fullname", "name", "year", "make", "model", "submodel"]
MAX_VALUE_LENGTH = 2000


# ---------------------------------------------------------------------------
# Where the payload lives on the contact
# ---------------------------------------------------------------------------

def find_payload_slot(custom_fields) -> Tuple[Optional[str], Optional[dict], bool]:
    """(custom_fields key, payload dict, stored_as_json_string) for the contact's lead payload."""
    cf = custom_fields if isinstance(custom_fields, dict) else {}

    def _parse(val):
        if isinstance(val, dict):
            return val, False
        if isinstance(val, str) and val.strip().startswith("{"):
            try:
                parsed = json.loads(val)
            except ValueError:
                return None, False
            return (parsed, True) if isinstance(parsed, dict) else (None, False)
        return None, False

    ordered = [k for k in PAYLOAD_FIELD_KEYS if k in cf] + [k for k in cf if k not in PAYLOAD_FIELD_KEYS]
    for key in ordered:
        payload, was_string = _parse(cf.get(key))
        if payload:
            return key, payload, was_string
    return None, None, False


# ---------------------------------------------------------------------------
# Value <-> input text
# ---------------------------------------------------------------------------

def _is_leaf(value) -> bool:
    """Values shown as one input: scalars, nulls, empty objects/lists, date-like objects, lists of scalars."""
    if isinstance(value, dict):
        return not value or bool(_date_from_parts(value) or _epoch_to_str(value))
    if isinstance(value, (list, tuple)):
        return not any(isinstance(v, (dict, list)) for v in value)
    return True


def _is_phone_key(key) -> bool:
    k = str(key).lower()
    return "phone" in k or k in ("mobile", "cell")


def edit_text(key, value) -> str:
    """How a stored value appears in its input ("" when there is no value)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return _date_from_parts(value) or _epoch_to_str(value) or ""
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in (edit_text(key, v) for v in value) if t)
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    if _is_phone_key(key):
        return _phone(text)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}"
    return _iso_to_str(text) or text


def _parse_date_parts(text: str):
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        return {"year": int(m.group(3)), "month": int(m.group(1)), "day": int(m.group(2))}
    m = re.match(r"^(\d{1,2})/(\d{4})$", text)
    if m:
        return {"year": int(m.group(2)), "month": int(m.group(1))}
    m = re.match(r"^(\d{4})$", text)
    if m:
        return {"year": int(m.group(1))}
    return None


def _parse_datetime(text: str):
    try:
        from dateutil import parser as _dparser
        parsed = _dparser.parse(text.replace("UTC", "").strip())
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _number(text: str, as_type):
    try:
        return as_type(text.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def parse_edit(text: str, original: Any, key) -> Any:
    """Turn what the user typed back into a value shaped like the original."""
    t = (text or "").strip()[:MAX_VALUE_LENGTH]
    if not t:
        if isinstance(original, list):
            return []
        if isinstance(original, dict) and not original:
            return {}
        return None
    if isinstance(original, bool):
        low = t.lower()
        if low in ("yes", "y", "true", "1"):
            return True
        if low in ("no", "n", "false", "0"):
            return False
        return t
    if isinstance(original, int):
        n = _number(t, int)
        return t if n is None else n
    if isinstance(original, float):
        n = _number(t, float)
        return t if n is None else n
    if isinstance(original, dict) and _date_from_parts(original):
        return _parse_date_parts(t) or t
    if isinstance(original, dict) and _epoch_to_str(original):
        parsed = _parse_datetime(t)
        if not parsed:
            return t
        ts = int(parsed.timestamp())
        if int(original.get("low") or 0) > 10_000_000_000:
            ts *= 1000
        return {"low": ts, "high": 0, "unsigned": bool(original.get("unsigned", False))}
    if isinstance(original, (list, tuple)):
        parts = [p.strip() for p in t.split(",") if p.strip()]
        if original and all(isinstance(v, int) and not isinstance(v, bool) for v in original):
            return [n if (n := _number(p, int)) is not None else p for p in parts]
        return parts
    if isinstance(original, str):
        raw = original.strip()
        if _is_phone_key(key) and re.fullmatch(r"\+?\d{10,11}", raw):
            digits = re.sub(r"\D", "", t)
            return digits if len(digits) in (10, 11) else t
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            dp = _parse_date_parts(t)
            if dp and "day" in dp:
                return f"{dp['year']:04d}-{dp['month']:02d}-{dp['day']:02d}"
            return t
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", raw):
            parsed = _parse_datetime(t)
            if not parsed:
                return t
            if raw.endswith("Z"):
                return parsed.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            return parsed.isoformat()
    return t


# ---------------------------------------------------------------------------
# Editor structure: sections -> cards / groups -> fields
# ---------------------------------------------------------------------------

def _items(obj: dict):
    items = _ordered_items(obj)
    rank = {k: i for i, k in enumerate(_FIRST_KEYS)}
    norm = lambda k: re.sub(r"[_\-\s]", "", str(k)).lower()  # noqa: E731
    return sorted(items, key=lambda kv: rank.get(norm(kv[0]), len(rank)))

def _field(label: str, key, value, path: Tuple) -> Dict[str, Any]:
    text = edit_text(key, value)
    hint = ""
    if _is_birth_key(key):
        age = _age(value)
        hint = f"{age} yrs" if age else ""
    return {
        "label": label,
        "name": FIELD_PREFIX + json.dumps(list(path), separators=(",", ":"), ensure_ascii=False),
        "value": text,
        "empty": text == "",
        "hint": hint,
    }


def _fields(obj: dict, path: Tuple, prefix: str = "") -> List[Dict[str, Any]]:
    """Every key of an object as a field; nested objects are folded in with a label prefix."""
    out = []
    for k, v in _items(obj):
        p = path + (k,)
        label = f"{prefix}{label_for(k)}"
        if _is_leaf(v):
            out.append(_field(label, k, v, p))
        elif isinstance(v, dict):
            out += _fields(v, p, prefix if str(k).lower() in _WRAPPER_KEYS else f"{label} · ")
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out += _fields(item, p + (i,), f"{label} {i + 1} · ")
                else:
                    out.append(_field(f"{label} {i + 1}", k, item, p + (i,)))
    return out


def _card(key, obj: dict, path: Tuple, index=None, is_primary=False) -> Dict[str, Any]:
    title, _ = _infer_title(obj)
    if not title:
        title = label_for(_ENTITY_PREFIX.sub("", str(key)) or key)
        if index:
            title = f"{title} {index}"
    badge = "Primary" if is_primary else (_relation_badge(obj) or (f"#{index}" if index else ""))
    return {"title": title, "badge": badge, "is_primary": is_primary, "fields": _fields(obj, path)}


def _group_table(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Side-by-side view of a group (vehicles, drivers...): one column per item, one row per field label.
    Labels keep each item's order; a label only some items have is slotted in after its neighbour.
    """
    labels: List[str] = []
    for card in cards:
        prev = None
        for f in card["fields"]:
            if f["label"] not in labels:
                labels.insert(labels.index(prev) + 1 if prev in labels else len(labels), f["label"])
            prev = f["label"]
    # identity rows first (a person's name, how they relate, a vehicle's year/make/model)
    lead = ["First Name", "Last Name", "Name Suffix", "Full Name", "Relation To Primary",
            "Year", "Make", "Model", "Submodel"]
    labels.sort(key=lambda lbl: lead.index(lbl) if lbl in lead else len(lead))
    rows = []
    for label in labels:
        cells = [next((f for f in card["fields"] if f["label"] == label), None) for card in cards]
        rows.append({"label": label, "cells": cells, "empty": all(c is None or c["empty"] for c in cells)})
    columns = [{"title": c["title"], "badge": c["badge"], "is_primary": c["is_primary"]} for c in cards]
    return {"columns": columns, "rows": rows,
            "search_titles": " | ".join(c["title"] for c in cards).lower()}


def _section(key, obj: dict) -> Dict[str, Any]:
    section = {"title": SECTION_TITLES.get(str(key), label_for(key)), "icon": icon_for(key),
               "cards": [], "groups": []}
    main_title, _ = _infer_title(obj)
    main_fields, side_cards = [], []
    entities: Dict[str, list] = {}   # stem -> [(key, obj, is_primary, from_list, path)]

    for k, v in _items(obj):
        p = (key, k)
        if _is_leaf(v):
            main_fields.append(_field(label_for(k), k, v, p))
        elif isinstance(v, dict):
            if _is_address(k, v):
                side_cards.append({"title": label_for(k), "badge": "", "is_primary": False, "fields": _fields(v, p)})
            elif str(k).lower() in _WRAPPER_KEYS:
                main_fields += _fields(v, p)
            else:
                entities.setdefault(_entity_stem(k), []).append((k, v, str(k).lower().startswith("primary"), False, p))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    entities.setdefault(_entity_stem(k), []).append((k, item, False, True, p + (i,)))
                else:
                    main_fields.append(_field(f"{label_for(k)} {i + 1}", k, item, p + (i,)))

    if main_fields:
        section["cards"].append({"title": main_title, "badge": "", "is_primary": False,
                                 "fields": main_fields})
    section["cards"] += side_cards

    for stem, members in entities.items():
        if len(members) == 1 and not members[0][3]:
            k, v, is_primary, _, p = members[0]
            card = _card(k, v, p, is_primary=is_primary)
            if not is_primary and card["title"] == label_for(_ENTITY_PREFIX.sub("", str(k)) or k):
                card["title"] = label_for(k)
            section["cards"].append(card)
            continue
        members.sort(key=lambda m: not m[2])
        cards = [_card(k, v, p, index=i, is_primary=prim)
                 for i, (k, v, prim, _, p) in enumerate(members[:MAX_CARDS_PER_GROUP], start=1)]
        section["groups"].append({"title": _group_title(stem), "icon": icon_for(stem), "cards": cards,
                                  "table": _group_table(cards), "truncated": len(members) > MAX_CARDS_PER_GROUP})
    return section


def build_lead_editor(payload) -> Optional[Dict[str, Any]]:
    """
    {
      "sections": [{"title", "icon", "cards": [card], "groups": [{"title", "icon", "cards", "truncated"}]}],
      "field_count": int, "empty_count": int,
    }
    with card = {"title", "badge", "is_primary", "fields": [{"label", "name", "value", "empty", "hint"}]}.
    """
    if not isinstance(payload, dict) or not payload:
        return None

    overview, person_sections, other_sections, list_sections = [], [], [], []
    for k, v in _items(payload):
        if _is_leaf(v):
            overview.append(_field(label_for(k), k, v, (k,)))
        elif isinstance(v, dict):
            if _is_address(k, v):
                sec = {"title": label_for(k), "icon": icon_for("address"), "groups": [],
                       "cards": [{"title": label_for(k), "badge": "", "is_primary": False,
                                  "fields": _fields(v, (k,))}]}
            else:
                sec = _section(k, v)
            (person_sections if str(k) in SECTION_TITLES else other_sections).append(sec)
        elif isinstance(v, list):
            items = [(i, item) for i, item in enumerate(v) if isinstance(item, dict)]
            loose = [_field(f"{label_for(k)} {i + 1}", k, item, (k, i))
                     for i, item in enumerate(v) if not isinstance(item, dict)]
            if loose:
                overview += loose
            if items:
                cards = [_card(k, item, (k, i), index=n)
                         for n, (i, item) in enumerate(items[:MAX_CARDS_PER_GROUP], start=1)]
                list_sections.append({"title": label_for(k), "icon": icon_for(k), "cards": [],
                                      "groups": [{"title": _group_title(_entity_stem(k)), "icon": icon_for(k),
                                                  "cards": cards, "table": _group_table(cards),
                                                  "truncated": len(items) > MAX_CARDS_PER_GROUP}]})

    sections = []
    if overview:
        sections.append({"title": "Lead Overview", "icon": OVERVIEW_ICON, "groups": [],
                         "cards": [{"title": "", "badge": "", "is_primary": False, "fields": overview}]})
    sections += person_sections + other_sections + list_sections

    all_fields = [f for s in sections for c in s["cards"] + [c for g in s["groups"] for c in g["cards"]]
                  for f in c["fields"]]
    for i, s in enumerate(sections, start=1):
        s["anchor"] = f"lead-sec-{i}"
        for j, g in enumerate(s["groups"], start=1):
            g["anchor"] = f"lead-grp-{i}-{j}"
        cards = s["cards"] + [c for g in s["groups"] for c in g["cards"]]
        s["field_count"] = sum(len(c["fields"]) for c in cards)
        s["empty_count"] = sum(1 for c in cards for f in c["fields"] if f["empty"])
    return {"sections": sections, "field_count": len(all_fields),
            "empty_count": sum(1 for f in all_fields if f["empty"])}


def contact_lead_editor(contact) -> Optional[Dict[str, Any]]:
    _, payload, _ = find_payload_slot(getattr(contact, "custom_fields", None))
    return build_lead_editor(payload)


# ---------------------------------------------------------------------------
# Saving edits
# ---------------------------------------------------------------------------

def apply_lead_edits(custom_fields: dict, posted: Iterable[Tuple[str, str]]) -> Tuple[dict, int]:
    """
    Write changed lead values back into the payload. `posted` is (input name, text) pairs from the form.
    Returns (custom_fields, number of values changed); custom_fields is untouched when nothing changed.
    """
    slot, payload, was_string = find_payload_slot(custom_fields)
    if not payload:
        return custom_fields, 0
    data = copy.deepcopy(payload)
    changed = 0
    for name, text in posted:
        if not str(name).startswith(FIELD_PREFIX):
            continue
        try:
            path = json.loads(name[len(FIELD_PREFIX):])
        except ValueError:
            continue
        if not isinstance(path, list) or not path:
            continue
        node = data
        try:
            for step in path[:-1]:
                node = node[step]
            last = path[-1]
            original = node[last]
        except (KeyError, IndexError, TypeError):
            continue
        fmt_key = next((p for p in reversed(path) if isinstance(p, str)), "")
        if (text or "").strip() == edit_text(fmt_key, original):
            continue
        node[last] = parse_edit(text, original, fmt_key)
        changed += 1
    if not changed:
        return custom_fields, 0
    updated = dict(custom_fields)
    updated[slot] = json.dumps(data, ensure_ascii=False) if was_string else data
    return updated, changed
