"""Tests for the v2 Load Balancer (endpoint-based, percentage/priority routing)."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from load_balancer import AppSettings, create_app

ADMIN_TOKEN = "admin-test-token"
CLIENT_TOKEN = "client-test-token"


def make_settings(
    db_path: Path,
    fail_threshold: int = 3,
    cooldown_seconds: float = 20.0,
    client_tokens: set[str] | None = None,
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


# ── 1. Admin auth ─────────────────────────────────────────────────────────


def test_admin_api_requires_token(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True})
    )
    app = create_app(
        settings=make_settings(tmp_path / "lb.db"), transport=transport
    )
    with TestClient(app) as c:
        assert c.get("/admin/api/state").status_code == 401
        r = c.get("/admin/api/state", headers=admin_headers())
        assert r.status_code == 200
        body = r.json()
        assert "routing_mode" in body
        assert "endpoints" in body


# ── 2. Endpoint CRUD + key masking ────────────────────────────────────────


def test_endpoint_crud_and_masking(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True})
    )
    app = create_app(
        settings=make_settings(tmp_path / "lb.db"), transport=transport
    )
    with TestClient(app) as c:
        ep = create_endpoint(c, "ep-secret", "super-secret-api-key")
        # Key must be masked
        assert "api_key" not in ep
        assert ep["has_api_key"] is True
        assert ep["api_key_preview"].startswith("su")
        eid = ep["id"]

        # Patch
        r = c.patch(
            f"/admin/api/endpoints/{eid}",
            headers=admin_headers(),
            json={"token_quota_percent": 75},
        )
        assert r.status_code == 200
        assert r.json()["token_quota_percent"] == 75
        assert "api_key" not in r.json()

        # State also masks
        state = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(state["endpoints"]) == 1
        assert "api_key" not in state["endpoints"][0]
        assert state["endpoints"][0]["has_api_key"] is True

        # Delete
        r = c.delete(
            f"/admin/api/endpoints/{eid}", headers=admin_headers()
        )
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
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 100,
                    "total_tokens": 110,
                },
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(tmp_path / "lb.db"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        create_endpoint(
            c, "ep-a", "key-a", token_quota_percent=70
        )
        create_endpoint(
            c, "ep-b", "key-b", token_quota_percent=30
        )
        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
            "stream": False,
        }
        for _ in range(20):
            r = c.post(
                "/v1/chat/completions",
                headers=client_headers(),
                json=payload,
            )
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
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 20,
                    "total_tokens": 25,
                },
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(tmp_path / "lb.db"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        # Set priority mode
        c.patch(
            "/admin/api/routing",
            headers=admin_headers(),
            json={"routing_mode": "priority"},
        )
        create_endpoint(
            c, "primary", "key-primary", priority_order=1
        )
        create_endpoint(
            c, "secondary", "key-secondary", priority_order=10
        )

        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
            "stream": False,
        }
        for _ in range(8):
            r = c.post(
                "/v1/chat/completions",
                headers=client_headers(),
                json=payload,
            )
            assert r.status_code == 200

    # All should go to primary (sequential – never hits max_concurrent)
    assert all(a == "Bearer key-primary" for a in seen), seen


# ── 5. Token tracking ────────────────────────────────────────────────────


def test_token_tracking_records_usage(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 200,
                    "total_tokens": 250,
                },
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(tmp_path / "lb.db"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        create_endpoint(c, "ep-tok", "key-tok")
        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
            "stream": False,
        }
        for _ in range(3):
            r = c.post(
                "/v1/chat/completions",
                headers=client_headers(),
                json=payload,
            )
            assert r.status_code == 200

        state = c.get(
            "/admin/api/state", headers=admin_headers()
        ).json()
        ep = state["endpoints"][0]
        assert ep["prompt_tokens_total"] == 150  # 3 × 50
        assert ep["completion_tokens_total"] == 600  # 3 × 200
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
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    settings = make_settings(
        tmp_path / "lb.db", fail_threshold=1, cooldown_seconds=90
    )
    app = create_app(
        settings=settings, transport=httpx.MockTransport(handler)
    )
    with TestClient(app) as c:
        create_endpoint(c, "ep-bad", "bad-key")
        create_endpoint(c, "ep-good", "good-key")

        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
            "stream": False,
        }
        # First goes to ep-bad → 500 → cooldown
        first = c.post(
            "/v1/chat/completions",
            headers=client_headers(),
            json=payload,
        )
        assert first.status_code == 500

        # Second should use ep-good
        second = c.post(
            "/v1/chat/completions",
            headers=client_headers(),
            json=payload,
        )
        assert second.status_code == 200
        assert second.headers.get("X-LB-Endpoint") == "ep-good"

        # State shows cooldown
        state = c.get(
            "/admin/api/state", headers=admin_headers()
        ).json()
        bad = next(
            e for e in state["endpoints"] if e["name"] == "ep-bad"
        )
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

    app = create_app(
        settings=make_settings(tmp_path / "lb.db"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        create_endpoint(c, "ep-stream", "stream-key")
        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "stream"}],
            "max_tokens": 4,
            "stream": True,
        }
        r = c.post(
            "/v1/chat/completions",
            headers=client_headers(),
            json=payload,
        )
        assert r.status_code == 200
        assert "data:" in r.text
        assert r.headers.get("X-LB-Endpoint") == "ep-stream"


# ── 8. Persistence across restart ────────────────────────────────────────


def test_persistence_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "lb-persist.db"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True})
    )

    app1 = create_app(settings=make_settings(db), transport=transport)
    with TestClient(app1) as c:
        create_endpoint(
            c,
            "ep-persist",
            "persist-secret-key",
            token_quota_percent=42,
        )

    app2 = create_app(settings=make_settings(db), transport=transport)
    with TestClient(app2) as c:
        state = c.get(
            "/admin/api/state", headers=admin_headers()
        ).json()
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
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "model-a"},
                        {"id": "model-shared"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-b"},
                    {"id": "model-shared"},
                ]
            },
        )

    app = create_app(
        settings=make_settings(tmp_path / "lb.db"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        create_endpoint(c, "target-a", "key-a")
        create_endpoint(c, "target-b", "key-b")
        r = c.get("/v1/models", headers=client_headers())
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["model-a", "model-b", "model-shared"]


# ── 10. Client token CRUD ────────────────────────────────────────────────


def test_client_token_crud(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True})
    )
    app = create_app(
        settings=make_settings(tmp_path / "lb.db"), transport=transport
    )
    with TestClient(app) as c:
        # List – initially empty
        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert r.status_code == 200
        assert r.json() == []

        # Create with auto-generated token
        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "load-tester-1"},
        )
        assert r.status_code == 200
        t1 = r.json()
        assert t1["name"] == "load-tester-1"
        assert t1["token"].startswith("ct-")
        assert len(t1["token"]) > 20
        tid = t1["id"]

        # Create with explicit token
        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "custom-tok", "token": "my-custom-token-123"},
        )
        assert r.status_code == 200
        t2 = r.json()
        assert t2["token"] == "my-custom-token-123"

        # List – now 2
        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert r.status_code == 200
        tokens = r.json()
        assert len(tokens) == 2
        # Full tokens returned in list (admin sees them to copy)
        assert any(tk["token"] == t1["token"] for tk in tokens)

        # State includes masked tokens
        r = c.get("/admin/api/state", headers=admin_headers())
        assert r.status_code == 200
        state_tokens = r.json()["client_tokens"]
        assert len(state_tokens) == 2
        # State only shows preview, not full token
        assert "token" not in state_tokens[0]
        assert "token_preview" in state_tokens[0]

        # Duplicate token fails
        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "dup", "token": "my-custom-token-123"},
        )
        assert r.status_code == 409

        # Delete
        r = c.delete(
            f"/admin/api/tokens/{tid}", headers=admin_headers()
        )
        assert r.status_code == 200
        r = c.get("/admin/api/tokens", headers=admin_headers())
        assert len(r.json()) == 1


# ── 11. Auth via DB-managed client token ─────────────────────────────────


def test_auth_via_db_client_token(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            headers={"content-type": "application/json"},
        )

    # No env client tokens – access is open until DB tokens exist
    app = create_app(
        settings=make_settings(
            tmp_path / "lb.db", client_tokens=set()
        ),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as c:
        create_endpoint(c, "ep-auth", "key-auth")
        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        # Open access (no tokens configured anywhere)
        r = c.post("/v1/chat/completions", json=payload)
        assert r.status_code == 200

        # Create a DB token
        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "tester", "token": "db-secret-token"},
        )
        assert r.status_code == 200

        # Now access without token fails
        r = c.post("/v1/chat/completions", json=payload)
        assert r.status_code == 401

        # Access with wrong token fails
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong-token"},
            json=payload,
        )
        assert r.status_code == 401

        # Access with DB token succeeds
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer db-secret-token"},
            json=payload,
        )
        assert r.status_code == 200


# ── 12. Token-level route + priority ────────────────────────────────────


def test_db_token_routes_to_mapped_endpoint_and_sets_priority(
    tmp_path: Path,
) -> None:
    seen_auth: list[str] = []
    seen_priority: list[int | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("Authorization", ""))
        body = json.loads(req.content.decode("utf-8"))
        seen_priority.append(body.get("priority"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(
            tmp_path / "lb.db", client_tokens=set()
        ),
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as c:
        create_endpoint(c, "target-1", "key-1")
        ep2 = create_endpoint(c, "target-2", "key-2")

        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={
                "name": "source-1",
                "token": "source-1-token",
                "preferred_endpoint_id": ep2["id"],
                "request_priority": 200,
            },
        )
        assert r.status_code == 200, r.text
        tok = r.json()
        assert tok["preferred_endpoint_id"] == ep2["id"]
        assert tok["request_priority"] == 200

        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer source-1-token"},
            json=payload,
        )
        assert r.status_code == 200, r.text

    # Must use mapped endpoint key (target-2), not target-1
    assert seen_auth
    assert seen_auth[0] == "Bearer key-2"
    assert all(v == "Bearer key-2" for v in seen_auth)
    # request_priority from client token is injected if request has none
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
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 7,
                    "total_tokens": 10,
                },
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(
            tmp_path / "lb.db", client_tokens=set()
        ),
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as c:
        create_endpoint(c, "ep-cache", "key-cache")

        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={
                "name": "quota-token",
                "token": "quota-token-value",
                "min_traffic_percent": 50,
            },
        )
        assert r.status_code == 200, r.text

        headers = {
            "Authorization": "Bearer quota-token-value",
        }
        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "same prompt"}],
            "stream": False,
        }

        first = c.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.headers.get("X-LB-Cache") == "MISS"

        second = c.post("/v1/chat/completions", headers=headers, json=payload)
        assert second.status_code == 200
        assert second.headers.get("X-LB-Cache") == "HIT"

        miss_payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "new uncached prompt"}],
            "stream": False,
        }
        third = c.post(
            "/v1/chat/completions", headers=headers, json=miss_payload
        )
        assert third.status_code == 503
        assert "cache miss" in third.text.lower()

    assert upstream_calls == 1


def test_priority_mode_with_prioritized_input_token(tmp_path: Path) -> None:
    seen_auth: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("Authorization", ""))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            headers={"content-type": "application/json"},
        )

    app = create_app(
        settings=make_settings(
            tmp_path / "lb.db", client_tokens=set()
        ),
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as c:
        create_endpoint(c, "primary", "key-primary", priority_order=1)
        create_endpoint(c, "secondary", "key-secondary", priority_order=10)

        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "prio-in", "token": "prio-in-token"},
        )
        assert r.status_code == 200, r.text
        prio_tok_id = r.json()["id"]

        r = c.post(
            "/admin/api/tokens",
            headers=admin_headers(),
            json={"name": "other-in", "token": "other-in-token"},
        )
        assert r.status_code == 200, r.text

        r = c.patch(
            "/admin/api/routing",
            headers=admin_headers(),
            json={
                "routing_mode": "priority",
                "priority_input_token_id": prio_tok_id,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["routing_mode"] == "priority"
        assert r.json()["priority_input_token_id"] == prio_tok_id

        payload = {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

        # Priorisierter Eingang -> primary endpoint
        rp = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer prio-in-token"},
            json=payload,
        )
        assert rp.status_code == 200, rp.text

        # Anderer Eingang -> bevorzugt secondary endpoint
        ro = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer other-in-token"},
            json=payload,
        )
        assert ro.status_code == 200, ro.text

    assert len(seen_auth) >= 2
    assert seen_auth[0] == "Bearer key-primary"
    assert seen_auth[1] == "Bearer key-secondary"
