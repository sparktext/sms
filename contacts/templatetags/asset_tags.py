"""Static file URLs that change whenever the file changes, so browsers never keep an old stylesheet."""
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path):
    """{% static_v 'css/app.css' %} -> /static/css/app.css?v=1764057600 (the file's last change).
    After a CSS rebuild the URL changes, so the browser fetches the new file instead of its cached copy."""
    url = static(path)
    try:
        found = finders.find(path)
        if isinstance(found, (list, tuple)):
            found = found[0] if found else None
        if found:
            return f"{url}?v={int(os.path.getmtime(found))}"
    except Exception:
        pass
    return url
