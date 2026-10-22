from django.urls import path

from apps.businesses import api_views

app_name = "businesses"

urlpatterns = [
    path("businesses/<uuid:business_id>/", api_views.BusinessDetailAPIView.as_view(), name="business-detail"),
    path("businesses/<uuid:business_id>/restore/", api_views.BusinessRestoreAPIView.as_view(), name="business-restore"),
    path("businesses/<uuid:business_id>/staff/", api_views.StaffListCreateAPIView.as_view(), name="staff-list-create"),
    path("businesses/<uuid:business_id>/staff/<uuid:membership_id>/", api_views.StaffDetailAPIView.as_view(), name="staff-detail"),
    path(
        "businesses/<uuid:business_id>/staff/<uuid:membership_id>/password-reset-code/",
        api_views.StaffResetCodeAPIView.as_view(),
        name="staff-reset-code",
    ),
]
