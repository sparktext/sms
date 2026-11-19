"""
Generic lead-payload dossier builder for the contact detail page.

Lead vendors post nested JSON whose shape depends on the vertical (auto, home,
life, health, medicare, solar, mortgage, ...). Instead of hard-coding each
shape, this module walks whatever payload arrives and maps *structure* to
display blocks:

    top-level scalars              -> "Lead Overview" section
    top-level object               -> its own section ("Applicant", "Auto Policy", ...)
        scalars of that object     -> the section's main card
        address-like object        -> an "Address" card with the parts broken out
        nested object              -> its own card (deeper objects folded into it)
        primaryX + additionalXs    -> one group of cards ("Vehicles", "Drivers", ...)
        list of objects            -> a group with one card per item
        list of scalars            -> one comma-joined row

Only presentation hints live here (labels, icons, value formatting). A new
vertical or a new vendor field shows up on the page without code changes.
"""

import json
import re
from datetime import datetime, timezone as dt_timezone
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Presentation hints
# ---------------------------------------------------------------------------

# custom_fields keys checked first for the raw payload
PAYLOAD_FIELD_KEYS = ("detail", "lead_data", "payload", "json_data", "application")

MAX_DEPTH = 5
MAX_CARDS_PER_GROUP = 25
RAW_JSON_LIMIT = 20000

_ACRONYMS = {
    "vin", "bi", "pd", "dob", "id", "ids", "url", "zip", "suv", "awd", "fwd",
    "rwd", "4wd", "us", "usa", "mpg", "hoa", "ssn", "utm", "ip", "tcpa", "kw",
    "kwh", "apr", "ltv", "fico", "hsa", "ppo", "hmo", "dui", "sr22", "ev",
}

LABEL_OVERRIDES = {
    "requestForQuote": "Request For Quote",
    "completedTs": "Completed At",
    "receivedAt": "Received At",
    "createdAt": "Created At",
    "currentBiLiability": "Current BI Liability",
    "vinTemplate": "VIN Pattern",
    "polkVehicleType": "Vehicle Class",
    "milesPerYear": "Miles / Year",
    "ageLicensed": "Age First Licensed",
    "everSuspendedRevoked": "Suspended / Revoked",
    "relationToPrimaryDriver": "Relation To Primary",
    "monthsInsured": "Months Continuously Insured",
    "insuranceExpiration": "Policy Expiration",
    "birthDate": "Date of Birth",
    "dob": "Date of Birth",
    "driverOrdinal": "Driver #",
    "coverageType": "Coverage Level",
    "zip": "ZIP",
    "zipCode": "ZIP",
    "zip_code": "ZIP",
    "postalCode": "ZIP",
    "line1": "Street",
    "line2": "Street 2",
    "address1": "Street",
    "address2": "Street 2",
    "street1": "Street",
}

# Keys that are pure plumbing and never worth a row
HIDDEN_KEYS = {"driverordinal", "unsigned", "_id", "__v"}

# Rows are shown in payload order, except these float to the top (in this order).
# Matching is case/underscore-insensitive. Unknown keys keep vendor order after them.
PRIORITY_KEYS = [
    # people
    "email", "phone", "mobile", "cell", "gender", "maritalstatus", "birthdate", "dob",
    "dateofbirth", "relationship", "relationtoprimarydriver", "education", "occupation",
    "licensestatus", "agelicensed", "eversuspendedrevoked",
    # vehicles
    "submodel", "polkvehicletype", "ownership", "primaryuse", "milesperyear", "parking",
    "coveragetype", "vintemplate",
    # policies
    "currentinsurer", "carrier", "plantype", "currentbiliability", "insuranceexpiration",
    "monthsinsured", "yearscontinuouslyinsured", "creditrating", "creditscore",
    # property
    "propertytype", "ownership", "squarefeet", "yearbuilt", "bedrooms", "bathrooms",
    "housevalue", "propertyvalue",
    # address
    "street", "street1", "address1", "line1", "street2", "address2", "line2", "city",
    "state", "zip", "zipcode", "postalcode", "county", "country",
]
_PRIORITY_INDEX = {k: i for i, k in reversed(list(enumerate(PRIORITY_KEYS)))}

# Integers under these key words are identifiers, not quantities: no thousands separator
_NO_COMMA = ("zip", "year", "id", "code", "phone", "number", "ordinal", "postal")

# Section titles for well-known top-level keys (anything else is humanized)
SECTION_TITLES = {
    "person": "Applicant", "contact": "Applicant", "applicant": "Applicant",
    "lead": "Applicant", "customer": "Applicant", "consumer": "Applicant",
    "subscriber": "Applicant", "borrower": "Borrower", "insured": "Insured",
}

# Titles for grouped entities, keyed by singular stem (primaryCar/additionalCars -> "car")
GROUP_TITLES = {
    "car": "Vehicles", "vehicle": "Vehicles", "auto": "Vehicles",
    "driver": "Drivers", "dependent": "Dependents", "child": "Children",
    "property": "Properties", "applicant": "Applicants", "member": "Members",
    "pet": "Pets", "incident": "Incidents", "violation": "Violations",
    "accident": "Accidents", "claim": "Claims", "condition": "Conditions",
    "medication": "Medications", "policy": "Policies",
}

# First matching keyword wins, so more specific words come first
ICON_RULES = (
    (("driver", "license"), "\U0001FAAA"),                            # id card
    (("car", "vehicle", "auto"), "\U0001F697"),                      # car
    (("address", "location", "garag"), "\U0001F4CD"),                # pin
    (("person", "contact", "applicant", "customer", "consumer",
      "subscriber", "borrower", "insured", "member", "spouse"), "\U0001F464"),
    (("dependent", "child", "household", "family"), "\U0001F46A"),
    (("home", "house", "property", "dwelling", "renter"), "\U0001F3E1"),
    (("life",), "❤️"),
    (("health", "medic", "condition", "medication", "doctor"), "\U0001FA7A"),
    (("solar", "energy", "electric", "utility"), "☀️"),
    (("mortgage", "loan", "credit", "debt", "finance", "bank"), "\U0001F3E6"),
    (("pet", "dog", "cat"), "\U0001F43E"),
    (("business", "commercial", "company", "employer"), "\U0001F3E2"),
    (("claim", "accident", "violation", "incident", "ticket"), "⚠️"),
    (("policy", "insurance", "coverage", "insurer"), "\U0001F6E1️"),
    (("consent", "tcpa", "optin", "opt_in"), "✅"),
    (("tracking", "utm", "campaign", "source", "vendor", "meta"), "\U0001F517"),
)
DEFAULT_ICON = "\U0001F5C2️"
OVERVIEW_ICON = "\U0001F4E8"

# Top-level scalar keys surfaced as header chips (first match per chip)
CHIP_RULES = (
    ("vertical", ("vertical", "category", "product", "productType", "lineOfBusiness", "insuranceType")),
    ("request", ("requestForQuote", "request", "interest", "quoteType", "leadType")),
    ("received", ("receivedAt", "createdAt", "submittedAt", "timestamp", "created", "date")),
)

_MONEYISH = ("value", "amount", "premium", "price", "income", "cost", "deductible",
             "balance", "salary", "payment", "bill")
_NAME_FIRST = ("firstName", "first_name", "firstname", "fname")
_NAME_LAST = ("lastName", "last_name", "lastname", "lname")
_NAME_FULL = ("fullName", "full_name", "name")
_NAME_SUFFIX = ("nameSuffix", "suffix")
_VEHICLE_KEYS = ("year", "make", "model")
_TITLE_FALLBACK_KEYS = ("title", "label", "planName", "carrier", "company", "provider",
                        "type", "kind", "description")
_ADDRESS_PARTS = {"street", "street1", "street2", "address1", "address2", "line1", "line2",
                  "city", "state", "zip", "zipcode", "postalcode", "county", "country"}
_WRAPPER_KEYS = {"person", "details", "detail", "info", "data", "attributes", "profile"}
_ENTITY_PREFIX = re.compile(r"^(primary|additional|secondary|other|extra)_?(?=[A-Za-z])", re.I)
_UPPER_MAKES = {"BMW", "GMC", "RAM", "VW", "MG", "AMC", "SRT", "KTM"}


# ---------------------------------------------------------------------------
# Labels, icons, value formatting
# ---------------------------------------------------------------------------

def label_for(key):
    """camelCase / snake_case key -> human readable label."""
    key = str(key)
    if key in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[key]
    s = re.sub(r"[_\-]+", " ", key)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    words = []
    for w in s.split():
        words.append(w.upper() if w.lower() in _ACRONYMS else w[:1].upper() + w[1:])
    return " ".join(words)


def icon_for(key):
    k = str(key).lower()
    for words, icon in ICON_RULES:
        if any(w in k for w in words):
            return icon
    return DEFAULT_ICON


def _date_from_parts(d):
    """{'day': 3, 'month': 10, 'year': 1985} -> '10/3/1985'; month+year -> '09/2026'."""
    if not isinstance(d, dict) or not d.get("year"):
        return None
    if set(d) - {"year", "month", "day"}:
        return None
    try:
        y, m, dd = int(d["year"]), d.get("month"), d.get("day")
        if m and dd:
            return f"{int(m)}/{int(dd)}/{y}"
        if m:
            return f"{int(m):02d}/{y}"
        return str(y)
    except (TypeError, ValueError):
        return None


def _epoch_to_str(value):
    """{'low': 1788158424, 'high': 0, 'unsigned': False} (Neo4j/Long) -> 'Aug 31, 2026 06:40 UTC'."""
    if not (isinstance(value, dict) and "low" in value and set(value) <= {"low", "high", "unsigned"}):
        return None
    try:
        ts = int(value.get("low") or 0) + (int(value.get("high") or 0) << 32)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 10_000_000_000:  # milliseconds
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts, tz=dt_timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return None


def _iso_to_str(text):
    """'2026-08-31T06:37:41.000Z' -> 'Aug 31, 2026 06:37 UTC'."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.strftime("%b %d, %Y %H:%M") + (" UTC" if text.endswith("Z") else "")


def _phone(text):
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return text


def format_value(key, value):
    """
    Format a scalar-ish value for display. Returns None when there is nothing
    to show, or when the value is structured (a real object / list of objects)
    and must be rendered as a card instead.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return _date_from_parts(value) or _epoch_to_str(value)
    if isinstance(value, (list, tuple)):
        if any(isinstance(v, (dict, list)) for v in value):
            return None
        parts = [format_value(key, v) for v in value]
        return ", ".join(p for p in parts if p) or None
    k = str(key).lower()
    if isinstance(value, (int, float)):
        if any(m in k for m in _MONEYISH):
            return f"${value:,.0f}"
        if "year" in k and 1800 < value < 2200:
            return str(int(value))
        is_identifier = any(t in k.replace("peryear", "") for t in _NO_COMMA)
        if abs(value) >= 1000 and not is_identifier:
            return f"{value:,}"
        return str(value)
    text = str(value).strip()
    if not text:
        return None
    if "phone" in k or k in ("mobile", "cell"):
        return _phone(text)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)  # date-only ISO -> M/D/YYYY
    if m:
        return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}"
    return _iso_to_str(text) or text


def _is_scalarish(value):
    if isinstance(value, dict):
        return bool(_date_from_parts(value) or _epoch_to_str(value))
    if isinstance(value, (list, tuple)):
        return not any(isinstance(v, (dict, list)) for v in value)
    return True


def _age(birth):
    """Age in years from a {year, month, day} dict or an 'M/D/YYYY' / ISO string."""
    year = month = day = None
    if isinstance(birth, dict):
        year, month, day = birth.get("year"), birth.get("month"), birth.get("day")
    elif isinstance(birth, str):
        s = birth.strip()
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
        if m:
            month, day, year = m.group(1), m.group(2), m.group(3)
        else:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                year, month, day = m.group(1), m.group(2), m.group(3)
    if not year:
        return None
    try:
        today = datetime.now(dt_timezone.utc).date()
        age = today.year - int(year)
        if month and day and (int(month), int(day)) > (today.month, today.day):
            age -= 1
    except (TypeError, ValueError):
        return None
    return age if 0 < age < 120 else None


def _is_birth_key(key):
    k = str(key).lower()
    return "birth" in k or k in ("dob", "date_of_birth")


def _norm_key(key):
    return re.sub(r"[_\-\s]", "", str(key)).lower()


def _ordered_items(obj):
    """obj.items() with HIDDEN_KEYS dropped and PRIORITY_KEYS floated to the top."""
    items = [(k, v) for k, v in obj.items() if _norm_key(k) not in HIDDEN_KEYS]
    big = len(PRIORITY_KEYS)
    return [kv for _, kv in sorted(
        enumerate(items), key=lambda p: (_PRIORITY_INDEX.get(_norm_key(p[1][0]), big), p[0])
    )]


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------

def _is_address(key, obj):
    if not isinstance(obj, dict):
        return False
    norm = {re.sub(r"[_\-]", "", str(k)).lower() for k in obj}
    hits = len(norm & _ADDRESS_PARTS)
    return hits >= 2 or ("address" in str(key).lower() and hits >= 1)


def _address_rows(obj):
    rows = []
    for k, v in _ordered_items(obj):
        val = format_value(k, v)
        if val:
            rows.append((label_for(k), val))
    return rows


def _address_oneline(obj):
    if not isinstance(obj, dict):
        return ""
    get = lambda *names: next((str(obj[n]).strip() for n in names if obj.get(n)), "")  # noqa: E731
    street = " ".join(filter(None, [get("street", "street1", "address1", "line1"),
                                    get("street2", "address2", "line2")]))
    state_zip = " ".join(filter(None, [get("state"), get("zip", "zipCode", "zip_code", "postalCode")]))
    return ", ".join(filter(None, [street, get("city"), state_zip]))


def _first_present(obj, keys):
    for k in keys:
        if isinstance(obj, dict) and obj.get(k) not in (None, "", {}, []):
            return k
    return None


def _person_name(obj):
    """Returns (name, keys_used) when the object looks like a person."""
    fk, lk = _first_present(obj, _NAME_FIRST), _first_present(obj, _NAME_LAST)
    if fk or lk:
        sk = _first_present(obj, _NAME_SUFFIX)
        parts = [obj.get(fk) if fk else "", obj.get(lk) if lk else "", obj.get(sk) if sk else ""]
        name = " ".join(str(p).strip() for p in parts if p).strip()
        return name, {k for k in (fk, lk, sk) if k}
    full = _first_present(obj, _NAME_FULL)
    if full and isinstance(obj.get(full), str):
        return obj[full].strip(), {full}
    return "", set()


def _vehicle_token(token):
    if token.upper() in _UPPER_MAKES or (token.isalpha() and len(token) <= 2):
        return token.upper()
    m = re.match(r"^(\d+)([A-Za-z]{4,})$", token)  # 4RUNNER -> 4Runner
    if m:
        return m.group(1) + m.group(2).capitalize()
    if any(ch.isdigit() for ch in token):            # F150, 300ZX, XL7
        return token.upper()
    return token.capitalize()


def _vehicle_name(text):
    """'MERCEDES-BENZ GLE' -> 'Mercedes-Benz GLE'; 'HONDA CR-V' -> 'Honda CR-V'."""
    return " ".join(
        "-".join(_vehicle_token(p) for p in word.split("-"))
        for word in str(text or "").split()
    )


def _vehicle_title(obj):
    if not isinstance(obj, dict) or not (obj.get("make") or obj.get("model")):
        return "", set()
    parts = [str(obj.get("year") or ""), _vehicle_name(obj.get("make")), _vehicle_name(obj.get("model"))]
    return " ".join(p for p in parts if p).strip(), {k for k in _VEHICLE_KEYS if k in obj}


def _infer_title(obj):
    """
    Best human title for an object: a person's name, a vehicle, or a
    descriptive field. Also looks one level into wrapper objects
    (e.g. driver -> person -> firstName). Returns (title, consumed) where
    consumed is a set of (path, key) pairs not to repeat as rows.
    """
    candidates = [((), obj)]
    for k, v in obj.items():
        if isinstance(v, dict) and str(k).lower() in _WRAPPER_KEYS:
            candidates.append(((k,), v))
    for finder in (_person_name, _vehicle_title):
        for path, o in candidates:
            title, used = finder(o)
            if title:
                return title, {(path, u) for u in used}
    for path, o in candidates:
        k = _first_present(o, _TITLE_FALLBACK_KEYS)
        if k and _is_scalarish(o[k]):
            val = format_value(k, o[k])
            if val and len(val) <= 60:
                return val, {(path, k)}
    return "", set()


def _entity_stem(key):
    """primaryCar / additionalCars / additional_drivers -> 'car' / 'car' / 'driver'."""
    s = _ENTITY_PREFIX.sub("", str(key))
    s = re.sub(r"[_\-\s]+", "", s).lower()
    if s.endswith("ies") and len(s) > 4:
        s = s[:-3] + "y"
    elif s.endswith("ren") and s[:-3] == "child":
        s = "child"
    elif s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        s = s[:-1]
    return s


def _group_title(stem):
    if stem in GROUP_TITLES:
        return GROUP_TITLES[stem]
    lbl = label_for(stem)
    return lbl if lbl.endswith("s") else lbl + "s"


# ---------------------------------------------------------------------------
# Rows / cards / sections
# ---------------------------------------------------------------------------

def _rows(obj, depth=1, prefix="", path=(), skip=frozenset()):
    """
    Flatten an object into (label, value) rows. Nested objects are folded in
    (wrapper keys like `person` without a label prefix), addresses become a
    single line, and lists of objects are summarised by their item titles.
    """
    rows = []
    if not isinstance(obj, dict):
        return rows
    for k, v in _ordered_items(obj):
        if (path, k) in skip:
            continue
        lbl = f"{prefix} · {label_for(k)}" if prefix else label_for(k)
        if _is_scalarish(v):
            val = format_value(k, v)
            if val:
                rows.append((lbl, val))
                if _is_birth_key(k):
                    age = _age(v)
                    if age:
                        rows.append(("Age", f"{age} yrs"))
            continue
        if isinstance(v, dict):
            if _is_address(k, v):
                line = _address_oneline(v)
                if line:
                    rows.append((lbl, line))
            elif depth < MAX_DEPTH:
                sub_prefix = prefix if str(k).lower() in _WRAPPER_KEYS else lbl
                rows += _rows(v, depth + 1, sub_prefix, path + (k,), skip)
        elif isinstance(v, list):
            titles = []
            for i, item in enumerate(v, start=1):
                if isinstance(item, dict):
                    t, _ = _infer_title(item)
                    titles.append(t or f"#{i}")
            if titles:
                rows.append((lbl, ", ".join(titles)))
    return rows


def _relation_badge(obj):
    for k, v in obj.items():
        if str(k).lower().startswith("relation") and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _card(key, obj, index=None, is_primary=False):
    """Build one card from an object."""
    if not isinstance(obj, dict) or not obj:
        return None
    title, consumed = _infer_title(obj)
    rows = _rows(obj, depth=1, skip=consumed)
    if not rows and not title:
        return None
    if not title:
        title = label_for(_ENTITY_PREFIX.sub("", str(key)) or key)
        if index:
            title = f"{title} {index}"
    if is_primary:
        badge = "Primary"
    else:
        badge = _relation_badge(obj) or (f"#{index}" if index else "")
    # "kind" names what this entry is (e.g. "Primary Driver"); used by the AI lead context, not shown in the UI
    return {"title": title, "badge": badge, "is_primary": is_primary, "rows": rows, "kind": label_for(key)}


def _section(key, obj):
    """A top-level object -> section with a main card, child cards and card groups."""
    section_title = SECTION_TITLES.get(str(key), label_for(key))
    section = {"key": key, "title": section_title, "icon": icon_for(key), "cards": [], "groups": []}

    main_title, consumed = _infer_title(obj)
    main_rows = []
    address_cards = []
    entities = {}  # stem -> [(key, obj, is_primary, from_list)] in payload order

    for k, v in _ordered_items(obj):
        if ((), k) in consumed:
            continue
        if _is_scalarish(v):
            val = format_value(k, v)
            if val:
                main_rows.append((label_for(k), val))
                if _is_birth_key(k):
                    age = _age(v)
                    if age:
                        main_rows.append(("Age", f"{age} yrs"))
        elif isinstance(v, dict):
            if not v:
                continue
            if _is_address(k, v):
                rows = _address_rows(v)
                if rows:
                    address_cards.append({"title": label_for(k), "badge": "", "is_primary": False,
                                          "icon": icon_for("address"), "rows": rows})
            elif str(k).lower() in _WRAPPER_KEYS:
                main_rows += _rows(v, depth=1, path=(k,), skip=consumed)
            else:
                stem = _entity_stem(k)
                is_primary = str(k).lower().startswith("primary")
                entities.setdefault(stem, []).append((k, v, is_primary, False))
        elif isinstance(v, list):
            dict_items = [item for item in v if isinstance(item, dict) and item]
            if dict_items:
                stem = _entity_stem(k)
                for item in dict_items:
                    entities.setdefault(stem, []).append((k, item, False, True))

    if main_rows or main_title:
        section["cards"].append({"title": main_title, "badge": "", "is_primary": False,
                                 "rows": main_rows})
    section["cards"] += address_cards

    for stem, members in entities.items():
        # A lone nested object (e.g. "coverage") is just another card in the section;
        # primary/additional pairs and lists become a titled group.
        if len(members) == 1 and not members[0][3]:
            k, v, is_primary, _ = members[0]
            card = _card(k, v, is_primary=is_primary)
            if card:
                if not is_primary and card["title"] == label_for(_ENTITY_PREFIX.sub("", str(k)) or k):
                    card["title"] = label_for(k)
                section["cards"].append(card)
            continue
        members.sort(key=lambda m: not m[2])  # primary first, otherwise payload order
        cards = []
        for i, (k, v, is_primary, _) in enumerate(members[:MAX_CARDS_PER_GROUP], start=1):
            card = _card(k, v, index=i, is_primary=is_primary)
            if card:
                cards.append(card)
        if cards:
            section["groups"].append({"title": _group_title(stem), "icon": icon_for(stem),
                                      "cards": cards, "truncated": len(members) > MAX_CARDS_PER_GROUP})

    return section if (section["cards"] or section["groups"]) else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_lead_payload(custom_fields):
    """Locate the raw lead JSON stored on a contact's custom_fields, if any."""
    cf = custom_fields if isinstance(custom_fields, dict) else {}

    def _parse(val):
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and val.strip().startswith("{"):
            try:
                parsed = json.loads(val)
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
        return None

    for k in PAYLOAD_FIELD_KEYS:
        parsed = _parse(cf.get(k))
        if parsed:
            return parsed
    for v in cf.values():
        parsed = _parse(v)
        if parsed:
            return parsed
    return None


def build_lead_dossier(payload):
    """
    Turn any lead payload dict into a display dossier:

        {
          "has_data": bool,
          "chips": [{"kind": "vertical"|"request"|"received", "label", "value"}],
          "sections": [{"title", "icon", "cards": [card], "groups": [{"title", "icon", "cards"}]}],
          "raw_json": str,
        }

    where card = {"title", "badge", "is_primary", "rows": [(label, value)]}.
    Returns None when the payload has nothing displayable.
    """
    if not isinstance(payload, dict) or not payload:
        return None

    chips = []
    for kind, keys in CHIP_RULES:
        for k in keys:
            if k in payload and _is_scalarish(payload[k]):
                val = format_value(k, payload[k])
                if val:
                    if kind == "vertical":
                        val = val[:1].upper() + val[1:]
                    chips.append({"kind": kind, "key": k, "label": label_for(k), "value": val})
                    break

    chip_keys = {c["key"] for c in chips}
    overview_rows = []
    person_sections, other_sections = [], []
    loose_lists = {}

    for k, v in _ordered_items(payload):
        if _is_scalarish(v):
            if k in chip_keys:
                continue  # already shown as a header chip
            val = format_value(k, v)
            if val:
                overview_rows.append((label_for(k), val))
        elif isinstance(v, dict):
            if not v:
                continue
            if _is_address(k, v):
                sec = {"key": k, "title": label_for(k), "icon": icon_for("address"),
                       "cards": [{"title": label_for(k), "badge": "", "is_primary": False,
                                  "rows": _address_rows(v)}], "groups": []}
            else:
                sec = _section(k, v)
            if sec:
                (person_sections if str(k) in SECTION_TITLES else other_sections).append(sec)
        elif isinstance(v, list):
            items = [item for item in v if isinstance(item, dict) and item]
            if items:
                loose_lists[k] = items

    sections = []
    if overview_rows:
        sections.append({"key": "_overview", "title": "Lead Overview", "icon": OVERVIEW_ICON,
                         "cards": [{"title": "", "badge": "", "is_primary": False,
                                    "rows": overview_rows}], "groups": []})
    sections += person_sections + other_sections

    for k, items in loose_lists.items():
        cards = [c for c in (_card(k, item, index=i) for i, item in
                             enumerate(items[:MAX_CARDS_PER_GROUP], start=1)) if c]
        if cards:
            sections.append({"key": k, "title": label_for(k), "icon": icon_for(k), "cards": [],
                             "groups": [{"title": _group_title(_entity_stem(k)), "icon": icon_for(k),
                                         "cards": cards,
                                         "truncated": len(items) > MAX_CARDS_PER_GROUP}]})

    # A payload with only header facts (e.g. just "vertical") still counts as lead data
    if not sections and not chips:
        return None

    try:
        raw_json = json.dumps(payload, indent=2, ensure_ascii=False)[:RAW_JSON_LIMIT]
    except (TypeError, ValueError):
        raw_json = ""

    return {"has_data": True, "chips": chips, "sections": sections, "raw_json": raw_json}


def extract_contact_lead_details(contact):
    """Dossier for a Contact, or None when no lead payload is stored on it."""
    return build_lead_dossier(find_lead_payload(getattr(contact, "custom_fields", None)))


# ---------------------------------------------------------------------------
# Flat "facts" view of a lead (used to auto-answer AI qualification questions)
# ---------------------------------------------------------------------------

def lead_facts(contact) -> List[Tuple[str, str]]:
    """
    Ordered (label, value) pairs for everything known about a lead, first occurrence of
    each label wins: header facts (Vertical, Request For Quote ...), every section card
    (Applicant, Address, policy ...) and the contact's saved custom fields. Entries inside
    repeating groups (each vehicle / driver) are skipped because they are not one value.
    Missing or blank values are simply absent.
    """
    facts: List[Tuple[str, str]] = []
    seen = set()

    def add(label, value):
        label = str(label or "").strip()
        value = "" if value is None else str(value).strip()
        if label and value and label.lower() not in seen:
            seen.add(label.lower())
            facts.append((label, value))

    cf = contact.custom_fields if isinstance(getattr(contact, "custom_fields", None), dict) else {}
    dossier = extract_contact_lead_details(contact) if cf else None
    if dossier:
        for chip in dossier.get("chips", []):
            add(chip.get("label"), chip.get("value"))
        for section in dossier.get("sections", []):
            for card in section.get("cards", []):
                for label, value in card.get("rows", []):
                    add(label, value)

    for key, value in cf.items():
        if key in PAYLOAD_FIELD_KEYS or isinstance(value, (dict, list)):
            continue
        if isinstance(value, str) and value.strip().startswith(("{", "[")):
            continue
        add(label_for(key), value)
    return facts


def lookup_lead_fact(contact, label: str) -> str:
    """Value of one lead fact by label (case-insensitive), or "" when this lead doesn't have it."""
    wanted = str(label or "").strip().lower()
    if not wanted:
        return ""
    for fact_label, value in lead_facts(contact):
        if fact_label.lower() == wanted:
            return value
    return ""
