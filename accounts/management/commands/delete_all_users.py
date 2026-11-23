from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import AccountEvent, EmailLog, User, UserProfile


class Command(BaseCommand):
    help = "Delete all users and their related auth data from the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip confirmation prompt",
        )

    def handle(self, *args, **options):
        count_before = User.objects.count()
        if count_before == 0:
            self.stdout.write(self.style.WARNING("No users found in the database."))
            return

        self.stdout.write(f"Found {count_before} user(s) in the database.")

        with transaction.atomic():
            EmailConfirmation.objects.all().delete()
            EmailAddress.objects.all().delete()
            SocialToken.objects.all().delete()
            SocialAccount.objects.all().delete()
            UserProfile.objects.all().delete()
            AccountEvent.objects.all().delete()
            EmailLog.objects.all().delete()
            deleted_users, _ = User.objects.all().delete()

        remaining = User.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully deleted {deleted_users} user(s) and associated auth records. Remaining users: {remaining}."
            )
        )
