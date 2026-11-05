import time
import logging
from datetime import timedelta
from typing import Tuple, Optional, Dict, Any

from django.utils import timezone
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction

from contacts.models import Contact
from compliance.services import classify_keyword
from messaging.models import AIAssistantConfig, Conversation, Message, AIJob, AIReply
from messaging.services import send_message, can_send_manual
from messaging.routing import assign_qualified_lead
from . import get_ai_client
from .prompt import build_system_prompt, build_conversation_messages, AI_RESPONSE_SCHEMA
from .validator import parse_ai_output, validate_reply_content, filter_collected_answers

logger = logging.getLogger(__name__)


def can_run_ai(
    config: Optional[AIAssistantConfig],
    conversation: Conversation,
    trigger_message: Message,
) -> Tuple[bool, str]:
    """
    Evaluates whether the AI may process a job and reply.
    Respects conversation-level ai_mode override (Off, Suggest, Auto).
    Returns (allowed, skip_reason).
    """
    effective_mode = conversation.get_effective_ai_mode(config)
    if effective_mode == AIAssistantConfig.Mode.OFF:
        if conversation.ai_mode == Conversation.AIMode.OFF:
            return False, "AI is turned off for this chat (Human Takeover active)."
        return False, "AI Assistant mode is off."

    if conversation.status == Conversation.Status.CLOSED:
        return False, "Conversation is closed."

    if conversation.status == Conversation.Status.NEEDS_HUMAN:
        if conversation.ai_mode == Conversation.AIMode.OFF:
            return False, "AI is turned off for this chat (Human Takeover active)."

    kw = classify_keyword(trigger_message.body)
    if kw in ["STOP", "START", "HELP"]:
        return False, f"Trigger message was compliance keyword '{kw}'."

    contact = conversation.contact
    if contact.sms_status == Contact.SMSStatus.OPTED_OUT:
        return False, "Contact is opted out of SMS."

    # Hourly cap
    max_per_hour = config.max_ai_replies_per_hour_per_contact or 6
    one_hour_ago = timezone.now() - timedelta(hours=1)
    recent_ai_replies = AIReply.objects.filter(
        conversation=conversation,
        status__in=[AIReply.Status.SENT, AIReply.Status.DRAFT],
        created_at__gte=one_hour_ago,
    ).count()
    if recent_ai_replies >= max_per_hour:
        return False, f"Contact reached max hourly limit ({max_per_hour}) of AI replies."

    # Never double-text: if last outbound message was an AI message, customer must have replied since
    last_outbound = conversation.messages.filter(
        direction=Message.Direction.OUTBOUND,
        sender_type__in=[Message.SenderType.AI, Message.SenderType.HUMAN],
    ).order_by("-created_at").first()

    if last_outbound and last_outbound.sender_type == Message.SenderType.AI:
        has_inbound_after = conversation.messages.filter(
            direction=Message.Direction.INBOUND,
            created_at__gt=last_outbound.created_at,
        ).exists()
        if not has_inbound_after:
            return False, "Last outbound message was from AI and customer has not replied since (never double-text)."

    return True, ""


def _trigger_handoff(
    conversation: Conversation,
    trigger_message: Message,
    job: Optional[AIJob],
    reason: str,
    raw_output: str = "",
    parsed_output: Optional[Dict[str, Any]] = None,
    error: str = "",
    latency_ms: int = 0,
    model_name: str = "",
    sent_message: Optional[Message] = None,
) -> AIReply:
    """
    Marks the conversation as needs_human, posts a system explanation,
    and logs the handoff in AIReply.
    """
    logger.info("Triggering human handoff for conversation %d: %s", conversation.id, reason)

    already_needs_human = (conversation.status == Conversation.Status.NEEDS_HUMAN)

    if not already_needs_human:
        conversation.status = Conversation.Status.NEEDS_HUMAN
        conversation.handoff_reason = reason or ""
        conversation.save(update_fields=["status", "handoff_reason"])

        # Create clean, readable system audit message in thread
        display_reason = reason or "Human review requested."
        if "HTTPConnectionPool" in display_reason or "WinError 10061" in display_reason or "Connection to LLM" in display_reason:
            display_reason = "AI Assistant offline / connection refused."
        elif len(display_reason) > 160:
            display_reason = display_reason[:157] + "..."

        Message.objects.create(
            conversation=conversation,
            organization=conversation.organization,
            direction=Message.Direction.OUTBOUND,
            sender_type=Message.SenderType.SYSTEM,
            body=f"Conversation handed off to team member: {display_reason}",
            status=Message.Status.DELIVERED,
        )

        # Attempt automated sales rep routing if configured
        try:
            assign_qualified_lead(conversation, trigger_reason=f"Human handoff: {reason}")
        except Exception as exc:
            logger.exception("Error during auto lead assignment on handoff: %s", exc)

    return AIReply.objects.create(
        conversation=conversation,
        trigger_message=trigger_message,
        resulting_message=sent_message,
        job=job,
        status=AIReply.Status.HANDED_OFF,
        skip_reason=reason,
        model_name=model_name,
        raw_output=raw_output,
        parsed_output=parsed_output or {},
        error=error,
        latency_ms=latency_ms,
    )


def _check_and_update_stage(conversation: Conversation, config: AIAssistantConfig):
    """
    Checks if all required qualification questions have been collected.
    If so, updates contact.custom_fields['stage'] to 'Qualified'.
    """
    questions = config.qualification_questions if isinstance(config.qualification_questions, list) else []
    required_keys = [q.get("key") for q in questions if isinstance(q, dict) and q.get("required") and q.get("key")]

    if not required_keys:
        return

    answers = conversation.qualification_answers if isinstance(conversation.qualification_answers, dict) else {}
    all_answered = all(answers.get(k) for k in required_keys)

    if all_answered:
        contact = conversation.contact
        if not isinstance(contact.custom_fields, dict):
            contact.custom_fields = {}
        if contact.custom_fields.get("stage") != "Qualified":
            contact.custom_fields["stage"] = "Qualified"
            contact.save(update_fields=["custom_fields"])
            logger.info("Contact %d updated to Stage 'Qualified'", contact.id)

        # Trigger automatic sales rep assignment for the newly qualified lead
        try:
            assign_qualified_lead(conversation, trigger_reason="Lead qualification complete")
        except Exception as exc:
            logger.exception("Error during auto lead assignment on qualification: %s", exc)


def process_ai_job(job: AIJob) -> Optional[AIReply]:
    """
    Main background processing pipeline for an AIJob:
    1. Checks preconditions.
    2. Invokes local LLM with cache-friendly prompt and schema.
    3. Retries once on invalid JSON/transient failure, hands off on second failure.
    4. Validates safety rules, character caps, and negative phrases.
    5. Saves qualification answers and updates Stage if complete.
    6. Dispatches to Suggest draft or Auto send.
    7. Logs complete audit row in AIReply.
    """
    conversation = job.conversation
    trigger_message = job.trigger_message
    org = conversation.organization

    config = getattr(org, "ai_config", None)
    if not config:
        config = AIAssistantConfig.objects.filter(organization=org).first()

    # Step 1: Pre-checks
    allowed, skip_reason = can_run_ai(config, conversation, trigger_message)
    if not allowed:
        logger.info("Skipping AI job %d: %s", job.id, skip_reason)
        job.status = AIJob.Status.DONE
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])

        return AIReply.objects.create(
            conversation=conversation,
            trigger_message=trigger_message,
            job=job,
            status=AIReply.Status.SKIPPED,
            skip_reason=skip_reason,
            model_name=getattr(config, "model_name", "") if config else "",
        )

    # Step 2: Prepare prompt and client
    client = get_ai_client()
    system_prompt = build_system_prompt(config, conversation)
    chat_messages = build_conversation_messages(conversation)

    model_name = getattr(client, "model", "local-gemma")
    queue_wait_ms = int((timezone.now() - job.created_at).total_seconds() * 1000)

    job.status = AIJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    raw_text = ""
    parsed_data: Optional[Dict[str, Any]] = None
    input_tokens = 0
    output_tokens = 0
    latency_ms = 0

    # Step 3: Call model (retry once on JSON parse/format failure)
    max_attempts = 2
    last_error = ""

    for attempt in range(max_attempts):
        try:
            resp = client.generate(
                system_prompt=system_prompt,
                messages=chat_messages,
                schema=AI_RESPONSE_SCHEMA,
            )
            raw_text = resp.text
            input_tokens = resp.input_tokens
            output_tokens = resp.output_tokens
            latency_ms = resp.latency_ms

            data, parse_err = parse_ai_output(raw_text)
            if not parse_err and data:
                parsed_data = data
                break
            else:
                last_error = parse_err or "Invalid JSON"
                logger.warning("AI output validation retry %d: %s", attempt + 1, last_error)

        except Exception as exc:
            last_error = str(exc)
            logger.warning("AI generation exception attempt %d: %s", attempt + 1, exc)

        time.sleep(0.5)

    # If both attempts failed -> handoff
    if not parsed_data:
        reply_obj = _trigger_handoff(
            conversation=conversation,
            trigger_message=trigger_message,
            job=job,
            reason=f"AI formatting/service error: {last_error}",
            raw_output=raw_text,
            error=last_error,
            latency_ms=latency_ms,
            model_name=model_name,
        )
        job.status = AIJob.Status.DONE
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        return reply_obj

    # Step 4: Validate content in plain Python
    is_valid, content_err = validate_reply_content(parsed_data, config)
    if not is_valid:
        reply_obj = _trigger_handoff(
            conversation=conversation,
            trigger_message=trigger_message,
            job=job,
            reason=f"Content safety check failed: {content_err}",
            raw_output=raw_text,
            parsed_output=parsed_data,
            latency_ms=latency_ms,
            model_name=model_name,
        )
        job.status = AIJob.Status.DONE
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        return reply_obj

    # Step 5: Save collected answers & update Stage (do this first so data is never lost on handoff)
    raw_collected = parsed_data.get("collected", {})
    valid_collected = filter_collected_answers(raw_collected, config)

    if valid_collected:
        if not isinstance(conversation.qualification_answers, dict):
            conversation.qualification_answers = {}
        conversation.qualification_answers.update(valid_collected)
        conversation.save(update_fields=["qualification_answers"])
        _check_and_update_stage(conversation, config)

    reply_body = (parsed_data.get("reply") or "").strip()

    # Step 6: Check if model determined needs_human
    if parsed_data.get("needs_human"):
        handoff_reason = parsed_data.get("handoff_reason") or "Customer inquiry requires human assistance."
        effective_mode = conversation.get_effective_ai_mode(config)
        resulting_msg: Optional[Message] = None

        # Deliver closing message to customer before handoff (e.g. "a licensed agent is reviewing your details")
        closing_text = reply_body or "Thank you! A member of our team is reviewing your details and will follow up with you shortly."
        if effective_mode == AIAssistantConfig.Mode.AUTO:
            can_send, block_reason = can_send_manual(conversation.contact, conversation)
            if can_send:
                try:
                    resulting_msg = send_message(
                        conversation=conversation,
                        body=closing_text,
                        user=None,
                        sender_type=Message.SenderType.AI,
                    )
                    logger.info("Sent handoff closing message (msg %d) for conv %d: %s", resulting_msg.id, conversation.id, closing_text)
                except Exception as exc:
                    logger.exception("Failed to send handoff closing message: %s", exc)
            else:
                logger.warning("Auto handoff closing message blocked by compliance for conv %d: %s", conversation.id, block_reason)
        elif effective_mode == AIAssistantConfig.Mode.SUGGEST:
            conversation.ai_draft_reply = closing_text
            conversation.save(update_fields=["ai_draft_reply"])

        reply_obj = _trigger_handoff(
            conversation=conversation,
            trigger_message=trigger_message,
            job=job,
            reason=handoff_reason,
            raw_output=raw_text,
            parsed_output=parsed_data,
            latency_ms=latency_ms,
            model_name=model_name,
            sent_message=resulting_msg,
        )
        job.status = AIJob.Status.DONE
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        return reply_obj

    # Step 7: Dispatch by Mode (SUGGEST vs AUTO)
    resulting_msg: Optional[Message] = None
    final_status = AIReply.Status.DRAFT

    effective_mode = conversation.get_effective_ai_mode(config)

    if effective_mode == AIAssistantConfig.Mode.SUGGEST:
        conversation.ai_draft_reply = reply_body
        conversation.save(update_fields=["ai_draft_reply"])
        final_status = AIReply.Status.DRAFT
        logger.info("Stored AI draft for conversation %d: %s", conversation.id, reply_body)
        try:
            from messaging.ws_broadcast import broadcast_chat_event
            broadcast_chat_event(
                conversation.id,
                {
                    "event": "ai_draft",
                    "draft": reply_body,
                },
            )
        except Exception as exc:
            logger.warning("Failed to broadcast AI draft: %s", exc)

    elif effective_mode == AIAssistantConfig.Mode.AUTO:
        # Check compliance policy before auto-sending
        can_send, block_reason = can_send_manual(conversation.contact, conversation)
        if not can_send:
            final_status = AIReply.Status.BLOCKED
            skip_reason = block_reason
            logger.warning("Auto AI reply blocked by compliance for conv %d: %s", conversation.id, block_reason)
        else:
            try:
                resulting_msg = send_message(
                    conversation=conversation,
                    body=reply_body,
                    user=None,
                    sender_type=Message.SenderType.AI,
                )
                final_status = AIReply.Status.SENT
                logger.info("Sent auto AI reply (msg %d) in conv %d", resulting_msg.id, conversation.id)
            except Exception as exc:
                final_status = AIReply.Status.FAILED
                last_error = str(exc)
                logger.exception("Failed to send auto AI message: %s", exc)

    job.status = AIJob.Status.DONE
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at"])

    return AIReply.objects.create(
        conversation=conversation,
        trigger_message=trigger_message,
        job=job,
        status=final_status,
        skip_reason=skip_reason,
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        queue_wait_ms=queue_wait_ms,
        raw_output=raw_text,
        parsed_output=parsed_data,
        error=last_error,
        resulting_message=resulting_msg,
    )
