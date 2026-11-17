from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from . import views_settings

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="core/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # Settings & Organization
    path("settings/", views_settings.settings_redirect, name="settings_index"),
    path("settings/profile/", views_settings.company_profile, name="company_profile"),
    path("settings/team/", views_settings.team_members, name="team_members"),
    path("settings/sounds/", views_settings.notification_sound_settings, name="notification_sound_settings"),
    path("settings/team/invite/", views_settings.team_invite_create, name="team_invite_create"),
    path("settings/team/invites/<int:invite_id>/revoke/", views_settings.team_invite_revoke, name="team_invite_revoke"),
    path("settings/team/members/<int:user_id>/role/", views_settings.team_member_update_role, name="team_member_update_role"),
    path("settings/team/members/<int:user_id>/remove/", views_settings.team_member_remove, name="team_member_remove"),

    # Lead Distribution & Routing Settings
    path("settings/routing/", views_settings.lead_routing_settings, name="lead_routing_settings"),
    path("settings/availability/toggle/", views_settings.toggle_agent_availability, name="toggle_my_availability"),
    path("settings/team/members/<int:user_id>/availability/", views_settings.toggle_agent_availability, name="toggle_agent_availability"),

    # Public Invite Acceptance
    path("join/<str:token>/", views_settings.accept_invite, name="accept_invite"),
]
