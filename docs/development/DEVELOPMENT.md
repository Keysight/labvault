# Development

```bash
sudo ./labvaultctl host-deps --install   # once
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp -n .env.example .env                  # strong DJANGO_SECRET_KEY
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Public gate: `python tools/check_public_source.py`  
Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
