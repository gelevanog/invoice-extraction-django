from typing import Any

from django import template

register = template.Library()


@register.filter
def get_item(mapping: dict[str, Any], key: str) -> Any:
    return mapping.get(key)


@register.filter
def humanize_code(code: str) -> str:
    return code.replace("_", " ").capitalize()
