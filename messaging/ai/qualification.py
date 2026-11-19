"""
Qualification questions: answered from lead data where possible, and checked against
the rules your team sets to decide whether a lead is a fit (can we sell to them?).

Each question (configured in AI Assistant settings by an owner, admin or agent) can have:
  * lead_field   - answer it from this lead field when the lead has it, instead of asking.
  * rule         - "not_fit_if": the lead is NOT a fit when the answer matches rule_values
                   (e.g. current insurer is "Allstate" for an Allstate agent);
                   "only_fit_if": the lead is only a fit when the answer matches rule_values
                   (e.g. state in "CA, TX").
  * rule_values  - the answers the rule matches.

Answers are checked by code, never by the model, so the decision is predictable.
Matching ignores case and punctuation and accepts the value inside a longer answer
("I'm with Allstate right now" matches "Allstate").

Campaigns keep their own copy of the questions; lead_field and rules set in AI settings
also apply to any campaign question with the same key unless it sets its own.
"""
import re
from typing import Any, Dict, List, Tuple

from messaging.models import AIAssistantConfig, Conversation

RULE_NOT_FIT_IF = "not_fit_if"
RULE_ONLY_FIT_IF = "only_fit_if"
RULES = (RULE_NOT_FIT_IF, RULE_ONLY_FIT_IF)

FIT = "fit"            # every required question answered and no rule failed
NOT_FIT = "not_fit"    # a rule says we can't sell to this lead
PENDING = "pending"    # still collecting answers

# Answers that mean "no real answer" and are never matched against rules
_NON_ANSWERS = {"", "declined", "skip", "skipped", "n/a", "na", "none given", "unknown"}
_INHERITED_ATTRS = ("lead_field", "rule", "rule_values", "step")

# ---------------------------------------------------------------------------
# Follow-up questions
#   A follow-up has "parent": <key of another question> and
#   "condition": {"op": "is" | "is_not" | "any", "values": [...]}.
#   It is only asked after its parent is answered AND the parent's answer meets the
#   condition. Follow-ups can be nested two levels deep (question -> follow-up ->
#   follow-up), always belong to their top-level question's step, and are asked
#   right after their parent.
# ---------------------------------------------------------------------------
COND_IS = "is"
COND_IS_NOT = "is_not"
COND_ANY = "any"
CONDITIONS = (COND_IS, COND_IS_NOT, COND_ANY)
MAX_FOLLOW_UP_DEPTH = 2


def _config_questions(config: AIAssistantConfig) -> Dict[str, Dict[str, Any]]:
    if config is None or not isinstance(config.qualification_questions, list):
        return {}
    return {q["key"]: q for q in config.qualification_questions if isinstance(q, dict) and q.get("key")}


def question_step(q: Dict[str, Any]) -> int:
    try:
        return 2 if int(q.get("step") or 1) == 2 else 1
    except (TypeError, ValueError):
        return 1


def order_questions(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Step 1 then Step 2; each top-level question followed by its follow-ups (and theirs).
    Follow-ups take their top-level question's step. Follow-ups whose parent is missing,
    or that are nested deeper than MAX_FOLLOW_UP_DEPTH, are dropped.
    Adds "_depth" (0 = main question, 1 = follow-up, 2 = follow-up of a follow-up).
    """
    keys = {q["key"] for q in questions}
    children: Dict[str, List[Dict[str, Any]]] = {}
    roots = []
    for q in questions:
        parent = q.get("parent")
        if parent and parent in keys and parent != q["key"]:
            children.setdefault(parent, []).append(q)
        elif not parent:
            roots.append(q)

    out: List[Dict[str, Any]] = []

    def walk(q, depth, step):
        q = dict(q, _depth=depth, step=step)
        out.append(q)
        if depth < MAX_FOLLOW_UP_DEPTH:
            for child in children.get(q["key"], []):
                walk(child, depth + 1, step)

    for step in (1, 2):
        for root in roots:
            if question_step(root) == step:
                walk(root, 0, step)
    return out


def effective_questions(conversation: Conversation, config: AIAssistantConfig) -> List[Dict[str, Any]]:
    """
    The questions in effect (a custom campaign's own list, else the business defaults), in asking
    order, with lead_field / rule / rule_values / step inherited from AI settings by key when missing.
    Follow-ups defined in AI settings are added to a campaign's list under the matching parent.
    """
    camp = getattr(conversation, "last_campaign", None)
    using_campaign = bool(camp and camp.ai_mode == "custom" and camp.ai_qualification_questions)
    questions = camp.ai_qualification_questions if using_campaign else (
        config.qualification_questions if isinstance(config.qualification_questions, list) else []
    )

    defaults = _config_questions(config)
    merged = []
    for q in questions:
        if not isinstance(q, dict) or not q.get("key"):
            continue
        q = dict(q)
        base = defaults.get(q["key"], {})
        for attr in _INHERITED_ATTRS:
            if not q.get(attr) and base.get(attr):
                q[attr] = base[attr]
        merged.append(q)

    if using_campaign:
        present = {q["key"] for q in merged}
        added = True
        while added:
            added = False
            for q in defaults.values():
                if q.get("parent") in present and q["key"] not in present:
                    merged.append(dict(q))
                    present.add(q["key"])
                    added = True
    return order_questions(merged)


# Backwards-compatible name used elsewhere
active_questions = effective_questions


def _verdict(checks: Dict[str, Any], check_key: str, answer: Any) -> Dict[str, Any]:
    """Gemma's verdict for this check, only if it was made for the answer we have now."""
    v = (checks or {}).get(check_key)
    if isinstance(v, dict) and _norm(v.get("answer")) == _norm(answer):
        return v
    return {}


def follow_up_active(q: Dict[str, Any], answers: Dict[str, Any], by_key: Dict[str, Dict[str, Any]],
                     checks: Dict[str, Any] = None) -> bool:
    """
    Main questions are always active. A follow-up is active only when its parent is active,
    the parent has a real answer (not declined/blank), and that answer meets the condition.
    Conditions can be written in plain language ("less than 6 months ago"): Gemma judges them
    (stored as checks["cond:<key>"]); an exact name match in code always counts.
    """
    parent_key = q.get("parent")
    if not parent_key:
        return True
    parent = by_key.get(parent_key)
    if not parent or not follow_up_active(parent, answers, by_key, checks):
        return False
    answer = answers.get(parent_key)
    if _norm(answer) in _NON_ANSWERS:
        return False
    cond = q.get("condition") if isinstance(q.get("condition"), dict) else {}
    op = cond.get("op") or COND_ANY
    values = parse_rule_values(cond.get("values"))
    if op == COND_ANY or not values:
        return True
    timed = time_rule_match(answer, values)
    if timed is not None:                     # date arithmetic done in code
        return timed if op == COND_IS else not timed
    code_match = answer_matches(answer, values)
    verdict = _verdict(checks, f"cond:{q['key']}", answer)
    if op == COND_IS:
        return True if code_match else bool(verdict.get("met"))
    if code_match:          # is_not
        return False
    return bool(verdict.get("met")) if verdict else True


# ---------------------------------------------------------------------------
# Steps
#   Step 1 - asked to every lead (e.g. what they need, ZIP).
#   Step 2 - asked ONLY to leads whose lead form already answered every main step-1
#            question. Leads we know little about get step 1 only, then the call offer.
# ---------------------------------------------------------------------------

def lead_form_answers_step1(conversation: Conversation, questions: List[Dict[str, Any]]) -> bool:
    """
    True when the lead's own form/data answers EVERY main (non-follow-up) step-1 question:
    each must be linked to a lead field and the lead must have a value for it. Answers given
    later in chat don't count: step 2 is only for leads that arrived with that information.
    """
    from contacts.lead_dossier import lead_facts

    step1 = [q for q in questions if question_step(q) == 1 and not q.get("parent")]
    if not step1:
        return True
    if any(not str(q.get("lead_field") or "").strip() for q in step1):
        return False
    contact = getattr(conversation, "contact", None)
    if contact is None:
        return False
    facts = {label.lower() for label, value in lead_facts(contact) if value}
    return all(str(q["lead_field"]).strip().lower() in facts for q in step1)


def questions_in_play(conversation: Conversation, config: AIAssistantConfig) -> List[Dict[str, Any]]:
    """
    What this lead may be asked, in order: step-1 questions for every lead, step-2 questions
    only for leads whose form answered step 1, and follow-ups only when their condition is met.
    """
    questions = effective_questions(conversation, config)
    if not lead_form_answers_step1(conversation, questions):
        questions = [q for q in questions if question_step(q) == 1]
    answers = answers_with_lead_data(conversation, config)
    checks = getattr(conversation, "qualification_checks", None) or {}
    by_key = {q["key"]: q for q in questions}
    return [q for q in questions if follow_up_active(q, answers, by_key, checks)]


def lead_prefilled_answers(
    conversation: Conversation,
    questions: List[Dict[str, Any]],
    config: AIAssistantConfig = None,
) -> Dict[str, str]:
    """Answers available from lead data for questions not answered yet. Never touches the DB."""
    from contacts.lead_dossier import lead_facts

    defaults = _config_questions(config)
    answers = conversation.qualification_answers if isinstance(conversation.qualification_answers, dict) else {}
    wanted = []
    for q in questions:
        if not isinstance(q, dict) or not q.get("key") or answers.get(q["key"]):
            continue
        lead_field = str(q.get("lead_field") or defaults.get(q["key"], {}).get("lead_field") or "").strip()
        if lead_field:
            wanted.append((q, lead_field))
    if not wanted or not getattr(conversation, "contact", None):
        return {}
    facts = {label.lower(): value for label, value in lead_facts(conversation.contact)}
    found = {}
    for q, lead_field in wanted:
        value = facts.get(lead_field.lower(), "")
        if value:
            found[q["key"]] = value
    return found


def answers_with_lead_data(conversation: Conversation, config: AIAssistantConfig) -> Dict[str, Any]:
    """
    All answers for this conversation: what the customer said in chat, plus answers looked up
    LIVE from the lead form for questions linked to a lead field. Nothing from the form is stored,
    so changing a question to "Always ask" (or relinking it) takes effect immediately.
    The customer's own chat answer always wins over the form.
    """
    chat = dict(conversation.qualification_answers) if isinstance(conversation.qualification_answers, dict) else {}
    from_form = lead_prefilled_answers(conversation, effective_questions(conversation, config), config)
    return {**from_form, **chat}


def next_unanswered_question(conversation: Conversation, config: AIAssistantConfig) -> Dict[str, Any]:
    """The next question to ask: Step 1 before Step 2, each question followed by its follow-ups."""
    answers = answers_with_lead_data(conversation, config)
    for q in questions_in_play(conversation, config):
        if not answers.get(q["key"]) and (q.get("question") or "").strip():
            return q
    return {}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def parse_rule_values(raw: Any) -> List[str]:
    """'Allstate, State Farm' / ['Allstate', ...] -> clean list."""
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for v in raw:
        v = str(v or "").strip()
        if v and v.lower() not in {x.lower() for x in out}:
            out.append(v[:100])
    return out[:50]


def answer_matches(answer: Any, values: List[str]) -> bool:
    """True when the answer equals, or contains as whole words, any of the values."""
    a = _norm(answer)
    if not a:
        return False
    padded = f" {a} "
    for v in values or []:
        nv = _norm(v)
        if nv and (a == nv or f" {nv} " in padded):
            return True
    return False


# ---------------------------------------------------------------------------
# Time rules: "less than 6 months", "more than 2 years", "within the last 30 days" ...
# Date arithmetic is done in code: small language models get it wrong too often.
# Gemma is still used for answers code can't read as a date (e.g. "last spring").
# ---------------------------------------------------------------------------
_TIME_RULE_RE = re.compile(
    r"\b(less|fewer|under|within|more|over|greater|older|at\s+least|at\s+most)\b"
    r"(?:\s+than)?(?:\s+the)?(?:\s+last|\s+past)?\s+(\d+)\s*(day|week|month|year)s?",
    re.I,
)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30.44, "year": 365.25}
_DATEISH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\b(19|20)\d{2}\b|\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b",
    re.I,
)
_AGO_RE = re.compile(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(day|week|month|year)s?\s+ago\b", re.I)
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def parse_time_rule(values: Any):
    """('lt' or 'gt', days) when the rule/condition text is about elapsed time, else None."""
    text = " ".join(parse_rule_values(values)) if not isinstance(values, str) else values
    m = _TIME_RULE_RE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower().replace("  ", " ")
    direction = "lt" if word in ("less", "fewer", "under", "within") or word.startswith("at most") else "gt"
    return direction, int(m.group(2)) * _UNIT_DAYS[m.group(3).lower()]


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def year_options(answer: Any, today=None) -> List[str]:
    """The two most recent real dates a year-less answer can mean: "august" on 19 Nov 2026 ->
    ["August 2026", "August 2025"]; "december" -> ["December 2025", "December 2024"] (no future dates)."""
    import datetime as _dt
    today = today or _dt.date.today()
    text = str(answer or "").lower()
    month = next((i + 1 for i, m in enumerate(_MONTHS) if re.search(r"\b" + m[:3] + r"[a-z]*\b", text)), None)
    if not month:
        return [f"this year ({today.year})", f"last year ({today.year - 1})"]
    year = today.year if month <= today.month else today.year - 1
    name = _MONTHS[month - 1].capitalize()
    day = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
    prefix = f"{name} {int(day.group(1))}," if day and 1 <= int(day.group(1)) <= 31 else name
    return [f"{prefix} {year}", f"{prefix} {year - 1}"]


def is_unclear_time_answer(answer: Any) -> bool:
    """A date with no year ("october", "20 july") can't tell us how long ago it was."""
    text = str(answer or "").strip().lower()
    if not text or _AGO_RE.search(text):
        return False
    if any(p in text for p in ("today", "yesterday", "last week", "last month", "last year", "this year", "this month")):
        return False
    return bool(_DATEISH_RE.search(text)) and not _YEAR_RE.search(text)


def is_time_question(key: str, questions: List[Dict[str, Any]]) -> bool:
    """Does a time rule depend on this question (its own fit rule, or a follow-up condition on it)?"""
    for q in questions:
        if q.get("key") == key and q.get("rule") in RULES and parse_time_rule(q.get("rule_values")):
            return True
        cond = q.get("condition") if isinstance(q.get("condition"), dict) else {}
        if q.get("parent") == key and parse_time_rule(cond.get("values") or ""):
            return True
    return False


def answer_age_days(answer: Any, today=None):
    """How many days ago the answer's date was ("20 july 2026", "January 2025", "3 months ago"), or None."""
    import datetime as _dt
    text = str(answer or "").strip().lower()
    if not text:
        return None
    today = today or _dt.date.today()
    m = _AGO_RE.search(text)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _WORD_NUM[m.group(1)]
        return n * _UNIT_DAYS[m.group(2)]
    simple = {"today": 0, "yesterday": 1, "last week": 7, "last month": 30.44, "last year": 365.25}
    for phrase, days in simple.items():
        if phrase in text:
            return days
    if not _DATEISH_RE.search(text) or is_unclear_time_answer(text):
        return None
    try:
        from dateutil import parser as _dparser
        d = _dparser.parse(text, fuzzy=True, default=_dt.datetime(today.year, today.month, 1)).date()
    except (ValueError, OverflowError, TypeError):
        return None
    return (today - d).days


def time_rule_match(answer: Any, values: Any, today=None):
    """True/False when code can decide a time rule for this answer; None when it can't."""
    rule = parse_time_rule(values)
    if not rule:
        return None
    age = answer_age_days(answer, today)
    if age is None:
        return None
    direction, days = rule
    return age < days if direction == "lt" else age > days


def evaluate_fit(
    questions: List[Dict[str, Any]],
    answers: Dict[str, Any],
    required_from: List[Dict[str, Any]] = None,
    checks: Dict[str, Any] = None,
) -> Tuple[str, List[str]]:
    """
    (status, reasons). NOT_FIT as soon as any answered question breaks its rule (every
    question is checked, so a known "Allstate" counts even if that question isn't asked).
    FIT needs every required question in `required_from` (default: `questions`) answered,
    i.e. only the questions actually in play for this lead.
    A declined / blank answer never triggers a rule (we can't decide on it).
    """
    reasons = []
    for q in questions:
        rule = q.get("rule")
        values = parse_rule_values(q.get("rule_values"))
        answer = answers.get(q.get("key"))
        if rule not in RULES or not values or _norm(answer) in _NON_ANSWERS:
            continue
        matched = answer_matches(answer, values)
        verdict = _verdict(checks, f"rule:{q['key']}", answer)
        why = f' ({verdict["reason"]})' if verdict.get("reason") else ""
        timed = time_rule_match(answer, values)
        if timed is not None:                 # date arithmetic done in code, not by the model
            age = answer_age_days(answer)
            verdict = {"result": "not_fit" if timed == (rule == RULE_NOT_FIT_IF) else "fit"}
            matched = timed
            why = f" (about {round(age / 30.44)} months ago)" if age is not None else ""
        if rule == RULE_NOT_FIT_IF and (matched or verdict.get("result") == "not_fit"):
            reasons.append(f'{q["key"]} is "{answer}": not a fit if {", ".join(values)}{why}')
        elif rule == RULE_ONLY_FIT_IF:
            fits = (verdict.get("result") == "fit") if verdict else matched
            if not (fits or matched):
                reasons.append(f'{q["key"]} is "{answer}": only a fit if {", ".join(values)}{why}')
    if reasons:
        return NOT_FIT, reasons
    required = [q["key"] for q in (required_from if required_from is not None else questions) if q.get("required")]
    if all(answers.get(k) for k in required):
        return FIT, []
    return PENDING, []


def conversation_fit(conversation: Conversation, config: AIAssistantConfig) -> Tuple[str, List[str]]:
    """Fit status using every source: chat answers, the lead form and Gemma's verdicts."""
    return evaluate_fit(
        effective_questions(conversation, config),
        answers_with_lead_data(conversation, config),
        required_from=questions_in_play(conversation, config),
        checks=getattr(conversation, "qualification_checks", None) or {},
    )


# ---------------------------------------------------------------------------
# Checks Gemma judges (plain-language fit rules and follow-up conditions)
# ---------------------------------------------------------------------------

def pending_checks(conversation: Conversation, config: AIAssistantConfig, next_key: str = "") -> List[Dict[str, Any]]:
    """
    Rules and follow-up conditions Gemma should judge now: those whose answer is already known
    but not yet judged for that answer, plus those depending on the question being asked next
    (so Gemma can judge the customer's reply in the same turn).
    """
    questions = effective_questions(conversation, config)
    by_key = {q["key"]: q for q in questions}
    answers = answers_with_lead_data(conversation, config)
    checks = getattr(conversation, "qualification_checks", None) or {}
    out = []
    for q in questions:
        if q.get("rule") in RULES and parse_rule_values(q.get("rule_values")):
            ans = answers.get(q["key"])
            known = _norm(ans) not in _NON_ANSWERS
            code_decides = known and time_rule_match(ans, q.get("rule_values")) is not None
            if not code_decides and ((known and not _verdict(checks, f"rule:{q['key']}", ans)) or q["key"] == next_key):
                out.append({"id": f"rule:{q['key']}", "kind": "rule", "q": q, "subject": q,
                            "answer": ans if known else ""})
        cond = q.get("condition") if isinstance(q.get("condition"), dict) else {}
        if q.get("parent") in by_key and cond.get("op") in (COND_IS, COND_IS_NOT) and parse_rule_values(cond.get("values")):
            parent = by_key[q["parent"]]
            ans = answers.get(parent["key"])
            known = _norm(ans) not in _NON_ANSWERS
            code_decides = known and time_rule_match(ans, cond.get("values")) is not None
            if not code_decides and ((known and not _verdict(checks, f"cond:{q['key']}", ans)) or parent["key"] == next_key):
                out.append({"id": f"cond:{q['key']}", "kind": "cond", "q": q, "subject": parent,
                            "answer": ans if known else ""})
    return out


def record_checks(conversation: Conversation, config: AIAssistantConfig, raw: Any) -> Dict[str, Any]:
    """Store Gemma's verdicts (only for checks that exist), tagged with the answer they judged."""
    if not isinstance(raw, dict) or not raw:
        return {}
    questions = effective_questions(conversation, config)
    by_key = {q["key"]: q for q in questions}
    answers = answers_with_lead_data(conversation, config)
    saved = {}
    for check_id, verdict in raw.items():
        if not isinstance(verdict, dict) or ":" not in str(check_id):
            continue
        kind, key = str(check_id).split(":", 1)
        q = by_key.get(key)
        if not q:
            continue
        reason = str(verdict.get("reason") or "")[:300]
        if kind == "rule" and q.get("rule") in RULES:
            ans = answers.get(key)
            result = str(verdict.get("result") or "").strip().lower().replace(" ", "_")
            if result in ("fit", "not_fit") and _norm(ans) not in _NON_ANSWERS:
                saved[check_id] = {"result": result, "reason": reason, "answer": ans}
        elif kind == "cond" and q.get("parent") in by_key:
            ans = answers.get(q["parent"])
            met = verdict.get("met")
            if isinstance(met, str):
                met = met.strip().lower() in ("true", "yes", "1")
            if isinstance(met, bool) and _norm(ans) not in _NON_ANSWERS:
                saved[check_id] = {"met": met, "reason": reason, "answer": ans}
    if saved:
        stored = dict(getattr(conversation, "qualification_checks", None) or {})
        stored.update(saved)
        conversation.qualification_checks = stored
        if conversation.pk:
            conversation.save(update_fields=["qualification_checks"])
    return saved


def describe_check(c: Dict[str, Any]) -> str:
    """One line describing a check for Gemma (used in the main prompt and the judging pass)."""
    subject = c["subject"]
    if c["kind"] == "rule":
        values = c["q"].get("rule_values")
    else:
        values = (c["q"].get("condition") or {}).get("values")
    values = ", ".join(values) if isinstance(values, list) else str(values or "")
    known = f'answer: "{c["answer"]}"' if c["answer"] else "not answered yet; judge it when they answer"
    if c["kind"] == "rule":
        verb = "NOT a fit if the answer is" if c["q"].get("rule") == RULE_NOT_FIT_IF else "ONLY a fit if the answer is"
        return (f'- "{c["id"]}": "{subject.get("question", subject["key"])}" ({known}) -> {verb}: {values}. '
                f'Give {{"result": "fit" or "not_fit", "reason": "..."}}')
    op = (c["q"].get("condition") or {}).get("op")
    verb = "is NOT" if op == COND_IS_NOT else "is"
    return (f'- "{c["id"]}": ask "{c["q"].get("question", c["q"]["key"])}" only if the answer to '
            f'"{subject.get("question", subject["key"])}" ({known}) {verb}: {values}. '
            f'Give {{"met": true or false, "reason": "..."}}')


def checks_needing_judgement(conversation: Conversation, config: AIAssistantConfig) -> List[Dict[str, Any]]:
    """Known answers with no verdict yet, skipping checks an exact name match already decides."""
    out = []
    for c in pending_checks(conversation, config):
        if not c["answer"]:
            continue
        if c["kind"] == "rule":
            values = parse_rule_values(c["q"].get("rule_values"))
            if c["q"].get("rule") == RULE_NOT_FIT_IF and answer_matches(c["answer"], values):
                continue
        else:
            values = parse_rule_values((c["q"].get("condition") or {}).get("values"))
            if answer_matches(c["answer"], values):
                continue
        out.append(c)
    return out
