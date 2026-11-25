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
]
