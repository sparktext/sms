from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/chat/(?P<conversation_id>\w+)/$", consumers.ChatConsumer.as_asgi()),
    re_path(r"^ws/inbox/(?P<org_id>\w+)/$", consumers.InboxConsumer.as_asgi()),
]

