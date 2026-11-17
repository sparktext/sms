from django.urls import path
from . import views

app_name = "compliance"

urlpatterns = [
    path("compliance/", views.compliance_dashboard, name="dashboard"),
    path("compliance/record/", views.record_consent_view, name="record_consent"),
    path("compliance/test-keyword/", views.test_keyword_view, name="test_keyword"),
    path("compliance/export/", views.export_ledger_csv, name="export_ledger"),
]

