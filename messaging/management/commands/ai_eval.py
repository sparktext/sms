import os
import json
import time
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings

from core.models import Organization
from contacts.models import Contact
from messaging.models import AIAssistantConfig, Conversation, PhoneNumber
from messaging.ai.openai_client import OpenAICompatibleClient
from messaging.ai.prompt import build_system_prompt, AI_RESPONSE_SCHEMA
from messaging.ai.validator import parse_ai_output, validate_reply_content, filter_collected_answers


class Command(BaseCommand):
    help = "Runs 20 sample customer evaluation cases against the AI model and prints quality, intent, handoff, and token statistics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default=str(Path(settings.BASE_DIR) / "messaging" / "ai" / "eval_cases.json"),
            help="Path to evaluation cases JSON file.",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="Override AI model name (defaults to settings.AI_MODEL).",
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help="Override AI base URL (defaults to settings.AI_BASE_URL).",
        )

    def handle(self, *args, **options):
        file_path = options["file"]
        model_override = options["model"]
        base_url_override = options["base_url"]

        if not os.path.exists(file_path):
            self.stderr.write(self.style.ERROR(f"Eval file not found: {file_path}"))
            return

        with open(file_path, "r", encoding="utf-8") as f:
            cases = json.load(f)

        self.stdout.write(self.style.NOTICE(f"\n========================================================"))
        self.stdout.write(self.style.NOTICE(f" SparkText AI Assistant Evaluation ({len(cases)} cases)"))
        self.stdout.write(self.style.NOTICE(f"========================================================\n"))

        client = OpenAICompatibleClient(
            base_url=base_url_override,
            model=model_override,
        )
        self.stdout.write(f"Connecting to: {client.base_url} (Model: {client.model})\n")

        # Mock dummy objects for prompt construction
        mock_org = Organization(name="SparkText Demo Services")
        mock_config = AIAssistantConfig(
            organization=mock_org,
            business_name="SparkText Demo Services",
            business_description="We provide emergency plumbing, HVAC, and electrical services across Dallas, TX.",
            knowledge=(
                "Hours: Monday to Friday 8:00 AM - 6:00 PM. Emergency on-call dispatch available 24/7.\n"
                "Services: Residential & commercial plumbing, AC repair, furnace maintenance, drain cleaning.\n"
                "Locations: Dallas, Plano, Frisco, and Arlington, Texas.\n"
                "Phone: +1 (213) 799-7090\n"
                "Website: https://sparktext.si\n"
                "FAQ: On-site evaluations are scheduled with licensed technicians. Exact quotes are given in person after inspection."
            ),
            tone="friendly, helpful, and concise",
            never_say="guaranteed approval\n100% free\ncall right now",
            handoff_rules="Hand off if the customer reports an active gas leak or asks for legal advice.",
            max_reply_chars=320,
            qualification_questions=[
                {"key": "service_type", "question": "What type of service or repair do you need?", "required": True},
                {"key": "location", "question": "What city or neighborhood are you located in?", "required": True},
                {"key": "timeline", "question": "When would you like our technician to visit?", "required": False},
            ],
        )

        dummy_conv = Conversation(
            id=9999,
            qualification_answers={},
        )

        system_prompt = build_system_prompt(mock_config, dummy_conv)

        results = []
        handoff_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_latency_ms = 0

        for c in cases:
            cid = c.get("id")
            category = c.get("category", "")
            user_msg = c.get("message", "")

            self.stdout.write(self.style.WARNING(f"\n[{cid}/20] Case: {category}"))
            self.stdout.write(f"Customer: \"{user_msg}\"")

            chat_messages = [
                {"role": "user", "content": f"<customer_message>\n{user_msg}\n</customer_message>"}
            ]

            t0 = time.perf_counter()
            try:
                resp = client.generate(
                    system_prompt=system_prompt,
                    messages=chat_messages,
                    schema=AI_RESPONSE_SCHEMA,
                )
                lat_ms = resp.latency_ms or int((time.perf_counter() - t0) * 1000)
                total_latency_ms += lat_ms
                total_input_tokens += resp.input_tokens
                total_output_tokens += resp.output_tokens

                parsed, err = parse_ai_output(resp.text)
                if err or not parsed:
                    self.stdout.write(self.style.ERROR(f"  FAILED to parse JSON: {err}"))
                    self.stdout.write(f"  Raw: {resp.text[:120]}")
                    results.append({"id": cid, "status": "error", "error": err})
                    continue

                is_valid, content_err = validate_reply_content(parsed, mock_config)
                collected = filter_collected_answers(parsed.get("collected", {}), mock_config)

                needs_human = parsed.get("needs_human", False)
                if not is_valid:
                    needs_human = True
                    handoff_reason = f"Safety block: {content_err}"
                else:
                    handoff_reason = parsed.get("handoff_reason")

                if needs_human:
                    handoff_count += 1

                self.stdout.write(self.style.SUCCESS(f"  AI Reply: \"{parsed.get('reply')}\""))
                self.stdout.write(f"  Intent: {parsed.get('intent')} | Needs Human: {needs_human} ({handoff_reason})")
                if collected:
                    self.stdout.write(f"  Collected: {collected}")
                self.stdout.write(f"  Tokens: {resp.input_tokens} in / {resp.output_tokens} out | Latency: {lat_ms}ms")

                results.append({
                    "id": cid,
                    "category": category,
                    "reply": parsed.get("reply"),
                    "intent": parsed.get("intent"),
                    "needs_human": needs_human,
                    "handoff_reason": handoff_reason,
                    "tokens_in": resp.input_tokens,
                    "tokens_out": resp.output_tokens,
                    "latency_ms": lat_ms,
                })

            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  Exception: {exc}"))
                results.append({"id": cid, "status": "exception", "error": str(exc)})

        # Summary
        self.stdout.write(self.style.NOTICE(f"\n========================================================"))
        self.stdout.write(self.style.NOTICE(f" EVALUATION SUMMARY"))
        self.stdout.write(self.style.NOTICE(f"========================================================"))
        self.stdout.write(f"Total Cases Evaluated: {len(cases)}")
        self.stdout.write(f"Human Handoffs Triggered: {handoff_count} / {len(cases)} ({int(handoff_count/len(cases)*100)}%)")
        if len(cases) > 0:
            avg_lat = total_latency_ms // len(cases)
            avg_tok_in = total_input_tokens // len(cases)
            avg_tok_out = total_output_tokens // len(cases)
            self.stdout.write(f"Average Latency: {avg_lat} ms")
            self.stdout.write(f"Average Tokens: {avg_tok_in} input, {avg_tok_out} output")
        self.stdout.write(self.style.SUCCESS("\nEvaluation completed successfully.\n"))
