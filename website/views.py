from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.core.mail import EmailMessage
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def home(request):
    return render(request, 'website/index.html')


def about(request):
    return render(request, 'website/about.html')


def pricing(request):
    return render(request, 'website/pricing.html')


@require_http_methods(["GET", "POST"])
def contact(request):
    if request.method == 'POST':
        name    = request.POST.get('name', '').strip()
        email   = request.POST.get('email', '').strip()
        phone   = request.POST.get('phone', '').strip()
        subject = request.POST.get('subject', '').strip() or 'New Website Inquiry'
        message = request.POST.get('message', '').strip()

        if name and email and message:
            email_subject = f"[SparkText Contact] {subject} (from {name})"
            email_body = (
                f"New contact form submission received:\n\n"
                f"Name: {name}\n"
                f"Email: {email}\n"
                f"Phone: {phone if phone else 'Not provided'}\n"
                f"Subject: {subject}\n\n"
                f"Message:\n{message}\n"
            )
            recipient = getattr(settings, 'CONTACT_EMAIL', 'info@sparktext.si')

            try:
                mail = EmailMessage(
                    subject=email_subject,
                    body=email_body,
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'info@sparktext.si'),
                    to=[recipient],
                    reply_to=[email],
                )
                mail.send(fail_silently=False)
            except Exception as e:
                logger.error("Failed to send contact email: %s", e)

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'ok'})

    return render(request, 'website/contact.html')



def terms(request):
    return render(request, 'website/terms.html')


def privacy(request):
    return render(request, 'website/privacy.html')
