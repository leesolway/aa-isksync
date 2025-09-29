from datetime import date
from decimal import Decimal

from allianceauth.groupmanagement.models import AuthGroup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from eveuniverse.models import EveSolarSystem

User = get_user_model()


class BaseModel(models.Model):
    """Base model with common fields for audit trail"""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OwnershipType(BaseModel):
    """Configurable ownership types for systems"""

    code = models.CharField(max_length=32, unique=True, help_text="Machine code")
    label = models.CharField(max_length=100, help_text="Human readable label")
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["label"]

    def clean(self):
        if self.code:
            normalized = self.code.strip().replace("-", "_").replace(" ", "_").upper()
            self.code = normalized
        if not self.code:
            raise ValidationError({"code": "Code is required"})
        if not self.label:
            raise ValidationError({"label": "Label is required"})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.label


class SystemOwnership(BaseModel):
    system = models.OneToOneField(EveSolarSystem, on_delete=models.CASCADE)
    ownership_type = models.ForeignKey(
        "OwnershipType",
        on_delete=models.PROTECT,
        help_text="Who controls the system",
        related_name="system_ownerships",
    )

    auth_group = models.ForeignKey(
        AuthGroup,
        on_delete=models.CASCADE,
        help_text="Alliance Auth group that contains users with access to this system",
    )

    primary_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="primary_systems",
        help_text="Primary user responsible for the system (must be a member of the auth group)",
    )

    notes = models.TextField(
        blank=True, help_text="Additional notes about system ownership"
    )

    discord_channel = models.CharField(
        max_length=255,
        blank=True,
        help_text="Discord channel name or ID for tax notifications",
    )
    
    ping_groups = models.ManyToManyField(
        Group,
        blank=True,
        related_name="system_ownerships_for_ping",
        verbose_name="ping groups",
        help_text="Groups to be pinged in Discord notifications for this system",
    )

    tax_active = models.BooleanField(
        default=True, help_text="Whether tax collection is active for this system"
    )
    default_tax_amount_isk = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Default monthly rent amount in ISK used for new cycles",
    )

    class Meta:
        indexes = [
            models.Index(fields=["ownership_type"]),
            models.Index(fields=["primary_user"]),
            models.Index(fields=["discord_channel"]),
            models.Index(fields=["auth_group"]),
            models.Index(fields=["tax_active"]),
        ]
        permissions = (
            ("config_system_ownership", "Can configure system ownership"),
            ("config_taxes", "Can configure taxes"),
            ("view_isksync_dashboard", "Can view ISKSYNC dashboard"),
        )

    def clean(self):
        errors = {}
        # Validate that primary_user is a member of the auth_group
        if self.primary_user and self.auth_group:
            # AuthGroup has a OneToOneField to Django's Group, so we access via .group.user_set
            if not self.auth_group.group.user_set.filter(
                pk=self.primary_user.pk
            ).exists():
                errors["primary_user"] = (
                    f"Primary user '{self.primary_user.username}' must be a member of the group '{self.auth_group.group.name}'"
                )
        if not self.ownership_type_id:
            errors["ownership_type"] = "Ownership type is required"
        if errors:
            raise ValidationError(errors)

    @property
    def associated_users(self):
        """Get all users associated with this system via the auth group"""
        if not self.auth_group:
            return User.objects.none()

        # AuthGroup has a OneToOneField to Django's Group, so we access via .group.user_set
        return self.auth_group.group.user_set.all()

    @property
    def user_count(self):
        """Get the number of users associated with this system"""
        return self.associated_users.count()

    def get_discord_channel(self):
        """Get the Discord channel for notifications"""
        return self.discord_channel if self.discord_channel else None

    def has_discord_notifications(self):
        """Check if Discord notifications are configured"""
        return bool(self.discord_channel)

    def get_current_tax_amount(self, as_of_date=None):
        """Return the default tax amount for this system."""
        return self.default_tax_amount_isk or Decimal("0.00")

    def __str__(self):
        return f"{self.system.name} ({self.ownership_type.label})"


class TaxCycle(BaseModel):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("PAID", "Paid"),
        ("OVERDUE", "Overdue"),
        ("WRITTEN_OFF", "Written Off"),
    ]

    system_ownership = models.ForeignKey(
        SystemOwnership, on_delete=models.CASCADE, related_name="tax_cycles"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField()

    # Replace simple boolean with status field
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="PENDING")

    paid_date = models.DateField(null=True, blank=True)
    paid_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actual amount paid (may differ from target amount)",
    )

    # Persist the expected amount on each cycle for simplicity and historical integrity
    target_amount = models.DecimalField(
        max_digits=20, decimal_places=2, help_text="Expected tax for this cycle"
    )

    notes = models.TextField(blank=True, help_text="Notes about this tax cycle")

    # End-user self-reporting (does not affect official status)
    user_marked_paid = models.BooleanField(
        default=False,
        help_text="User toggled 'I have paid' (for managers to review)",
    )
    user_marked_paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user last toggled to paid",
    )

    class Meta:
        unique_together = ("system_ownership", "period_start")
        indexes = [
            models.Index(fields=["system_ownership", "period_start"]),
            models.Index(fields=["due_date", "status"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-period_start"]
        permissions = (("manage_tax_cycles", "Can manage tax cycles and payments"),)

    def clean(self):
        if self.period_start and self.period_end:
            if self.period_start >= self.period_end:
                raise ValidationError("Period start must be before period end")

        if self.due_date and self.period_end:
            if self.due_date < self.period_end:
                raise ValidationError("Due date should be after or equal to period end")

        if self.paid_date and self.status not in ["PAID", "WRITTEN_OFF"]:
            raise ValidationError(
                "Paid date can only be set for paid or written off cycles"
            )

        if self.paid_amount is not None and self.paid_amount < 0:
            raise ValidationError("Paid amount cannot be negative")

        # Validate full payments
        if self.status == "PAID" and self.paid_amount is not None:
            expected = self.expected_amount
            if expected and self.paid_amount < expected:
                raise ValidationError(
                    "Full payment cannot be less than expected amount"
                )

    def save(self, *args, **kwargs):
        # Auto-set paid_date when status changes to PAID
        if self.status == "PAID" and not self.paid_date:
            self.paid_date = date.today()

        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        """Check if the tax cycle is overdue"""
        return self.status == "PENDING" and date.today() > self.due_date

    @property
    def expected_amount(self):
        """Expected tax amount stored on the cycle"""
        return self.target_amount or Decimal("0.00")

    @property
    def remaining_amount(self):
        """Get the remaining amount to be paid"""
        expected = self.expected_amount
        paid = self.paid_amount or Decimal("0.00")
        return max(expected - paid, Decimal("0.00"))

    @property
    def is_fully_paid(self):
        """Check if the cycle is fully paid"""
        return self.status == "PAID"

    @property
    def system(self):
        """Get the system from the system ownership"""
        return self.system_ownership.system if self.system_ownership else None

    @property
    def affected_users(self):
        """Get all users affected by this tax"""
        return self.system_ownership.associated_users

    def mark_paid(self, amount=None, paid_date=None):
        """Mark the cycle as paid"""
        self.status = "PAID"
        self.paid_date = paid_date or date.today()
        # If no amount specified, default to the target amount
        if amount is not None:
            self.paid_amount = amount
        else:
            self.paid_amount = self.target_amount
        self.save()

    def mark_overdue(self):
        """Mark the cycle as overdue"""
        if self.status == "PENDING":
            self.status = "OVERDUE"
            self.save()

    def write_off(self, notes=None):
        """Write off the tax cycle"""
        self.status = "WRITTEN_OFF"
        if notes:
            self.notes = notes
        self.save()

    @property
    def obligation_count(self):
        """Get total number of obligations for this cycle"""
        return self.obligations.count()

    @property
    def fulfilled_obligation_count(self):
        """Get number of fulfilled obligations for this cycle"""
        return self.obligations.filter(status="COMPLETED").count()

    @property
    def has_obligations(self):
        """Check if this cycle has any obligations"""
        return self.obligation_count > 0

    @property
    def all_obligations_fulfilled(self):
        """Check if all obligations are fulfilled"""
        if not self.has_obligations:
            return True  # No obligations means all are "fulfilled"
        return self.fulfilled_obligation_count == self.obligation_count

    @property
    def is_fully_complete(self):
        """Check if both tax is paid AND all obligations are fulfilled"""
        return self.is_fully_paid and self.all_obligations_fulfilled

    @property
    def completion_status(self):
        """Get a comprehensive completion status without partial state"""
        tax_done = self.is_fully_paid
        obligations_done = self.all_obligations_fulfilled

        if tax_done and obligations_done:
            return "complete"  # ✅ Everything done
        else:
            return "outstanding"  # ❌ Anything missing counts as outstanding

    def __str__(self):
        return f"{self.system_ownership.system.name} Tax ({self.period_start.strftime('%b %Y')}) - {self.get_status_display()}"


class ObligationType(BaseModel):
    """Define types of obligations that systems can have"""

    name = models.CharField(
        max_length=100, unique=True, help_text="Name of the obligation type"
    )
    description = models.TextField(
        blank=True, help_text="Description of what this obligation entails"
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this obligation type is currently in use"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SystemObligationType(BaseModel):
    """Link systems to their required obligation types"""

    system_ownership = models.ForeignKey(
        SystemOwnership, on_delete=models.CASCADE, related_name="obligation_types"
    )
    obligation_type = models.ForeignKey(ObligationType, on_delete=models.CASCADE)
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this obligation is currently required for this system",
    )

    class Meta:
        unique_together = ("system_ownership", "obligation_type")
        indexes = [
            models.Index(fields=["system_ownership", "is_active"]),
            models.Index(fields=["obligation_type"]),
        ]

    def __str__(self):
        return f"{self.system_ownership.system.name} - {self.obligation_type.name}"


class TaxCycleObligation(BaseModel):
    """Track obligation fulfillment per tax cycle"""

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]

    tax_cycle = models.ForeignKey(
        TaxCycle, on_delete=models.CASCADE, related_name="obligations"
    )
    obligation_type = models.ForeignKey(ObligationType, on_delete=models.CASCADE)

    # Single source of truth for state
    status = models.CharField(
        max_length=15,
        choices=STATUS_CHOICES,
        default="PENDING",
        help_text="Current state of this obligation",
    )

    fulfilled_date = models.DateTimeField(
        null=True, blank=True, help_text="When the obligation was marked as fulfilled"
    )
    fulfilled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fulfilled_obligations",
        help_text="User who marked this obligation as fulfilled",
    )

    notes = models.TextField(
        blank=True, help_text="Notes about the obligation fulfillment"
    )

    class Meta:
        unique_together = ("tax_cycle", "obligation_type")
        indexes = [
            models.Index(fields=["fulfilled_date"]),
            models.Index(fields=["tax_cycle", "status"]),
            models.Index(fields=["status"]),
        ]

    @property
    def is_overdue(self):
        """Check if the obligation is overdue (uses tax cycle due date)"""
        if self.status == "COMPLETED":
            return False
        return date.today() > self.tax_cycle.due_date

    def mark_fulfilled(self, user, notes=None):
        """Mark the obligation as fulfilled"""
        from django.utils import timezone

        self.status = "COMPLETED"
        self.fulfilled_date = timezone.now()
        self.fulfilled_by = user
        if notes:
            self.notes = notes
        self.save()

    def mark_unfulfilled(self):
        """Reset the obligation to pending"""
        self.status = "PENDING"
        self.fulfilled_date = None
        self.fulfilled_by = None
        self.save()

    def mark_failed(self, user=None, notes=None):
        """Mark the obligation as failed (not completed)."""
        self.status = "FAILED"
        self.fulfilled_date = None
        self.fulfilled_by = user if user and getattr(user, "pk", None) else None
        if notes:
            self.notes = notes
        self.save()

    def __str__(self):
        return f"{self.tax_cycle.system_ownership.system.name} - {self.obligation_type.name} ({self.status})"


class AuditLog(BaseModel):
    """Generic audit log for user/admin actions within isksync.
    Use GenericForeignKey to point at any target object (e.g., TaxCycle, TaxCycleObligation).
    """

    action = models.CharField(max_length=50)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="isksync_audit_logs",
    )

    target_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_object_id = models.PositiveIntegerField()
    target = GenericForeignKey("target_content_type", "target_object_id")

    target_repr = models.CharField(
        max_length=255,
        blank=True,
        help_text="Readable target summary at time of action",
    )
    details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional structured metadata for the action",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action"]),
            models.Index(fields=["target_content_type", "target_object_id"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        who = getattr(self.user, "username", "system") if self.user_id else "system"
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {who} -> {self.action} on {self.target_repr or self.target}"


class DiscordNotificationConfig(BaseModel):
    """Global Discord notification configuration"""

    SEVERITY_CHOICES = [
        ("LOW", "Low - Info only"),
        ("MEDIUM", "Medium - Warning"),
        ("HIGH", "High - Critical"),
    ]

    name = models.CharField(
        max_length=100,
        unique=True,
        default="Default",
        help_text="Configuration name (usually 'Default' for single config)",
    )

    webhook_base_url = models.URLField(
        help_text="Base Discord webhook URL (without channel-specific parts)"
    )

    webhook_url_template = models.CharField(
        max_length=500,
        default="{base_url}",
        help_text="Template for webhook URL. Use {base_url} and {channel} placeholders",
    )

    advance_notice_days = models.PositiveIntegerField(
        default=7, help_text="Days before due date to send advance notice"
    )

    role_mention_template = models.CharField(
        max_length=255,
        blank=True,
        help_text="Template for role mention (e.g., '@{channel}' -> '@farm-l')",
        default="@{channel}",
    )

    advance_severity = models.CharField(
        max_length=10,
        choices=SEVERITY_CHOICES,
        default="MEDIUM",
        help_text="Severity for advance notice notifications",
    )

    due_severity = models.CharField(
        max_length=10,
        choices=SEVERITY_CHOICES,
        default="HIGH",
        help_text="Severity for due date notifications",
    )

    overdue_severity = models.CharField(
        max_length=10,
        choices=SEVERITY_CHOICES,
        default="HIGH",
        help_text="Severity for overdue notifications",
    )

    send_advance_notice = models.BooleanField(
        default=True, help_text="Send advance notice X days before due date"
    )

    send_due_notice = models.BooleanField(
        default=True, help_text="Send notification on due date"
    )

    send_overdue_notice = models.BooleanField(
        default=True, help_text="Send daily notifications when overdue"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether Discord notifications are enabled for this system",
    )

    class Meta:
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["name"]),
        ]

    def get_webhook_url_for_channel(self, discord_channel: str) -> str:
        """Generate webhook URL for a specific discord channel"""
        if not discord_channel:
            return self.webhook_base_url

        return self.webhook_url_template.format(
            base_url=self.webhook_base_url, channel=discord_channel
        )

    def get_role_mention(self, discord_channel: str) -> str:
        """Generate role mention based on discord_channel and template"""
        if not discord_channel:
            return ""

        return self.role_mention_template.format(channel=discord_channel)

    def get_color_for_severity(self, severity: str) -> int:
        """Get Discord embed color based on severity"""
        colors = {
            "LOW": 0x3498DB,  # Blue
            "MEDIUM": 0xF39C12,  # Orange
            "HIGH": 0xE74C3C,  # Red
        }
        return colors.get(severity, colors["MEDIUM"])

    def __str__(self):
        return f"Discord Config: {self.name}"


class DiscordNotificationLog(BaseModel):
    """Track Discord notifications sent for audit and monitoring purposes"""

    NOTIFICATION_TYPES = [
        ("BATCHED_ADVANCE", "Batched Advance Notice"),
        ("BATCHED_DUE", "Batched Due Date"),
        ("BATCHED_OVERDUE", "Batched Overdue"),
        ("TEST_ADVANCE", "Test Advance Notice"),
        ("TEST_DUE", "Test Due Date"),
        ("TEST_OVERDUE", "Test Overdue"),
        ("ADMIN_ADVANCE", "Admin Advance Notice"),
        ("ADMIN_DUE", "Admin Due Date"),
        ("ADMIN_OVERDUE", "Admin Overdue"),
    ]

    tax_cycle = models.ForeignKey(
        TaxCycle, on_delete=models.CASCADE, related_name="discord_notifications"
    )

    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)

    sent_date = models.DateField(
        default=date.today, help_text="Date this notification was sent"
    )

    webhook_url = models.URLField(help_text="Webhook URL used for this notification")

    success = models.BooleanField(
        default=True, help_text="Whether the notification was sent successfully"
    )

    response_status = models.IntegerField(
        null=True, blank=True, help_text="HTTP response status from Discord"
    )

    error_message = models.TextField(
        blank=True, help_text="Error message if notification failed"
    )

    class Meta:
        indexes = [
            models.Index(fields=["tax_cycle", "notification_type"]),
            models.Index(fields=["sent_date"]),
            models.Index(fields=["success"]),
        ]
        ordering = ["-sent_date"]

    def __str__(self):
        status = "✓" if self.success else "✗"
        return f"{status} {self.get_notification_type_display()} for {self.tax_cycle.system_ownership.system.name} on {self.sent_date}"
