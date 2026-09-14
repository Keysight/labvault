# Testing

```bash
export DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
python manage.py test connect.tests --verbosity=1
python tools/check_public_source.py
```

Deploy smoke: `/health/ready`, `/login/`, staff `/cli/` invoke `diag cheap`.
