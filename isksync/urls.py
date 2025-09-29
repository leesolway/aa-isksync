from django.urls import path
from . import views

app_name = "isksync"

urlpatterns = [
    path("", views.MyDueTaxesView.as_view(), name="my_due"),
    path("history/", views.MyPaymentHistoryView.as_view(), name="my_history"),
    path("cycle/<int:pk>/toggle-paid/", views.toggle_user_marked_paid, name="toggle_user_marked_paid"),
    path("obligation/<int:pk>/toggle/", views.toggle_obligation_fulfilled, name="toggle_obligation_fulfilled"),
    path("ownership/<int:pk>/", views.OwnershipDetailView.as_view(), name="ownership_detail"),
    path("manage/", views.ManageView.as_view(), name="manage"),
    path("manage/all/", views.ManageAllCyclesView.as_view(), name="manage_all"),
    path("manage/obligations/", views.ManageAllObligationsView.as_view(), name="manage_all_obligations"),
    path("manage/cycle/<int:pk>/mark-paid/", views.mark_cycle_paid, name="mark_cycle_paid"),
    path("manage/cycle/<int:pk>/set-pending/", views.mark_cycle_pending, name="mark_cycle_pending"),
    path("manage/cycle/<int:pk>/exempt/", views.exempt_cycle, name="exempt_cycle"),
    path("manage/obligation/<int:pk>/toggle/", views.admin_toggle_obligation, name="admin_toggle_obligation"),
]
