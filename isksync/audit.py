from __future__ import annotations
import typing as _t
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from .models import AuditLog


def log_action(
    *,
    user,
    action: str,
    target,
    details: _t.Optional[dict] = None,
    request=None,
) -> AuditLog:
    """Create an AuditLog entry."""
    if details is None:
        details = {}

    ct = ContentType.objects.get_for_model(target.__class__)

    ip = None
    if request is not None:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR"))
        if isinstance(ip, str) and "," in ip:
            ip = ip.split(",")[0].strip()

    with transaction.atomic():
        entry = AuditLog.objects.create(
            action=action[:50],
            user=user if getattr(user, "pk", None) else None,
            target_content_type=ct,
            target_object_id=target.pk,
            target_repr=str(target)[:255],
            details=details,
            ip_address=ip,
            created_at=timezone.now(),
        )
    return entry
