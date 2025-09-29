from allianceauth import hooks
from allianceauth.services.hooks import UrlHook, MenuItemHook
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.template.loader import render_to_string

from .models import SystemOwnership, TaxCycle
from . import urls as isksync_urls


@hooks.register("dashboard_hook")
def register_isksync_dashboard():
    """Register a dashboard widget for AllianceAuth (BS5)."""

    class IskSyncDashboardPanel:
        order = 100

        def render(self, request):
            # Only show the widget if the user is assigned to at least one SystemOwnership
            so_qs = SystemOwnership.objects.filter(auth_group__group__user=request.user)
            if not so_qs.exists():
                return ""

            today = timezone.now().date()
            month_start = today.replace(day=1)

            # Scope to the user's assigned systems
            tc_qs = TaxCycle.objects.select_related("system_ownership", "system_ownership__system").filter(system_ownership__in=so_qs)

            # Outstanding cycles for end user: pending or explicitly overdue (any period)
            outstanding = (
                tc_qs.filter(status__in=["PENDING", "OVERDUE"])\
                    .order_by("due_date", "system_ownership__system__name")[:5]
            )

            # attach compact currency formatting for remaining amount
            from decimal import Decimal
            def _fmt_isk_short(amount: Decimal | None) -> str:
                if amount is None:
                    return "-"
                try:
                    amt = Decimal(amount)
                except Exception:
                    return str(amount)
                abs_amt = abs(amt)
                if abs_amt >= Decimal("1000000000"):
                    val = float(amt / Decimal("1000000000"))
                    text = (f"{val:.1f}").rstrip("0").rstrip(".")
                    return f"{text} bil"
                if abs_amt >= Decimal("1000000"):
                    val = float(amt / Decimal("1000000"))
                    text = (f"{val:.1f}").rstrip("0").rstrip(".")
                    return f"{text} mil"
                return f"{float(amt):,.2f}"

            for c in outstanding:
                c.remaining_fmt = _fmt_isk_short(c.remaining_amount)

            context = {
                "title": _("Farm Agreements"),
                "outstanding": outstanding,
                "can_manage": _can_manage(request.user),
            }
            # AllianceAuth (BS5 dashboard_hook) expects render() to return HTML
            return render_to_string("isksync/dashboard_widget.html", context=context, request=request)

    return IskSyncDashboardPanel()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(isksync_urls, "isksync", r"^isksync/")


def _user_has_assignment(user) -> bool:
    return SystemOwnership.objects.filter(auth_group__group__user=user).exists()


def _can_manage(user) -> bool:
    return getattr(user, "is_staff", False) or user.has_perm("isksync.manage_tax_cycles")


class IskSyncMainMenu(MenuItemHook):
    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Farm Agreements"),
            "fa-solid fa-coins",
            "isksync:my_due",
            navactive=[
                "isksync:my_due",
                "isksync:my_history",
                "isksync:manage",
                "isksync:manage_all",
            ]
        )

    def render(self, request):
        # Show menu if user has assignment OR can manage
        if _user_has_assignment(request.user) or _can_manage(request.user):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_isksync_menu():
    return IskSyncMainMenu()
