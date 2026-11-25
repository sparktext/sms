from django.urls import path

from . import views

app_name = "scheduling"

urlpatterns = [
    path("calendar/", views.calendar_page, name="calendar"),
    path("calendar/events/", views.event_feed, name="event_feed"),
    path("calendar/events/create/", views.event_create, name="event_create"),
    path("calendar/events/<int:event_id>/update/", views.event_update, name="event_update"),
    path("calendar/events/<int:event_id>/delete/", views.event_delete, name="event_delete"),
    path("calendar/timezone/", views.set_my_timezone, name="set_timezone"),
    path("calendar/calls/options/", views.call_options, name="call_options"),
    path("calendar/calls/schedule/", views.schedule_call, name="schedule_call"),
    path("calendar/calls/unclaimed/", views.unclaimed_calls, name="unclaimed_calls"),
    path("calendar/calls/<int:event_id>/claim/", views.claim_call, name="claim_call"),
    path("calendar/closed-days/", views.closed_days, name="closed_days"),
    path("calls/hot/", views.hot_leads, name="hot_leads"),
    path("calls/hot/<int:event_id>/answer/", views.answer_hot_lead, name="answer_hot_lead"),
    path("calls/hot/<int:event_id>/ignore/", views.ignore_hot_lead, name="ignore_hot_lead"),
]
