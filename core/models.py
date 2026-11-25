import secrets
import uuid
from datetime import time, timedelta

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


def generate_api_key():
    return f"spk_live_{secrets.token_hex(16)}"


def generate_invite_token():
    return secrets.token_urlsafe(32)


def default_call_days():
    return [0, 1, 2, 3, 4]          # Monday-Friday (Python weekday numbers)


class Organization(models.Model):
    """One customer company. Everything else (contacts, messages...) points to this."""

    class RegistrationStatus(models.TextChoices):
        UNREGISTERED = "unregistered", "Unregistered"
        IN_REVIEW = "in_review", "In Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class LeadRoutingStrategy(models.TextChoices):
        DISABLED = "disabled", "Manual Team Queue (Off)"
        ROUND_ROBIN = "round_robin", "Round-Robin Rotation"
        WORKLOAD_BALANCED = "workload_balanced", "Workload-Balanced (Least Busy Rep)"

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    timezone = models.CharField(max_length=64, default="America/Los_Angeles")
    # Days the team takes calls (0 = Monday ... 6 = Sunday). Gemma only books calls on these days.
    call_days = models.JSONField(default=default_call_days, blank=True)
    # Hours the team calls leads, in the LEAD's local time (kept inside the legal 8 AM-9 PM window)
    call_start = models.TimeField(default=time(8, 0))
    call_end = models.TimeField(default=time(21, 0))
    ein =models.CharField(max_length=30, blank=True, default="")
    registration_status = models.CharField(
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.UNREGISTERED,
    )
    brand_sid = models.CharField(max_length=64, blank=True, default="")
    campaign_sid = models.CharField(max_length=64, blank=True, default="")
    verification_notes = models.TextField(blank=True, default="")
    api_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="API key for ingesting leads via webhook or REST API",
    )
    # Profile & Address Details
    address_street = models.CharField(max_length=255, blank=True, default="")
    address_city = models.CharField(max_length=100, blank=True, default="")
    address_state = models.CharField(max_length=50, blank=True, default="")
    address_postal_code = models.CharField(max_length=20, blank=True, default="")
    address_country = models.CharField(max_length=50, blank=True, default="USA")
    website = models.URLField(max_length=255, blank=True, default="")
    notification_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=30, blank=True, default="")
    # Lead Distribution & Routing Configuration
    lead_routing_strategy = models.CharField(
        max_length=30,
        choices=LeadRoutingStrategy.choices,
        default=LeadRoutingStrategy.WORKLOAD_BALANCED,
    )
    max_leads_per_agent = models.PositiveIntegerField(
        default=5,
        help_text="Maximum simultaneous active conversations per agent before they are bypassed (0 = unlimited)",
    )
    route_only_available = models.BooleanField(
        default=True,
        help_text="Only route to team members currently marked Available for leads",
    )
    last_assigned_user = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Last user assigned a lead, used for round-robin rotation",
    )
    templates_seeded = models.BooleanField(
        default=False,
        help_text="Whether default starter SMS templates and categories have been initialized for this workspace",
    )
    # Notification Chime & Sound Preferences
    class NotificationSound(models.TextChoices):
        SALES_FLOURISH = "sales_flourish", "Sales Flourish (Ascending Major Chime)"
        CASH_REGISTER = "cash_register", "Cash Register (Cha-Ching)"
        CRYSTAL_BELL = "crystal_bell", "Crystal Bell Ding"
        BRIGHT_POP = "bright_pop", "Crisp Pop & Marimba"
        CHIME_TRIO = "chime_trio", "Elevate (Harmonic Trio)"
        CLASSIC_RING = "classic_ring", "Classic Digital Ring"
        CUSTOM = "custom", "Custom Audio File"

    notification_sound = models.CharField(
        max_length=50,
        choices=NotificationSound.choices,
        default=NotificationSound.SALES_FLOURISH,
        help_text="Notification chime played when a lead needs human attention or is assigned",
    )
    custom_notification_sound = models.FileField(
        upload_to="notification_sounds/",
        blank=True,
        null=True,
        help_text="Custom audio file (MP3, WAV, OGG, M4A) for notification chime",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = generate_api_key()
        super().save(*args, **kwargs)

    def regenerate_api_key(self):
        self.api_key = generate_api_key()
        self.save(update_fields=["api_key"])
        return self.api_key

    def __str__(self):
        return f"{self.name} ({self.slug})"


class UserManager(BaseUserManager):
    """Creates accounts that log in with an email instead of a username."""
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email.strip().lower())
        user = self.model(email=email, **extra)
        user.set_password(password)  # hashes the password, once
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", "owner")
        extra.setdefault("is_staff", True) 
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    """A login account. Belongs to one Organization."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        AGENT = "agent", "Agent"

    username = None
    email = models.EmailField(unique=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE,
        null=True, blank=True, related_name="users",
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(max_length=20, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    is_available_for_leads = models.BooleanField(
        default=True,
        help_text="Rep is on-duty and accepting lead assignments",
    )
    last_lead_assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of when the user was last assigned a lead, used for fair tiebreaking",
    )

    # --- Auth enhancement fields (migration 0006) ---
    public_id = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False, db_index=True,
        help_text="Public-facing opaque identifier for URLs",
    )
    status = models.CharField(
        max_length=20,
        choices=[('active', 'Active'), ('suspended', 'Suspended'), ('deleted', 'Deleted')],
        default='active',
        db_index=True,
    )
    signup_method = models.CharField(
        max_length=10,
        choices=[('email', 'Email'), ('google', 'Google')],
        default='email',
    )
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    password_set_at = models.DateTimeField(null=True, blank=True)
    welcome_email_sent_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # Consent
    terms_version = models.CharField(max_length=20, blank=True, default='1.0')
    marketing_opt_in = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email

    @property
    def is_admin_or_owner(self) -> bool:
        return self.role in [self.Role.OWNER, self.Role.ADMIN]


class OrganizationInvite(models.Model):
    """
    Pending invitation to join an Organization.
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=10,
        choices=User.Role.choices,
        default=User.Role.AGENT,
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_invite_token,
        db_index=True,
    )
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invites",
    )
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    @property
    def is_valid(self) -> bool:
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()

    def __str__(self):
        return f"Invite for {self.email} to {self.organization.name} ({self.role})"



class UserProfile(models.Model):
    """Extended profile information for a User. Created automatically on user creation."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    company = models.CharField(max_length=200, blank=True)
    avatar_url = models.URLField(blank=True)
    # Personal time zone for the calendar; blank = follow the workspace time zone (Organization.timezone)
    timezone = models.CharField(max_length=64, blank=True, default='')
    language = models.CharField(max_length=10, default='en')
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Profile({self.user.email})"


class AccountEvent(models.Model):
    """Immutable audit log for all significant account lifecycle events."""

    class EventType(models.TextChoices):
        ACCOUNT_CREATED = 'account_created', 'Account Created'
        EMAIL_VERIFICATION_SENT = 'email_verification_sent', 'Email Verification Sent'
        EMAIL_VERIFIED = 'email_verified', 'Email Verified'
        PASSWORD_SET = 'password_set', 'Password Set'
        PASSWORD_CHANGED = 'password_changed', 'Password Changed'
        PASSWORD_RESET_REQUESTED = 'password_reset_requested', 'Password Reset Requested'
        PASSWORD_RESET = 'password_reset', 'Password Reset'
        LOGIN = 'login', 'Login'
        LOGIN_FAILED = 'login_failed', 'Login Failed'
        LOGOUT = 'logout', 'Logout'
        SOCIAL_LINKED = 'social_linked', 'Social Account Linked'
        WELCOME_EMAIL_SENT = 'welcome_email_sent', 'Welcome Email Sent'
        PROFILE_UPDATED = 'profile_updated', 'Profile Updated'
        ONBOARDING_COMPLETED = 'onboarding_completed', 'Onboarding Completed'
        ACCOUNT_SUSPENDED = 'account_suspended', 'Account Suspended'
        ACCOUNT_DELETED = 'account_deleted', 'Account Deleted'
        ACCOUNT_RECOVERED = 'account_recovered', 'Account Recovered'

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='events',
    )
    # Store public_id separately so we can query events even after user deletion
    user_public_id = models.UUIDField(null=True, blank=True, db_index=True)
    event_type = models.CharField(max_length=50, choices=EventType.choices, db_index=True)
    metadata = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.event_type} @ {self.created_at}"


class EmailLog(models.Model):
    """Tracks every transactional email sent through the system."""

    class State(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        DELIVERED = 'delivered', 'Delivered'
        BOUNCED = 'bounced', 'Bounced'

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='email_logs',
    )
    email_type = models.CharField(max_length=50, db_index=True)
    to_address = models.EmailField()
    state = models.CharField(max_length=15, choices=State.choices, default=State.QUEUED)
    provider_message_id = models.CharField(max_length=200, blank=True)
    error = models.TextField(blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-queued_at']

    def __str__(self) -> str:
        return f"{self.email_type} → {self.to_address} [{self.state}]"
