from django.apps import AppConfig


class IskSyncConfig(AppConfig):
    name = "isksync"

    def ready(self):
        try:
            from . import auth_hooks  # noqa: F401
        except Exception:
            pass
