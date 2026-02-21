# Load Tester Stack

Eigenständiger Load-Tester-Stack mit eigener Compose-Datei.

## Start

```bash
docker compose up -d --build
```

## Stop

```bash
docker compose down
```

## Zugriff

- UI: `http://localhost:8088`
- WS-Proxy läuft intern auf Port `8765`

## Verbindung zum Balancer

Der WS-Proxy rewritet `localhost:8090` auf `host.docker.internal:8090`.
Der Load Balancer muss daher separat laufen (z. B. mit `apps/load-balancer/docker-compose.yml`).
