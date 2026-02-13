"""Tests for the v3 Load Balancer (enterprise overhaul)."""

import json
import time
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from load_balancer import (
    AppSettings,
    create_app,
    SlidingWindow,
    TTLCache,
    RateLimiter,
    __version__,
)

ADMIN_TOKEN = "admin-test-token"
CLIENT_TOKEN = "client-test-token"


def make_settings(
    db_path: Path,
    fail_threshold: int = 3,
    cooldown_seconds: float = 20.0,
    client_tokens: set[str] | None = None,
    **kw,
) -> AppSettings:
    return AppSettings(
        db_path=str(db_path),
        port=8090,
        admin_token=ADMIN_TOKEN,
        client_tokens=client_tokens if client_tokens is not None else {CLIENT_TOKEN},
        fail_threshold=fail_threshold,
        cooldown_seconds=cooldown_seconds,
        max_connections=200,
        max_keepalive=50,
        default_timeout_seconds=30.0,
        cache_max_entries=1000,
        log_level="INFO",
        **kw,
    )


def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


def client_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CLIENT_TOKEN}"}


def create_endpoint(
    client: TestClient,
    name: str,
    api_key: str = "test-key",
    connected: bool = True,
    **kw,
) -> dict:
    payload = {
        "name": name,
        "base_url": "https://upstream.example/v1",
        "api_key": api_key,
        "connected": connected,
        **kw,
    }
    r = client.post(
        "/admin/api/endpoints", headers=admin_headers(), json=payload
    )
    assert r.status_code == 200, r.text
    return r.json()


def ok_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 110},
        },
        headers={"content-type": "application/json"},
    )


def ok_transport() -> httpx.MockTransport:
    return httpx.MockTransport(ok_handler)


# ══════════════════════════════════════════════════════════════════════════════
#  Original Tests (1-15) – preserved from v2
# ══════════════════════════════════════════════════════════════════════════════


# ── 1. Admin auth ─────────────────────────────────────────────────────────


def test_admin_api_requires_token(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        assert c.get("/admin/api/state").status_code == 401
        r = c.get("/admin/api/state", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert "routing_mode" in body
        assert "endpoints" in body


# ── 2. Endpoint CRUD + key masking ────────────────────────────────────────


def test_endpoint_crud_and_masking(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        ep = create_endpoint(c, "ep-secret", "super-secret-api-key")
        assert "api_key" not in ep
        assert ep["has_api_key"] is True
        assert ep["api_key_preview"].startswith("su")
        eid = ep["id"]

        r = c.patch(f"/admin/api/endpoints/{eid}", headers=admin_headers(), json={"token_quota_percent": 75})
        assert r.status_code == 200
        assert r.json()["token_quota_percent"] == 75
        assert "api_key" not in r.json()

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(state["endpoints"]) == 1
        assert "api_key" not in state["endpoints"][0]
        assert state["endpoints"][0]["has_api_key"] is True

        r = c.delete(f"/admin/api/endpoints/{eid}", headers=admin_headers())
        assert r.status_code == 200
        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(state["endpoints"]) == 0


# ── 3. Percentage routing ─────────────────────────────────────────────────


def test_percentage_routing_distributes_by_quota(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("Authorization", ""))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 110},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-a", "key-a", token_quota_percent=70)
        create_endpoint(c, "ep-b", "key-b", token_quota_percent=30)
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, "stream": False}
        for _ in range(20):
            r = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
            assert r.status_code == 200

    a = seen.count("Bearer key-a")
    b = seen.count("Bearer key-b")
    assert a > b, f"Expected ep-a ({a}) > ep-b ({b})"
    assert a >= 12, f"Expected ep-a >= 12, got {a}"


# ── 4. Priority routing ──────────────────────────────────────────────────


def test_priority_routing_prefers_primary(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("Authorization", ""))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 20, "total_tokens": 25},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        c.patch("/admin/api/routing", headers=admin_headers(), json={"routing_mode": "priority"})
        create_endpoint(c, "primary", "key-primary", priority_order=1)
        create_endpoint(c, "secondary", "key-secondary", priority_order=10)

        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, "stream": False}
        for _ in range(8):
            r = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
            assert r.status_code == 200

    assert all(a == "Bearer key-primary" for a in seen), seen


# ── 5. Token tracking ────────────────────────────────────────────────────


def test_token_tracking_records_usage(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 200, "total_tokens": 250},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-tok", "key-tok")
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, "stream": False}
        for _ in range(3):
            r = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
            assert r.status_code == 200

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        ep = state["endpoints"][0]
        assert ep["prompt_tokens_total"] == 150
        assert ep["completion_tokens_total"] == 600
        assert ep["total_requests"] == 3
        assert ep["total_success"] == 3


# ── 6. Cooldown / failover ───────────────────────────────────────────────


def test_cooldown_and_failover(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        if auth == "Bearer bad-key":
            return httpx.Response(500, json={"error": "fail"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    settings = make_settings(tmp_path / "lb.db", fail_threshold=1, cooldown_seconds=90)
    app = create_app(settings=settings, transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-bad", "bad-key")
        create_endpoint(c, "ep-good", "good-key")
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, "stream": False}

        first = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
        assert first.status_code == 500

        second = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
        assert second.status_code == 200
        assert second.headers.get("X-LB-Endpoint") == "ep-good"

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        bad = next(e for e in state["endpoints"] if e["name"] == "ep-bad")
        assert bad["cooldown_remaining_s"] > 0


# ── 7. Streaming passthrough ─────────────────────────────────────────────


def test_streaming_response_passthrough(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content.decode("utf-8"))
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-stream", "stream-key")
        payload = {"model": "demo", "messages": [{"role": "user", "content": "stream"}], "max_tokens": 4, "stream": True}
        r = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
        assert r.status_code == 200
        assert "data:" in r.text
        assert r.headers.get("X-LB-Endpoint") == "ep-stream"


# ── 8. Persistence across restart ────────────────────────────────────────


def test_persistence_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "lb-persist.db"
    transport = ok_transport()

    app1 = create_app(settings=make_settings(db), transport=transport)
    with TestClient(app1) as c:
        create_endpoint(c, "ep-persist", "persist-secret-key", token_quota_percent=42)

    app2 = create_app(settings=make_settings(db), transport=transport)
    with TestClient(app2) as c:
        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(state["endpoints"]) == 1
        ep = state["endpoints"][0]
        assert ep["name"] == "ep-persist"
        assert ep["token_quota_percent"] == 42
        assert "api_key" not in ep
        assert ep["has_api_key"] is True
        assert ep["api_key_preview"].startswith("pe")


# ── 9. Models aggregation ────────────────────────────────────────────────


def test_models_aggregated(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        if auth == "Bearer key-a":
            return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-shared"}]})
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-shared"}]})

    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "target-a", "key-a")
        create_endpoint(c, "target-b", "key-b")
        r = c.get("/v1/models", headers=client_headers())
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["model-a", "model-b", "model-shared"]


# ── 10. Client token CRUD ────────────────────────────────────────────────


def test_client_token_crud(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert r.status_code == 200
        assert r.json() == []

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "load-tester-1"})
        assert r.status_code == 200
        t1 = r.json()
        assert t1["name"] == "load-tester-1"
        assert t1["token"].startswith("ct-")
        assert len(t1["token"]) > 20
        tid = t1["id"]

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "custom-tok", "token": "my-custom-token-123"})
        assert r.status_code == 200
        t2 = r.json()
        assert t2["token"] == "my-custom-token-123"

        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert r.status_code == 200
        tokens = r.json()
        assert len(tokens) == 2
        assert any(tk["token"] == t1["token"] for tk in tokens)

        r = c.get("/admin/api/state", headers=admin_headers())
        assert r.status_code == 200
        state_tokens = r.json()["client_tokens"]
        assert len(state_tokens) == 2
        assert "token" not in state_tokens[0]
        assert "token_preview" in state_tokens[0]

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "dup", "token": "my-custom-token-123"})
        assert r.status_code == 409

        r = c.delete(f"/admin/api/tokens/{tid}", headers=admin_headers())
        assert r.status_code == 200
        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert len(r.json()) == 1


def test_multiple_incoming_blocks_and_token_assignment(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db", client_tokens=set()), transport=ok_transport())
    with TestClient(app) as c:
        base_state = c.get("/admin/api/state", headers=admin_headers())
        assert base_state.status_code == 200
        blocks = base_state.json()["incoming_blocks"]
        assert len(blocks) == 1
        default_block_id = blocks[0]["id"]

        r = c.post("/admin/api/incoming-blocks", headers=admin_headers(), json={"name": "Setup B", "pos_x": 300, "pos_y": 420})
        assert r.status_code == 200, r.text
        block_b_id = r.json()["id"]
        assert block_b_id != default_block_id

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "parallel-source", "token": "parallel-source-token", "incoming_block_id": block_b_id})
        assert r.status_code == 200, r.text
        token_id = r.json()["id"]

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(state["incoming_blocks"]) == 2
        token = next(t for t in state["client_tokens"] if t["id"] == token_id)
        assert token["incoming_block_id"] == block_b_id

        r = c.delete(f"/admin/api/incoming-blocks/{block_b_id}", headers=admin_headers())
        assert r.status_code == 200, r.text

        state_after = c.get("/admin/api/state", headers=admin_headers()).json()
        token_after = next(t for t in state_after["client_tokens"] if t["id"] == token_id)
        assert token_after["incoming_block_id"] == default_block_id


# ── 11. Auth via DB-managed client token ─────────────────────────────────


def test_auth_via_db_client_token(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db", client_tokens=set()), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-auth", "key-auth")
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}

        r = c.post("/v1/chat/completions", json=payload)
        assert r.status_code == 200

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "tester", "token": "db-secret-token"})
        assert r.status_code == 200

        r = c.post("/v1/chat/completions", json=payload)
        assert r.status_code == 401

        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer wrong-token"}, json=payload)
        assert r.status_code == 401

        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer db-secret-token"}, json=payload)
        assert r.status_code == 200


# ── 12. Token-level route + priority ────────────────────────────────────


def test_db_token_routes_to_mapped_endpoint_and_sets_priority(tmp_path: Path) -> None:
    seen_auth: list[str] = []
    seen_priority: list[int | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("Authorization", ""))
        body = json.loads(req.content.decode("utf-8"))
        seen_priority.append(body.get("priority"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db", client_tokens=set()), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "target-1", "key-1")
        ep2 = create_endpoint(c, "target-2", "key-2")

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "source-1", "token": "source-1-token", "preferred_endpoint_id": ep2["id"], "request_priority": 200})
        assert r.status_code == 200, r.text
        tok = r.json()
        assert tok["preferred_endpoint_id"] == ep2["id"]
        assert tok["request_priority"] == 200

        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer source-1-token"}, json=payload)
        assert r.status_code == 200, r.text

    assert seen_auth
    assert seen_auth[0] == "Bearer key-2"
    assert all(v == "Bearer key-2" for v in seen_auth)
    assert seen_priority
    assert seen_priority[0] == 200


def test_token_min_traffic_cache_rest(tmp_path: Path) -> None:
    upstream_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db", client_tokens=set()), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "ep-cache", "key-cache")
        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "quota-token", "token": "quota-token-value", "min_traffic_percent": 50})
        assert r.status_code == 200, r.text

        headers = {"Authorization": "Bearer quota-token-value"}
        payload = {"model": "demo", "messages": [{"role": "user", "content": "same prompt"}], "stream": False}

        first = c.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.headers.get("X-LB-Cache") == "MISS"

        second = c.post("/v1/chat/completions", headers=headers, json=payload)
        assert second.status_code == 200
        assert second.headers.get("X-LB-Cache") == "HIT"

        miss_payload = {"model": "demo", "messages": [{"role": "user", "content": "new uncached prompt"}], "stream": False}
        third = c.post("/v1/chat/completions", headers=headers, json=miss_payload)
        assert third.status_code == 503
        assert "cache miss" in third.text.lower()

    assert upstream_calls == 1


def test_priority_mode_with_prioritized_input_token(tmp_path: Path) -> None:
    seen_auth: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("Authorization", ""))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            headers={"content-type": "application/json"},
        )

    app = create_app(settings=make_settings(tmp_path / "lb.db", client_tokens=set()), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        create_endpoint(c, "primary", "key-primary", priority_order=1)
        create_endpoint(c, "secondary", "key-secondary", priority_order=10)

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "prio-in", "token": "prio-in-token"})
        assert r.status_code == 200, r.text
        prio_tok_id = r.json()["id"]

        r = c.post("/admin/api/tokens", headers=admin_headers(), json={"name": "other-in", "token": "other-in-token"})
        assert r.status_code == 200, r.text

        r = c.patch("/admin/api/routing", headers=admin_headers(), json={"routing_mode": "priority", "priority_input_token_id": prio_tok_id})
        assert r.status_code == 200, r.text
        assert r.json()["routing_mode"] == "priority"
        assert r.json()["priority_input_token_id"] == prio_tok_id

        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}

        rp = c.post("/v1/chat/completions", headers={"Authorization": "Bearer prio-in-token"}, json=payload)
        assert rp.status_code == 200, rp.text

        ro = c.post("/v1/chat/completions", headers={"Authorization": "Bearer other-in-token"}, json=payload)
        assert ro.status_code == 200, ro.text

    assert len(seen_auth) >= 2
    assert seen_auth[0] == "Bearer key-primary"
    assert seen_auth[1] == "Bearer key-secondary"


# ══════════════════════════════════════════════════════════════════════════════
#  New Tests (16-35) – v3 enterprise features
# ══════════════════════════════════════════════════════════════════════════════


# ── 16. Health endpoint ──────────────────────────────────────────────────


def test_health_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        # No endpoints connected → degraded
        r = c.get("/health")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "degraded"
        assert body["version"] == __version__
        assert body["connected"] == 0

        # Add connected endpoint → healthy
        create_endpoint(c, "ep-health", "key")
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["connected"] == 1
        assert "cache_size" in body


# ── 17. HEAD /health ─────────────────────────────────────────────────────


def test_health_head(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.head("/health")
        assert r.status_code == 503  # no connected endpoints
        assert r.text == "" or len(r.content) == 0

        create_endpoint(c, "ep-h", "k")
        r = c.head("/health")
        assert r.status_code == 200


# ── 18. Prometheus metrics ───────────────────────────────────────────────


def test_metrics_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        create_endpoint(c, "ep-met", "key-met")
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        text = r.text
        assert "lb_requests_total" in text
        assert "lb_errors_total" in text
        assert "lb_endpoint_requests_total" in text


# ── 19. Security headers ────────────────────────────────────────────────


def test_security_headers_present(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "X-Request-ID" in r.headers


# ── 20. Request ID tracking ─────────────────────────────────────────────


def test_request_id_tracking(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        # Auto-generated request ID
        r = c.get("/health")
        rid1 = r.headers.get("X-Request-ID", "")
        assert len(rid1) > 0

        # Client-provided request ID is echoed
        r = c.get("/health", headers={"X-Request-ID": "my-custom-id-42"})
        assert r.headers.get("X-Request-ID") == "my-custom-id-42"


# ── 21. Root redirects to admin ──────────────────────────────────────────


def test_root_redirects_to_admin(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code in (301, 302, 307, 308)
        assert "/admin" in r.headers.get("location", "")


# ── 22. Admin page returns HTML ──────────────────────────────────────────


def test_admin_page_returns_html(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.get("/admin")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "Load Balancer" in r.text


# ── 23. Cache clear endpoint ────────────────────────────────────────────


def test_cache_clear_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        # Requires admin
        r = c.post("/admin/api/cache/clear")
        assert r.status_code == 401

        r = c.post("/admin/api/cache/clear", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ── 24. DB vacuum endpoint ──────────────────────────────────────────────


def test_db_vacuum_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.post("/admin/api/db/vacuum")
        assert r.status_code == 401

        r = c.post("/admin/api/db/vacuum", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ── 25. Reset stats clears counters ─────────────────────────────────────


def test_reset_stats_clears_counters(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        create_endpoint(c, "ep-rs", "key-rs")
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        c.post("/v1/chat/completions", headers=client_headers(), json=payload)

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert state["endpoints"][0]["total_requests"] > 0

        r = c.post("/admin/api/reset-stats", headers=admin_headers())
        assert r.status_code == 200

        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert state["endpoints"][0]["total_requests"] == 0


# ── 26. Endpoint not found ──────────────────────────────────────────────


def test_endpoint_not_found_returns_404(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        r = c.patch("/admin/api/endpoints/99999", headers=admin_headers(), json={"name": "x"})
        assert r.status_code == 404

        r = c.delete("/admin/api/endpoints/99999", headers=admin_headers())
        assert r.status_code == 404


# ── 27. SlidingWindow unit test ──────────────────────────────────────────


def test_sliding_window_rate_tracking() -> None:
    sw = SlidingWindow(window_seconds=60.0, max_entries=100)
    assert sw.count() == 0
    assert sw.sum() == 0

    sw.add(10.0)
    sw.add(20.0)
    sw.add(5.0)
    assert sw.count() == 3
    assert sw.sum() == 35.0
    assert sw.rate_per_second() > 0

    sw.clear()
    assert sw.count() == 0


# ── 28. SlidingWindow max_entries enforcement ────────────────────────────


def test_sliding_window_max_entries() -> None:
    sw = SlidingWindow(window_seconds=60.0, max_entries=5)
    for i in range(10):
        sw.add(1.0)
    # Should have been trimmed to max_entries
    assert len(sw._entries) <= 5


# ── 29. TTLCache basic operations ────────────────────────────────────────


def test_ttl_cache_operations() -> None:
    cache = TTLCache(max_entries=10, ttl_seconds=1.0)

    async def _test():
        assert await cache.get("missing") is None

        await cache.put("key1", "value1")
        assert await cache.get("key1") == "value1"
        assert cache.size == 1

        await cache.put("key2", "value2")
        assert cache.size == 2

        await cache.clear()
        assert cache.size == 0
        assert await cache.get("key1") is None

    asyncio.run(_test())


# ── 30. TTLCache max entries eviction ────────────────────────────────────


def test_ttl_cache_eviction() -> None:
    cache = TTLCache(max_entries=3, ttl_seconds=60.0)

    async def _test():
        for i in range(5):
            await cache.put(f"key{i}", f"val{i}")
        # Only 3 should remain
        assert cache.size == 3
        # Oldest entries evicted
        assert await cache.get("key0") is None
        assert await cache.get("key1") is None
        assert await cache.get("key4") == "val4"

    asyncio.run(_test())


# ── 31. RateLimiter enforcement ──────────────────────────────────────────


def test_rate_limiter_enforcement() -> None:
    rl = RateLimiter(rpm=5)
    # Should allow first 5
    for _ in range(5):
        assert rl.check() is True
    # 6th should be blocked
    assert rl.check() is False
    assert rl.remaining == 0


# ── 32. Input validation rejects bad names ───────────────────────────────


def test_input_validation_rejects_bad_endpoint_name(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        # Empty name
        r = c.post("/admin/api/endpoints", headers=admin_headers(), json={
            "name": "", "base_url": "https://x.com/v1", "api_key": "k"
        })
        assert r.status_code == 422

        # Name with script injection (< > characters)
        r = c.post("/admin/api/endpoints", headers=admin_headers(), json={
            "name": "<script>alert(1)</script>", "base_url": "https://x.com/v1", "api_key": "k"
        })
        assert r.status_code == 422


# ── 33. No endpoints returns 503 ────────────────────────────────────────


def test_no_endpoints_returns_503(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        r = c.post("/v1/chat/completions", headers=client_headers(), json=payload)
        assert r.status_code == 503


# ── 34. Version constant exists ──────────────────────────────────────────


def test_version_constant() -> None:
    assert __version__
    parts = __version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)


# ── 35. Incoming blocks CRUD ────────────────────────────────────────────


def test_incoming_blocks_crud(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path / "lb.db"), transport=ok_transport())
    with TestClient(app) as c:
        # List initial blocks
        r = c.get("/admin/api/incoming-blocks", headers=admin_headers())
        assert r.status_code == 200
        blocks = r.json()
        initial_count = len(blocks)

        # Create
        r = c.post("/admin/api/incoming-blocks", headers=admin_headers(), json={"name": "New Block", "pos_x": 100, "pos_y": 200})
        assert r.status_code == 200
        new_id = r.json()["id"]

        # Verify created
        r = c.get("/admin/api/incoming-blocks", headers=admin_headers())
        assert len(r.json()) == initial_count + 1

        # Update
        r = c.patch(f"/admin/api/incoming-blocks/{new_id}", headers=admin_headers(), json={"name": "Renamed Block"})
        assert r.status_code == 200

        # Delete
        r = c.delete(f"/admin/api/incoming-blocks/{new_id}", headers=admin_headers())
        assert r.status_code == 200
        r = c.get("/admin/api/incoming-blocks", headers=admin_headers())
        assert len(r.json()) == initial_count
