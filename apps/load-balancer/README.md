# Load Balancer Stack

Eigenständiger Load-Balancer-Stack mit eigener Compose-Datei.

## Start

```bash
docker compose up -d --build
```

## Stop

```bash
docker compose down
```

## Endpoints

- Admin UI: `http://localhost:8090/admin`
- Health: `http://localhost:8090/health`
- Metrics: `http://localhost:8090/metrics`

## Wichtige ENV

- `LB_ADMIN_TOKEN`
- `LB_CLIENT_TOKENS`
- `LB_CORS_ORIGINS`
- `LB_CACHE_TTL_SECONDS`
- `LB_ADMIN_RATE_LIMIT_RPM`
