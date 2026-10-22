from django.urls import path

from apps.routes import views

app_name = "routes"

urlpatterns = [
    path("routes/", views.RouteListCreateView.as_view(), name="route-list-create"),
    path("routes/<uuid:pk>/", views.RouteDetailView.as_view(), name="route-detail"),
    path("routes/<uuid:pk>/customers/", views.RouteCustomersView.as_view(), name="route-customers"),
    path("standing-orders/", views.StandingOrderListCreateView.as_view(), name="standing-order-list-create"),
    path("standing-orders/<uuid:pk>/", views.StandingOrderDetailView.as_view(), name="standing-order-detail"),
    path("route-runs/", views.RunListStartView.as_view(), name="run-list-start"),
    path("route-runs/<uuid:pk>/tasks/", views.RunTasksView.as_view(), name="run-tasks"),
    path("route-runs/<uuid:pk>/entries/", views.RunEntryView.as_view(), name="run-entry"),
    path("route-runs/<uuid:pk>/close/", views.RunCloseView.as_view(), name="run-close"),
    path("route-runs/<uuid:pk>/reopen/", views.RunReopenView.as_view(), name="run-reopen"),
]
