from django.shortcuts import render

# Create your views here.


def inbox(request):
    # logic for inbox view
    return render(request, 'message/inbox.html')
