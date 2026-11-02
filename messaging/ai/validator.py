import re
import json
import logging
from typing import Tuple, Optional, Dict, Any, List, Set
from messaging.models import AIAssistantConfig

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"(https?://\S+|www\.\S+|[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?:/\S*)?)", re.IGNORECASE)
PHONE_REGEX = re.compile(r"(\+?1?[-.\s]?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4})")

# Banned topic terms
PRICE_TERMS = ["$", "dollar", "price", "discount", "promo code", "coupon", "cost you", "hourly rate"]
GUARANTEE_TERMS = ["guarantee", "100% guaranteed", "money back", "warranty"]
LEGAL_MEDICAL_TERMS = ["legal advice", "medical advice", "diagnosis", "prescription", "lawyer up", "sue them", "lawsuit"]


def parse_ai_output(raw_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Parses LLM output into validated JSON dictionary.
    Handles markdown codeblocks (```json ... ```) and leading/trailing chatter.
    """
    if not raw_text or not raw_text.strip():
        return None, "Empty model response."

    cleaned = raw_text.strip()

    # Extract JSON from markdown fences if present
    if "```json" in cleaned:
        parts = cleaned.split("```json")
        if len(parts) > 1:
            cleaned = parts[1].split("```")[0].strip()
    elif "```" in cleaned:
        parts = cleaned.split("```")
        if len(parts) > 1:
            cleaned = parts[1].split("```")[0].strip()

    # If LLM returned chatter before the first '{' and after the last '}'
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        cleaned = cleaned[first_brace : last_brace + 1]

    try:
        data = json.loads(cleaned)
    except Exception as exc:
        return None, f"JSON parsing failed: {exc}"

    if not isinstance(data, dict):
        return None, "Output is not a JSON object."

    # Verify required keys
    for req_key in ["reply", "intent", "collected", "needs_human"]:
        if req_key not in data:
            return None, f"Missing required JSON key '{req_key}'."

    if not isinstance(data["reply"], str):
        return None, "Field 'reply' must be a string."
    if not isinstance(data["collected"], dict):
        data["collected"] = {}
    if not isinstance(data["needs_human"], bool):
        data["needs_human"] = bool(data["needs_human"])

    return data, None


def validate_reply_content(data: Dict[str, Any], config: AIAssistantConfig) -> Tuple[bool, Optional[str]]:
    """
    Validates the generated reply string against negative rules, safety topics, length,
    and unknown URLs/phone numbers.
    Returns (is_valid, failure_reason).
    """
    reply = data.get("reply", "").strip()

    # 1. Non-empty check
    if not reply:
        return False, "Generated reply is empty."

    # 2. Length check
    max_chars = config.max_reply_chars or 320
    if len(reply) > max_chars:
        return False, f"Reply length ({len(reply)} chars) exceeds maximum allowed limit ({max_chars} chars)."

    reply_lower = reply.lower()
    knowledge_lower = (config.knowledge or "").lower()

    # 3. Forbidden phrases in config.never_say
    if config.never_say:
        for line in config.never_say.splitlines():
            phrase = line.strip().lower()
            if phrase and phrase in reply_lower:
                return False, f"Reply contains forbidden phrase from never_say rules: '{phrase}'."

    # 4. Forbidden topics: Price / Discount mentions
    for term in PRICE_TERMS:
        if term in reply_lower:
            return False, f"Reply mentions prohibited price/discount topic: '{term}'."

    # 5. Forbidden topics: Guarantees / Legal / Medical advice
    for term in GUARANTEE_TERMS:
        if term in reply_lower:
            return False, f"Reply mentions prohibited guarantee topic: '{term}'."

    for term in LEGAL_MEDICAL_TERMS:
        if term in reply_lower:
            return False, f"Reply mentions prohibited legal/medical advice: '{term}'."

    # 6. URLs not present in Knowledge text
    found_urls = URL_REGEX.findall(reply)
    for url in found_urls:
        url_clean = url.lower().rstrip(".,;!?:")
        if url_clean not in knowledge_lower:
            return False, f"Reply contains URL not found in approved business knowledge: '{url}'."

    # 7. Phone numbers not present in Knowledge text
    found_phones = PHONE_REGEX.findall(reply)
    for phone in found_phones:
        phone_digits = "".join(filter(str.isdigit, phone))
        knowledge_digits = "".join(filter(str.isdigit, config.knowledge or ""))
        if phone_digits and phone_digits not in knowledge_digits:
            return False, f"Reply contains phone number not found in approved business knowledge: '{phone}'."

    return True, None


def filter_collected_answers(collected: Dict[str, Any], config: AIAssistantConfig) -> Dict[str, Any]:
    """
    Filters collected answers so only keys explicitly configured in qualification_questions
    are kept.
    """
    if not isinstance(collected, dict):
        return {}

    allowed_keys: Set[str] = set()
    questions = config.qualification_questions if isinstance(config.qualification_questions, list) else []
    for q in questions:
        if isinstance(q, dict) and "key" in q:
            allowed_keys.add(q["key"])

    return {k: v for k, v in collected.items() if k in allowed_keys and v is not None}

