from django.apps import AppConfig


class IskSyncConfig(AppConfig):
    name = "isksync"

    def ready(self):
        # Ensure hooks are imported so AA can discover dashboard widgets
        try:
            from . import auth_hooks  # noqa: F401
        except Exception:
            # Avoid crashing app startup if hooks import fails in migrations
            pass
