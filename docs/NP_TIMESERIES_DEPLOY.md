# NP Resource Time-Series — Production Deploy

## Database

The `NPResourceSample` model is stored in **`np_timeseries`** (default file: `np_timeseries.sqlite3` next to `manage.py`). Override with:

```bash
export NP_TIMESERIES_DATABASE_URL=sqlite:////opt/labvault/np_timeseries.sqlite3
```

## Migrations

After deploying code:

```bash
cd /opt/labvault
source .venv/bin/activate
python manage.py migrate --database np_timeseries
python manage.py migrate   # default DB if other migrations pending
```

## Service

Restart the LabVault application server (gunicorn/uwsgi/systemd) so the background refresh thread picks up `_collect_np_samples`.

## Retention (cron)

Purge samples older than 31 days daily:

```cron
0 3 * * * /opt/labvault/.venv/bin/python /opt/labvault/manage.py cleanup_np_timeseries
```

## Verification

- Open a chassis detail page → **NP Resource History** should load charts (after ~2 minutes of online chassis for first points).
- `GET /keysight/api/chassis/<id>/np-timeseries/?range=24h` should return `{ "range", "series" }`.
