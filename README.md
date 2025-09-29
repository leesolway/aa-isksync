# aa-isksync (AllianceAuth ISK Sync)

A lightweight AllianceAuth module to manage ISK rent agreements for EVE Online solar systems. It tracks system ownership, monthly rent (tax) cycles.

Features
- System ownership records with ownership modes
- Monthly TaxCycle generation task (Celery)
- **Discord notifications** for tax reminders with configurable timing
- Dashboard widget with per-user scoped stats
- Member views: Due this month, Agreements, History
- Admin view: All cycles with quick actions and Discord test notifications
- AllianceAuth navigation/menu integration
- Simple pricing: set a default monthly amount on each system; the amount is stored on each cycle
- Role-based Discord mentions with flexible webhook templates

```python
# Monthly tax cycle generation
CELERY_BEAT_SCHEDULE.setdefault('isksync_generate_monthly_tax_cycles', {
    'task': 'isksync.tasks.generate_monthly_tax_cycles',
    # Run on the 1st of each month at 02:00 server time
    'schedule': crontab(minute=0, hour=2, day_of_month=1),
})

# Discord notifications (optional, requires Discord setup)
CELERY_BEAT_SCHEDULE.setdefault('isksync_discord_notifications', {
    'task': 'isksync.tasks.send_discord_notifications', 
    # Run daily at 09:00 server time to check for due notifications
    'schedule': crontab(minute=0, hour=9),
})
```
