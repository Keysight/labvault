"""Template context for the customer LabVault SKU."""
from django.conf import settings


def global_context(request):
    """Variables available in every template (registered in ``TEMPLATES`` settings).

    Returns product flags plus two auth-derived booleans used by ``base.html``:
    ``password_change_allowed`` (hide the Password link for locked accounts) and
    ``show_breakglass_password_reset`` (show the ADMIN > Reset user password link).
    """
    user = getattr(request, "user", None)
    authed = bool(getattr(user, "is_authenticated", False))
    uname = (getattr(user, "username", None) or "").strip().lower() if authed else ""
    locked = getattr(settings, "LABVAULT_PASSWORD_LOCKED_USERNAMES", frozenset())
    breakglass = getattr(settings, "LABVAULT_PASSWORD_BREAKGLASS_USERNAMES", frozenset())
    return {
        "labvault_customer_sku": True,
        "labvault_cli_enabled": bool(getattr(settings, "LABVAULT_CLI_ENABLED", True)),
        "product_name": "LabVault",
        "support_no_sla": True,
        "password_change_allowed": authed and uname not in locked,
        "show_breakglass_password_reset": authed and uname in breakglass,
    }
