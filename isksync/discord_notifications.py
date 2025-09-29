"""
Discord notification utilities for ISK Sync tax cycles
"""
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import requests
from django.conf import settings
from django.utils import timezone
from requests.exceptions import HTTPError

from .models import (
    DiscordNotificationConfig,
    DiscordNotificationLog,
    TaxCycle,
)

logger = logging.getLogger(__name__)


def _import_discord_user():
    """Safely import DiscordUser if available"""
    try:
        from allianceauth.services.modules.discord.models import DiscordUser
        return DiscordUser
    except ImportError:
        return None


def _add_discord_group_pings(system_ownership) -> str:
    """Add Discord group pings for the given system ownership.
    
    Args:
        system_ownership: SystemOwnership instance with ping_groups
        
    Returns:
        String containing Discord role pings (e.g., " <@&123456789> <@&987654321>")
        or empty string if Discord service is not available
    """
    if not system_ownership.ping_groups.exists():
        return ""
    
    # Check if Discord app is available (matching structures module pattern)
    try:
        from django.apps import apps
        if not apps.is_installed('allianceauth.services.modules.discord'):
            logger.debug("Discord service not installed - skipping group pings")
            return ""
    except Exception:
        logger.debug("Could not check Discord service installation")
        return ""
    
    DiscordUser = _import_discord_user()
    if not DiscordUser:
        logger.debug("Discord service not available - skipping group pings")
        return ""
    
    groups = system_ownership.ping_groups.all()
    content = ""
    
    for group in groups:
        try:
            role = DiscordUser.objects.group_to_role(group)
        except HTTPError:
            logger.warning(f"Failed to get Discord roles for group {group.name}", exc_info=True)
        except Exception as e:
            logger.warning(f"Error getting Discord role for group {group.name}: {e}")
        else:
            if role:
                content += f" <@&{role['id']}>"
            else:
                logger.debug(f"No Discord role found for group {group.name}")
    
    return content


def _fmt_isk_short(amount: Decimal | None) -> str:
    """Format ISK with compact suffixes"""
    if amount is None:
        return "-"
    try:
        amt = Decimal(amount)
    except Exception:
        return str(amount)
    abs_amt = abs(amt)
    billion = Decimal("1000000000")
    million = Decimal("1000000")
    if abs_amt >= billion:
        val = float(amt / billion)
        text = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{text} bil"
    if abs_amt >= million:
        val = float(amt / million)
        text = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{text} mil"
    return f"{float(amt):,.2f}"


def create_discord_embed(
    tax_cycle: TaxCycle,
    notification_type: str,
    severity: str,
    role_mention: str = "",
    config: DiscordNotificationConfig = None
) -> Dict:
    """Create Discord embed for tax cycle notification"""
    
    if not config:
        # Fallback color mapping
        color_map = {
            "LOW": 0x3498db,     # Blue
            "MEDIUM": 0xf39c12,  # Orange
            "HIGH": 0xe74c3c,    # Red
        }
        color = color_map.get(severity, color_map["MEDIUM"])
    else:
        color = config.get_color_for_severity(severity)
    
    system_name = tax_cycle.system_ownership.system.name
    period = tax_cycle.period_start.strftime('%B %Y')
    due_date = tax_cycle.due_date.strftime('%Y-%m-%d')
    
    # Calculate days until/since due date
    today = date.today()
    days_diff = (tax_cycle.due_date - today).days
    
    if notification_type == "ADVANCE":
        title = f"🚨 Tax Due Soon: {system_name}"
        description = f"Tax payment for **{period}** is due in **{days_diff} days**"
    elif notification_type == "DUE":
        title = f"⚠️ Tax Due Today: {system_name}"
        description = f"Tax payment for **{period}** is **due today**"
    elif notification_type == "OVERDUE":
        overdue_days = abs(days_diff)
        title = f"🔴 Overdue Tax: {system_name}"
        description = f"Tax payment for **{period}** is **{overdue_days} days overdue**"
    else:
        title = f"Tax Notification: {system_name}"
        description = f"Tax cycle update for **{period}**"
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": timezone.now().isoformat(),
        "fields": [
            {
                "name": "System",
                "value": system_name,
                "inline": True
            },
            {
                "name": "Period",
                "value": period,
                "inline": True
            },
            {
                "name": "Due Date",
                "value": due_date,
                "inline": True
            },
            {
                "name": "Amount Due",
                "value": f"{_fmt_isk_short(tax_cycle.remaining_amount)} ISK",
                "inline": True
            },
            {
                "name": "Status",
                "value": tax_cycle.get_status_display(),
                "inline": True
            }
        ]
    }
    
    # Add obligations if any
    if hasattr(tax_cycle, 'obligations') and tax_cycle.obligations.exists():
        outstanding_obligations = tax_cycle.obligations.filter(status__in=["PENDING", "FAILED"])
        if outstanding_obligations.exists():
            obligation_names = [obj.obligation_type.name for obj in outstanding_obligations]
            embed["fields"].append({
                "name": "Outstanding Obligations",
                "value": ", ".join(obligation_names),
                "inline": False
            })
    
    # Add footer with severity
    embed["footer"] = {
        "text": f"Priority: {severity} | Farm Manager"
    }
    
    return embed


def send_discord_notification(
    webhook_url: str,
    tax_cycle: TaxCycle,
    notification_type: str,
    severity: str,
    role_mention: str = "",
    config: DiscordNotificationConfig = None
) -> Tuple[bool, int, str]:
    """
    Send Discord notification for a tax cycle
    
    Returns:
        (success: bool, status_code: int, error_message: str)
    """
    
    try:
        embed = create_discord_embed(tax_cycle, notification_type, severity, role_mention, config)
        
        payload = {
            "embeds": [embed]
        }
        
        # Add role mention and group pings as content
        content = ""
        if role_mention:
            content += role_mention
        
        # Add Discord group pings
        group_pings = _add_discord_group_pings(tax_cycle.system_ownership)
        content += group_pings
        
        if content:
            payload["content"] = content
        
        # Add some debugging info in development
        if getattr(settings, 'DEBUG', False):
            logger.info(f"Sending Discord notification: {notification_type} for {tax_cycle}")
            logger.debug(f"Webhook URL: {webhook_url[:50]}...")
            logger.debug(f"Payload: {json.dumps(payload, indent=2)}")
        
        headers = {
            "Content-Type": "application/json",
        }
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=10  # 10 second timeout
        )
        
        if response.status_code == 204:
            logger.info(f"Successfully sent Discord notification: {notification_type} for {tax_cycle}")
            return True, response.status_code, ""
        else:
            error_msg = f"Discord API returned status {response.status_code}: {response.text}"
            logger.error(error_msg)
            return False, response.status_code, error_msg
            
    except requests.exceptions.Timeout:
        error_msg = "Discord webhook request timed out"
        logger.error(error_msg)
        return False, 0, error_msg
    except requests.exceptions.RequestException as e:
        error_msg = f"Discord webhook request failed: {str(e)}"
        logger.error(error_msg)
        return False, 0, error_msg
    except Exception as e:
        error_msg = f"Unexpected error sending Discord notification: {str(e)}"
        logger.error(error_msg)
        return False, 0, error_msg


def log_notification(
    tax_cycle: TaxCycle,
    notification_type: str,
    webhook_url: str,
    success: bool,
    status_code: int = None,
    error_message: str = ""
) -> DiscordNotificationLog:
    """Log a Discord notification attempt"""
    
    return DiscordNotificationLog.objects.create(
        tax_cycle=tax_cycle,
        notification_type=notification_type,
        sent_date=date.today(),
        webhook_url=webhook_url,
        success=success,
        response_status=status_code,
        error_message=error_message
    )


# Removed should_send_notification function - now using batched approach only


# Removed process_tax_cycle_notifications function - now using batched approach only


def create_batched_discord_embed(
    cycles_by_type: Dict[str, List[TaxCycle]],
    config: DiscordNotificationConfig
) -> Dict:
    """Create a batched Discord embed for multiple tax cycles by notification type"""
    
    total_cycles = sum(len(cycles) for cycles in cycles_by_type.values())
    
    # Determine overall severity (highest priority wins)
    severity_priority = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    max_severity = "LOW"
    
    for notification_type, cycles in cycles_by_type.items():
        if not cycles:
            continue
        if notification_type == "ADVANCE":
            type_severity = config.advance_severity
        elif notification_type == "DUE":
            type_severity = config.due_severity
        else:  # OVERDUE
            type_severity = config.overdue_severity
            
        if severity_priority[type_severity] > severity_priority[max_severity]:
            max_severity = type_severity
    
    color = config.get_color_for_severity(max_severity)
    
    # Create title and description (single type only, since they occur at different times)
    notification_type = list(cycles_by_type.keys())[0]  # Should only be one type
    if notification_type == "ADVANCE":
        title = f"🚨 Tax Payments Due Soon ({total_cycles} systems)"
        description = f"**{total_cycles}** systems have tax payments due soon"
    elif notification_type == "DUE":
        title = f"🚨 Tax Payments Due Today ({total_cycles} systems)"
        description = f"**{total_cycles}** systems have tax payments due today"
    else:  # OVERDUE
        title = f"🚨 Overdue Tax Payments ({total_cycles} systems)"
        description = f"**{total_cycles}** systems have overdue tax payments"
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": timezone.now().isoformat(),
        "fields": []
    }
    
    # Add fields for each notification type (using group name instead of system count)
    for notification_type in ["ADVANCE", "DUE", "OVERDUE"]:
        cycles = cycles_by_type.get(notification_type, [])
        if not cycles:
            continue
            
        # Get all unique group names for this notification type
        group_names = set()
        for cycle in cycles:
            if cycle.system_ownership.auth_group:
                group_names.add(cycle.system_ownership.auth_group.group.name)
        
        if len(group_names) == 1:
            group_name = list(group_names)[0]
        elif len(group_names) > 1:
            group_name = f"{len(group_names)} Groups"
        else:
            group_name = "Unknown Group"
            
        if notification_type == "ADVANCE":
            field_name = f"Due Soon - {group_name}"
        elif notification_type == "DUE":
            field_name = f"Due Today - {group_name}"
        else:  # OVERDUE
            field_name = f"Overdue - {group_name}"
        
        # Group cycles by due date for cleaner display
        systems_by_date = {}
        for cycle in cycles:
            due_date = cycle.due_date.strftime('%Y-%m-%d')
            if due_date not in systems_by_date:
                systems_by_date[due_date] = []
            systems_by_date[due_date].append(cycle.system_ownership.system.name)
        
        field_value = ""
        for due_date, systems in sorted(systems_by_date.items()):
            if notification_type == "ADVANCE":
                today = date.today()
                days_until = (cycles[0].due_date - today).days
                field_value += f"**{due_date}** ({days_until} days): {', '.join(systems[:5])}"
            elif notification_type == "DUE":
                field_value += f"**{due_date}**: {', '.join(systems[:5])}"
            else:  # OVERDUE
                today = date.today()
                days_overdue = (today - cycles[0].due_date).days
                field_value += f"**{due_date}** ({days_overdue} days overdue): {', '.join(systems[:5])}"
                
            if len(systems) > 5:
                field_value += f" (+{len(systems) - 5} more)"
            field_value += "\n"
        
        embed["fields"].append({
            "name": field_name,
            "value": field_value.strip(),
            "inline": False
        })
    
    # Add link to Farm Manager Dashboard
    from django.conf import settings
    site_url = getattr(settings, 'SITE_URL', 'https://your-auth-site.com')
    embed["fields"].append({
        "name": "📊 View Details",
        "value": f"[Open Farm Manager Dashboard]({site_url}/isksync/)",
        "inline": False
    })
    
    # Add footer
    embed["footer"] = {
        "text": f"Priority: {max_severity} | Farm Manager"
    }
    
    return embed


def send_batched_discord_notification(
    webhook_url: str,
    cycles_by_type: Dict[str, List[TaxCycle]],
    config: DiscordNotificationConfig,
    all_ping_groups: set = None
) -> Tuple[bool, int, str]:
    """Send a batched Discord notification for multiple tax cycles"""
    
    try:
        embed = create_batched_discord_embed(cycles_by_type, config)
        
        payload = {
            "embeds": [embed]
        }
        
        # Add combined group pings from all affected systems
        content = ""
        if all_ping_groups:
            # Try to get Discord role pings for all unique groups
            try:
                from django.apps import apps
                if apps.is_installed('allianceauth.services.modules.discord'):
                    DiscordUser = _import_discord_user()
                    if DiscordUser:
                        for group in all_ping_groups:
                            try:
                                role = DiscordUser.objects.group_to_role(group)
                                if role:
                                    content += f" <@&{role['id']}>"
                            except Exception as e:
                                logger.debug(f"Could not get Discord role for {group.name}: {e}")
            except Exception:
                pass
        
        if content:
            payload["content"] = content.strip()
        
        headers = {
            "Content-Type": "application/json",
        }
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 204:
            logger.info(f"Successfully sent batched Discord notification for {sum(len(cycles) for cycles in cycles_by_type.values())} cycles")
            return True, response.status_code, ""
        else:
            error_msg = f"Discord API returned status {response.status_code}: {response.text}"
            logger.error(error_msg)
            return False, response.status_code, error_msg
            
    except Exception as e:
        error_msg = f"Unexpected error sending batched Discord notification: {str(e)}"
        logger.error(error_msg)
        return False, 0, error_msg


def process_all_tax_cycle_notifications() -> Dict[str, int]:
    """
    Process Discord notifications for all active tax cycles using batched approach
    
    Returns summary of notifications processed
    """
    
    summary = {
        "cycles_checked": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "batched_messages_sent": 0,
        "config_found": False,
    }
    
    try:
        # Get the global Discord configuration
        config = DiscordNotificationConfig.objects.filter(is_active=True).first()
        if not config:
            logger.warning("No active Discord notification configuration found")
            return summary
        summary["config_found"] = True
        
        # Get all tax cycles that might need notifications
        tax_cycles = TaxCycle.objects.select_related(
            'system_ownership',
            'system_ownership__system'
        ).filter(
            status__in=["PENDING", "OVERDUE"]
        )
        
        summary["cycles_checked"] = tax_cycles.count()
        
        # Group all cycles by notification type (ignore discord_channel)
        cycles_by_type = {
            "ADVANCE": [],
            "DUE": [],
            "OVERDUE": [],
        }
        all_ping_groups = set()
        today = date.today()
        
        for cycle in tax_cycles:
            # Determine notification type based on timing rules
            days_until_due = (cycle.due_date - today).days
            notification_type = None
            
            if days_until_due == config.advance_notice_days and config.send_advance_notice:
                notification_type = "ADVANCE"
            elif days_until_due == 0 and config.send_due_notice:
                notification_type = "DUE"
            elif days_until_due < 0 and config.send_overdue_notice and cycle.status in ["PENDING", "OVERDUE"]:
                notification_type = "OVERDUE"
            
            if not notification_type:
                continue
                
            # Check if already sent today (for automated notifications)
            existing_log = DiscordNotificationLog.objects.filter(
                tax_cycle=cycle,
                notification_type=f"BATCHED_{notification_type}",
                sent_date=today,
                success=True
            ).exists()
            
            if existing_log:
                continue
            
            cycles_by_type[notification_type].append(cycle)
            
            # Collect all ping groups from all systems
            if hasattr(cycle.system_ownership, 'ping_groups'):
                all_ping_groups.update(cycle.system_ownership.ping_groups.all())
        
        # Remove empty notification types
        cycles_by_type = {k: v for k, v in cycles_by_type.items() if v}
        
        if not cycles_by_type:
            logger.debug("No cycles need notifications today")
            return summary
            
        # Use base webhook URL (ignore individual discord_channel)
        webhook_url = config.webhook_base_url
        
        # Send single batched notification per day
        success, status_code, error_message = send_batched_discord_notification(
            webhook_url=webhook_url,
            cycles_by_type=cycles_by_type,
            config=config,
            all_ping_groups=all_ping_groups
        )
        
        # Log the batched notification for all affected cycles
        all_cycles = []
        for cycles in cycles_by_type.values():
            all_cycles.extend(cycles)
        
        for cycle in all_cycles:
            # Determine the specific notification type for this cycle
            days_until_due = (cycle.due_date - today).days
            if days_until_due == config.advance_notice_days:
                cycle_notification_type = "ADVANCE"
            elif days_until_due == 0:
                cycle_notification_type = "DUE"
            else:
                cycle_notification_type = "OVERDUE"
                
            log_notification(
                tax_cycle=cycle,
                notification_type=f"BATCHED_{cycle_notification_type}",
                webhook_url=webhook_url,
                success=success,
                status_code=status_code,
                error_message=error_message
            )
        
        if success:
            summary["batched_messages_sent"] = 1
            summary["notifications_sent"] = len(all_cycles)
        else:
            summary["notifications_failed"] = len(all_cycles)
        
        logger.info(f"Batched Discord notification summary: {summary}")
        
    except Exception as e:
        logger.error(f"Error in process_all_tax_cycle_notifications: {str(e)}")
    
    return summary
