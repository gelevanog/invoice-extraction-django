from typing import Any

from django import template

register = template.Library()


@register.filter
def get_item(mapping: dict[str, Any], key: str) -> Any:
    return mapping.get(key)


@register.filter
def humanize_code(code: str) -> str:
    return code.replace("_", " ").capitalize()


@register.filter
def percent(rate: float | None) -> str:
    """0.834 -> '83%'; None (nothing measured yet) -> '-'."""
    return "-" if rate is None else f"{rate:.0%}"


@register.filter
def percent_width(rate: float | None) -> str:
    """Width for a CSS meter: 0.834 -> '83.4'."""
    return f"{(rate or 0) * 100:.1f}"
