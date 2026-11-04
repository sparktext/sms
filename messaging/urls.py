from django.urls import path
from . import views
from . import views_webhooks

app_name = "messaging"

urlpatterns = [
    # Dashboard & Inbox
    path("dashboard/", views.dashboard, name="dashboard"),
    path("ai-assistant/", views.ai_assistant_settings, name="ai_assistant_settings"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/new/", views.start_new_chat, name="start_new_chat"),
    path("inbox/<int:conversation_id>/", views.conversation_detail, name="conversation_detail"),
    path("inbox/<int:conversation_id>/send/", views.send_conversation_message, name="send_message"),
    path("inbox/<int:conversation_id>/messages/", views.poll_messages, name="poll_messages"),
    path("inbox/<int:conversation_id>/status/", views.update_conversation_status, name="update_status"),
    path("inbox/<int:conversation_id>/draft-action/", views.handle_ai_draft_action, name="ai_draft_action"),
    path("inbox/<int:conversation_id>/resume-ai/", views.resume_ai_conversation, name="resume_ai"),
    path("inbox/<int:conversation_id>/ai-mode/", views.update_conversation_ai_mode, name="update_ai_mode"),
    path("inbox/<int:conversation_id>/assign/", views.update_conversation_assigned_to, name="update_assigned_to"),
    path("contacts/<int:contact_id>/stage/", views.update_contact_stage, name="update_contact_stage"),

    # Settings & Phone Numbers
    path("settings/phone-numbers/", views.phone_numbers_settings, name="phone_numbers_settings"),
    path("settings/phone-numbers/<int:phone_id>/action/", views.phone_number_action, name="phone_number_action"),

    # Direct contact message shortcut
    path("contacts/<int:contact_id>/message/", views.contact_message_redirect, name="contact_message"),

    # Twilio Webhooks
    path("webhooks/twilio/sms/", views_webhooks.twilio_inbound_sms, name="twilio_inbound_sms"),
    path("webhooks/twilio/status/", views_webhooks.twilio_status_callback, name="twilio_status_callback"),

    # Telnyx Webhooks
    path("webhooks/telnyx/sms/", views_webhooks.telnyx_inbound_sms, name="telnyx_inbound_sms"),
    path("webhooks/telnyx/status/", views_webhooks.telnyx_status_callback, name="telnyx_status_callback"),
]

# DEV ONLY SIMULATOR (can be deleted anytime)
from . import views_dev
urlpatterns += [
    path("inbox/<int:conversation_id>/simulate-reply/", views_dev.simulate_customer_reply, name="simulate_reply"),
]
