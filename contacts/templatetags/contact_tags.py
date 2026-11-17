from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """
    Looks up key in dictionary safely: {{ dictionary|get_item:key }}
    """
    if isinstance(dictionary, dict):
        return dictionary.get(key, "")
    return ""


@register.filter
def get_custom_field(contact, key):
    """
    Returns contact's custom field value safely: {{ contact|get_custom_field:key }}
    """
    if hasattr(contact, "custom_fields") and isinstance(contact.custom_fields, dict):
        return contact.custom_fields.get(key, "")
    return ""

