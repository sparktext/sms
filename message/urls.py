from django.urls import path
from . import views

app_name = 'message'

urlpatterns = [
    path('', views.inbox, name='inbox'),           # /messages/       → list all messages
    
]
