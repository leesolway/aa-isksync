from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.html import format_html
from django import forms

from .audit import log_action
from .models import (
    AuditLog,
    DiscordNotificationConfig,
    DiscordNotificationLog,
    ObligationType,
    OwnershipType,
    SystemObligationType,
    SystemOwnership,
    TaxCycle,
    TaxCycleObligation,
)

User = get_user_model()


class UserModelChoiceField(forms.ModelChoiceField):
    """Custom ModelChoiceField that displays main character names in dropdowns"""
    
    def label_from_instance(self, obj):
        """Return the representation of the choice field label"""
        try:
            if hasattr(obj, 'profile') and obj.profile.main_character:
                main_char = obj.profile.main_character
                return f"{main_char.character_name} ({obj.username})"
            else:
                return f"{obj.username} (no main character)"
        except Exception:
            return obj.username


class SystemOwnershipAdminForm(forms.ModelForm):
    """Custom form for SystemOwnership admin to show character names in primary_user dropdown"""
    
    primary_user = UserModelChoiceField(
        queryset=User.objects.all(),
        required=False,
        help_text="The primary user responsible for this system. Must be a member of the auth group."
    )
    
    class Meta:
        model = SystemOwnership
        fields = '__all__'
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Order auth_group dropdown alphabetically by group name
        if 'auth_group' in self.fields:
            from allianceauth.groupmanagement.models import AuthGroup
            self.fields['auth_group'].queryset = AuthGroup.objects.select_related('group').order_by('group__name')
        
        # Set default ordering for primary_user dropdown (alphabetically by character name, then username)
        # Order by character name first, then username for users without main characters
        self.fields['primary_user'].queryset = User.objects.select_related(
            'profile', 'profile__main_character'
        ).order_by(
            'profile__main_character__character_name',
            'username'
        )
        
        # If we have an existing instance with an auth_group, limit primary_user choices
        if self.instance and self.instance.pk and hasattr(self.instance, 'auth_group'):
            try:
                if self.instance.auth_group:
                    group_users = self.instance.auth_group.group.user_set.all()
                    if group_users.exists():
                        self.fields['primary_user'].queryset = group_users.select_related(
                            'profile', 'profile__main_character'
                        ).order_by(
                            'profile__main_character__character_name',
                            'username'
                        )
            except SystemOwnership.auth_group.RelatedObjectDoesNotExist:
                # auth_group not set yet, use default queryset
                pass


class TaxCycleInline(admin.TabularInline):
    model = TaxCycle
    extra = 0
    fields = (
        "period_start",
        "period_end",
        "due_date",
        "status",
        "target_amount",
        "paid_amount",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "expected_amount",
        "remaining_amount",
    )
    ordering = ["-period_start"]
    max_num = 10

    def expected_amount(self, obj):
        return f"{obj.expected_amount:,.2f} ISK"

    expected_amount.short_description = "Expected Amount"

    def remaining_amount(self, obj):
        return f"{obj.remaining_amount:,.2f} ISK"

    remaining_amount.short_description = "Remaining"


class SystemObligationTypeInline(admin.TabularInline):
    model = SystemObligationType
    extra = 0
    fields = ("obligation_type", "is_active")
    ordering = ["obligation_type__name"]
    autocomplete_fields = ("obligation_type",)




class TaxCycleObligationInline(admin.TabularInline):
    model = TaxCycleObligation
    extra = 0
    fields = ("obligation_type", "status", "fulfilled_date", "fulfilled_by")
    readonly_fields = ("fulfilled_date", "fulfilled_by")
    ordering = ["obligation_type__name"]
    autocomplete_fields = ("obligation_type",)


@admin.register(SystemOwnership)
class SystemOwnershipAdmin(admin.ModelAdmin):
    form = SystemOwnershipAdminForm
    autocomplete_fields = ("system",)
    search_fields = ("system__name",)  # Enable autocomplete for this model

    list_display = (
        "system",
        "ownership_type",
        "auth_group",
        "primary_user_character",
        "tax_active",
        "current_tax_amount",
        "discord_channel",
        "user_count_display",
        "created_at",
    )
    list_filter = ("ownership_type", "tax_active", "created_at", "auth_group")
    search_fields = (
        "system__name",
        "primary_user__username",
        "primary_user__profile__main_character__character_name",
        "discord_channel",
        "auth_group__name",
    )

    list_per_page = 50
    list_max_show_all = 200
    list_select_related = (
        "system", 
        "primary_user", 
        "primary_user__profile", 
        "primary_user__profile__main_character",
        "auth_group"
    )

    fieldsets = (
        ("System Information", {"fields": ("system", "ownership_type")}),
        (
            "Access Control",
            {
                "fields": ("auth_group", "primary_user"),
                "description": "The auth group contains all users with access to this system. Primary user must be a member of this group.",
            },
        ),
        ("Tax Configuration", {"fields": ("tax_active", "default_tax_amount_isk")}),
        ("Discord Integration", {"fields": ("discord_channel", "ping_groups")}),
        ("Additional Information", {"fields": ("notes",)}),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    readonly_fields = ("created_at", "updated_at")
    filter_horizontal = ("ping_groups",)
    inlines = [TaxCycleInline, SystemObligationTypeInline]

    def user_count_display(self, obj):
        """Display the number of users in the auth group"""
        count = obj.user_count
        return format_html(
            '<span style="color: {};">{} users</span>',
            "#28a745" if count > 0 else "#dc3545",
            count,
        )

    user_count_display.short_description = "Group Members"
    
    def primary_user_character(self, obj):
        """Display the main character name instead of username"""
        if not obj.primary_user:
            return "-"
        
        # Get the main character from the user profile
        try:
            if hasattr(obj.primary_user, 'profile') and obj.primary_user.profile.main_character:
                main_char = obj.primary_user.profile.main_character
                return format_html(
                    '<span title="{}">{}</span>',
                    obj.primary_user.username,  # Show username on hover
                    main_char.character_name
                )
            else:
                # Fallback to username if no main character
                return format_html(
                    '<span style="color: #dc3545;" title="No main character set">{}</span>',
                    obj.primary_user.username
                )
        except Exception:
            # Fallback to username if there's any error
            return obj.primary_user.username
    
    primary_user_character.short_description = "Primary User"
    primary_user_character.admin_order_field = "primary_user__profile__main_character__character_name"

    def current_tax_amount(self, obj):
        """Display the current default tax amount"""
        if not obj.tax_active:
            return format_html('<span style="color: #6c757d;">Inactive</span>')

        amount = obj.get_current_tax_amount()
        if amount > 0:
            return f"{amount:,.2f} ISK"
        return format_html('<span style="color: #dc3545;">No rate set</span>')

    current_tax_amount.short_description = "Current Tax Rate"

    def tax_cycles_count(self, obj):
        """Display link to tax cycles"""
        count = obj.tax_cycles.count()
        if count == 0:
            return "0 cycles"

        app_name = obj._meta.app_label
        return format_html(
            '<a href="{}?system_ownership__id__exact={}">{} cycles</a>',
            reverse(f"admin:{app_name}_taxcycle_changelist"),
            obj.pk,
            count,
        )

    tax_cycles_count.short_description = "Tax Cycles"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "system", 
                "primary_user", 
                "primary_user__profile", 
                "primary_user__profile__main_character",
                "auth_group"
            )
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Primary user field is now handled by the custom form
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(OwnershipType)
class OwnershipTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "label", "created_at")
    search_fields = ("code", "label")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TaxCycle)
class TaxCycleAdmin(admin.ModelAdmin):
    list_display = (
        "system_ownership",
        "period_start",
        "status",
        "expected_amount_formatted",
        "paid_amount_formatted",
        "due_date",
        "overdue_status",
        "obligation_status",
    )
    list_filter = (
        "status",
        "due_date",
        "period_start",
        ("system_ownership__system", admin.RelatedOnlyFieldListFilter),
        ("system_ownership__ownership_type", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ("system_ownership__system__name", "notes")
    date_hierarchy = "period_start"

    raw_id_fields = ("system_ownership",)

    list_per_page = 100
    list_max_show_all = 500

    list_select_related = ("system_ownership", "system_ownership__system")

    fieldsets = (
        (
            "Cycle Information",
            {
                "fields": (
                    "system_ownership",
                    "period_start",
                    "period_end",
                    "due_date",
                    "target_amount",
                )
            },
        ),
        ("Payment Information", {"fields": ("status", "paid_amount", "paid_date")}),
        ("Additional Information", {"fields": ("notes",)}),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    readonly_fields = ("created_at", "updated_at")
    inlines = [TaxCycleObligationInline]

    def expected_amount_formatted(self, obj):
        return f"{obj.expected_amount:,.2f} ISK"

    expected_amount_formatted.short_description = "Expected Amount"

    def paid_amount_formatted(self, obj):
        if obj.paid_amount:
            return f"{obj.paid_amount:,.2f} ISK"
        return "-"

    paid_amount_formatted.short_description = "Paid Amount"
    paid_amount_formatted.admin_order_field = "paid_amount"

    def overdue_status(self, obj):
        if obj.is_overdue:
            return format_html(
                '<span style="color: #dc3545; font-weight: bold;">OVERDUE</span>'
            )
        return format_html('<span style="color: #28a745;">On Time</span>')

    overdue_status.short_description = "Payment Status"

    def obligation_status(self, obj):
        if obj.all_obligations_fulfilled:
            return format_html('<span style="color: #28a745;">✓ Complete</span>')
        else:
            return format_html('<span style="color: #dc3545;">⚠ Pending</span>')

    obligation_status.short_description = "Obligations"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("system_ownership", "system_ownership__system")
        )

    actions = [
        "mark_as_paid",
        "mark_as_overdue",
        "write_off_selected",
        "send_discord_reminder_advance",
        "send_discord_reminder_due",
        "send_discord_reminder_overdue",
        "send_test_discord_notifications",
    ]

    def _send_discord_reminder_type(self, request, queryset, chosen_type: str):
        from isksync.discord_notifications import send_batched_discord_notification, log_notification
        from isksync.models import DiscordNotificationConfig
        from datetime import date
        
        config = DiscordNotificationConfig.objects.filter(is_active=True).first()
        if not config:
            self.message_user(request, "No active Discord configuration found. Please create one first.", level=messages.ERROR)
            return
        
        cycles_by_type = {"ADVANCE": [], "DUE": [], "OVERDUE": []}
        all_ping_groups = set()
        today = date.today()
        
        for cycle in queryset:
            cycles_by_type[chosen_type].append(cycle)
            if hasattr(cycle.system_ownership, 'ping_groups'):
                all_ping_groups.update(cycle.system_ownership.ping_groups.all())
        
        cycles_by_type = {k: v for k, v in cycles_by_type.items() if v}
        if not cycles_by_type:
            self.message_user(request, "No cycles to notify.", level=messages.WARNING)
            return
        
        webhook_url = config.webhook_base_url
        success, status_code, error_message = send_batched_discord_notification(
            webhook_url=webhook_url,
            cycles_by_type=cycles_by_type,
            config=config,
            all_ping_groups=all_ping_groups,
        )
        
        all_cycles = []
        for cycles in cycles_by_type.values():
            all_cycles.extend(cycles)
        
        for cycle in all_cycles:
            cycle_notification_type = f"ADMIN_{chosen_type}"
            log_notification(
                tax_cycle=cycle,
                notification_type=cycle_notification_type,
                webhook_url=webhook_url,
                success=success,
                status_code=status_code,
                error_message=error_message,
            )
            log_action(
                user=request.user,
                action="admin_send_discord_reminder",
                target=cycle,
                details={"notification_type": cycle_notification_type, "success": success},
            )
        
        sent_count = len(all_cycles) if success else 0
        failed_count = len(all_cycles) if not success else 0
        message = (
            f"Sent 1 batched Discord notification with {sent_count} cycles successfully." if success
            else f"Failed to send batched Discord notification with {failed_count} cycles."
        )
        level = messages.SUCCESS if success else messages.ERROR
        self.message_user(request, message, level=level)

    def send_discord_reminder_advance(self, request, queryset):
        return self._send_discord_reminder_type(request, queryset, "ADVANCE")

    send_discord_reminder_advance.short_description = "Send batched Discord reminder: Due Soon"

    def send_discord_reminder_due(self, request, queryset):
        return self._send_discord_reminder_type(request, queryset, "DUE")

    send_discord_reminder_due.short_description = "Send batched Discord reminder: Due Today"

    def send_discord_reminder_overdue(self, request, queryset):
        return self._send_discord_reminder_type(request, queryset, "OVERDUE")

    send_discord_reminder_overdue.short_description = "Send batched Discord reminder: Overdue"
    
    def send_test_discord_notifications(self, request, queryset):
        """Send batched test Discord notifications for all notification types"""
        from isksync.discord_notifications import send_batched_discord_notification, log_notification
        from isksync.models import DiscordNotificationConfig
        
        if queryset.count() > 10:
            self.message_user(
                request,
                "Too many cycles selected for testing. Please select 10 or fewer.",
                level=messages.ERROR
            )
            return
        
        # Get active Discord config
        config = DiscordNotificationConfig.objects.filter(is_active=True).first()
        if not config:
            self.message_user(
                request,
                "No active Discord configuration found. Please create one first.",
                level=messages.ERROR
            )
            return
        
        # Group all cycles for batched testing (ignore individual discord_channel)
        cycles_by_type = {
            "ADVANCE": list(queryset),  # Add all cycles to each type for testing
            "DUE": list(queryset),
            "OVERDUE": list(queryset),
        }
        all_ping_groups = set()
        
        # Collect all ping groups from all systems
        for cycle in queryset:
            if hasattr(cycle.system_ownership, 'ping_groups'):
                all_ping_groups.update(cycle.system_ownership.ping_groups.all())
        
        # Use base webhook URL (ignore individual discord_channel)
        webhook_url = config.webhook_base_url
        
        # Send single batched test notification
        success, status_code, error_message = send_batched_discord_notification(
            webhook_url=webhook_url,
            cycles_by_type=cycles_by_type,
            config=config,
            all_ping_groups=all_ping_groups
        )
        
        # Log for all affected cycles (each cycle appears in all 3 types for testing)
        for cycle in queryset:
            for notification_type in ["ADVANCE", "DUE", "OVERDUE"]:
                log_notification(
                    tax_cycle=cycle,
                    notification_type=f"TEST_{notification_type}",
                    webhook_url=webhook_url,
                    success=success,
                    status_code=status_code,
                    error_message=error_message
                )
            
            # Log audit action
            log_action(
                user=request.user,
                action="admin_send_test_discord_notifications",
                target=cycle,
                details={
                    "success": success,
                }
            )
        
        total_sent = queryset.count() * 3 if success else 0  # 3 notification types per cycle
        total_failed = queryset.count() * 3 if not success else 0
        
        if success:
            message = f"Sent 1 batched test notification with {total_sent} test entries successfully."
            level = messages.SUCCESS
        else:
            message = f"Failed to send batched test notification with {total_failed} test entries."
            level = messages.ERROR
        
        self.message_user(request, message, level=level)
    
    send_test_discord_notifications.short_description = "Send BATCHED test Discord notifications"

    def mark_as_paid(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request,
                "Too many records selected. Please select 100 or fewer.",
                level=messages.ERROR,
            )
            return

        count = 0
        for cycle in queryset:
            if cycle.status in ["PENDING", "OVERDUE"]:
                cycle.mark_paid()
                log_action(
                    user=request.user,
                    action="admin_mark_cycle_paid",
                    target=cycle,
                    details={
                        "paid_amount": (
                            str(cycle.paid_amount)
                            if cycle.paid_amount is not None
                            else None
                        ),
                        "paid_date": (
                            cycle.paid_date.isoformat() if cycle.paid_date else None
                        ),
                    },
                )
                count += 1
        self.message_user(request, f"{count} cycles were marked as paid.")

    mark_as_paid.short_description = "Mark selected cycles as paid"

    def mark_as_overdue(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request,
                "Too many records selected. Please select 100 or fewer.",
                level=messages.ERROR,
            )
            return

        count = 0
        for cycle in queryset:
            if cycle.status == "PENDING":
                cycle.mark_overdue()
                log_action(
                    user=request.user,
                    action="admin_mark_cycle_overdue",
                    target=cycle,
                    details={},
                )
                count += 1
        self.message_user(request, f"{count} cycles were marked as overdue.")

    mark_as_overdue.short_description = "Mark selected cycles as overdue"

    def write_off_selected(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request,
                "Too many records selected. Please select 100 or fewer.",
                level=messages.ERROR,
            )
            return

        count = 0
        for cycle in queryset:
            if cycle.status != "WRITTEN_OFF":
                cycle.write_off()
                log_action(
                    user=request.user,
                    action="admin_write_off_cycle",
                    target=cycle,
                    details={},
                )
                count += 1
        self.message_user(request, f"{count} cycles were written off.")

    write_off_selected.short_description = "Write off selected cycles"


@admin.register(ObligationType)
class ObligationTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "description")  # Enable autocomplete for this model
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Basic Information", {"fields": ("name", "description", "is_active")}),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )


@admin.register(SystemObligationType)
class SystemObligationTypeAdmin(admin.ModelAdmin):
    list_display = ("system_ownership", "obligation_type", "is_active", "created_at")
    list_filter = ("is_active", "obligation_type", "created_at")
    search_fields = ("system_ownership__system__name", "obligation_type__name")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            "Assignment",
            {"fields": ("system_ownership", "obligation_type", "is_active")},
        ),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    autocomplete_fields = ("system_ownership", "obligation_type")


@admin.register(TaxCycleObligation)
class TaxCycleObligationAdmin(admin.ModelAdmin):
    list_display = (
        "tax_cycle",
        "obligation_type",
        "status",
        "fulfilled_date",
        "fulfilled_by",
    )
    list_filter = ("status", "obligation_type", "fulfilled_date", "tax_cycle__status")
    search_fields = (
        "tax_cycle__system_ownership__system__name",
        "obligation_type__name",
        "fulfilled_by__username",
    )
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Obligation Information", {"fields": ("tax_cycle", "obligation_type")}),
        (
            "Fulfillment Status",
            {"fields": ("status", "fulfilled_date", "fulfilled_by")},
        ),
        ("Additional Information", {"fields": ("notes",)}),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    raw_id_fields = ("tax_cycle",)

    actions = ["mark_as_fulfilled", "mark_as_unfulfilled"]

    def mark_as_fulfilled(self, request, queryset):
        count = 0
        for obligation in queryset:
            if not obligation.status == "COMPLETED":
                obligation.mark_fulfilled(request.user)
                count += 1
        self.message_user(request, f"{count} obligations were marked as fulfilled.")

    mark_as_fulfilled.short_description = "Mark selected obligations as fulfilled"

    def mark_as_unfulfilled(self, request, queryset):
        count = 0
        for obligation in queryset:
            if obligation.status != "PENDING":
                obligation.mark_unfulfilled()
                count += 1
        self.message_user(request, f"{count} obligations were marked as unfulfilled.")

    mark_as_unfulfilled.short_description = "Mark selected obligations as unfulfilled"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "user", "target_repr", "ip_address")
    list_filter = ("action", "user")
    search_fields = ("action", "user__username", "target_repr")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(DiscordNotificationConfig)
class DiscordNotificationConfigAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "is_active",
        "advance_notice_days", 
        "webhook_status",
        "role_mention_template_preview",
        "notification_types_enabled",
        "created_at"
    )
    list_filter = (
        "is_active",
        "send_advance_notice",
        "send_due_notice",
        "send_overdue_notice",
        "advance_severity",
        "due_severity",
        "overdue_severity"
    )
    search_fields = (
        "name",
        "webhook_base_url",
        "webhook_url_template"
    )
    readonly_fields = ("created_at", "updated_at")
    
    fieldsets = (
        (
            "Basic Configuration",
            {"fields": ("name", "is_active")}
        ),
        (
            "Discord Webhook Configuration",
            {
                "fields": ("webhook_base_url", "webhook_url_template"),
                "description": "Base URL should be your Discord webhook. Template can use {base_url} and {channel} placeholders."
            }
        ),
        (
            "Notification Timing",
            {
                "fields": (
                    "advance_notice_days",
                    "send_advance_notice",
                    "send_due_notice",
                    "send_overdue_notice"
                )
            }
        ),
        (
            "Message Configuration",
            {
                "fields": (
                    "role_mention_template",
                    "advance_severity",
                    "due_severity", 
                    "overdue_severity"
                )
            }
        ),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}
        )
    )
    
    def webhook_status(self, obj):
        if obj.webhook_base_url:
            return format_html('<span style="color: #28a745;">✓ Configured</span>')
        return format_html('<span style="color: #dc3545;">✗ Missing</span>')
    
    webhook_status.short_description = "Webhook"
    
    def role_mention_template_preview(self, obj):
        if obj.role_mention_template:
            # Show with example channel
            example = obj.role_mention_template.replace('{channel}', 'farm-l')
            return format_html('<code>{}</code>', example)
        return format_html('<span style="color: #6c757d;">No template</span>')
    
    role_mention_template_preview.short_description = "Role Mention Preview"
    
    def notification_types_enabled(self, obj):
        enabled = []
        if obj.send_advance_notice:
            enabled.append("Advance")
        if obj.send_due_notice:
            enabled.append("Due")
        if obj.send_overdue_notice:
            enabled.append("Overdue")
        
        if enabled:
            return ", ".join(enabled)
        return format_html('<span style="color: #dc3545;">None</span>')
    
    notification_types_enabled.short_description = "Notifications Enabled"


@admin.register(DiscordNotificationLog)
class DiscordNotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "tax_cycle",
        "notification_type",
        "sent_date",
        "success_status",
        "response_status",
        "webhook_domain"
    )
    list_filter = (
        "notification_type",
        "success",
        "sent_date",
        "response_status"
    )
    search_fields = (
        "tax_cycle__system_ownership__system__name",
        "webhook_url",
        "error_message"
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "tax_cycle",
        "notification_type",
        "sent_date",
        "webhook_url",
        "success",
        "response_status",
        "error_message"
    )
    date_hierarchy = "sent_date"
    ordering = ("-sent_date", "-created_at")
    
    fieldsets = (
        (
            "Notification Details",
            {
                "fields": (
                    "tax_cycle",
                    "notification_type",
                    "sent_date"
                )
            }
        ),
        (
            "Webhook Information",
            {"fields": ("webhook_url",)}
        ),
        (
            "Response Status",
            {
                "fields": (
                    "success",
                    "response_status",
                    "error_message"
                )
            }
        ),
        (
            "Audit Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}
        )
    )
    
    raw_id_fields = ("tax_cycle",)
    
    def success_status(self, obj):
        if obj.success:
            return format_html('<span style="color: #28a745;">✓ Success</span>')
        return format_html('<span style="color: #dc3545;">✗ Failed</span>')
    
    success_status.short_description = "Status"
    success_status.admin_order_field = "success"
    
    def webhook_domain(self, obj):
        try:
            from urllib.parse import urlparse
            domain = urlparse(obj.webhook_url).netloc
            return domain if domain else "Invalid URL"
        except:
            return "Invalid URL"
    
    webhook_domain.short_description = "Webhook Domain"
    
    def has_add_permission(self, request):
        # Don't allow manual creation of notification logs
        return False
    
    def has_change_permission(self, request, obj=None):
        # Make logs read-only
        return False


admin.site.site_header = "EVE Tax Management System"
admin.site.site_title = "Tax Admin"
admin.site.index_title = "Welcome to the Tax Management System"
