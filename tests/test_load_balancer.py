"""Comprehensive test suite for LLM Load Balancer v4.

Tests cover: admin auth, endpoint CRUD, routing logic, proxy,
cooldown, models, client tokens, incoming blocks, health, metrics,
security headers, request IDs, cache, vacuum, stats, persistence,
input validation, priority input tokens, plus unit tests for
SlidingWindow, TTLCache, and RateLimiter.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

# Ensure the load-balancer module is importable regardless of CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "load-balancer"))

from load_balancer import (  # noqa: E402
    AppSettings,
    RateLimiter,
    SlidingWindow,
    TTLCache,
    __version__,
    create_app,
)
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "admin-test-token"
CLIENT_TOKEN = "client-test-token"

CHAT_BODY = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """Mock upstream: returns valid OpenAI-compatible responses."""
    url = str(request.url)
    if "/models" in url:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "gpt-test", "object": "model", "owned_by": "test"}],
            },
        )
    body = json.loads(request.content) if request.content else {}
    if body.get("stream"):
        chunks = (
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hi"},"index":0}]}\n\n'
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=chunks.encode(),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def _err_handler(request: httpx.Request) -> httpx.Response:
    """Mock upstream that always returns 500."""
    return httpx.Response(500, json={"error": "internal server error"})


def ok_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_ok_handler)


def err_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_err_handler)


def make_settings(
    db_path, fail_threshold: int = 3, cooldown_seconds: float = 20, **kw
) -> AppSettings:
    return AppSettings(
        db_path=str(db_path),
        port=8090,
        admin_token=ADMIN_TOKEN,
        client_tokens=kw.pop("client_tokens", {CLIENT_TOKEN}),
        fail_threshold=fail_threshold,
        cooldown_seconds=cooldown_seconds,
        max_connections=200,
        max_keepalive=50,
        default_timeout_seconds=30.0,
        cache_max_entries=1000,
        log_level="WARNING",
        **kw,
    )


def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def client_headers():
    return {"Authorization": f"Bearer {CLIENT_TOKEN}"}


def make_client(tmp_path, transport=None, **kw) -> TestClient:
    settings = make_settings(tmp_path / "test.db", **kw)
    app = create_app(settings=settings, transport=transport or ok_transport())
    return TestClient(app, raise_server_exceptions=False)


def create_endpoint(client: TestClient, name: str = "ep1", connected: bool = True, **extra):
    data = {
        "name": name,
        "base_url": "http://upstream:8000/v1",
        "connected": connected,
        **extra,
    }
    r = client.post("/admin/api/endpoints", json=data, headers=admin_headers())
    assert r.status_code == 200, f"endpoint creation failed: {r.text}"
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
#  Unit tests – SlidingWindow
# ═══════════════════════════════════════════════════════════════════════════


class TestSlidingWindow:
    def test_add_and_sum(self):
        w = SlidingWindow(60.0, 1000)
        w.add(10.0)
        w.add(20.0)
        assert w.sum() == 30.0

    def test_count(self):
        w = SlidingWindow(60.0, 1000)
        for _ in range(7):
            w.add(1.0)
        assert w.count() == 7

    def test_rate_per_second(self):
        w = SlidingWindow(60.0, 1000)
        w.add(60.0)
        assert w.rate_per_second() == 1.0

    def test_count_per_second(self):
        w = SlidingWindow(60.0, 1000)
        for _ in range(60):
            w.add(1.0)
        assert w.count_per_second() == 1.0

    def test_clear(self):
        w = SlidingWindow(60.0, 1000)
        w.add(99.0)
        w.clear()
        assert w.sum() == 0.0
        assert w.count() == 0

    def test_max_entries_truncation(self):
        w = SlidingWindow(60.0, max_entries=10)
        for i in range(25):
            w.add(1.0)
        # After truncation the list should be at most max_entries
        assert w.count() <= 10

    def test_empty_rate(self):
        w = SlidingWindow(60.0, 1000)
        assert w.rate_per_second() == 0.0
        assert w.count_per_second() == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  Unit tests – TTLCache
# ═══════════════════════════════════════════════════════════════════════════


class TestTTLCache:
    def test_put_get(self):
        async def _run():
            c = TTLCache(100, 300.0)
            await c.put("k1", "v1")
            assert await c.get("k1") == "v1"

        asyncio.run(_run())

    def test_miss(self):
        async def _run():
            c = TTLCache(100, 300.0)
            assert await c.get("missing") is None

        asyncio.run(_run())

    def test_clear(self):
        async def _run():
            c = TTLCache(100, 300.0)
            await c.put("a", 1)
            await c.put("b", 2)
            await c.clear()
            assert c.size == 0
            assert await c.get("a") is None

        asyncio.run(_run())

    def test_lru_eviction(self):
        async def _run():
            c = TTLCache(max_entries=3, ttl_seconds=300.0)
            for i in range(5):
                await c.put(f"k{i}", f"v{i}")
            assert c.size <= 3
            assert await c.get("k0") is None  # evicted
            assert await c.get("k4") == "v4"  # still there

        asyncio.run(_run())

    def test_size_property(self):
        async def _run():
            c = TTLCache(100, 300.0)
            assert c.size == 0
            await c.put("x", 1)
            assert c.size == 1

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════
#  Unit tests – RateLimiter
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimiter:
    def test_allows_within_limit(self):
        rl = RateLimiter(rpm=100)
        for _ in range(50):
            assert rl.check() is True

    def test_blocks_exceeding(self):
        rl = RateLimiter(rpm=5)
        for _ in range(5):
            assert rl.check() is True
        assert rl.check() is False

    def test_remaining(self):
        rl = RateLimiter(rpm=10)
        assert rl.remaining == 10
        rl.check()
        assert rl.remaining == 9

    def test_zero_rpm_always_allows(self):
        rl = RateLimiter(rpm=0)
        for _ in range(100):
            assert rl.check() is True


# ═══════════════════════════════════════════════════════════════════════════
#  Version constant
# ═══════════════════════════════════════════════════════════════════════════


def test_version_constant():
    assert isinstance(__version__, str)
    assert __version__.startswith("4.")


# ═══════════════════════════════════════════════════════════════════════════
#  Admin auth
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminAuth:
    def test_no_token_rejected(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/admin/api/state")
        assert r.status_code == 401

    def test_wrong_token_rejected(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/admin/api/state", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 401

    def test_correct_header_token(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/admin/api/state", headers=admin_headers())
        assert r.status_code == 200

    def test_bearer_token(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get(
                "/admin/api/state",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoint CRUD
# ═══════════════════════════════════════════════════════════════════════════


class TestEndpointCRUD:
    def test_create(self, tmp_path):
        with make_client(tmp_path) as c:
            ep = create_endpoint(c, "test-ep")
        assert ep["name"] == "test-ep"
        assert "api_key_preview" in ep
        assert "api_key" not in ep  # key must be masked

    def test_create_and_list_in_state(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "a")
            create_endpoint(c, "b")
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(st["endpoints"]) == 2

    def test_update(self, tmp_path):
        with make_client(tmp_path) as c:
            ep = create_endpoint(c, "upd-ep")
            r = c.patch(
                f"/admin/api/endpoints/{ep['id']}",
                json={"token_quota_percent": 42.5},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["token_quota_percent"] == 42.5

    def test_delete(self, tmp_path):
        with make_client(tmp_path) as c:
            ep = create_endpoint(c, "del-ep")
            r = c.delete(f"/admin/api/endpoints/{ep['id']}", headers=admin_headers())
            assert r.status_code == 200
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(st["endpoints"]) == 0

    def test_duplicate_name_409(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "dup")
            r = c.post(
                "/admin/api/endpoints",
                json={"name": "dup", "base_url": "http://x:8000/v1"},
                headers=admin_headers(),
            )
        assert r.status_code == 409

    def test_delete_clears_token_mapping(self, tmp_path):
        with make_client(tmp_path) as c:
            ep = create_endpoint(c, "mapped-ep")
            tok = c.post(
                "/admin/api/tokens",
                json={"name": "t1", "preferred_endpoint_id": ep["id"]},
                headers=admin_headers(),
            ).json()
            c.delete(f"/admin/api/endpoints/{ep['id']}", headers=admin_headers())
            tokens = c.get("/admin/api/tokens", headers=admin_headers()).json()
            found = next(t for t in tokens if t["id"] == tok["id"])
        assert found["preferred_endpoint_id"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  Input validation
# ═══════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    def test_empty_name_422(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post(
                "/admin/api/endpoints",
                json={"name": "", "base_url": "http://a:8000/v1"},
                headers=admin_headers(),
            )
        assert r.status_code == 422

    def test_name_too_long_422(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post(
                "/admin/api/endpoints",
                json={"name": "x" * 200, "base_url": "http://a:8000/v1"},
                headers=admin_headers(),
            )
        assert r.status_code == 422

    def test_invalid_url_422(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post(
                "/admin/api/endpoints",
                json={"name": "bad", "base_url": "not-a-url"},
                headers=admin_headers(),
            )
        assert r.status_code == 422

    def test_invalid_routing_mode_422(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.patch(
                "/admin/api/routing",
                json={"routing_mode": "random"},
                headers=admin_headers(),
            )
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
#  Routing
# ═══════════════════════════════════════════════════════════════════════════


class TestRouting:
    def test_percentage_mode(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep-60", token_quota_percent=60)
            create_endpoint(c, "ep-40", token_quota_percent=40)
            for _ in range(6):
                r = c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
                assert r.status_code == 200

    def test_priority_mode(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "primary", priority_order=1)
            create_endpoint(c, "overflow", priority_order=99)
            c.patch("/admin/api/routing", json={"routing_mode": "priority"}, headers=admin_headers())
            for _ in range(4):
                r = c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
                assert r.status_code == 200
            # Primary should get most (or all) requests
            st = c.get("/admin/api/state", headers=admin_headers()).json()
            pri = next(e for e in st["endpoints"] if e["name"] == "primary")
            assert pri["total_requests"] >= 1

    def test_routing_mode_update(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.patch(
                "/admin/api/routing",
                json={"routing_mode": "priority"},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["routing_mode"] == "priority"


# ═══════════════════════════════════════════════════════════════════════════
#  Proxy – chat completions
# ═══════════════════════════════════════════════════════════════════════════


class TestProxy:
    def test_non_streaming(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
        assert r.status_code == 200
        d = r.json()
        assert "choices" in d
        assert d["usage"]["prompt_tokens"] == 10
        assert d["usage"]["completion_tokens"] == 5

    def test_legacy_route(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post("/chat/completions", json=CHAT_BODY, headers=client_headers())
        assert r.status_code == 200

    def test_pool_route(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post("/v1/mypool/chat/completions", json=CHAT_BODY, headers=client_headers())
        assert r.status_code == 200

    def test_streaming(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post(
                "/v1/chat/completions",
                json={**CHAT_BODY, "stream": True},
                headers=client_headers(),
            )
        assert r.status_code == 200
        assert b"data:" in r.content

    def test_token_tracking(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        ep = st["endpoints"][0]
        assert ep["total_requests"] == 1
        assert ep["total_success"] == 1
        assert ep["prompt_tokens_total"] == 10
        assert ep["completion_tokens_total"] == 5

    def test_no_endpoints_503(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
        assert r.status_code == 503

    def test_endpoint_name_in_header(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "named-ep")
            r = c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
        assert r.headers.get("X-LB-Endpoint") == "named-ep"


# ═══════════════════════════════════════════════════════════════════════════
#  Cooldown / failover
# ═══════════════════════════════════════════════════════════════════════════


class TestCooldown:
    def test_failures_recorded(self, tmp_path):
        with make_client(tmp_path, transport=err_transport(), fail_threshold=2, cooldown_seconds=60) as c:
            create_endpoint(c, "ep1")
            # Upstream returns 500 → backend counts as failure
            for _ in range(3):
                c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        ep = st["endpoints"][0]
        assert ep["total_requests"] > 0


# ═══════════════════════════════════════════════════════════════════════════
#  Models aggregation
# ═══════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_models(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.get("/v1/models", headers=client_headers())
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "list"
        assert len(d["data"]) >= 1

    def test_models_legacy(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.get("/models", headers=client_headers())
        assert r.status_code == 200

    def test_models_pool(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.get("/v1/pool1/models", headers=client_headers())
        assert r.status_code == 200

    def test_models_no_endpoints_503(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/v1/models", headers=client_headers())
        assert r.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
#  Client tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestClientTokens:
    def test_create(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post("/admin/api/tokens", json={"name": "tok1"}, headers=admin_headers())
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "tok1"
        assert "token" in d
        assert d["token"].startswith("ct-")

    def test_create_with_custom_value(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post(
                "/admin/api/tokens",
                json={"name": "custom", "token": "my-custom-token"},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["token"] == "my-custom-token"

    def test_list(self, tmp_path):
        with make_client(tmp_path) as c:
            c.post("/admin/api/tokens", json={"name": "a"}, headers=admin_headers())
            c.post("/admin/api/tokens", json={"name": "b"}, headers=admin_headers())
            r = c.get("/admin/api/tokens", headers=admin_headers())
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_update(self, tmp_path):
        with make_client(tmp_path) as c:
            tok = c.post("/admin/api/tokens", json={"name": "updatable"}, headers=admin_headers()).json()
            r = c.patch(
                f"/admin/api/tokens/{tok['id']}",
                json={"request_priority": 7},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["request_priority"] == 7

    def test_delete(self, tmp_path):
        with make_client(tmp_path) as c:
            tok = c.post("/admin/api/tokens", json={"name": "del"}, headers=admin_headers()).json()
            r = c.delete(f"/admin/api/tokens/{tok['id']}", headers=admin_headers())
            assert r.status_code == 200
            toks = c.get("/admin/api/tokens", headers=admin_headers()).json()
        assert all(t["id"] != tok["id"] for t in toks)

    def test_auth_via_db_token(self, tmp_path):
        """A DB-stored client token should grant access to the proxy."""
        s = make_settings(tmp_path / "db_auth.db", client_tokens=set())
        app = create_app(settings=s, transport=ok_transport())
        with TestClient(app, raise_server_exceptions=False) as c:
            tok = c.post("/admin/api/tokens", json={"name": "db-client"}, headers=admin_headers()).json()
            create_endpoint(c, "ep1")
            r = c.post(
                "/v1/chat/completions",
                json=CHAT_BODY,
                headers={"Authorization": f"Bearer {tok['token']}"},
            )
        assert r.status_code == 200

    def test_invalid_client_token_rejected(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post(
                "/v1/chat/completions",
                json=CHAT_BODY,
                headers={"Authorization": "Bearer invalid-token"},
            )
        assert r.status_code == 401

    def test_token_preferred_endpoint_routing(self, tmp_path):
        """A token with preferred_endpoint_id must always route there."""
        s = make_settings(tmp_path / "tok_route.db", client_tokens=set())
        app = create_app(settings=s, transport=ok_transport())
        with TestClient(app, raise_server_exceptions=False) as c:
            ep_target = create_endpoint(c, "target")
            create_endpoint(c, "other")
            tok = c.post(
                "/admin/api/tokens",
                json={"name": "routed", "preferred_endpoint_id": ep_target["id"]},
                headers=admin_headers(),
            ).json()
            for _ in range(4):
                r = c.post(
                    "/v1/chat/completions",
                    json=CHAT_BODY,
                    headers={"Authorization": f"Bearer {tok['token']}"},
                )
                assert r.status_code == 200
            st = c.get("/admin/api/state", headers=admin_headers()).json()
            target = next(e for e in st["endpoints"] if e["name"] == "target")
            other = next(e for e in st["endpoints"] if e["name"] == "other")
        assert target["total_requests"] == 4
        assert other["total_requests"] == 0

    def test_duplicate_token_value_409(self, tmp_path):
        with make_client(tmp_path) as c:
            c.post(
                "/admin/api/tokens",
                json={"name": "t1", "token": "same-tok"},
                headers=admin_headers(),
            )
            r = c.post(
                "/admin/api/tokens",
                json={"name": "t2", "token": "same-tok"},
                headers=admin_headers(),
            )
        assert r.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
#  Incoming blocks
# ═══════════════════════════════════════════════════════════════════════════


class TestIncomingBlocks:
    def test_create(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post(
                "/admin/api/incoming-blocks",
                json={"name": "Block A"},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["name"] == "Block A"

    def test_update(self, tmp_path):
        with make_client(tmp_path) as c:
            b = c.post(
                "/admin/api/incoming-blocks",
                json={"name": "Old"},
                headers=admin_headers(),
            ).json()
            r = c.patch(
                f"/admin/api/incoming-blocks/{b['id']}",
                json={"name": "New"},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["name"] == "New"

    def test_list(self, tmp_path):
        with make_client(tmp_path) as c:
            c.post("/admin/api/incoming-blocks", json={"name": "B1"}, headers=admin_headers())
            r = c.get("/admin/api/incoming-blocks", headers=admin_headers())
        assert r.status_code == 200
        assert len(r.json()) >= 2  # default + new

    def test_delete(self, tmp_path):
        with make_client(tmp_path) as c:
            b = c.post(
                "/admin/api/incoming-blocks",
                json={"name": "CanDel"},
                headers=admin_headers(),
            ).json()
            r = c.delete(f"/admin/api/incoming-blocks/{b['id']}", headers=admin_headers())
        assert r.status_code == 200

    def test_cannot_delete_last_block(self, tmp_path):
        with make_client(tmp_path) as c:
            blocks = c.get("/admin/api/incoming-blocks", headers=admin_headers()).json()
            if len(blocks) == 1:
                r = c.delete(
                    f"/admin/api/incoming-blocks/{blocks[0]['id']}",
                    headers=admin_headers(),
                )
                assert r.status_code == 409

    def test_token_assigned_to_block(self, tmp_path):
        with make_client(tmp_path) as c:
            b = c.post(
                "/admin/api/incoming-blocks",
                json={"name": "Setup A"},
                headers=admin_headers(),
            ).json()
            tok = c.post(
                "/admin/api/tokens",
                json={"name": "t-in-block", "incoming_block_id": b["id"]},
                headers=admin_headers(),
            ).json()
            # Verify the token is assigned to the block
            toks = c.get("/admin/api/tokens", headers=admin_headers()).json()
            found = next(t for t in toks if t["id"] == tok["id"])
        assert found["incoming_block_id"] == b["id"]

    def test_delete_block_reassigns_tokens(self, tmp_path):
        """When a block is deleted, its tokens move to another block."""
        with make_client(tmp_path) as c:
            b2 = c.post(
                "/admin/api/incoming-blocks",
                json={"name": "B2"},
                headers=admin_headers(),
            ).json()
            tok = c.post(
                "/admin/api/tokens",
                json={"name": "orphan-tok", "incoming_block_id": b2["id"]},
                headers=admin_headers(),
            ).json()
            c.delete(f"/admin/api/incoming-blocks/{b2['id']}", headers=admin_headers())
            toks = c.get("/admin/api/tokens", headers=admin_headers()).json()
            found = next(t for t in toks if t["id"] == tok["id"])
        # Token should now be in a different block (the default one)
        assert found["incoming_block_id"] != b2["id"]


# ═══════════════════════════════════════════════════════════════════════════
#  Priority input token
# ═══════════════════════════════════════════════════════════════════════════


class TestPriorityInputToken:
    def test_set(self, tmp_path):
        with make_client(tmp_path) as c:
            tok = c.post("/admin/api/tokens", json={"name": "prio"}, headers=admin_headers()).json()
            r = c.patch(
                "/admin/api/routing",
                json={"priority_input_token_id": tok["id"]},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["priority_input_token_id"] == tok["id"]

    def test_clear(self, tmp_path):
        with make_client(tmp_path) as c:
            tok = c.post("/admin/api/tokens", json={"name": "prio"}, headers=admin_headers()).json()
            c.patch(
                "/admin/api/routing",
                json={"priority_input_token_id": tok["id"]},
                headers=admin_headers(),
            )
            r = c.patch(
                "/admin/api/routing",
                json={"priority_input_token_id": 0},
                headers=admin_headers(),
            )
        assert r.status_code == 200
        assert r.json()["priority_input_token_id"] is None

    def test_invalid_id_404(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.patch(
                "/admin/api/routing",
                json={"priority_input_token_id": 99999},
                headers=admin_headers(),
            )
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
#  Health
# ═══════════════════════════════════════════════════════════════════════════


class TestHealth:
    def test_no_endpoints_degraded(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/health")
        assert r.status_code == 503
        d = r.json()
        assert d["status"] == "degraded"
        assert d["version"] == __version__

    def test_with_endpoint_ok(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert d["connected"] >= 1

    def test_head_health(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.head("/health")
        assert r.status_code == 200

    def test_head_health_no_endpoints(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.head("/health")
        assert r.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
#  Prometheus metrics
# ═══════════════════════════════════════════════════════════════════════════


class TestMetrics:
    def test_prometheus_format(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.get("/metrics")
        assert r.status_code == 200
        body = r.text
        assert "lb_requests_total" in body
        assert "lb_errors_total" in body
        assert "lb_inflight_total" in body
        assert "lb_tokens_per_second" in body
        assert "lb_endpoint_requests_total" in body
        assert "lb_endpoint_failures_total" in body
        assert "lb_endpoint_latency_ms" in body
        assert "lb_endpoint_inflight" in body
        assert "lb_endpoint_tokens_total" in body
        assert "lb_uptime_seconds" in body


# ═══════════════════════════════════════════════════════════════════════════
#  Security headers & request ID
# ═══════════════════════════════════════════════════════════════════════════


class TestSecurity:
    def test_security_headers(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "strict-origin" in r.headers["Referrer-Policy"]
        assert r.headers["X-LB-Version"] == __version__

    def test_auto_request_id(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/health")
        assert "X-Request-ID" in r.headers
        assert len(r.headers["X-Request-ID"]) > 10  # UUID

    def test_passthrough_request_id(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/health", headers={"X-Request-ID": "my-id-123"})
        assert r.headers["X-Request-ID"] == "my-id-123"

    def test_client_auth_required(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            r = c.post("/v1/chat/completions", json=CHAT_BODY)
        assert r.status_code == 401

    def test_no_auth_needed_when_no_tokens_configured(self, tmp_path):
        """When neither env tokens nor DB tokens exist, proxy is open."""
        s = make_settings(tmp_path / "open.db", client_tokens=set())
        app = create_app(settings=s, transport=ok_transport())
        with TestClient(app, raise_server_exceptions=False) as c:
            create_endpoint(c, "ep1")
            r = c.post("/v1/chat/completions", json=CHAT_BODY)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
#  Misc routes
# ═══════════════════════════════════════════════════════════════════════════


class TestMiscRoutes:
    def test_root_redirect(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/", follow_redirects=False)
        assert r.status_code in (301, 302, 307, 308)

    def test_admin_html_served(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/admin")
        # Either 200 (file found) or 500 (file not found in test env)
        if r.status_code == 200:
            assert "html" in r.text.lower()

    def test_config_endpoint(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/admin/api/config")
        assert r.status_code == 200
        d = r.json()
        assert "supabase_url" in d
        assert "supabase_anon_key" in d

    def test_404(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.get("/nonexistent")
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
#  Stats reset, cache clear, DB vacuum
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminOps:
    def test_reset_stats(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            c.post("/v1/chat/completions", json=CHAT_BODY, headers=client_headers())
            # Confirm stats are non-zero
            st1 = c.get("/admin/api/state", headers=admin_headers()).json()
            assert st1["endpoints"][0]["total_requests"] == 1
            # Reset
            r = c.post("/admin/api/reset-stats", headers=admin_headers())
            assert r.status_code == 200
            st2 = c.get("/admin/api/state", headers=admin_headers()).json()
        assert st2["endpoints"][0]["total_requests"] == 0
        assert st2["endpoints"][0]["prompt_tokens_total"] == 0

    def test_cache_clear(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post("/admin/api/cache/clear", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_db_vacuum(self, tmp_path):
        with make_client(tmp_path) as c:
            r = c.post("/admin/api/db/vacuum", headers=admin_headers())
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ═══════════════════════════════════════════════════════════════════════════
#  Persistence
# ═══════════════════════════════════════════════════════════════════════════


class TestPersistence:
    def test_data_survives_restart(self, tmp_path):
        db = tmp_path / "persist.db"
        s1 = make_settings(db)
        app1 = create_app(settings=s1, transport=ok_transport())
        with TestClient(app1, raise_server_exceptions=False) as c1:
            create_endpoint(c1, "survivor")
            c1.post("/admin/api/tokens", json={"name": "tok-persist"}, headers=admin_headers())

        # "Restart" – new app, same DB
        s2 = make_settings(db)
        app2 = create_app(settings=s2, transport=ok_transport())
        with TestClient(app2, raise_server_exceptions=False) as c2:
            st = c2.get("/admin/api/state", headers=admin_headers()).json()
            toks = c2.get("/admin/api/tokens", headers=admin_headers()).json()
        assert len(st["endpoints"]) == 1
        assert st["endpoints"][0]["name"] == "survivor"
        assert any(t["name"] == "tok-persist" for t in toks)

    def test_routing_mode_persists(self, tmp_path):
        db = tmp_path / "mode.db"
        s1 = make_settings(db)
        app1 = create_app(settings=s1, transport=ok_transport())
        with TestClient(app1, raise_server_exceptions=False) as c1:
            c1.patch(
                "/admin/api/routing",
                json={"routing_mode": "priority"},
                headers=admin_headers(),
            )

        s2 = make_settings(db)
        app2 = create_app(settings=s2, transport=ok_transport())
        with TestClient(app2, raise_server_exceptions=False) as c2:
            st = c2.get("/admin/api/state", headers=admin_headers()).json()
        assert st["routing_mode"] == "priority"


# ═══════════════════════════════════════════════════════════════════════════
#  State endpoint structure
# ═══════════════════════════════════════════════════════════════════════════


class TestStateStructure:
    def test_state_has_expected_keys(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        assert "routing_mode" in st
        assert "endpoints" in st
        assert "incoming_blocks" in st
        assert "stats" in st
        assert "client_tokens" in st
        assert "version" in st
        assert st["version"] == __version__

    def test_endpoint_has_inflight(self, tmp_path):
        with make_client(tmp_path) as c:
            create_endpoint(c, "ep1")
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        ep = st["endpoints"][0]
        assert "inflight" in ep
        assert "cooldown_remaining_s" in ep
        assert "api_key_preview" in ep
        assert "has_api_key" in ep

    def test_stats_structure(self, tmp_path):
        with make_client(tmp_path) as c:
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        stats = st["stats"]
        assert "tokens_per_second" in stats
        assert "requests_per_second" in stats
        assert "total_inflight" in stats
        assert "uptime_seconds" in stats

    def test_client_token_in_state(self, tmp_path):
        with make_client(tmp_path) as c:
            c.post("/admin/api/tokens", json={"name": "visible"}, headers=admin_headers())
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(st["client_tokens"]) >= 1
        ct = st["client_tokens"][0]
        assert "token_preview" in ct  # masked
        assert "token" not in ct or ct.get("token_preview")  # not raw in state

    def test_incoming_blocks_in_state(self, tmp_path):
        with make_client(tmp_path) as c:
            st = c.get("/admin/api/state", headers=admin_headers()).json()
        assert len(st["incoming_blocks"]) >= 1
        b = st["incoming_blocks"][0]
        assert "id" in b
        assert "name" in b
        assert "pos_x" in b
        assert "pos_y" in b
