"""LLM Load Balancer v2 – Visual Flow Routing with Token Throughput Tracking.

Endpoints replace the old pool/target model.  Incoming requests are routed
to *connected* endpoints using either **percentage** (token-quota) or
**priority** (primary + overflow) mode.  Every response's ``usage`` block
is parsed so that per-endpoint and global token throughput can be displayed
in the n8n-style admin UI.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

LOG = logging.getLogger("load-balancer")

# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass
class AppSettings:
    db_path: str
    port: int
    admin_token: str
    client_tokens: set[str]
    fail_threshold: int
    cooldown_seconds: float
    max_connections: int
    max_keepalive: int
    default_timeout_seconds: float
    cache_max_entries: int
    log_level: str


@dataclass
class RuntimeState:
    inflight: int = 0
    served: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bearer_token(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def normalize_base_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    return url


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def should_count_as_success(status_code: int) -> bool:
    return not (status_code >= 500 or status_code in (401, 403, 429))


def filtered_response_headers(
    headers: httpx.Headers, endpoint_name: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        lo = k.lower()
        if lo in ("content-type", "cache-control", "retry-after"):
            out[k] = v
        elif lo.startswith("x-ratelimit") or lo.startswith("x-request-id"):
            out[k] = v
    out["X-LB-Endpoint"] = endpoint_name
    return out


def build_upstream_headers(
    request: Request, endpoint: dict[str, Any]
) -> dict[str, str]:
    blocked = {
        "host",
        "connection",
        "content-length",
        "authorization",
        "accept-encoding",
        "x-admin-token",
    }
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in blocked or k.lower().startswith("x-lb-"):
            continue
        headers[k] = v
    api_key = endpoint.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if "content-type" not in {h.lower() for h in headers}:
        headers["Content-Type"] = "application/json"
    return headers


def mask_secret(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return f"{s[:2]}***"
    return f"{s[:4]}...{s[-4:]}"


def sanitize_endpoint(ep: dict[str, Any]) -> dict[str, Any]:
    out = dict(ep)
    raw = str(out.get("api_key", ""))
    out["api_key_preview"] = mask_secret(raw)
    out["has_api_key"] = bool(raw.strip())
    out.pop("api_key", None)
    return out


def extract_usage(body: bytes) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from a JSON response body."""
    try:
        data = json.loads(body)
        usage = data.get("usage") or {}
        return int(usage.get("prompt_tokens", 0)), int(
            usage.get("completion_tokens", 0)
        )
    except Exception:
        return 0, 0


# ── SQLite Store ──────────────────────────────────────────────────────────────


class SQLiteStore:
    """Persistent storage for endpoints and settings."""

    def __init__(self, db_path: str) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(p, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    # ── schema ────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS endpoints (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                  TEXT    NOT NULL UNIQUE,
                    base_url              TEXT    NOT NULL,
                    api_key               TEXT    NOT NULL DEFAULT '',
                    timeout_seconds       REAL    NOT NULL DEFAULT 120,
                    verify_tls            INTEGER NOT NULL DEFAULT 1,
                    enabled               INTEGER NOT NULL DEFAULT 1,
                    connected             INTEGER NOT NULL DEFAULT 0,
                    role                  TEXT    NOT NULL DEFAULT 'percentage',
                    token_quota_percent   REAL    NOT NULL DEFAULT 0,
                    priority_order        INTEGER NOT NULL DEFAULT 10,
                    max_concurrent        INTEGER NOT NULL DEFAULT 0,
                    cooldown_until        REAL    NOT NULL DEFAULT 0,
                    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                    total_requests        INTEGER NOT NULL DEFAULT 0,
                    total_success         INTEGER NOT NULL DEFAULT 0,
                    total_failures        INTEGER NOT NULL DEFAULT 0,
                    avg_latency_ms        REAL    NOT NULL DEFAULT 0,
                    prompt_tokens_total   INTEGER NOT NULL DEFAULT 0,
                    completion_tokens_total INTEGER NOT NULL DEFAULT 0,
                    pos_x                 REAL    NOT NULL DEFAULT 500,
                    pos_y                 REAL    NOT NULL DEFAULT 200,
                    created_at            TEXT    NOT NULL,
                    updated_at            TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS client_tokens (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT    NOT NULL,
                    token      TEXT    NOT NULL UNIQUE,
                    preferred_endpoint_id INTEGER,
                    request_priority INTEGER NOT NULL DEFAULT 0,
                    min_traffic_percent REAL NOT NULL DEFAULT 0,
                    created_at TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ep_conn
                    ON endpoints(connected, enabled, cooldown_until);
                """
            )
            self._conn.commit()
            # Migration: add columns to client_tokens if missing
            for col_def in [
                "pos_x REAL NOT NULL DEFAULT 80",
                "pos_y REAL NOT NULL DEFAULT 100",
                "total_requests INTEGER NOT NULL DEFAULT 0",
                "prompt_tokens_total INTEGER NOT NULL DEFAULT 0",
                "completion_tokens_total INTEGER NOT NULL DEFAULT 0",
                "preferred_endpoint_id INTEGER",
                "request_priority INTEGER NOT NULL DEFAULT 0",
                "min_traffic_percent REAL NOT NULL DEFAULT 0",
            ]:
                try:
                    self._conn.execute(
                        f"ALTER TABLE client_tokens ADD COLUMN {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for f in ("enabled", "verify_tls", "connected"):
            if f in d:
                d[f] = bool(d[f])
        return d

    # ── settings ──────────────────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now_iso()),
            )
            self._conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            r = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return str(r["value"]) if r else None

    # ── endpoints CRUD ────────────────────────────────────────────────────

    def create_endpoint(self, d: dict[str, Any]) -> dict[str, Any]:
        now = now_iso()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO endpoints(
                        name, base_url, api_key, timeout_seconds, verify_tls,
                        enabled, connected, role, token_quota_percent,
                        priority_order, max_concurrent, pos_x, pos_y,
                        created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    d["name"],
                    d["base_url"],
                    d.get("api_key", ""),
                    float(d.get("timeout_seconds", 120)),
                    int(bool(d.get("verify_tls", True))),
                    int(bool(d.get("enabled", True))),
                    int(bool(d.get("connected", False))),
                    d.get("role", "percentage"),
                    float(d.get("token_quota_percent", 0)),
                    int(d.get("priority_order", 10)),
                    int(d.get("max_concurrent", 0)),
                    float(d.get("pos_x", 500)),
                    float(d.get("pos_y", 200)),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM endpoints WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return self._row(row)

    def update_endpoint(
        self, eid: int, u: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not u:
            return self.get_endpoint(eid)
        allowed = {
            "name",
            "base_url",
            "api_key",
            "timeout_seconds",
            "verify_tls",
            "enabled",
            "connected",
            "role",
            "token_quota_percent",
            "priority_order",
            "max_concurrent",
            "cooldown_until",
            "consecutive_failures",
            "total_requests",
            "total_success",
            "total_failures",
            "avg_latency_ms",
            "prompt_tokens_total",
            "completion_tokens_total",
            "pos_x",
            "pos_y",
        }
        parts: list[str] = []
        vals: list[Any] = []
        for k, v in u.items():
            if k not in allowed:
                continue
            if k in ("enabled", "verify_tls", "connected"):
                v = int(bool(v))
            parts.append(f"{k}=?")
            vals.append(v)
        if not parts:
            return self.get_endpoint(eid)
        vals += [now_iso(), eid]
        with self._lock:
            self._conn.execute(
                f"UPDATE endpoints SET {','.join(parts)}, updated_at=? WHERE id=?",
                vals,
            )
            self._conn.commit()
            r = self._conn.execute(
                "SELECT * FROM endpoints WHERE id=?", (eid,)
            ).fetchone()
        return self._row(r) if r else None

    def delete_endpoint(self, eid: int) -> bool:
        with self._lock:
            c = self._conn.execute("DELETE FROM endpoints WHERE id=?", (eid,))
            self._conn.commit()
        return c.rowcount > 0

    def get_endpoint(self, eid: int) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM endpoints WHERE id=?", (eid,)
            ).fetchone()
        return self._row(r) if r else None

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM endpoints ORDER BY id"
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_connected_healthy(self, now_epoch: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM endpoints "
                "WHERE connected=1 AND enabled=1 AND cooldown_until<=? "
                "ORDER BY id",
                (now_epoch,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_connected_enabled(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM endpoints "
                "WHERE connected=1 AND enabled=1 ORDER BY id"
            ).fetchall()
        return [self._row(r) for r in rows]

    def record_result(
        self,
        eid: int,
        success: bool,
        latency_ms: float,
        fail_threshold: int,
        cooldown_seconds: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> Optional[dict[str, Any]]:
        ep = self.get_endpoint(eid)
        if not ep:
            return None
        tr = int(ep["total_requests"]) + 1
        ts = int(ep["total_success"]) + (1 if success else 0)
        tf = int(ep["total_failures"]) + (0 if success else 1)
        pa = float(ep["avg_latency_ms"])
        avg = ((pa * (tr - 1)) + latency_ms) / tr
        upd: dict[str, Any] = {
            "total_requests": tr,
            "total_success": ts,
            "total_failures": tf,
            "avg_latency_ms": avg,
            "prompt_tokens_total": int(ep["prompt_tokens_total"]) + prompt_tokens,
            "completion_tokens_total": int(ep["completion_tokens_total"])
            + completion_tokens,
        }
        if success:
            upd["consecutive_failures"] = 0
            upd["cooldown_until"] = 0
        else:
            cf = int(ep["consecutive_failures"]) + 1
            upd["consecutive_failures"] = cf
            if cf >= fail_threshold:
                upd["cooldown_until"] = time.time() + cooldown_seconds
        return self.update_endpoint(eid, upd)

    def reset_all_stats(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE endpoints SET "
                "total_requests=0, total_success=0, total_failures=0, "
                "avg_latency_ms=0, prompt_tokens_total=0, "
                "completion_tokens_total=0, cooldown_until=0, "
                "consecutive_failures=0"
            )
            self._conn.commit()

    # ── client tokens CRUD ────────────────────────────────────────────────

    def create_client_token(
        self,
        name: str,
        token: Optional[str] = None,
        preferred_endpoint_id: Optional[int] = None,
        request_priority: int = 0,
        min_traffic_percent: float = 0,
    ) -> dict[str, Any]:
        tok = (token or "").strip() or ("ct-" + secrets.token_urlsafe(32))
        now = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO client_tokens("
                "name, token, preferred_endpoint_id, request_priority, min_traffic_percent, created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    name,
                    tok,
                    preferred_endpoint_id,
                    int(request_priority),
                    float(min_traffic_percent),
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM client_tokens WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_client_tokens(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM client_tokens ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_client_token(self, tid: int) -> bool:
        with self._lock:
            c = self._conn.execute(
                "DELETE FROM client_tokens WHERE id=?", (tid,)
            )
            self._conn.commit()
        return c.rowcount > 0

    def client_token_exists(self, token: str) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM client_tokens WHERE token=?", (token,)
            ).fetchone()
        return r is not None

    def find_client_token_by_value(
        self, token: str
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM client_tokens WHERE token=?", (token,)
            ).fetchone()
        return dict(r) if r else None

    def update_client_token(
        self, tid: int, u: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        allowed = {
            "name",
            "pos_x",
            "pos_y",
            "preferred_endpoint_id",
            "request_priority",
            "min_traffic_percent",
        }
        parts: list[str] = []
        vals: list[Any] = []
        for k, v in u.items():
            if k in allowed:
                parts.append(f"{k}=?")
                vals.append(v)
        if not parts:
            return None
        vals.append(tid)
        with self._lock:
            self._conn.execute(
                f"UPDATE client_tokens SET {','.join(parts)} WHERE id=?",
                vals,
            )
            self._conn.commit()
            r = self._conn.execute(
                "SELECT * FROM client_tokens WHERE id=?", (tid,)
            ).fetchone()
        return dict(r) if r else None

    def clear_client_token_endpoint_mapping(self, eid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE client_tokens SET preferred_endpoint_id=NULL "
                "WHERE preferred_endpoint_id=?",
                (eid,),
            )
            self._conn.commit()

    def record_client_token_usage(
        self, tid: int, prompt_tokens: int, completion_tokens: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE client_tokens SET total_requests=total_requests+1, "
                "prompt_tokens_total=prompt_tokens_total+?, "
                "completion_tokens_total=completion_tokens_total+? "
                "WHERE id=?",
                (prompt_tokens, completion_tokens, tid),
            )
            self._conn.commit()

    def get_client_token_traffic_stats(self, tid: int) -> tuple[int, int]:
        with self._lock:
            total_row = self._conn.execute(
                "SELECT COALESCE(SUM(total_requests),0) AS total FROM client_tokens"
            ).fetchone()
            token_row = self._conn.execute(
                "SELECT total_requests FROM client_tokens WHERE id=?",
                (tid,),
            ).fetchone()
        total = int(total_row["total"]) if total_row else 0
        token_total = int(token_row["total_requests"]) if token_row else 0
        return total, token_total

    def reset_client_token_stats(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE client_tokens SET total_requests=0, "
                "prompt_tokens_total=0, completion_tokens_total=0"
            )
            self._conn.commit()


# ── Pydantic Models ───────────────────────────────────────────────────────────


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=2000)
    timeout_seconds: float = Field(default=120.0, ge=5, le=600)
    verify_tls: bool = True
    enabled: bool = True
    connected: bool = False
    role: str = Field(default="percentage")
    token_quota_percent: float = Field(default=0, ge=0, le=100)
    priority_order: int = Field(default=10, ge=1, le=100)
    max_concurrent: int = Field(default=0, ge=0, le=10000)
    pos_x: float = Field(default=500)
    pos_y: float = Field(default=200)


class EndpointPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    base_url: Optional[str] = Field(default=None, min_length=8, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=2000)
    timeout_seconds: Optional[float] = Field(default=None, ge=5, le=600)
    verify_tls: Optional[bool] = None
    enabled: Optional[bool] = None
    connected: Optional[bool] = None
    role: Optional[str] = None
    token_quota_percent: Optional[float] = Field(default=None, ge=0, le=100)
    priority_order: Optional[int] = Field(default=None, ge=1, le=100)
    max_concurrent: Optional[int] = Field(default=None, ge=0, le=10000)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None


class RoutingPatch(BaseModel):
    routing_mode: Optional[str] = None


class ClientTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    token: Optional[str] = Field(default=None, max_length=500)
    pos_x: float = Field(default=80)
    pos_y: float = Field(default=100)
    preferred_endpoint_id: Optional[int] = Field(default=None, ge=1)
    request_priority: int = Field(default=0, ge=0, le=1000)
    min_traffic_percent: float = Field(default=0, ge=0, le=100)


class ClientTokenPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None
    preferred_endpoint_id: Optional[int] = Field(default=None, ge=1)
    request_priority: Optional[int] = Field(default=None, ge=0, le=1000)
    min_traffic_percent: Optional[float] = Field(default=None, ge=0, le=100)


# ── Routing Engine ────────────────────────────────────────────────────────────


class LoadBalancerEngine:
    def __init__(
        self,
        store: SQLiteStore,
        settings: AppSettings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._transport = transport
        self._runtime: dict[int, RuntimeState] = {}
        self._lock = asyncio.Lock()
        self._verified: Optional[httpx.AsyncClient] = None
        self._insecure: Optional[httpx.AsyncClient] = None
        self._tok_window: list[tuple[float, int]] = []
        self._req_window: list[float] = []
        self._client_req_window: dict[int, list[float]] = {}

    def _make_client(self, verify: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=verify,
            http2=True,
            timeout=None,
            transport=self._transport,
            limits=httpx.Limits(
                max_connections=self.settings.max_connections,
                max_keepalive_connections=self.settings.max_keepalive,
            ),
        )

    async def startup(self) -> None:
        self._verified = self._make_client(True)
        self._insecure = self._make_client(False)

    async def shutdown(self) -> None:
        for c in (self._verified, self._insecure):
            if c and not c.is_closed:
                await c.aclose()

    def client_for(self, ep: dict[str, Any]) -> httpx.AsyncClient:
        c = self._verified if ep.get("verify_tls", True) else self._insecure
        if not c:
            raise RuntimeError("client not initialised")
        return c

    @property
    def routing_mode(self) -> str:
        return self.store.get_setting("routing_mode") or "percentage"

    # ── endpoint selection ────────────────────────────────────────────────

    async def acquire(
        self, preferred_endpoint_id: Optional[int] = None
    ) -> dict[str, Any]:
        now = time.time()
        healthy = await asyncio.to_thread(
            self.store.list_connected_healthy, now
        )
        if not healthy:
            connected = await asyncio.to_thread(
                self.store.list_connected_enabled
            )
            if not connected:
                raise HTTPException(503, "no connected endpoints available")
            raise HTTPException(
                503, "all connected endpoints in cooldown"
            )

        if preferred_endpoint_id is not None:
            chosen = next(
                (
                    e
                    for e in healthy
                    if int(e["id"]) == int(preferred_endpoint_id)
                ),
                None,
            )
            if not chosen:
                connected = await asyncio.to_thread(
                    self.store.list_connected_enabled
                )
                if any(
                    int(e["id"]) == int(preferred_endpoint_id)
                    for e in connected
                ):
                    raise HTTPException(
                        503, "mapped endpoint currently in cooldown"
                    )
                raise HTTPException(503, "mapped endpoint not connected")
        elif self.routing_mode == "priority":
            chosen = await self._pick_priority(healthy)
        else:
            chosen = await self._pick_percentage(healthy)

        async with self._lock:
            rt = self._runtime.setdefault(int(chosen["id"]), RuntimeState())
            rt.inflight += 1
            rt.served += 1
            self._req_window.append(time.time())
        return chosen

    async def _pick_percentage(
        self, eps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        total_tok = sum(
            int(e.get("completion_tokens_total", 0)) for e in eps
        )
        # Cold-start: pick least loaded
        if total_tok == 0:
            async with self._lock:
                return min(
                    eps,
                    key=lambda e: self._runtime.get(
                        int(e["id"]), RuntimeState()
                    ).inflight,
                )

        total_quota = sum(
            float(e.get("token_quota_percent", 0)) for e in eps
        )
        best, best_d = eps[0], -1e18
        for e in eps:
            q = float(e.get("token_quota_percent", 0))
            if total_quota <= 0:
                q = 100.0 / len(eps)
            actual = int(e.get("completion_tokens_total", 0)) / total_tok * 100
            deficit = q - actual
            if deficit > best_d:
                best, best_d = e, deficit
        return best

    async def _pick_priority(
        self, eps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        ordered = sorted(
            eps, key=lambda e: int(e.get("priority_order", 10))
        )
        async with self._lock:
            for e in ordered:
                mc = int(e.get("max_concurrent", 0))
                inf = self._runtime.get(
                    int(e["id"]), RuntimeState()
                ).inflight
                if mc > 0 and inf >= mc:
                    continue
                return e
            return min(
                ordered,
                key=lambda e: self._runtime.get(
                    int(e["id"]), RuntimeState()
                ).inflight,
            )

    async def release(self, eid: int) -> None:
        async with self._lock:
            rt = self._runtime.setdefault(eid, RuntimeState())
            rt.inflight = max(0, rt.inflight - 1)

    def note_tokens(self, n: int) -> None:
        if n > 0:
            self._tok_window.append((time.time(), n))

    def note_request(self) -> None:
        self._req_window.append(time.time())

    def note_client_request(self, token_id: int) -> None:
        w = self._client_req_window.setdefault(token_id, [])
        w.append(time.time())

    def client_stats(self) -> dict[int, dict[str, Any]]:
        now = time.time()
        cut = now - 60
        result: dict[int, dict[str, Any]] = {}
        for tid in list(self._client_req_window):
            self._client_req_window[tid] = [
                t for t in self._client_req_window[tid] if t > cut
            ]
            req60 = len(self._client_req_window[tid])
            result[tid] = {
                "requests_per_second": round(req60 / 60, 1) if req60 else 0,
            }
        return result

    async def stats(self) -> dict[str, Any]:
        now = time.time()
        cut = now - 60
        self._tok_window = [
            (t, n) for t, n in self._tok_window if t > cut
        ]
        self._req_window = [t for t in self._req_window if t > cut]
        tok60 = sum(n for _, n in self._tok_window)
        req60 = len(self._req_window)
        async with self._lock:
            inf = {
                eid: rt.inflight for eid, rt in self._runtime.items()
            }
            total_inf = sum(rt.inflight for rt in self._runtime.values())
        return {
            "tokens_per_second": round(tok60 / 60, 1) if tok60 else 0,
            "requests_per_second": round(req60 / 60, 1) if req60 else 0,
            "total_inflight": total_inf,
            "inflight_per_endpoint": inf,
        }


# ── App Factory ───────────────────────────────────────────────────────────────


def load_settings() -> AppSettings:
    ct_raw = os.getenv("LB_CLIENT_TOKENS", "")
    ct = {t.strip() for t in ct_raw.split(",") if t.strip()}
    return AppSettings(
        db_path=os.getenv("LB_DB_PATH", "./data/load_balancer.db"),
        port=int(os.getenv("LB_PORT", "8090")),
        admin_token=os.getenv(
            "LB_ADMIN_TOKEN", "change-me-admin-token"
        ).strip(),
        client_tokens=ct,
        fail_threshold=max(1, int(os.getenv("LB_FAIL_THRESHOLD", "3"))),
        cooldown_seconds=max(
            1.0, float(os.getenv("LB_COOLDOWN_SECONDS", "20"))
        ),
        max_connections=max(
            10, int(os.getenv("LB_MAX_CONNECTIONS", "500"))
        ),
        max_keepalive=max(10, int(os.getenv("LB_MAX_KEEPALIVE", "200"))),
        default_timeout_seconds=max(
            5.0, float(os.getenv("LB_DEFAULT_TIMEOUT_SECONDS", "120"))
        ),
        cache_max_entries=max(
            100, int(os.getenv("LB_CACHE_MAX_ENTRIES", "5000"))
        ),
        log_level=os.getenv("LB_LOG_LEVEL", "INFO").upper(),
    )


def create_app(
    settings: Optional[AppSettings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    cfg = settings or load_settings()
    logging.basicConfig(level=cfg.log_level)

    store = SQLiteStore(cfg.db_path)
    engine = LoadBalancerEngine(store, cfg, transport=transport)
    cache_lock = asyncio.Lock()
    response_cache: OrderedDict[str, tuple[int, bytes, str, str]] = (
        OrderedDict()
    )

    def build_cache_key(
        client_token_id: int,
        mapped_endpoint_id: Optional[int],
        body: bytes,
    ) -> str:
        h = hashlib.sha256()
        h.update(str(client_token_id).encode("utf-8"))
        h.update(b"|")
        h.update(
            str(mapped_endpoint_id).encode("utf-8")
            if mapped_endpoint_id is not None
            else b"-"
        )
        h.update(b"|")
        h.update(body)
        return h.hexdigest()

    async def cache_get(
        key: str,
    ) -> Optional[tuple[int, bytes, str, str]]:
        async with cache_lock:
            val = response_cache.get(key)
            if val is None:
                return None
            response_cache.move_to_end(key)
            return val

    async def cache_put(
        key: str,
        val: tuple[int, bytes, str, str],
    ) -> None:
        async with cache_lock:
            response_cache[key] = val
            response_cache.move_to_end(key)
            while len(response_cache) > cfg.cache_max_entries:
                response_cache.popitem(last=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.startup()
        LOG.info("Load Balancer v2 ready on port %s", cfg.port)
        if cfg.admin_token == "change-me-admin-token":
            LOG.warning(
                "LB_ADMIN_TOKEN uses the default – set a strong token!"
            )
        try:
            yield
        finally:
            await engine.shutdown()
            store.close()

    app = FastAPI(
        title="LLM Load Balancer", version="2.0.0", lifespan=lifespan
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.engine = engine

    # ── auth dependencies ─────────────────────────────────────────────────

    def require_admin(
        authorization: Optional[str] = Header(None),
        x_admin_token: Optional[str] = Header(None),
    ) -> None:
        # Skip auth when admin token is empty or default placeholder
        if not cfg.admin_token or cfg.admin_token == "change-me-admin-token":
            return
        tok = (x_admin_token or "").strip() or parse_bearer_token(
            authorization
        )
        if tok != cfg.admin_token:
            raise HTTPException(401, "admin authorization failed")

    def require_client(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> None:
        request.state.client_token_id = None
        request.state.client_token = None
        # If no env tokens AND no DB tokens → open access
        has_env = bool(cfg.client_tokens)
        has_db = bool(store.list_client_tokens())
        if not has_env and not has_db:
            return
        tok = parse_bearer_token(authorization)
        if not tok:
            raise HTTPException(401, "client token invalid")
        if tok in cfg.client_tokens:
            return
        rec = store.find_client_token_by_value(tok)
        if rec:
            request.state.client_token_id = rec["id"]
            request.state.client_token = rec
            return
        raise HTTPException(401, "client token invalid")

    # ── internal helpers ──────────────────────────────────────────────────

    async def finish_request(
        eid: int,
        status: int,
        lat_ms: float,
        prompt_t: int = 0,
        compl_t: int = 0,
        client_token_id: Optional[int] = None,
    ) -> None:
        ok = should_count_as_success(status)
        await asyncio.to_thread(
            store.record_result,
            eid,
            ok,
            lat_ms,
            cfg.fail_threshold,
            cfg.cooldown_seconds,
            prompt_t,
            compl_t,
        )
        engine.note_tokens(prompt_t + compl_t)
        if client_token_id is not None:
            await asyncio.to_thread(
                store.record_client_token_usage,
                client_token_id,
                prompt_t,
                compl_t,
            )
            engine.note_client_request(client_token_id)

    async def fail_request(eid: int, lat_ms: float) -> None:
        await asyncio.to_thread(
            store.record_result,
            eid,
            False,
            lat_ms,
            cfg.fail_threshold,
            cfg.cooldown_seconds,
        )

    # ── proxy: chat completions ───────────────────────────────────────────

    async def proxy_chat(request: Request) -> Response:
        client_tid: Optional[int] = getattr(
            request.state, "client_token_id", None
        )
        client_tok: Optional[dict[str, Any]] = getattr(
            request.state, "client_token", None
        )
        mapped_eid: Optional[int] = None
        if client_tok and client_tok.get("preferred_endpoint_id") is not None:
            mapped_eid = int(client_tok["preferred_endpoint_id"])

        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")

        if (
            client_tok
            and client_tok.get("request_priority") is not None
            and body.get("priority") is None
        ):
            body["priority"] = int(client_tok["request_priority"])
            raw = json.dumps(body).encode("utf-8")

        stream = bool(body.get("stream", False))
        min_share = float(client_tok.get("min_traffic_percent", 0)) if client_tok else 0
        cache_key: Optional[str] = None
        cache_only = False
        if client_tid is not None and min_share > 0:
            total_req, token_req = await asyncio.to_thread(
                store.get_client_token_traffic_stats,
                client_tid,
            )
            share = (token_req / total_req * 100) if total_req > 0 else 0.0
            cache_only = total_req > 0 and share >= min_share
            if stream and cache_only:
                raise HTTPException(
                    409,
                    "cache-only traffic requires non-stream requests",
                )
            if not stream:
                cache_key = build_cache_key(client_tid, mapped_eid, raw)

        if cache_only and cache_key:
            cached = await cache_get(cache_key)
            if not cached:
                raise HTTPException(
                    503,
                    "cache miss for over-quota token traffic",
                )
            c_status, c_body, c_media, c_ep = cached
            prompt_t, comp_t = extract_usage(c_body)
            if client_tid is not None:
                await asyncio.to_thread(
                    store.record_client_token_usage,
                    client_tid,
                    prompt_t,
                    comp_t,
                )
                engine.note_client_request(client_tid)
            engine.note_tokens(prompt_t + comp_t)
            engine.note_request()
            return Response(
                content=c_body,
                status_code=c_status,
                headers={
                    "X-LB-Cache": "HIT",
                    "X-LB-Endpoint": c_ep,
                },
                media_type=c_media,
            )

        ep = await engine.acquire(mapped_eid)
        eid = int(ep["id"])
        ep_name = str(ep["name"])

        headers = build_upstream_headers(request, ep)
        url = join_url(str(ep["base_url"]), "/chat/completions")
        timeout = float(
            ep.get("timeout_seconds") or cfg.default_timeout_seconds
        )
        client = engine.client_for(ep)
        start = time.perf_counter()

        # ── non-streaming ─────────────────────────────────────────────
        if not stream:
            try:
                up = await client.post(
                    url, content=raw, headers=headers, timeout=timeout
                )
                lat = (time.perf_counter() - start) * 1000
                pt, ct = extract_usage(up.content)
                await finish_request(
                    eid, up.status_code, lat, pt, ct, client_tid
                )
                if (
                    cache_key
                    and 200 <= up.status_code < 300
                    and up.headers.get("content-type")
                ):
                    await cache_put(
                        cache_key,
                        (
                            up.status_code,
                            bytes(up.content),
                            up.headers.get(
                                "content-type", "application/json"
                            ),
                            ep_name,
                        ),
                    )
                rh = filtered_response_headers(up.headers, ep_name)
                rh["X-LB-Cache"] = "MISS"
                return Response(
                    content=up.content,
                    status_code=up.status_code,
                    headers=rh,
                    media_type=up.headers.get(
                        "content-type", "application/json"
                    ),
                )
            except HTTPException:
                raise
            except Exception as exc:
                await fail_request(
                    eid, (time.perf_counter() - start) * 1000
                )
                raise HTTPException(
                    502, f"upstream error: {exc}"
                )
            finally:
                await engine.release(eid)

        # ── streaming ─────────────────────────────────────────────────
        ctx = client.stream(
            "POST", url, content=raw, headers=headers, timeout=timeout
        )
        try:
            up = await ctx.__aenter__()
        except Exception as exc:
            await fail_request(
                eid, (time.perf_counter() - start) * 1000
            )
            await engine.release(eid)
            raise HTTPException(
                502, f"upstream stream failed: {exc}"
            )

        status = int(up.status_code)
        rh = filtered_response_headers(up.headers, ep_name)

        async def gen():
            failed = False
            last_data = b""
            try:
                try:
                    async for chunk in up.aiter_raw():
                        if chunk:
                            last_data = chunk
                            yield chunk
                except httpx.StreamConsumed:
                    buf = up.content
                    if buf:
                        last_data = buf
                        yield buf
            except Exception:
                failed = True
                raise
            finally:
                try:
                    await ctx.__aexit__(None, None, None)
                finally:
                    lat = (time.perf_counter() - start) * 1000
                    if failed:
                        await fail_request(eid, lat)
                    else:
                        pt, ct = 0, 0
                        try:
                            for line in last_data.decode(
                                "utf-8", errors="ignore"
                            ).split("\n"):
                                stripped = line.strip()
                                if stripped.startswith("data:") and stripped != "data: [DONE]":
                                    j = json.loads(stripped[5:].strip())
                                    u = j.get("usage") or {}
                                    if u.get("completion_tokens"):
                                        pt = int(
                                            u.get("prompt_tokens", 0)
                                        )
                                        ct = int(
                                            u.get(
                                                "completion_tokens", 0
                                            )
                                        )
                        except Exception:
                            pass
                        await finish_request(
                            eid, status, lat, pt, ct, client_tid
                        )
                    await engine.release(eid)

        mt = up.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            gen(), status_code=status, headers=rh, media_type=mt
        )

    # ── proxy: models ─────────────────────────────────────────────────

    async def proxy_models(request: Request) -> JSONResponse:
        client_tok: Optional[dict[str, Any]] = getattr(
            request.state, "client_token", None
        )
        mapped_eid: Optional[int] = None
        if client_tok and client_tok.get("preferred_endpoint_id") is not None:
            mapped_eid = int(client_tok["preferred_endpoint_id"])

        endpoints = await asyncio.to_thread(store.list_connected_enabled)
        if mapped_eid is not None:
            endpoints = [
                e for e in endpoints if int(e["id"]) == int(mapped_eid)
            ]
            if not endpoints:
                raise HTTPException(503, "mapped endpoint not connected")
        if not endpoints:
            raise HTTPException(503, "no connected endpoints")
        combined: dict[str, dict[str, Any]] = {}
        for ep in endpoints:
            eid = int(ep["id"])
            url = join_url(str(ep["base_url"]), "/models")
            headers = build_upstream_headers(request, ep)
            timeout = float(
                ep.get("timeout_seconds") or cfg.default_timeout_seconds
            )
            client = engine.client_for(ep)
            start = time.perf_counter()
            try:
                r = await client.get(
                    url, headers=headers, timeout=timeout
                )
                lat = (time.perf_counter() - start) * 1000
                await finish_request(eid, r.status_code, lat)
                if 200 <= r.status_code < 300:
                    for m in r.json().get("data", []):
                        mid = m.get("id")
                        if mid:
                            combined[mid] = m
            except Exception:
                await fail_request(
                    eid, (time.perf_counter() - start) * 1000
                )
        if not combined:
            raise HTTPException(
                502, "no models returned from any endpoint"
            )
        ordered = [combined[k] for k in sorted(combined)]
        return JSONResponse({"object": "list", "data": ordered})

    # ── routes: generic ───────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin")

    @app.get("/health")
    async def health():
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        return JSONResponse(
            {
                "status": "ok",
                "endpoints": len(eps),
                "connected": sum(
                    1 for e in eps if e.get("connected")
                ),
                "routing_mode": engine.routing_mode,
                **st,
            }
        )

    @app.get(
        "/admin", response_class=HTMLResponse, include_in_schema=False
    )
    async def admin_ui():
        p = Path(__file__).with_name("load_balancer_admin.html")
        if not p.exists():
            raise HTTPException(500, "admin UI missing")
        return HTMLResponse(p.read_text(encoding="utf-8"))

    # ── routes: admin API ─────────────────────────────────────────────

    @app.get(
        "/admin/api/state", dependencies=[Depends(require_admin)]
    )
    async def get_state():
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        inf = st.get("inflight_per_endpoint", {})
        safe = []
        for e in eps:
            eid = int(e["id"])
            enriched = dict(e)
            enriched["inflight"] = inf.get(eid, 0)
            enriched["cooldown_remaining_s"] = max(
                0.0, float(e.get("cooldown_until", 0)) - time.time()
            )
            safe.append(sanitize_endpoint(enriched))
        ix = float(store.get_setting("incoming_pos_x") or 80)
        iy = float(store.get_setting("incoming_pos_y") or 300)
        # client tokens with stats
        tokens_raw = await asyncio.to_thread(store.list_client_tokens)
        cstats = engine.client_stats()
        tokens_safe = []
        endpoint_name_by_id = {
            int(e["id"]): str(e["name"]) for e in eps
        }
        for t in tokens_raw:
            tid = t["id"]
            cs = cstats.get(tid, {})
            preferred_eid = t.get("preferred_endpoint_id")
            tokens_safe.append({
                "id": tid,
                "name": t["name"],
                "token_preview": mask_secret(t["token"]),
                "created_at": t["created_at"],
                "pos_x": t.get("pos_x", 80),
                "pos_y": t.get("pos_y", 100),
                "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "requests_per_second": cs.get("requests_per_second", 0),
                "preferred_endpoint_id": preferred_eid,
                "preferred_endpoint_name": endpoint_name_by_id.get(
                    int(preferred_eid)
                )
                if preferred_eid is not None
                else None,
                "request_priority": t.get("request_priority", 0),
                "min_traffic_percent": t.get("min_traffic_percent", 0),
            })
        return JSONResponse(
            {
                "routing_mode": engine.routing_mode,
                "endpoints": safe,
                "incoming_pos": {"x": ix, "y": iy},
                "stats": st,
                "client_tokens": tokens_safe,
            }
        )

    @app.post(
        "/admin/api/endpoints",
        dependencies=[Depends(require_admin)],
    )
    async def create_ep(payload: EndpointCreate):
        d = payload.model_dump()
        d["base_url"] = normalize_base_url(d["base_url"])
        try:
            ep = await asyncio.to_thread(store.create_endpoint, d)
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        return JSONResponse(sanitize_endpoint(ep))

    @app.patch(
        "/admin/api/endpoints/{eid}",
        dependencies=[Depends(require_admin)],
    )
    async def patch_ep(eid: int, payload: EndpointPatch):
        u = payload.model_dump(exclude_none=True)
        if "base_url" in u:
            u["base_url"] = normalize_base_url(u["base_url"])
        if "name" in u:
            u["name"] = u["name"].strip()
        try:
            ep = await asyncio.to_thread(store.update_endpoint, eid, u)
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        if not ep:
            raise HTTPException(404, "endpoint not found")
        return JSONResponse(sanitize_endpoint(ep))

    @app.delete(
        "/admin/api/endpoints/{eid}",
        dependencies=[Depends(require_admin)],
    )
    async def delete_ep(eid: int):
        await asyncio.to_thread(
            store.clear_client_token_endpoint_mapping, eid
        )
        if not await asyncio.to_thread(store.delete_endpoint, eid):
            raise HTTPException(404, "endpoint not found")
        return JSONResponse({"ok": True})

    @app.patch(
        "/admin/api/routing",
        dependencies=[Depends(require_admin)],
    )
    async def patch_routing(payload: RoutingPatch):
        if payload.routing_mode and payload.routing_mode in (
            "percentage",
            "priority",
        ):
            await asyncio.to_thread(
                store.set_setting, "routing_mode", payload.routing_mode
            )
        return JSONResponse({"routing_mode": engine.routing_mode})

    @app.post(
        "/admin/api/reset-stats",
        dependencies=[Depends(require_admin)],
    )
    async def reset_stats():
        await asyncio.to_thread(store.reset_all_stats)
        await asyncio.to_thread(store.reset_client_token_stats)
        engine._tok_window.clear()
        engine._req_window.clear()
        engine._client_req_window.clear()
        return JSONResponse({"ok": True})

    # ── routes: client token management ────────────────────────────────

    @app.get(
        "/admin/api/tokens",
        dependencies=[Depends(require_admin)],
    )
    async def list_tokens():
        tokens = await asyncio.to_thread(store.list_client_tokens)
        safe = []
        for t in tokens:
            preferred_eid = t.get("preferred_endpoint_id")
            safe.append({
                "id": t["id"],
                "name": t["name"],
                "token_preview": mask_secret(t["token"]),
                "token": t["token"],
                "created_at": t["created_at"],
                "pos_x": t.get("pos_x", 80),
                "pos_y": t.get("pos_y", 100),
                "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "preferred_endpoint_id": preferred_eid,
                "request_priority": t.get("request_priority", 0),
                "min_traffic_percent": t.get("min_traffic_percent", 0),
            })
        return JSONResponse(safe)

    @app.post(
        "/admin/api/tokens",
        dependencies=[Depends(require_admin)],
    )
    async def create_token(payload: ClientTokenCreate):
        if payload.preferred_endpoint_id is not None:
            ep = await asyncio.to_thread(
                store.get_endpoint, payload.preferred_endpoint_id
            )
            if not ep:
                raise HTTPException(404, "preferred endpoint not found")
        try:
            tok = await asyncio.to_thread(
                store.create_client_token,
                payload.name,
                payload.token,
                payload.preferred_endpoint_id,
                payload.request_priority,
                payload.min_traffic_percent,
            )
            # Update position if provided
            if tok and (payload.pos_x != 80 or payload.pos_y != 100):
                await asyncio.to_thread(
                    store.update_client_token,
                    tok["id"],
                    {"pos_x": payload.pos_x, "pos_y": payload.pos_y},
                )
                tok["pos_x"] = payload.pos_x
                tok["pos_y"] = payload.pos_y
        except sqlite3.IntegrityError:
            raise HTTPException(409, "token already exists")
        return JSONResponse(tok)

    @app.patch(
        "/admin/api/tokens/{tid}",
        dependencies=[Depends(require_admin)],
    )
    async def patch_token(tid: int, payload: ClientTokenPatch):
        u = payload.model_dump(exclude_none=True)
        if not u:
            raise HTTPException(400, "nothing to update")
        if u.get("preferred_endpoint_id") is not None:
            ep = await asyncio.to_thread(
                store.get_endpoint, int(u["preferred_endpoint_id"])
            )
            if not ep:
                raise HTTPException(404, "preferred endpoint not found")
        result = await asyncio.to_thread(
            store.update_client_token, tid, u
        )
        if not result:
            raise HTTPException(404, "token not found")
        return JSONResponse(result)

    @app.delete(
        "/admin/api/tokens/{tid}",
        dependencies=[Depends(require_admin)],
    )
    async def delete_token(tid: int):
        if not await asyncio.to_thread(store.delete_client_token, tid):
            raise HTTPException(404, "token not found")
        return JSONResponse({"ok": True})

    @app.patch(
        "/admin/api/incoming-pos",
        dependencies=[Depends(require_admin)],
    )
    async def patch_incoming_pos(request: Request):
        body = await request.json()
        if "x" in body:
            await asyncio.to_thread(
                store.set_setting, "incoming_pos_x", str(float(body["x"]))
            )
        if "y" in body:
            await asyncio.to_thread(
                store.set_setting, "incoming_pos_y", str(float(body["y"]))
            )
        return JSONResponse({"ok": True})

    # ── routes: proxy ─────────────────────────────────────────────────

    @app.post(
        "/v1/chat/completions",
        dependencies=[Depends(require_client)],
    )
    async def chat(request: Request):
        return await proxy_chat(request)

    @app.post(
        "/chat/completions",
        dependencies=[Depends(require_client)],
    )
    async def chat_legacy(request: Request):
        return await proxy_chat(request)

    @app.post(
        "/v1/{pool_name}/chat/completions",
        dependencies=[Depends(require_client)],
    )
    async def chat_pool(pool_name: str, request: Request):
        return await proxy_chat(request)

    @app.get(
        "/v1/models", dependencies=[Depends(require_client)]
    )
    async def models(request: Request):
        return await proxy_models(request)

    @app.get(
        "/models", dependencies=[Depends(require_client)]
    )
    async def models_legacy(request: Request):
        return await proxy_models(request)

    @app.get(
        "/v1/{pool_name}/models",
        dependencies=[Depends(require_client)],
    )
    async def models_pool(pool_name: str, request: Request):
        return await proxy_models(request)

    return app


app = create_app()

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run(
        "load_balancer:app",
        host="0.0.0.0",
        port=s.port,
        reload=False,
        log_level=s.log_level.lower(),
    )
