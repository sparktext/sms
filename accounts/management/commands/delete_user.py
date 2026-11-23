from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import User, UserProfile, AccountEvent, EmailLog
from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.socialaccount.models import SocialAccount, SocialToken


class Command(BaseCommand):
    help = "Delete a specific user by ID or email."

    def add_arguments(self, parser):
        parser.add_argument("identifier", help="User ID (number) or Email address to delete")
        parser.add_argument(
            "--yes",
            "-y",
            action="store_true",
            help="Skip confirmation prompt",
        )

    def handle(self, *args, **options):
        identifier = options["identifier"].strip()
        auto_confirm = options["yes"]

        # Lookup by ID if numeric, otherwise by email
        user = None
        if identifier.isdigit():
            user = User.objects.filter(id=int(identifier)).first()
        if not user:
            user = User.objects.filter(email__iexact=identifier).first()

        if not user:
            self.stdout.write(self.style.ERROR(f"No user found matching '{identifier}'."))
            return

        self.stdout.write(
            f"Found user: ID={user.id}, Email='{user.email}', Role='{user.role}'"
        )

        if not auto_confirm:
            confirm = input(f"Are you sure you want to permanently delete user '{user.email}' (ID: {user.id})? [y/N]: ")
            if confirm.strip().lower() not in ("y", "yes"):
                self.stdout.write(self.style.WARNING("Deletion cancelled."))
                return

        with transaction.atomic():
            EmailConfirmation.objects.filter(email_address__user=user).delete()
            EmailAddress.objects.filter(user=user).delete()
            SocialToken.objects.filter(account__user=user).delete()
            SocialAccount.objects.filter(user=user).delete()
            UserProfile.objects.filter(user=user).delete()
            AccountEvent.objects.filter(user=user).delete()
            EmailLog.objects.filter(user=user).delete()
            user.delete()

        self.stdout.write(self.style.SUCCESS(f"Successfully deleted user (ID: {identifier})."))
