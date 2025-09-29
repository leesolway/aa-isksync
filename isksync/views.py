from decimal import Decimal, InvalidOperation
from typing import List
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django.core.paginator import Paginator
from django.contrib.contenttypes.models import ContentType

from .models import SystemOwnership, TaxCycle, TaxCycleObligation, ObligationType, AuditLog
from .audit import log_action


def _fmt_isk_short(amount: Decimal | None) -> str:
    """Format ISK with compact suffixes without using custom template tags.
    Examples: 3,000,000,000 -> "3 bil", 2500000 -> "2.5 mil", 12345.67 -> "12,345.67"
    """
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


def _user_systems(user):
    return (
        SystemOwnership.objects.select_related("system", "ownership_type", "auth_group")
        .filter(auth_group__group__user=user)
        .order_by("system__name")
    )


@method_decorator(login_required, name="dispatch")
class MyDueTaxesView(TemplateView):
    template_name = "isksync/my_due.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.now().date()
        month_start = today.replace(day=1)

        systems = (
            _user_systems(user)
            .filter(tax_active=True)
            .prefetch_related("auth_group__group__user_set")
        )
        # attach formatted current rate
        for s in systems:
            try:
                s.current_rate_fmt = _fmt_isk_short(s.get_current_tax_amount())
            except Exception:
                s.current_rate_fmt = "-"

        # Show all outstanding cycles (payments/obligations) for the user
        cycles_qs = (
            TaxCycle.objects.select_related("system_ownership", "system_ownership__system")
            .prefetch_related("obligations__obligation_type")
            .filter(system_ownership__in=systems, status__in=["PENDING", "OVERDUE"])
            .order_by("due_date", "system_ownership__system__name")
        )
        # Materialize queryset to avoid re-evaluation losing attached attributes
        cycles = list(cycles_qs)
        # attach formatted amounts
        for c in cycles:
            c.expected_fmt = _fmt_isk_short(c.expected_amount)
            c.paid_fmt = "-" if c.paid_amount is None else _fmt_isk_short(c.paid_amount)
            # Outstanding obligations (Pending or Failed)
            try:
                c.outstanding_obligations = [
                    o for o in c.obligations.all() if getattr(o, "status", "PENDING") in ("PENDING", "FAILED")
                ]
            except Exception:
                c.outstanding_obligations = []

        # Build a list of cycles with at least one outstanding obligation
        cycles_with_outstanding = [c for c in cycles if getattr(c, "outstanding_obligations", [])]

        # Flatten outstanding obligations into rows for a table view
        outstanding_obligations = []
        for c in cycles_with_outstanding:
            for o in c.outstanding_obligations:
                outstanding_obligations.append({"cycle": c, "obligation": o})

        ctx.update({
            "page_title": "Tax Due",
            "systems": systems,
            "cycles": cycles,
            "cycles_with_outstanding": cycles_with_outstanding,
            "outstanding_obligations": outstanding_obligations,
        })
        return ctx




@method_decorator(login_required, name="dispatch")
class MyPaymentHistoryView(TemplateView):
    template_name = "isksync/my_history.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        systems = _user_systems(user)
        cycles_qs = (
            TaxCycle.objects.select_related("system_ownership", "system_ownership__system")
            .prefetch_related("obligations__obligation_type")
            .filter(system_ownership__in=systems, status__in=["PAID", "WRITTEN_OFF"])
            .order_by("-period_start")
        )
        cycles = list(cycles_qs)
        for c in cycles:
            c.expected_fmt = _fmt_isk_short(c.expected_amount)
            c.paid_fmt = "-" if c.paid_amount is None else _fmt_isk_short(c.paid_amount)
        
        # Fetch all completed/failed obligations for the user's systems, regardless of cycle status
        obligations_qs = (
            TaxCycleObligation.objects.select_related(
                "tax_cycle__system_ownership__system",
                "obligation_type",
            )
            .filter(
                tax_cycle__system_ownership__in=systems,
                status__in=["COMPLETED", "FAILED"],
            )
            .order_by("-tax_cycle__period_start", "obligation_type__name")
        )
        obligations_history = [
            {"cycle": o.tax_cycle, "obligation": o} for o in obligations_qs
        ]

        ctx.update({
            "page_title": "Payment History",
            "history_cycles": cycles,
            "obligations_history": obligations_history,
        })
        return ctx


def _can_manage(user) -> bool:
    return user.is_staff or user.has_perm("isksync.manage_tax_cycles")


@method_decorator(login_required, name="dispatch")
class ManageView(TemplateView):
    template_name = "isksync/manage.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not _can_manage(request.user):
            return HttpResponseForbidden("You do not have access to this page.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Base: cycles needing attention
        base_qs = (
            TaxCycle.objects.select_related("system_ownership", "system_ownership__system")
            .prefetch_related(
                "system_ownership__auth_group__group__user_set",
                "obligations__obligation_type",
            )
            .filter(status__in=["PENDING", "OVERDUE"])
        )

        cycles_marked_paid = list(
            base_qs.filter(user_marked_paid=True).order_by("due_date", "system_ownership__system__name")
        )
        cycles_unmarked = list(
            base_qs.filter(user_marked_paid=False).order_by("due_date", "system_ownership__system__name")
        )

        for c in cycles_marked_paid + cycles_unmarked:
            c.expected_fmt = _fmt_isk_short(c.expected_amount)

        # Obligations queues (for cycles that are still open)
        # Completed obligations are reviewed on the obligations page; not shown here
        obligations_outstanding = list(
            TaxCycleObligation.objects.select_related(
                "tax_cycle__system_ownership__system",
                "obligation_type",
            )
            .filter(tax_cycle__status__in=["PENDING", "OVERDUE"], status__in=["PENDING", "FAILED"])
            .order_by("tax_cycle__due_date", "tax_cycle__system_ownership__system__name", "obligation_type__name")
        )

        # Attach recent logs (last 5) to visible cycles
        try:
            cycle_ct = ContentType.objects.get_for_model(TaxCycle)
            visible_ids = list({*(c.pk for c in cycles_marked_paid), *(c.pk for c in cycles_unmarked)})
            logs_group = {}
            if visible_ids:
                logs_qs = (
                    AuditLog.objects.select_related("user")
                    .filter(target_content_type=cycle_ct, target_object_id__in=visible_ids)
                    .order_by("-created_at")
                )
                for log in logs_qs:
                    logs_group.setdefault(log.target_object_id, [])
                    if len(logs_group[log.target_object_id]) < 5:
                        logs_group[log.target_object_id].append(log)
            for c in cycles_marked_paid:
                c.recent_logs = logs_group.get(c.pk, [])
            for c in cycles_unmarked:
                c.recent_logs = logs_group.get(c.pk, [])
        except Exception:
            for c in cycles_marked_paid:
                c.recent_logs = []
            for c in cycles_unmarked:
                c.recent_logs = []

        recent_logs = AuditLog.objects.select_related("user").order_by("-created_at")[:50]

        ctx.update(
            {
                "page_title": "Manage",
                "cycles_marked_paid": cycles_marked_paid,
                "cycles_unmarked": cycles_unmarked,
                "obligations_outstanding": obligations_outstanding,
                "recent_logs": recent_logs,
            }
        )
        return ctx


@method_decorator(login_required, name="dispatch")
class ManageAllCyclesView(TemplateView):
    template_name = "isksync/manage_all.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not _can_manage(request.user):
            return HttpResponseForbidden("You do not have access to this page.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = (
            TaxCycle.objects.select_related("system_ownership", "system_ownership__system", "system_ownership__auth_group__group")
            .prefetch_related("obligations__obligation_type", "system_ownership__auth_group__group__user_set")
            .all()
            .order_by("-period_start")
        )
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        cycles = list(qs[:500])
        for c in cycles:
            c.expected_fmt = _fmt_isk_short(c.expected_amount)
            c.paid_fmt = "-" if c.paid_amount is None else _fmt_isk_short(c.paid_amount)
        ctx.update({
            "page_title": "All Cycles",
            "cycles": cycles,  # safety cap already applied
        })
        return ctx


@method_decorator(login_required, name="dispatch")
class ManageAllObligationsView(TemplateView):
    template_name = "isksync/manage_all_obligations.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not _can_manage(request.user):
            return HttpResponseForbidden("You do not have access to this page.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = (
            TaxCycleObligation.objects.select_related(
                "tax_cycle__system_ownership__system",
                "tax_cycle__system_ownership__auth_group__group",
                "obligation_type",
            )
            .prefetch_related("tax_cycle__system_ownership__auth_group__group__user_set")
            .order_by("-tax_cycle__period_start", "tax_cycle__system_ownership__system__name", "obligation_type__name")
        )
        status = (self.request.GET.get("status") or "").upper()
        if status in {"PENDING", "COMPLETED", "FAILED"}:
            qs = qs.filter(status=status)
        ctx.update({
            "page_title": "All Obligations",
            "obligations": qs[:2000],  # larger cap
        })
        return ctx




@method_decorator(login_required, name="dispatch")
class OwnershipDetailView(TemplateView):
    template_name = "isksync/ownership_detail.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        pk = kwargs.get("pk")
        ownership = get_object_or_404(
            SystemOwnership.objects.select_related(
                "system",
                "ownership_type",
                "primary_user",
                "auth_group__group",
            ).prefetch_related(
                "auth_group__group__user_set",
                "obligation_types__obligation_type",
            ),
            pk=pk,
        )
        # Permission: manager or member of auth group
        if not _can_manage(request.user):
            group = getattr(ownership, "auth_group", None)
            if not group or not group.group.user_set.filter(pk=request.user.pk).exists():
                return HttpResponseForbidden("Not allowed")
        self.ownership = ownership
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        so = self.ownership
        admin_mode = _can_manage(self.request.user)
        try:
            current_rate_fmt = _fmt_isk_short(so.get_current_tax_amount())
        except Exception:
            current_rate_fmt = "-"
        # Active agreements
        agreements = [sot for sot in so.obligation_types.all() if getattr(sot, "is_active", True)]
        members = []
        try:
            members = list(so.auth_group.group.user_set.all())
        except Exception:
            members = []

        # Recent activity by group members on this ownership's cycles/obligations
        from django.contrib.contenttypes.models import ContentType
        member_ids = [u.pk for u in members]
        cycle_ids = list(
            TaxCycle.objects.filter(system_ownership=so).values_list("pk", flat=True)
        )
        obligation_ids = list(
            TaxCycleObligation.objects.filter(tax_cycle__system_ownership=so).values_list(
                "pk", flat=True
            )
        )
        member_activity = []
        if member_ids and (cycle_ids or obligation_ids):
            try:
                ct_cycle = ContentType.objects.get_for_model(TaxCycle)
                ct_ob = ContentType.objects.get_for_model(TaxCycleObligation)
                q = AuditLog.objects.select_related("user").filter(user_id__in=member_ids)
                from django.db.models import Q
                cond = Q()
                if cycle_ids:
                    cond |= Q(target_content_type=ct_cycle, target_object_id__in=cycle_ids)
                if obligation_ids:
                    cond |= Q(target_content_type=ct_ob, target_object_id__in=obligation_ids)
                if cond:
                    member_activity = list(q.filter(cond).order_by("-created_at")[:50])
            except Exception:
                member_activity = []

        ctx.update(
            {
                "page_title": f"{so.system.name} — {so.ownership_type.label}",
                "ownership": so,
                "current_rate_fmt": current_rate_fmt,
                "agreements": agreements,
                "members": members,
                "member_activity": member_activity,
                "admin_mode": admin_mode,
            }
        )
        return ctx


@login_required
def mark_cycle_paid(request: HttpRequest, pk: int) -> HttpResponse:
    if not _can_manage(request.user):
        return HttpResponseForbidden("Not allowed")
    if request.method != "POST":
        return redirect("isksync:manage")
    cycle = get_object_or_404(TaxCycle, pk=pk)
    cycle.mark_paid()
    log_action(
        user=request.user,
        action="cycle_mark_paid",
        target=cycle,
        details={
            "paid_amount": (str(cycle.paid_amount) if cycle.paid_amount is not None else None),
            "paid_date": (cycle.paid_date.isoformat() if cycle.paid_date else None),
        },
        request=request,
    )
    messages.success(request, f"Marked paid: {cycle}")
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:manage"))


@login_required
def toggle_obligation_fulfilled(request: HttpRequest, pk: int) -> HttpResponse:
    """Allow users to set obligation status: complete, pending, fail (default toggle if no action)."""
    if request.method != "POST":
        return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:my_due"))
    
    # Get the obligation and verify access
    systems = _user_systems(request.user)
    obligation = get_object_or_404(
        TaxCycleObligation.objects.select_related(
            "tax_cycle__system_ownership",
            "tax_cycle__system_ownership__system",
            "obligation_type"
        ),
        pk=pk,
        tax_cycle__system_ownership__in=systems,
    )

    action_name = (request.POST.get("action") or "").strip().lower()

    if action_name == "complete":
        obligation.mark_fulfilled(request.user)
        log_action(
            user=request.user,
            action="obligation_completed",
            target=obligation,
            details={
                "obligation_type": obligation.obligation_type.name,
                "system": obligation.tax_cycle.system_ownership.system.name,
                "period_start": obligation.tax_cycle.period_start.isoformat(),
            },
            request=request,
        )
        messages.success(
            request,
            "Marked obligation '{}' as completed for {} ({})".format(
                obligation.obligation_type.name,
                obligation.tax_cycle.system_ownership.system.name,
                obligation.tax_cycle.period_start.strftime('%b %Y')
            )
        )
    elif action_name == "pending":
        obligation.mark_unfulfilled()
        log_action(
            user=request.user,
            action="obligation_set_pending",
            target=obligation,
            details={
                "obligation_type": obligation.obligation_type.name,
                "system": obligation.tax_cycle.system_ownership.system.name,
                "period_start": obligation.tax_cycle.period_start.isoformat(),
            },
            request=request,
        )
        messages.info(
            request,
            "Set pending: '{}' for {} ({})".format(
                obligation.obligation_type.name,
                obligation.tax_cycle.system_ownership.system.name,
                obligation.tax_cycle.period_start.strftime('%b %Y')
            )
        )
    elif action_name == "fail":
        obligation.mark_failed(request.user)
        log_action(
            user=request.user,
            action="obligation_failed",
            target=obligation,
            details={
                "obligation_type": obligation.obligation_type.name,
                "system": obligation.tax_cycle.system_ownership.system.name,
                "period_start": obligation.tax_cycle.period_start.isoformat(),
            },
            request=request,
        )
        messages.warning(
            request,
            "Marked obligation '{}' as failed for {} ({})".format(
                obligation.obligation_type.name,
                obligation.tax_cycle.system_ownership.system.name,
                obligation.tax_cycle.period_start.strftime('%b %Y')
            )
        )
    else:
        # Fallback toggle: completed <-> pending
        if obligation.status == "COMPLETED":
            obligation.mark_unfulfilled()
            log_action(
                user=request.user,
                action="obligation_set_pending",
                target=obligation,
                details={},
                request=request,
            )
            messages.info(request, "Set pending: '{}'".format(obligation.obligation_type.name))
        else:
            obligation.mark_fulfilled(request.user)
            log_action(
                user=request.user,
                action="obligation_completed",
                target=obligation,
                details={},
                request=request,
            )
            messages.success(request, "Completed: '{}'".format(obligation.obligation_type.name))
    
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:my_due"))


@login_required
def admin_toggle_obligation(request: HttpRequest, pk: int) -> HttpResponse:
    """Allow admins to set obligation status: complete, pending, fail (default toggle if no action)."""
    if not _can_manage(request.user):
        return HttpResponseForbidden("Not allowed")
    
    if request.method != "POST":
        return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:manage"))
    
    obligation = get_object_or_404(
        TaxCycleObligation.objects.select_related(
            "tax_cycle__system_ownership__system",
            "obligation_type"
        ),
        pk=pk
    )

    action_name = (request.POST.get("action") or "").strip().lower()

    if action_name == "complete":
        obligation.mark_fulfilled(request.user)
        log_action(user=request.user, action="admin_obligation_completed", target=obligation, details={}, request=request)
        messages.success(request, "Admin: completed '{}'".format(obligation.obligation_type.name))
    elif action_name == "pending":
        obligation.mark_unfulfilled()
        log_action(user=request.user, action="admin_obligation_set_pending", target=obligation, details={}, request=request)
        messages.info(request, "Admin: set pending '{}'".format(obligation.obligation_type.name))
    elif action_name == "fail":
        obligation.mark_failed(request.user)
        log_action(user=request.user, action="admin_obligation_failed", target=obligation, details={}, request=request)
        messages.warning(request, "Admin: failed '{}'".format(obligation.obligation_type.name))
    else:
        # Fallback toggle
        if obligation.status == "COMPLETED":
            obligation.mark_unfulfilled()
            log_action(user=request.user, action="admin_obligation_set_pending", target=obligation, details={}, request=request)
            messages.info(request, "Admin: set pending '{}'".format(obligation.obligation_type.name))
        else:
            obligation.mark_fulfilled(request.user)
            log_action(user=request.user, action="admin_obligation_completed", target=obligation, details={}, request=request)
            messages.success(request, "Admin: completed '{}'".format(obligation.obligation_type.name))
    
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:manage"))


@login_required
def exempt_cycle(request: HttpRequest, pk: int) -> HttpResponse:
    if not _can_manage(request.user):
        return HttpResponseForbidden("Not allowed")
    if request.method != "POST":
        return redirect("isksync:manage")
    cycle = get_object_or_404(TaxCycle, pk=pk)
    cycle.write_off(notes="Exempted via frontend")
    log_action(
        user=request.user,
        action="cycle_write_off",
        target=cycle,
        details={"notes": "Exempted via frontend"},
        request=request,
    )
    messages.success(request, f"Exempted: {cycle}")
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:manage"))


@login_required
def toggle_user_marked_paid(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("isksync:my_due")

    # Ensure the cycle belongs to a system the user has access to
    systems = _user_systems(request.user)
    cycle = get_object_or_404(
        TaxCycle.objects.select_related("system_ownership", "system_ownership__system"),
        pk=pk,
        system_ownership__in=systems,
    )

    # Toggle the user_marked_paid flag; do not change official status
    cycle.user_marked_paid = not cycle.user_marked_paid
    if cycle.user_marked_paid:
        cycle.user_marked_paid_at = timezone.now()
        messages.success(
            request,
            f"Marked as paid for {cycle.system_ownership.system.name} period {cycle.period_start:%b %Y} (now completed).",
        )
    else:
        cycle.user_marked_paid_at = None
        messages.info(
            request,
            f"Cleared 'marked as paid' for {cycle.system_ownership.system.name} ({cycle.period_start:%b %Y}).",
        )
    cycle.save(update_fields=["user_marked_paid", "user_marked_paid_at", "updated_at"])

    # Audit log
    from_date = cycle.user_marked_paid_at.isoformat() if cycle.user_marked_paid_at else None
    log_action(
        user=request.user,
        action="user_toggle_marked_paid",
        target=cycle,
        details={
            "new_value": cycle.user_marked_paid,
            "timestamp": from_date,
        },
        request=request,
    )

    # Prefer to return to referrer; fallback to My Due
    next_url = request.META.get("HTTP_REFERER") or reverse("isksync:my_due")
    return HttpResponseRedirect(next_url)




@login_required
def mark_cycle_pending(request: HttpRequest, pk: int) -> HttpResponse:
    if not _can_manage(request.user):
        return HttpResponseForbidden("Not allowed")
    if request.method != "POST":
        return redirect("isksync:manage")

    cycle = get_object_or_404(TaxCycle, pk=pk)
    # Reset to pending and clear payment fields
    cycle.status = "PENDING"
    cycle.paid_amount = None
    cycle.paid_date = None
    cycle.save(update_fields=["status", "paid_amount", "paid_date", "updated_at"])
    log_action(
        user=request.user,
        action="cycle_set_pending",
        target=cycle,
        details={},
        request=request,
    )
    messages.success(request, f"Set to pending: {cycle}.")
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("isksync:manage"))
