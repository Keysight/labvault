"""Auth-related views (password change restrictions)."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import ValidationError

from connect.password_policy import set_user_password, validate_password_for_user
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods



def is_password_locked_user(user) -> bool:
    if not getattr(user, 'is_authenticated', False):
        return False
    uname = (getattr(user, 'username', None) or '').lower()
    locked = getattr(settings, 'LABVAULT_PASSWORD_LOCKED_USERNAMES', frozenset())
    return uname in locked


def is_breakglass_user(user) -> bool:
    if not getattr(user, 'is_authenticated', False):
        return False
    uname = (getattr(user, 'username', None) or '').lower()
    names = getattr(settings, 'LABVAULT_PASSWORD_BREAKGLASS_USERNAMES', frozenset())
    return uname in names


def _breakglass_password_reset_candidates(request):
    """Active users a break-glass account may set a new password for (LabVault UI)."""
    User = get_user_model()
    breakglass = getattr(settings, 'LABVAULT_PASSWORD_BREAKGLASS_USERNAMES', frozenset())
    resettable = getattr(settings, 'LABVAULT_BREAKGLASS_RESETTABLE_USERNAMES', frozenset())
    actor_lower = (request.user.username or '').lower()
    out = []
    for u in User.objects.filter(is_active=True).order_by('username'):
        ul = (u.username or '').lower()
        if ul in breakglass and ul != actor_lower:
            continue
        if ul in resettable:
            out.append(u)
        elif u.is_superuser and ul not in breakglass:
            out.append(u)
    return out


@require_http_methods(['GET', 'POST'])
def breakglass_reset_user_password(request):
    """Break-glass only: set another user's password (mgmt/finance/admin, etc.)."""
    if not request.user.is_authenticated:
        return redirect(settings.LOGIN_URL)
    if not is_breakglass_user(request.user):
        return HttpResponseForbidden('This tool is only for break-glass accounts.')

    candidates = _breakglass_password_reset_candidates(request)
    if request.method == 'POST':
        uid = request.POST.get('user_id', '').strip()
        p1 = request.POST.get('password1', '')
        p2 = request.POST.get('password2', '')
        allowed_ids = {str(u.pk) for u in candidates}
        if uid not in allowed_ids:
            messages.error(request, 'Invalid user selection.')
        elif p1 != p2:
            messages.error(request, 'The two password fields did not match.')
        elif not p1:
            messages.error(request, 'Password is required.')
        else:
            target = get_user_model().objects.get(pk=int(uid))
            if target.pk not in {u.pk for u in candidates}:
                messages.error(request, 'You cannot reset that user.')
            else:
                try:
                    validate_password_for_user(p1, target)
                except ValidationError as e:
                    for err in e.messages:
                        messages.error(request, err)
                else:
                    set_user_password(target, p1, validate=False)
                    messages.success(
                        request,
                        f'Password updated for {target.username}. They can log in immediately.',
                    )
                    return redirect('breakglass_reset_user_password')

    return render(
        request,
        'connect/auth/breakglass_reset_password.html',
        {'reset_candidates': candidates},
    )


class LabvaultPasswordChangeView(PasswordChangeView):
    template_name = 'connect/auth/password_change_form.html'
    success_url = reverse_lazy('password_change_done')

    def dispatch(self, request, *args, **kwargs):
        if is_password_locked_user(request.user):
            messages.warning(
                request,
                'Password changes are not available for this account.',
            )
            return redirect('dashboard')
        if False:  # HARD_DUMP_REMOVED capex password gate
            messages.warning(
                request,
                'Password self-service is not available for Capex requester accounts. '
                'Contact an administrator to reset your password.',
            )
            return redirect('dashboard')
        return super().dispatch(request, *args, **kwargs)
