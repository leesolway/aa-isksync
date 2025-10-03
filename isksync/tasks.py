import logging
from calendar import monthrange
from datetime import date, timedelta

from celery import shared_task
from django.db import transaction

from .models import SystemOwnership, TaxCycle, SystemObligationType, TaxCycleObligation
from .discord_notifications import process_all_tax_cycle_notifications
from .constants import TAXCYCLE_STATUS_PENDING

logger = logging.getLogger(__name__)


def _ensure_cycle_obligations(cycle):
    """Ensure TaxCycleObligation rows exist for active system obligations.
    Returns the number of obligations created.
    Idempotent: will not duplicate existing rows.
    """
    created = 0
    active_types = SystemObligationType.objects.filter(
        system_ownership=cycle.system_ownership,
        is_active=True,
    ).select_related("obligation_type")

    for sot in active_types:
        _, was_created = TaxCycleObligation.objects.get_or_create(
            tax_cycle=cycle,
            obligation_type=sot.obligation_type,
        )
        if was_created:
            created += 1
    return created


def _first_of_month(d):
    return date(d.year, d.month, 1)


def _next_month(d):
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


@shared_task
def generate_monthly_tax_cycles():
    """
    Generate any missing monthly TaxCycle rows up to and including the current month.
    The task is idempotent and relies on the existence check per month.
    """
    today = date.today()

    logger.info("Running monthly tax cycle generation (backfilling missing months up to current month).")

    # Compute current month start; cycles are due at end of the same month (no grace)
    current_month_start = _first_of_month(today)

    created_count = 0
    skipped_count = 0
    error_count = 0
    obligations_created = 0

    # Get all active system ownerships with tax enabled
    active_systems = SystemOwnership.objects.filter(tax_active=True).select_related("system")

    for system_ownership in active_systems:
        try:
            # Always ensure we process the current month
            # Find the earliest month that needs processing
            earliest_existing = (
                TaxCycle.objects.filter(system_ownership=system_ownership)
                .order_by("period_start")
                .values_list("period_start", flat=True)
                .first()
            )

            # Start from earliest existing cycle, or current month if no cycles exist
            start_month = _first_of_month(earliest_existing) if earliest_existing else current_month_start
            # Always end at current month (inclusive)
            end_month = current_month_start
            
            iter_month = start_month

            while iter_month <= end_month:
                last_day = monthrange(iter_month.year, iter_month.month)[1]
                period_start = iter_month
                period_end = date(iter_month.year, iter_month.month, last_day)
                due_date = period_end

                try:
                    with transaction.atomic():
                        # Skip if already exists
                        existing = TaxCycle.objects.filter(
                            system_ownership=system_ownership, period_start=period_start
                        ).first()
                        if existing:
                            skipped_count += 1
                            # Ensure obligations exist for existing cycle
                            obligations_created += _ensure_cycle_obligations(existing)
                        else:
                            # Resolve amount from system default
                            amount = system_ownership.get_current_tax_amount(period_start)
                            if not amount or amount <= 0:
                                logger.debug(
                                    f"No valid default rate for {system_ownership.system.name} on {period_start} - skipping month"
                                )
                                skipped_count += 1
                            else:
                                cycle = TaxCycle.objects.create(
                                    system_ownership=system_ownership,
                                    period_start=period_start,
                                    period_end=period_end,
                                    due_date=due_date,
                                    status=TAXCYCLE_STATUS_PENDING,
                                    target_amount=amount,
                                    notes=f"Auto-generated cycle for {period_start.strftime('%B %Y')}",
                                )
                                created_count += 1
                                # Create obligations for newly created cycle
                                obligations_created += _ensure_cycle_obligations(cycle)
                except Exception as e:
                    logger.error(
                        f"Error creating cycle for {system_ownership.system.name} ({period_start}): {str(e)}"
                    )
                    error_count += 1

                # Next month
                iter_month = _next_month(iter_month)

        except Exception as e:
            logger.error(
                f"Error processing ownership {getattr(system_ownership.system, 'name', system_ownership.pk)}: {str(e)}"
            )
            error_count += 1

    # Log summary
    logger.info(
        f"Tax cycle generation complete. Created: {created_count}, Skipped: {skipped_count}, Errors: {error_count}, Obligations created: {obligations_created}"
    )

    return {
        "created": created_count,
        "skipped": skipped_count,
        "errors": error_count,
        "obligations_created": obligations_created,
        "total_systems": active_systems.count(),
        "run_date": today.isoformat(),
        "range_description": f"Up to and including {current_month_start.strftime('%B %Y')}",
    }


@shared_task
def send_discord_notifications():
    """
    Send Discord notifications for tax cycles based on configured timing:
    - Advance notice (default 7 days before due)
    - Due date notifications
    - Daily overdue notifications
    
    This task should be run daily to ensure timely notifications.
    """
    
    logger.info("Starting Discord notification task")
    
    try:
        summary = process_all_tax_cycle_notifications()
        
        logger.info(
            f"Discord notifications complete. "
            f"Cycles checked: {summary['cycles_checked']}, "
            f"Notifications sent: {summary['notifications_sent']}, "
            f"Failed: {summary['notifications_failed']}, "
            f"Batched messages sent: {summary['batched_messages_sent']}, "
            f"Config found: {summary['config_found']}"
        )
        
        return summary
        
    except Exception as e:
        logger.error(f"Error in Discord notification task: {str(e)}")
        raise
