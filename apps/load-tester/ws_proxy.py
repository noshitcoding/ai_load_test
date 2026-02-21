"""
WebSocket-Proxy für den LLM Load Tester.
Browser → WebSocket → dieser Server → HTTP(S) → LiteLLM
Umgeht das Browser-Limit von 6 gleichzeitigen HTTP/1.1 Verbindungen.
"""
__version__ = "1.1.0"

import asyncio
import json
import os
import re
import signal
import ssl
import sys
import traceback
import websockets
import httpx
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
log = logging.getLogger('ws-proxy')

# Connection metrics
_active_connections = 0
_total_connections = 0
_total_requests = 0

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
_LB_LEGACY_CHAT_RE = re.compile(
    r'^(https?://(?:load-balancer:8090|localhost:8090|127\.0\.0\.1:8090))/chat/completions(?=$|[/?#])',
    re.IGNORECASE,
)
_LB_LEGACY_MODELS_RE = re.compile(
    r'^(https?://(?:load-balancer:8090|localhost:8090|127\.0\.0\.1:8090))/models(?=$|[/?#])',
    re.IGNORECASE,
)

def rewrite_url(url: str) -> str:
    """Rewrite localhost URLs to Docker-internal service names."""
    m = _REWRITE_RE.match(url)
    if m:
        new_host = _HOST_REWRITES.get(m.group(2), m.group(2))
        rewritten = m.group(1) + new_host + m.group(3)
        log.info(f"URL rewrite: {url[:60]} → {rewritten[:60]}")
        url = rewritten

    fixed = _LB_LEGACY_CHAT_RE.sub(r'\1/v1/chat/completions', url)
    fixed = _LB_LEGACY_MODELS_RE.sub(r'\1/v1/models', fixed)
    if fixed != url:
        log.info(f"Path rewrite: {url[:80]} → {fixed[:80]}")
    return fixed


async def handle_request(ws, msg):
    """Einzelnen Request verarbeiten: HTTP-Call machen, Antwort zurücksenden."""
    global _total_requests
    _total_requests += 1
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
    global _active_connections, _total_connections
    _active_connections += 1
    _total_connections += 1
    log.info(f"WS verbunden: {ws.remote_address} (active={_active_connections})")
    tasks = set()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "error": "Invalid JSON"}))
                continue
            task = asyncio.create_task(handle_request(ws, msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except websockets.exceptions.ConnectionClosed:
        log.info(f"WS getrennt: {ws.remote_address}")
    finally:
        _active_connections -= 1
        for t in tasks:
            t.cancel()


_shutdown_event = asyncio.Event()


async def main():
    print(f"WS-Proxy v{__version__} gestartet auf :8765", flush=True)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Graceful shutdown handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: _shutdown_event.set())
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    async with websockets.serve(
        handler,
        "0.0.0.0",
        8765,
        max_size=10 * 1024 * 1024,
        ping_interval=30,
        ping_timeout=10,
    ) as server:
        log.info(f"WS-Proxy ready (pid={os.getpid()})")
        await _shutdown_event.wait()
        log.info("Shutting down gracefully...")
        server.close()
        await server.wait_closed()
        # Close shared HTTP client
        global http_client
        if http_client and not http_client.is_closed:
            await http_client.aclose()
        log.info(f"Shutdown complete. Total connections: {_total_connections}, requests: {_total_requests}")


if __name__ == "__main__":
    asyncio.run(main())
