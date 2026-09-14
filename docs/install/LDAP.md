# LDAP

Optional via `django-auth-ldap` / `python-ldap`.

1. Install OpenLDAP devel packages **before** pip (see [REQUIREMENTS.md](REQUIREMENTS.md)).
2. Set `LDAP_SERVER_URI`, bind DN/password, user/group search bases, staff/superuser group DNs in env.
3. Restart `labvault-web`.

Full variable list: `connect/settings.py`. Local Django superusers still work for break-glass.
