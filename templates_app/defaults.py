from typing import List, Dict, Any


DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "Auto Insurance Annual Review",
        "category": "marketing",
        "body": "Hi {{ first_name }}, this is {{ business_name }}. Drivers in your area are saving an average of $450/year by reviewing their auto coverage. Reply YES to compare your rates today! Reply STOP to opt out.",
    },
    {
        "name": "New Quote Request Follow-up",
        "category": "follow_up",
        "body": "Hi {{ first_name }}! Thanks for requesting a quote from {{ business_name }}. I am preparing your customized rate options right now. What time today works best for a quick 5-min review? Reply STOP to opt out.",
    },
    {
        "name": "Office Hours & Location",
        "category": "quick_reply",
        "body": "Our office is located at 475 El Camino Real, Suite 301, Millbrae, CA 94030. We are open Mon - Fri, 9:00 AM - 6:00 PM PST. Feel free to call us anytime!",
    },
    {
        "name": "Consultation Appointment Reminder",
        "category": "transactional",
        "body": "Appointment Reminder: Hi {{ first_name }}, your insurance consultation with {{ business_name }} is set for tomorrow. Reply C to confirm or RESCHEDULE if you need to pick a new time. Reply STOP to opt out.",
    },
    {
        "name": "Home & Auto Bundle Promotion",
        "category": "marketing",
        "body": "Exclusive VIP Deal: Hi {{ first_name }}, bundle your home and auto policies with {{ business_name }} this month to unlock up to 25% in multi-policy savings! Reply REVIEW to see your numbers. Reply STOP to opt out.",
    },
    {
        "name": "Quote In Progress Update",
        "category": "quick_reply",
        "body": "Thank you for your patience! A licensed agent from our team is reviewing your details to prepare your customized quotes and will message you shortly.",
    },
    {
        "name": "Quick Phone Call Invitation",
        "category": "quick_reply",
        "body": "Would you prefer a quick 5-minute phone call to go over the policy options and rates together? Let us know what time works best for you!",
    },
]


def ensure_default_templates(organization):
    """
    Ensures the organization has the pre-loaded starter templates.
    """
    from .models import MessageTemplate
    if not MessageTemplate.objects.filter(organization=organization).exists():
        created_list = []
        for item in DEFAULT_TEMPLATES:
            created_list.append(
                MessageTemplate(
                    organization=organization,
                    name=item["name"],
                    category=item["category"],
                    body=item["body"],
                    is_default=True,
                )
            )
        MessageTemplate.objects.bulk_create(created_list)
