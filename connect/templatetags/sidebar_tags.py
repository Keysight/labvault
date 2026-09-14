"""Template tags for sidebar group ID generation."""
import re
from django import template

register = template.Library()


@register.simple_tag
def sidebar_group_id(label):
    """Convert a chassis type display label to a slug suitable for DOM IDs.
    e.g. 'XGS2' -> 'xgs2', 'APS-M8400' -> 'aps-m8400', 'XG (Legacy)' -> 'xg-legacy'
    """
    slug = label.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug
