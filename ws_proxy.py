"""
WebSocket-Proxy für den LLM Load Tester.
Browser → WebSocket → dieser Server → HTTP(S) → LiteLLM
Umgeht das Browser-Limit von 6 gleichzeitigen HTTP/1.1 Verbindungen.
"""
import asyncio
import json
import os
import re
import ssl
import sys
import traceback
import websockets
import httpx
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
log = logging.getLogger('ws-proxy')

# SSL-Verifikation deaktivieren (für self-signed LiteLLM)
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# Shared async HTTP client — kein Connection-Limit
http_client = None

async def get_client():
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=500,
                max_keepalive_connections=100,
            ),
            http2=True,
        )
    return http_client


# Docker-interne URL-Rewrite-Map:
# Browser sagt "localhost:8090" → ws_proxy muss "load-balancer:8090" nehmen
_HOST_REWRITES: dict[str, str] = {}

def _build_host_rewrites() -> dict[str, str]:
    """Lade optionale Host-Rewrites aus Umgebungsvariable WS_HOST_REWRITES.
    Format: 'localhost:8090=load-balancer:8090,localhost:4000=litellm:4000'
    Zusätzlich automatisch: localhost/127.0.0.1 auf Port 8090 → load-balancer:8090
    """
    m: dict[str, str] = {
        'localhost:8090': 'load-balancer:8090',
        '127.0.0.1:8090': 'load-balancer:8090',
    }
    raw = os.environ.get('WS_HOST_REWRITES', '')
    for pair in raw.split(','):
        pair = pair.strip()
        if '=' in pair:
            src, dst = pair.split('=', 1)
            m[src.strip()] = dst.strip()
    return m

_HOST_REWRITES = _build_host_rewrites()
_REWRITE_RE = re.compile(
    r'^(https?://)(' + '|'.join(re.escape(k) for k in _HOST_REWRITES) + r')(/.*)$',
    re.IGNORECASE,
)

def rewrite_url(url: str) -> str:
    """Rewrite localhost URLs to Docker-internal service names."""
    m = _REWRITE_RE.match(url)
    if m:
        new_host = _HOST_REWRITES.get(m.group(2), m.group(2))
        rewritten = m.group(1) + new_host + m.group(3)
        log.info(f"URL rewrite: {url[:60]} → {rewritten[:60]}")
        return rewritten
    return url


async def handle_request(ws, msg):
    """Einzelnen Request verarbeiten: HTTP-Call machen, Antwort zurücksenden."""
    req_id = msg.get("id", "?")
    try:
        url = rewrite_url(msg["url"])
        method = msg.get("method", "POST")
        headers = msg.get("headers", {})
        body = msg.get("body")
        stream = msg.get("stream", False)
        log.info(f"[{req_id}] {method} {url[:80]} stream={stream}")

        client = await get_client()

        if stream:
            # Streaming: Chunks einzeln weiterleiten
            async with client.stream(
                method, url, headers=headers, content=body
            ) as resp:
                # Erste Antwort: Status + Headers
                resp_headers = dict(resp.headers)
                await ws.send(json.dumps({
                    "id": req_id,
                    "type": "stream_start",
                    "status": resp.status_code,
                    "headers": resp_headers,
                }))
                # Chunks weiterleiten
                async for chunk in resp.aiter_bytes():
                    await ws.send(json.dumps({
                        "id": req_id,
                        "type": "stream_chunk",
                        "data": chunk.decode("utf-8", errors="replace"),
                    }))
                # Stream Ende
                await ws.send(json.dumps({
                    "id": req_id,
                    "type": "stream_end",
                }))
        else:
            # Nicht-Streaming: komplette Antwort
            resp = await client.request(
                method, url, headers=headers, content=body
            )
            resp_headers = dict(resp.headers)
            log.info(f"[{req_id}] → HTTP {resp.status_code} ({len(resp.text)} bytes)")
            await ws.send(json.dumps({
                "id": req_id,
                "type": "response",
                "status": resp.status_code,
                "headers": resp_headers,
                "body": resp.text,
            }))

    except Exception as e:
        log.error(f"[{req_id}] ERROR: {e}")
        traceback.print_exc()
        await ws.send(json.dumps({
            "id": req_id,
            "type": "error",
            "error": str(e),
        }))


async def handler(ws):
    """WebSocket-Verbindung verwalten. Jede Nachricht = 1 HTTP-Request."""
    log.info(f"WS verbunden: {ws.remote_address}")
    tasks = set()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "error": "Invalid JSON"}))
                continue
            # Jeden Request als eigene Aufgabe starten (parallel)
            task = asyncio.create_task(handle_request(ws, msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except websockets.exceptions.ConnectionClosed:
        log.info(f"WS getrennt: {ws.remote_address}")
    finally:
        # Offene Requests bei Disconnect abbrechen
        for t in tasks:
            t.cancel()


async def main():
    print("WS-Proxy gestartet auf :8765", flush=True)
    # Suppress noisy websockets internal logs
    logging.getLogger("websockets").setLevel(logging.WARNING)
    async with websockets.serve(
        handler,
        "0.0.0.0",
        8765,
        max_size=10 * 1024 * 1024,  # 10 MB max message
        ping_interval=30,
        ping_timeout=10,
    ):
        await asyncio.Future()  # forever


if __name__ == "__main__":
    asyncio.run(main())
