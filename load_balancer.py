"""LLM Load Balancer v3 – Enterprise-Grade Visual Flow Routing.

Endpoints replace the old pool/target model.  Incoming requests are routed
to *connected* endpoints using either **percentage** (token-quota) or
**priority** (primary + overflow) mode.  Every response’s ``usage`` block
is parsed so that per-endpoint and global token throughput can be displayed
in the n8n-style admin UI.

Enterprise improvements (v3):
- Structured logging with correlation / request IDs
- RFC 7807 Problem Details error responses
- Cache TTL + LRU eviction
- Bounded sliding windows (memory-safe)
- Graceful shutdown with drain timeout
- CORS configuration
- Parallel /models aggregation
- Input validation hardening
- Security headers middleware
- Rate limiting for admin API
- Constant-time token comparison
- Prometheus-compatible /metrics endpoint
- Audit logging for admin operations
- Request body size enforcement
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field, field_validator

# ── Logging ───────────────────────────────────────────────────────────────────────────

LOG = logging.getLogger("load-balancer")

# ── Version ───────────────────────────────────────────────────────────────────────────

__version__ = "3.0.0"


# ── Settings ──────────────────────────────────────────────────────────────────────────


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
    cache_ttl_seconds: float = 300.0
    log_level: str = "INFO"
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MB
    admin_rate_limit_rpm: int = 300
    drain_timeout_seconds: float = 30.0
    sliding_window_seconds: float = 60.0
    sliding_window_max_entries: int = 100_000


@dataclass
class RuntimeState:
    inflight: int = 0
    served: int = 0


# ── Custom Exceptions ───────────────────────────────────────────────────────────────


class LoadBalancerError(Exception):
    """Base exception for load balancer errors."""

    def __init__(
        self,
        detail: str,
        status_code: int = 500,
        error_type: str = "internal_error",
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.error_type = error_type


# ── Helpers ───────────────────────────────────────────────────────────────────────────


def now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def generate_request_id() -> str:
    """Generate a unique request ID for correlation."""
    return str(uuid.uuid4())


def parse_bearer_token(value: Optional[str]) -> str:
    """Extract bearer token from Authorization header."""
    if not value:
        return ""
    parts = value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def normalize_base_url(value: str) -> str:
    """Normalize and validate a base URL."""
    url = (value or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    return url


def join_url(base: str, path: str) -> str:
    """Join base URL and path safely."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def should_count_as_success(status_code: int) -> bool:
    """Determine if an HTTP status code should be counted as success."""
    return not (status_code >= 500 or status_code in (401, 403, 429))


def filtered_response_headers(
    headers: httpx.Headers, endpoint_name: str, request_id: str = ""
) -> dict[str, str]:
    """Filter upstream response headers and add LB metadata."""
    out: dict[str, str] = {}
    _passthrough = {"content-type", "cache-control", "retry-after"}
    for k, v in headers.items():
        lo = k.lower()
        if lo in _passthrough:
            out[k] = v
        elif lo.startswith("x-ratelimit") or lo.startswith("x-request-id"):
            out[k] = v
    out["X-LB-Endpoint"] = endpoint_name
    out["X-LB-Version"] = __version__
    if request_id:
        out["X-Request-ID"] = request_id
    return out


def build_upstream_headers(
    request: Request, endpoint: dict[str, Any]
) -> dict[str, str]:
    """Build headers for upstream request, sanitizing blocked headers."""
    _blocked = {
        "host", "connection", "content-length", "authorization",
        "accept-encoding", "x-admin-token", "transfer-encoding",
    }
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _blocked or k.lower().startswith("x-lb-"):
            continue
        headers[k] = v
    api_key = endpoint.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if "content-type" not in {h.lower() for h in headers}:
        headers["Content-Type"] = "application/json"
    req_id = getattr(request.state, "request_id", "")
    if req_id:
        headers["X-Request-ID"] = req_id
    return headers


def mask_secret(value: str) -> str:
    """Mask a secret value for display, showing only prefix/suffix."""
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return f"{s[:2]}***"
    return f"{s[:4]}...{s[-4:]}"


def sanitize_endpoint(ep: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive data from endpoint dict for API responses."""
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


def problem_detail(
    status: int,
    detail: str,
    error_type: str = "about:blank",
    request_id: str = "",
    **extra: Any,
) -> JSONResponse:
    """Return an RFC 7807 Problem Details JSON response."""
    body: dict[str, Any] = {
        "type": error_type,
        "status": status,
        "detail": detail,
    }
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return JSONResponse(body, status_code=status)


# ── Bounded Sliding Window ────────────────────────────────────────────────────────


class SlidingWindow:
    """Thread-safe bounded sliding window for rate/throughput tracking."""

    def __init__(
        self, window_seconds: float = 60.0, max_entries: int = 100_000
    ) -> None:
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._entries: list[tuple[float, float]] = []

    def add(self, value: float = 1.0) -> None:
        now = time.time()
        self._entries.append((now, value))
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries:]

    def trim(self) -> None:
        cutoff = time.time() - self._window_seconds
        self._entries = [(t, v) for t, v in self._entries if t > cutoff]

    def sum(self) -> float:
        self.trim()
        return sum(v for _, v in self._entries)

    def count(self) -> int:
        self.trim()
        return len(self._entries)

    def rate_per_second(self) -> float:
        s = self.sum()
        return round(s / self._window_seconds, 1) if s else 0

    def count_per_second(self) -> float:
        c = self.count()
        return round(c / self._window_seconds, 1) if c else 0

    def clear(self) -> None:
        self._entries.clear()


# ── TTL Cache ─────────────────────────────────────────────────────────────────────


class TTLCache:
    """LRU cache with TTL expiration for response caching."""

    def __init__(self, max_entries: int = 5000, ttl_seconds: float = 300.0) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.time() - ts > self._ttl_seconds:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return val

    async def put(self, key: str, val: Any) -> None:
        async with self._lock:
            self._store[key] = (time.time(), val)
            self._store.move_to_end(key)
            self._evict()

    def _evict(self) -> None:
        now = time.time()
        expired = [
            k for k, (ts, _) in self._store.items()
            if now - ts > self._ttl_seconds
        ]
        for k in expired:
            del self._store[k]
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# ── Rate Limiter ──────────────────────────────────────────────────────────────────


class RateLimiter:
    """Simple in-memory rate limiter using sliding window."""

    def __init__(self, rpm: int = 300) -> None:
        self._rpm = rpm
        self._window = SlidingWindow(window_seconds=60.0, max_entries=rpm * 2)

    def check(self) -> bool:
        if self._rpm <= 0:
            return True
        count = self._window.count()
        if count >= self._rpm:
            return False
        self._window.add(1.0)
        return True

    @property
    def remaining(self) -> int:
        return max(0, self._rpm - self._window.count())


# ── Audit Logger ─────────────────────────────────────────────────────────────────


class AuditLogger:
    """Structured audit logging for admin operations."""

    def __init__(self) -> None:
        self._log = logging.getLogger("load-balancer.audit")

    def log(
        self,
        action: str,
        request_id: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        entry = {
            "timestamp": now_iso(),
            "action": action,
            "request_id": request_id,
        }
        if details:
            entry["details"] = details
        self._log.info(json.dumps(entry))


# ── SQLite Store ─────────────────────────────────────────────────────────────────


class SQLiteStore:
    """Persistent storage for endpoints and settings.

    Uses WAL mode for better concurrent read performance and
    thread-safe access via a threading lock.
    """

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
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA cache_size = -8000")  # 8MB cache
        self._init_schema()

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
                    incoming_block_id INTEGER,
                    preferred_endpoint_id INTEGER,
                    request_priority INTEGER NOT NULL DEFAULT 0,
                    min_traffic_percent REAL NOT NULL DEFAULT 0,
                    created_at TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incoming_blocks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT    NOT NULL,
                    pos_x      REAL    NOT NULL DEFAULT 256,
                    pos_y      REAL    NOT NULL DEFAULT 300,
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ep_conn
                    ON endpoints(connected, enabled, cooldown_until);

                CREATE INDEX IF NOT EXISTS idx_ct_token
                    ON client_tokens(token);
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
                "incoming_block_id INTEGER",
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

            block_count_row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM incoming_blocks"
            ).fetchone()
            block_count = int(block_count_row["c"]) if block_count_row else 0
            if block_count == 0:
                ix_row = self._conn.execute(
                    "SELECT value FROM settings WHERE key='incoming_pos_x'"
                ).fetchone()
                iy_row = self._conn.execute(
                    "SELECT value FROM settings WHERE key='incoming_pos_y'"
                ).fetchone()
                ix = float(ix_row["value"]) if ix_row else 256.0
                iy = float(iy_row["value"]) if iy_row else 300.0
                now = now_iso()
                self._conn.execute(
                    "INSERT INTO incoming_blocks(name,pos_x,pos_y,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    ("Incoming 1", ix, iy, now, now),
                )

            default_block = self._conn.execute(
                "SELECT id FROM incoming_blocks ORDER BY id LIMIT 1"
            ).fetchone()
            if default_block:
                self._conn.execute(
                    "UPDATE client_tokens SET incoming_block_id=? "
                    "WHERE incoming_block_id IS NULL",
                    (int(default_block["id"]),),
                )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection and run optimize."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            self._conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for f in ("enabled", "verify_tls", "connected"):
            if f in d:
                d[f] = bool(d[f])
        return d

    # -- settings --

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

    # -- endpoints CRUD --

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
            "name", "base_url", "api_key", "timeout_seconds", "verify_tls",
            "enabled", "connected", "role", "token_quota_percent",
            "priority_order", "max_concurrent", "cooldown_until",
            "consecutive_failures", "total_requests", "total_success",
            "total_failures", "avg_latency_ms", "prompt_tokens_total",
            "completion_tokens_total", "pos_x", "pos_y",
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

    # -- incoming blocks CRUD --

    def list_incoming_blocks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incoming_blocks ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_incoming_block(self, bid: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incoming_blocks WHERE id=?", (bid,)
            ).fetchone()
        return dict(row) if row else None

    def create_incoming_block(
        self, name: str, pos_x: float, pos_y: float
    ) -> dict[str, Any]:
        now = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO incoming_blocks(name,pos_x,pos_y,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (name, float(pos_x), float(pos_y), now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM incoming_blocks WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def update_incoming_block(
        self, bid: int, u: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        allowed = {"name", "pos_x", "pos_y"}
        parts: list[str] = []
        vals: list[Any] = []
        for k, v in u.items():
            if k not in allowed:
                continue
            parts.append(f"{k}=?")
            vals.append(v)
        if not parts:
            return self.get_incoming_block(bid)
        vals += [now_iso(), bid]
        with self._lock:
            self._conn.execute(
                f"UPDATE incoming_blocks SET {','.join(parts)}, updated_at=? WHERE id=?",
                vals,
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM incoming_blocks WHERE id=?", (bid,)
            ).fetchone()
        return dict(row) if row else None

    def delete_incoming_block(self, bid: int) -> bool:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM incoming_blocks ORDER BY id"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if bid not in ids:
                return False
            if len(ids) <= 1:
                raise ValueError("cannot delete last incoming block")
            fallback_id = next(i for i in ids if i != bid)
            self._conn.execute(
                "UPDATE client_tokens SET incoming_block_id=? WHERE incoming_block_id=?",
                (fallback_id, bid),
            )
            cur = self._conn.execute(
                "DELETE FROM incoming_blocks WHERE id=?", (bid,)
            )
            self._conn.commit()
        return cur.rowcount > 0

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
        # Use exponential moving average for latency (stable over time)
        alpha = 0.1
        pa = float(ep["avg_latency_ms"])
        if pa == 0:
            avg = latency_ms
        else:
            avg = alpha * latency_ms + (1 - alpha) * pa
        upd: dict[str, Any] = {
            "total_requests": tr,
            "total_success": ts,
            "total_failures": tf,
            "avg_latency_ms": round(avg, 2),
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
                LOG.warning(
                    "Endpoint %s entered cooldown after %d consecutive failures",
                    ep.get("name", eid),
                    cf,
                )
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

    # -- client tokens CRUD --

    def create_client_token(
        self,
        name: str,
        token: Optional[str] = None,
        incoming_block_id: Optional[int] = None,
        preferred_endpoint_id: Optional[int] = None,
        request_priority: int = 0,
        min_traffic_percent: float = 0,
    ) -> dict[str, Any]:
        tok = (token or "").strip() or ("ct-" + secrets.token_urlsafe(32))
        now = now_iso()
        with self._lock:
            ibid = incoming_block_id
            if ibid is None:
                default_block = self._conn.execute(
                    "SELECT id FROM incoming_blocks ORDER BY id LIMIT 1"
                ).fetchone()
                ibid = int(default_block["id"]) if default_block else None
            cur = self._conn.execute(
                "INSERT INTO client_tokens("
                "name, token, incoming_block_id, preferred_endpoint_id, "
                "request_priority, min_traffic_percent, created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    name, tok, ibid, preferred_endpoint_id,
                    int(request_priority), float(min_traffic_percent), now,
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
            "name", "pos_x", "pos_y", "incoming_block_id",
            "preferred_endpoint_id", "request_priority",
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

    def vacuum(self) -> None:
        """Run VACUUM to reclaim space and optimize the database."""
        with self._lock:
            self._conn.execute("VACUUM")


# ── Pydantic Models ───────────────────────────────────────────────────────────────


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=2000)
    api_key: str = Field(default="", max_length=4000)
    timeout_seconds: float = Field(default=120.0, ge=1, le=3600)
    verify_tls: bool = True
    enabled: bool = True
    connected: bool = False
    role: str = Field(default="percentage")
    token_quota_percent: float = Field(default=0, ge=0, le=100)
    priority_order: int = Field(default=10, ge=1, le=1000)
    max_concurrent: int = Field(default=0, ge=0, le=100_000)
    pos_x: float = Field(default=500)
    pos_y: float = Field(default=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if not re.match(r"^[\w\-. ]+$", v):
            raise ValueError("name contains invalid characters")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"percentage", "primary", "overflow"}
        if v not in allowed:
            raise ValueError(f"role must be one of {allowed}")
        return v


class EndpointPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    base_url: Optional[str] = Field(default=None, min_length=8, max_length=2000)
    api_key: Optional[str] = Field(default=None, max_length=4000)
    timeout_seconds: Optional[float] = Field(default=None, ge=1, le=3600)
    verify_tls: Optional[bool] = None
    enabled: Optional[bool] = None
    connected: Optional[bool] = None
    role: Optional[str] = None
    token_quota_percent: Optional[float] = Field(default=None, ge=0, le=100)
    priority_order: Optional[int] = Field(default=None, ge=1, le=1000)
    max_concurrent: Optional[int] = Field(default=None, ge=0, le=100_000)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None


class RoutingPatch(BaseModel):
    routing_mode: Optional[str] = None
    priority_input_token_id: Optional[int] = Field(default=None, ge=0)

    @field_validator("routing_mode")
    @classmethod
    def validate_routing_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("percentage", "priority"):
            raise ValueError("routing_mode must be 'percentage' or 'priority'")
        return v


class ClientTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    token: Optional[str] = Field(default=None, max_length=500)
    pos_x: float = Field(default=80)
    pos_y: float = Field(default=100)
    incoming_block_id: Optional[int] = Field(default=None, ge=1)
    preferred_endpoint_id: Optional[int] = Field(default=None, ge=1)
    request_priority: int = Field(default=0, ge=0, le=100_000)
    min_traffic_percent: float = Field(default=0, ge=0, le=100)


class ClientTokenPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None
    incoming_block_id: Optional[int] = Field(default=None, ge=1)
    preferred_endpoint_id: Optional[int] = Field(default=None, ge=1)
    request_priority: Optional[int] = Field(default=None, ge=0, le=100_000)
    min_traffic_percent: Optional[float] = Field(default=None, ge=0, le=100)


class IncomingBlockCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    pos_x: float = Field(default=256)
    pos_y: float = Field(default=300)


class IncomingBlockPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None


# ── Routing Engine ─────────────────────────────────────────────────────────────


class LoadBalancerEngine:
    """Core routing engine with connection pooling and throughput tracking."""

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
        self._tok_window = SlidingWindow(
            settings.sliding_window_seconds, settings.sliding_window_max_entries
        )
        self._req_window = SlidingWindow(
            settings.sliding_window_seconds, settings.sliding_window_max_entries
        )
        self._client_req_windows: dict[int, SlidingWindow] = {}
        self._total_requests = 0
        self._total_errors = 0
        self._start_time = time.time()

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
        self._start_time = time.time()
        LOG.info(
            "Engine started: max_connections=%d, max_keepalive=%d",
            self.settings.max_connections,
            self.settings.max_keepalive,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown: wait for in-flight requests then close clients."""
        LOG.info("Initiating graceful shutdown...")
        deadline = time.time() + self.settings.drain_timeout_seconds
        while time.time() < deadline:
            async with self._lock:
                total_inf = sum(rt.inflight for rt in self._runtime.values())
            if total_inf == 0:
                break
            LOG.info("Draining %d in-flight requests...", total_inf)
            await asyncio.sleep(0.5)
        for c in (self._verified, self._insecure):
            if c and not c.is_closed:
                await c.aclose()
        LOG.info("Engine shutdown complete")

    def client_for(self, ep: dict[str, Any]) -> httpx.AsyncClient:
        c = self._verified if ep.get("verify_tls", True) else self._insecure
        if not c:
            raise RuntimeError("HTTP client not initialised")
        return c

    @property
    def routing_mode(self) -> str:
        return self.store.get_setting("routing_mode") or "percentage"

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    async def acquire(
        self,
        preferred_endpoint_id: Optional[int] = None,
        prefer_non_primary: bool = False,
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
            raise HTTPException(503, "all connected endpoints in cooldown")

        if preferred_endpoint_id is not None:
            chosen = next(
                (e for e in healthy if int(e["id"]) == int(preferred_endpoint_id)),
                None,
            )
            if not chosen:
                connected = await asyncio.to_thread(
                    self.store.list_connected_enabled
                )
                if any(int(e["id"]) == int(preferred_endpoint_id) for e in connected):
                    raise HTTPException(503, "mapped endpoint currently in cooldown")
                raise HTTPException(503, "mapped endpoint not connected")
        elif self.routing_mode == "priority":
            chosen = await self._pick_priority(
                healthy, prefer_non_primary=prefer_non_primary
            )
        else:
            chosen = await self._pick_percentage(healthy)

        async with self._lock:
            rt = self._runtime.setdefault(int(chosen["id"]), RuntimeState())
            rt.inflight += 1
            rt.served += 1
            self._req_window.add(1.0)
            self._total_requests += 1
        return chosen

    async def _pick_percentage(
        self, eps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        total_tok = sum(int(e.get("completion_tokens_total", 0)) for e in eps)
        if total_tok == 0:
            async with self._lock:
                return min(
                    eps,
                    key=lambda e: self._runtime.get(
                        int(e["id"]), RuntimeState()
                    ).inflight,
                )
        total_quota = sum(float(e.get("token_quota_percent", 0)) for e in eps)
        best, best_d = eps[0], -1e18
        for e in eps:
            q = float(e.get("token_quota_percent", 0))
            if total_quota <= 0:
                q = 100.0 / len(eps)
            actual = int(e.get("completion_tokens_total", 0)) / total_tok * 100
            deficit = q - actual
            async with self._lock:
                inf = self._runtime.get(int(e["id"]), RuntimeState()).inflight
            adjusted = deficit - inf * 0.01
            if adjusted > best_d:
                best, best_d = e, adjusted
        return best

    async def _pick_priority(
        self,
        eps: list[dict[str, Any]],
        prefer_non_primary: bool = False,
    ) -> dict[str, Any]:
        def _priority_sort_key(e: dict[str, Any]) -> tuple[int, int, int, float, int]:
            req_count = int(e.get("total_requests", 0) or 0)
            avg_latency = float(e.get("avg_latency_ms", 0.0) or 0.0)
            latency_rank = avg_latency if req_count > 0 else 0.0
            return (
                int(e.get("priority_order", 10) or 10),
                int(e.get("consecutive_failures", 0) or 0),
                int(e.get("total_failures", 0) or 0),
                latency_rank,
                int(e.get("id", 0) or 0),
            )

        ordered = sorted(eps, key=_priority_sort_key)
        if prefer_non_primary and len(ordered) > 1:
            ordered = ordered[1:] + ordered[:1]
        async with self._lock:
            for e in ordered:
                mc = int(e.get("max_concurrent", 0))
                inf = self._runtime.get(int(e["id"]), RuntimeState()).inflight
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
            self._tok_window.add(float(n))

    def note_request(self) -> None:
        self._req_window.add(1.0)

    def note_error(self) -> None:
        self._total_errors += 1

    def note_client_request(self, token_id: int) -> None:
        if token_id not in self._client_req_windows:
            self._client_req_windows[token_id] = SlidingWindow(60.0, 10_000)
        self._client_req_windows[token_id].add(1.0)

    def client_stats(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for tid, window in list(self._client_req_windows.items()):
            result[tid] = {"requests_per_second": window.count_per_second()}
        return result

    async def stats(self) -> dict[str, Any]:
        tok_rate = self._tok_window.rate_per_second()
        req_rate = self._req_window.count_per_second()
        async with self._lock:
            inf = {eid: rt.inflight for eid, rt in self._runtime.items()}
            total_inf = sum(rt.inflight for rt in self._runtime.values())
        return {
            "tokens_per_second": tok_rate,
            "requests_per_second": req_rate,
            "total_inflight": total_inf,
            "inflight_per_endpoint": inf,
            "total_requests_served": self._total_requests,
            "total_errors": self._total_errors,
            "uptime_seconds": round(self.uptime_seconds, 1),
        }

    async def prometheus_metrics(self) -> str:
        """Generate Prometheus-compatible metrics output."""
        st = await self.stats()
        eps = await asyncio.to_thread(self.store.list_endpoints)
        lines = [
            "# HELP lb_requests_total Total requests served",
            "# TYPE lb_requests_total counter",
            f"lb_requests_total {st['total_requests_served']}",
            "",
            "# HELP lb_errors_total Total errors",
            "# TYPE lb_errors_total counter",
            f"lb_errors_total {st['total_errors']}",
            "",
            "# HELP lb_inflight_total Current in-flight requests",
            "# TYPE lb_inflight_total gauge",
            f"lb_inflight_total {st['total_inflight']}",
            "",
            "# HELP lb_tokens_per_second Token throughput",
            "# TYPE lb_tokens_per_second gauge",
            f"lb_tokens_per_second {st['tokens_per_second']}",
            "",
            "# HELP lb_requests_per_second Request throughput",
            "# TYPE lb_requests_per_second gauge",
            f"lb_requests_per_second {st['requests_per_second']}",
            "",
            "# HELP lb_uptime_seconds Load balancer uptime",
            "# TYPE lb_uptime_seconds gauge",
            f"lb_uptime_seconds {st['uptime_seconds']}",
            "",
            "# HELP lb_endpoint_requests_total Requests per endpoint",
            "# TYPE lb_endpoint_requests_total counter",
        ]
        for ep in eps:
            name = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(
                f'lb_endpoint_requests_total{{endpoint="{name}"}} {ep.get("total_requests", 0)}'
            )
        lines.append("")
        lines.append("# HELP lb_endpoint_failures_total Failures per endpoint")
        lines.append("# TYPE lb_endpoint_failures_total counter")
        for ep in eps:
            name = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(
                f'lb_endpoint_failures_total{{endpoint="{name}"}} {ep.get("total_failures", 0)}'
            )
        lines.append("")
        lines.append("# HELP lb_endpoint_latency_ms Average latency per endpoint")
        lines.append("# TYPE lb_endpoint_latency_ms gauge")
        for ep in eps:
            name = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(
                f'lb_endpoint_latency_ms{{endpoint="{name}"}} {ep.get("avg_latency_ms", 0)}'
            )
        lines.append("")
        lines.append("# HELP lb_endpoint_inflight In-flight per endpoint")
        lines.append("# TYPE lb_endpoint_inflight gauge")
        for ep in eps:
            name = ep.get("name", "unknown").replace('"', '\\"')
            inf_val = st["inflight_per_endpoint"].get(int(ep["id"]), 0)
            lines.append(
                f'lb_endpoint_inflight{{endpoint="{name}"}} {inf_val}'
            )
        lines.append("")
        lines.append("# HELP lb_endpoint_tokens_total Tokens per endpoint")
        lines.append("# TYPE lb_endpoint_tokens_total counter")
        for ep in eps:
            name = ep.get("name", "unknown").replace('"', '\\"')
            total = int(ep.get("prompt_tokens_total", 0)) + int(
                ep.get("completion_tokens_total", 0)
            )
            lines.append(
                f'lb_endpoint_tokens_total{{endpoint="{name}"}} {total}'
            )
        return "\n".join(lines) + "\n"


# ── App Factory ─────────────────────────────────────────────────────────────────


def load_settings() -> AppSettings:
    """Load settings from environment variables with validation."""
    ct_raw = os.getenv("LB_CLIENT_TOKENS", "")
    ct = {t.strip() for t in ct_raw.split(",") if t.strip()}
    cors_raw = os.getenv("LB_CORS_ORIGINS", "*")
    cors = [o.strip() for o in cors_raw.split(",") if o.strip()]

    settings = AppSettings(
        db_path=os.getenv("LB_DB_PATH", "./data/load_balancer.db"),
        port=int(os.getenv("LB_PORT", "8090")),
        admin_token=os.getenv("LB_ADMIN_TOKEN", "change-me-admin-token").strip(),
        client_tokens=ct,
        fail_threshold=max(1, int(os.getenv("LB_FAIL_THRESHOLD", "3"))),
        cooldown_seconds=max(1.0, float(os.getenv("LB_COOLDOWN_SECONDS", "20"))),
        max_connections=max(10, int(os.getenv("LB_MAX_CONNECTIONS", "500"))),
        max_keepalive=max(10, int(os.getenv("LB_MAX_KEEPALIVE", "200"))),
        default_timeout_seconds=max(5.0, float(os.getenv("LB_DEFAULT_TIMEOUT_SECONDS", "120"))),
        cache_max_entries=max(100, int(os.getenv("LB_CACHE_MAX_ENTRIES", "5000"))),
        cache_ttl_seconds=max(10.0, float(os.getenv("LB_CACHE_TTL_SECONDS", "300"))),
        log_level=os.getenv("LB_LOG_LEVEL", "INFO").upper(),
        cors_origins=cors,
        max_request_body_bytes=int(os.getenv("LB_MAX_REQUEST_BODY_BYTES", str(10 * 1024 * 1024))),
        admin_rate_limit_rpm=int(os.getenv("LB_ADMIN_RATE_LIMIT_RPM", "300")),
        drain_timeout_seconds=float(os.getenv("LB_DRAIN_TIMEOUT_SECONDS", "30")),
    )

    if settings.admin_token == "change-me-admin-token":
        LOG.warning("LB_ADMIN_TOKEN uses the default - set a strong token!")
    if len(settings.admin_token) < 16 and settings.admin_token != "change-me-admin-token":
        LOG.warning("LB_ADMIN_TOKEN is short (%d chars) - use >=16", len(settings.admin_token))

    return settings


def create_app(
    settings: Optional[AppSettings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    cfg = settings or load_settings()

    log_fmt = (
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        if cfg.log_level != "DEBUG"
        else "%(asctime)s %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s"
    )
    logging.basicConfig(level=cfg.log_level, format=log_fmt, stream=sys.stdout)

    store = SQLiteStore(cfg.db_path)
    engine = LoadBalancerEngine(store, cfg, transport=transport)
    response_cache = TTLCache(cfg.cache_max_entries, cfg.cache_ttl_seconds)
    admin_rate_limiter = RateLimiter(cfg.admin_rate_limit_rpm)
    audit = AuditLogger()

    def build_cache_key(
        client_token_id: int,
        mapped_endpoint_id: Optional[int],
        body: bytes,
    ) -> str:
        h = hashlib.sha256()
        h.update(str(client_token_id).encode("utf-8"))
        h.update(b"|")
        h.update(str(mapped_endpoint_id).encode("utf-8") if mapped_endpoint_id is not None else b"-")
        h.update(b"|")
        h.update(body)
        return h.hexdigest()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.startup()
        LOG.info("Load Balancer v%s ready on port %s (routing=%s)", __version__, cfg.port, engine.routing_mode)
        try:
            yield
        finally:
            await engine.shutdown()
            store.close()

    app = FastAPI(
        title="LLM Load Balancer",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.engine = engine

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-LB-Endpoint", "X-LB-Cache", "X-LB-Version", "X-Request-ID"],
    )

    # Security headers middleware
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        req_id = request.headers.get("x-request-id", "") or generate_request_id()
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Request-ID"] = req_id
        response.headers["X-LB-Version"] = __version__
        return response

    # Request size limit middleware
    @app.middleware("http")
    async def body_size_middleware(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > cfg.max_request_body_bytes:
            return problem_detail(413, f"Request body too large (max {cfg.max_request_body_bytes} bytes)", "request_too_large")
        return await call_next(request)

    # Exception handlers
    @app.exception_handler(LoadBalancerError)
    async def lb_error_handler(request: Request, exc: LoadBalancerError):
        req_id = getattr(request.state, "request_id", "")
        return problem_detail(exc.status_code, exc.detail, exc.error_type, req_id)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        req_id = getattr(request.state, "request_id", "")
        return problem_detail(exc.status_code, str(exc.detail), request_id=req_id)

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        req_id = getattr(request.state, "request_id", "")
        LOG.exception("Unhandled error: %s [req=%s]", exc, req_id)
        return problem_detail(500, "internal server error", "internal_error", req_id)

    def get_priority_input_token_id() -> Optional[int]:
        raw = store.get_setting("priority_input_token_id")
        if not raw:
            return None
        try:
            val = int(raw)
        except Exception:
            return None
        return val if val > 0 else None

    # Auth dependencies
    def require_admin(
        request: Request,
        authorization: Optional[str] = Header(None),
        x_admin_token: Optional[str] = Header(None),
    ) -> None:
        if not admin_rate_limiter.check():
            raise HTTPException(429, "admin API rate limit exceeded")
        if not cfg.admin_token or cfg.admin_token == "change-me-admin-token":
            return
        tok = (x_admin_token or "").strip() or parse_bearer_token(authorization)
        if not constant_time_compare(tok, cfg.admin_token):
            raise HTTPException(401, "admin authorization failed")

    def require_client(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> None:
        request.state.client_token_id = None
        request.state.client_token = None
        has_env = bool(cfg.client_tokens)
        has_db = bool(store.list_client_tokens())
        if not has_env and not has_db:
            return
        tok = parse_bearer_token(authorization)
        if not tok:
            raise HTTPException(401, "client token required")
        if any(constant_time_compare(tok, ct) for ct in cfg.client_tokens):
            return
        rec = store.find_client_token_by_value(tok)
        if rec:
            request.state.client_token_id = rec["id"]
            request.state.client_token = rec
            return
        raise HTTPException(401, "client token invalid")

    # Internal helpers
    async def finish_request(
        eid: int, status: int, lat_ms: float,
        prompt_t: int = 0, compl_t: int = 0,
        client_token_id: Optional[int] = None,
    ) -> None:
        ok = should_count_as_success(status)
        if not ok:
            engine.note_error()
        await asyncio.to_thread(
            store.record_result, eid, ok, lat_ms,
            cfg.fail_threshold, cfg.cooldown_seconds, prompt_t, compl_t,
        )
        engine.note_tokens(prompt_t + compl_t)
        if client_token_id is not None:
            await asyncio.to_thread(
                store.record_client_token_usage, client_token_id, prompt_t, compl_t,
            )
            engine.note_client_request(client_token_id)

    async def fail_request(eid: int, lat_ms: float) -> None:
        engine.note_error()
        await asyncio.to_thread(
            store.record_result, eid, False, lat_ms,
            cfg.fail_threshold, cfg.cooldown_seconds,
        )

    # Proxy: chat completions
    async def proxy_chat(request: Request) -> Response:
        req_id = getattr(request.state, "request_id", "")
        client_tid: Optional[int] = getattr(request.state, "client_token_id", None)
        client_tok: Optional[dict[str, Any]] = getattr(request.state, "client_token", None)
        mapped_eid: Optional[int] = None
        if client_tok and client_tok.get("preferred_endpoint_id") is not None:
            mapped_eid = int(client_tok["preferred_endpoint_id"])

        priority_input_token_id = None
        if engine.routing_mode == "priority":
            priority_input_token_id = get_priority_input_token_id()
        prefer_non_primary = (
            mapped_eid is None
            and engine.routing_mode == "priority"
            and priority_input_token_id is not None
            and (client_tid is None or int(client_tid) != priority_input_token_id)
        )

        raw = await request.body()
        if len(raw) > cfg.max_request_body_bytes:
            raise HTTPException(413, "request body too large")

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
                store.get_client_token_traffic_stats, client_tid,
            )
            share = (token_req / total_req * 100) if total_req > 0 else 0.0
            cache_only = total_req > 0 and share >= min_share
            if stream and cache_only:
                raise HTTPException(409, "cache-only traffic requires non-stream requests")
            if not stream:
                cache_key = build_cache_key(client_tid, mapped_eid, raw)

        if cache_only and cache_key:
            cached = await response_cache.get(cache_key)
            if not cached:
                raise HTTPException(503, "cache miss for over-quota token traffic")
            c_status, c_body, c_media, c_ep = cached
            prompt_t, comp_t = extract_usage(c_body)
            if client_tid is not None:
                await asyncio.to_thread(
                    store.record_client_token_usage, client_tid, prompt_t, comp_t,
                )
                engine.note_client_request(client_tid)
            engine.note_tokens(prompt_t + comp_t)
            engine.note_request()
            return Response(
                content=c_body, status_code=c_status,
                headers={"X-LB-Cache": "HIT", "X-LB-Endpoint": c_ep, "X-Request-ID": req_id},
                media_type=c_media,
            )

        ep = await engine.acquire(mapped_eid, prefer_non_primary=prefer_non_primary)
        eid = int(ep["id"])
        ep_name = str(ep["name"])
        headers = build_upstream_headers(request, ep)
        url = join_url(str(ep["base_url"]), "/chat/completions")
        timeout = float(ep.get("timeout_seconds") or cfg.default_timeout_seconds)
        client = engine.client_for(ep)
        start = time.perf_counter()
        LOG.debug("Routing request %s to %s (%s) stream=%s", req_id, ep_name, url[:60], stream)

        if not stream:
            try:
                up = await client.post(url, content=raw, headers=headers, timeout=timeout)
                lat = (time.perf_counter() - start) * 1000
                pt, ct = extract_usage(up.content)
                await finish_request(eid, up.status_code, lat, pt, ct, client_tid)
                if cache_key and 200 <= up.status_code < 300 and up.headers.get("content-type"):
                    await response_cache.put(
                        cache_key,
                        (up.status_code, bytes(up.content), up.headers.get("content-type", "application/json"), ep_name),
                    )
                rh = filtered_response_headers(up.headers, ep_name, req_id)
                rh["X-LB-Cache"] = "MISS"
                return Response(
                    content=up.content, status_code=up.status_code,
                    headers=rh,
                    media_type=up.headers.get("content-type", "application/json"),
                )
            except HTTPException:
                raise
            except Exception as exc:
                lat = (time.perf_counter() - start) * 1000
                LOG.warning("Upstream error for %s via %s: %s [req=%s, lat=%.0fms]", url[:60], ep_name, exc, req_id, lat)
                await fail_request(eid, lat)
                raise HTTPException(502, f"upstream error: {type(exc).__name__}: {exc}")
            finally:
                await engine.release(eid)

        # Streaming
        ctx = client.stream("POST", url, content=raw, headers=headers, timeout=timeout)
        try:
            up = await ctx.__aenter__()
        except Exception as exc:
            lat = (time.perf_counter() - start) * 1000
            LOG.warning("Upstream stream failed for %s via %s: %s [req=%s]", url[:60], ep_name, exc, req_id)
            await fail_request(eid, lat)
            await engine.release(eid)
            raise HTTPException(502, f"upstream stream failed: {type(exc).__name__}: {exc}")

        status = int(up.status_code)
        rh = filtered_response_headers(up.headers, ep_name, req_id)

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
                        pt, ct_val = 0, 0
                        try:
                            for line in last_data.decode("utf-8", errors="ignore").split("\n"):
                                stripped = line.strip()
                                if stripped.startswith("data:") and stripped != "data: [DONE]":
                                    j = json.loads(stripped[5:].strip())
                                    u = j.get("usage") or {}
                                    if u.get("completion_tokens"):
                                        pt = int(u.get("prompt_tokens", 0))
                                        ct_val = int(u.get("completion_tokens", 0))
                        except Exception:
                            pass
                        await finish_request(eid, status, lat, pt, ct_val, client_tid)
                    await engine.release(eid)

        mt = up.headers.get("content-type", "text/event-stream")
        return StreamingResponse(gen(), status_code=status, headers=rh, media_type=mt)

    # Proxy: models (parallel aggregation)
    async def proxy_models(request: Request) -> JSONResponse:
        req_id = getattr(request.state, "request_id", "")
        client_tok: Optional[dict[str, Any]] = getattr(request.state, "client_token", None)
        mapped_eid: Optional[int] = None
        if client_tok and client_tok.get("preferred_endpoint_id") is not None:
            mapped_eid = int(client_tok["preferred_endpoint_id"])

        endpoints = await asyncio.to_thread(store.list_connected_enabled)
        if mapped_eid is not None:
            endpoints = [e for e in endpoints if int(e["id"]) == int(mapped_eid)]
            if not endpoints:
                raise HTTPException(503, "mapped endpoint not connected")
        if not endpoints:
            raise HTTPException(503, "no connected endpoints")

        async def fetch_models_from(ep: dict[str, Any]) -> list[dict[str, Any]]:
            eid = int(ep["id"])
            url = join_url(str(ep["base_url"]), "/models")
            ep_headers = build_upstream_headers(request, ep)
            to = float(ep.get("timeout_seconds") or cfg.default_timeout_seconds)
            hclient = engine.client_for(ep)
            start = time.perf_counter()
            try:
                r = await hclient.get(url, headers=ep_headers, timeout=to)
                lat = (time.perf_counter() - start) * 1000
                await finish_request(eid, r.status_code, lat)
                if 200 <= r.status_code < 300:
                    return r.json().get("data", [])
            except Exception:
                await fail_request(eid, (time.perf_counter() - start) * 1000)
            return []

        results = await asyncio.gather(
            *(fetch_models_from(ep) for ep in endpoints),
            return_exceptions=True,
        )
        combined: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, Exception):
                LOG.warning("Error fetching models: %s", result)
                continue
            for m in result:
                mid = m.get("id")
                if mid:
                    combined[mid] = m
        if not combined:
            raise HTTPException(502, "no models returned from any endpoint")
        ordered = [combined[k] for k in sorted(combined)]
        return JSONResponse(
            {"object": "list", "data": ordered},
            headers={"X-Request-ID": req_id},
        )

    # Routes: generic
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin")

    @app.get("/health")
    async def health():
        """Health check endpoint with dependency status."""
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        connected = sum(1 for e in eps if e.get("connected"))
        healthy = connected > 0
        return JSONResponse(
            {
                "status": "ok" if healthy else "degraded",
                "version": __version__,
                "endpoints": len(eps),
                "connected": connected,
                "routing_mode": engine.routing_mode,
                "priority_input_token_id": get_priority_input_token_id(),
                "cache_size": response_cache.size,
                **st,
            },
            status_code=200 if healthy else 503,
        )

    @app.head("/health")
    async def health_head():
        """HEAD health check for monitoring probes."""
        eps = await asyncio.to_thread(store.list_endpoints)
        connected = sum(1 for e in eps if e.get("connected"))
        return Response(status_code=200 if connected > 0 else 503)

    @app.get("/metrics")
    async def metrics():
        """Prometheus-compatible metrics endpoint."""
        text = await engine.prometheus_metrics()
        return Response(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_ui():
        p = Path(__file__).with_name("load_balancer_admin.html")
        if not p.exists():
            raise HTTPException(500, "admin UI missing")
        return HTMLResponse(p.read_text(encoding="utf-8"))

    # Routes: admin API
    @app.get("/admin/api/state", dependencies=[Depends(require_admin)])
    async def get_state():
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        inf = st.get("inflight_per_endpoint", {})
        safe = []
        for e in eps:
            eid = int(e["id"])
            enriched = dict(e)
            enriched["inflight"] = inf.get(eid, 0)
            enriched["cooldown_remaining_s"] = max(0.0, float(e.get("cooldown_until", 0)) - time.time())
            safe.append(sanitize_endpoint(enriched))
        incoming_blocks = await asyncio.to_thread(store.list_incoming_blocks)
        if not incoming_blocks:
            created = await asyncio.to_thread(store.create_incoming_block, "Incoming 1", 256.0, 300.0)
            incoming_blocks = [created]
        incoming_safe = [
            {"id": int(b["id"]), "name": str(b["name"]), "pos_x": float(b.get("pos_x", 256)), "pos_y": float(b.get("pos_y", 300))}
            for b in incoming_blocks
        ]
        legacy_incoming = incoming_safe[0]
        tokens_raw = await asyncio.to_thread(store.list_client_tokens)
        cstats = engine.client_stats()
        tokens_safe = []
        endpoint_name_by_id = {int(e["id"]): str(e["name"]) for e in eps}
        incoming_ids = {int(b["id"]) for b in incoming_blocks}
        default_incoming_id = int(incoming_blocks[0]["id"])
        for t in tokens_raw:
            tid = t["id"]
            cs = cstats.get(tid, {})
            preferred_eid = t.get("preferred_endpoint_id")
            ibid = t.get("incoming_block_id")
            if ibid is None or int(ibid) not in incoming_ids:
                ibid = default_incoming_id
            tokens_safe.append({
                "id": tid, "name": t["name"],
                "token_preview": mask_secret(t["token"]),
                "created_at": t["created_at"],
                "pos_x": t.get("pos_x", 80), "pos_y": t.get("pos_y", 100),
                "incoming_block_id": int(ibid),
                "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "requests_per_second": cs.get("requests_per_second", 0),
                "preferred_endpoint_id": preferred_eid,
                "preferred_endpoint_name": endpoint_name_by_id.get(int(preferred_eid)) if preferred_eid is not None else None,
                "request_priority": t.get("request_priority", 0),
                "min_traffic_percent": t.get("min_traffic_percent", 0),
            })
        return JSONResponse({
            "routing_mode": engine.routing_mode,
            "priority_input_token_id": get_priority_input_token_id(),
            "endpoints": safe,
            "incoming_blocks": incoming_safe,
            "incoming_pos": {"x": legacy_incoming["pos_x"], "y": legacy_incoming["pos_y"]},
            "stats": st,
            "client_tokens": tokens_safe,
            "version": __version__,
        })

    @app.post("/admin/api/endpoints", dependencies=[Depends(require_admin)])
    async def create_ep(request: Request, payload: EndpointCreate):
        d = payload.model_dump()
        d["base_url"] = normalize_base_url(d["base_url"])
        try:
            ep = await asyncio.to_thread(store.create_endpoint, d)
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        audit.log("endpoint_created", getattr(request.state, "request_id", ""), {"name": d["name"], "id": ep.get("id")})
        return JSONResponse(sanitize_endpoint(ep))

    @app.patch("/admin/api/endpoints/{eid}", dependencies=[Depends(require_admin)])
    async def patch_ep(eid: int, request: Request, payload: EndpointPatch):
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
        audit.log("endpoint_updated", getattr(request.state, "request_id", ""), {"id": eid, "fields": list(u.keys())})
        return JSONResponse(sanitize_endpoint(ep))

    @app.delete("/admin/api/endpoints/{eid}", dependencies=[Depends(require_admin)])
    async def delete_ep(eid: int, request: Request):
        await asyncio.to_thread(store.clear_client_token_endpoint_mapping, eid)
        if not await asyncio.to_thread(store.delete_endpoint, eid):
            raise HTTPException(404, "endpoint not found")
        audit.log("endpoint_deleted", getattr(request.state, "request_id", ""), {"id": eid})
        return JSONResponse({"ok": True})

    @app.patch("/admin/api/routing", dependencies=[Depends(require_admin)])
    async def patch_routing(request: Request, payload: RoutingPatch):
        if payload.routing_mode and payload.routing_mode in ("percentage", "priority"):
            await asyncio.to_thread(store.set_setting, "routing_mode", payload.routing_mode)
        if payload.priority_input_token_id is not None:
            if payload.priority_input_token_id <= 0:
                await asyncio.to_thread(store.set_setting, "priority_input_token_id", "")
            else:
                tokens = await asyncio.to_thread(store.list_client_tokens)
                if not any(int(t["id"]) == payload.priority_input_token_id for t in tokens):
                    raise HTTPException(404, "priority input token not found")
                await asyncio.to_thread(store.set_setting, "priority_input_token_id", str(payload.priority_input_token_id))
        audit.log("routing_updated", getattr(request.state, "request_id", ""), {"mode": payload.routing_mode, "priority_input": payload.priority_input_token_id})
        return JSONResponse({"routing_mode": engine.routing_mode, "priority_input_token_id": get_priority_input_token_id()})

    @app.post("/admin/api/reset-stats", dependencies=[Depends(require_admin)])
    async def reset_stats(request: Request):
        await asyncio.to_thread(store.reset_all_stats)
        await asyncio.to_thread(store.reset_client_token_stats)
        engine._tok_window.clear()
        engine._req_window.clear()
        engine._client_req_windows.clear()
        engine._total_requests = 0
        engine._total_errors = 0
        await response_cache.clear()
        audit.log("stats_reset", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True})

    @app.post("/admin/api/cache/clear", dependencies=[Depends(require_admin)])
    async def clear_cache(request: Request):
        """Clear the response cache."""
        await response_cache.clear()
        audit.log("cache_cleared", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True, "message": "cache cleared"})

    @app.post("/admin/api/db/vacuum", dependencies=[Depends(require_admin)])
    async def vacuum_db(request: Request):
        """Run VACUUM on the database to reclaim space."""
        await asyncio.to_thread(store.vacuum)
        audit.log("db_vacuum", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True, "message": "database vacuumed"})

    # Routes: client token management
    @app.get("/admin/api/tokens", dependencies=[Depends(require_admin)])
    async def list_tokens():
        tokens = await asyncio.to_thread(store.list_client_tokens)
        incoming_blocks = await asyncio.to_thread(store.list_incoming_blocks)
        incoming_ids = {int(b["id"]) for b in incoming_blocks}
        default_incoming_id = int(incoming_blocks[0]["id"]) if incoming_blocks else 1
        safe = []
        for t in tokens:
            preferred_eid = t.get("preferred_endpoint_id")
            ibid = t.get("incoming_block_id")
            if ibid is None or int(ibid) not in incoming_ids:
                ibid = default_incoming_id
            safe.append({
                "id": t["id"], "name": t["name"],
                "token_preview": mask_secret(t["token"]),
                "token": t["token"],
                "created_at": t["created_at"],
                "pos_x": t.get("pos_x", 80), "pos_y": t.get("pos_y", 100),
                "incoming_block_id": int(ibid),
                "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "preferred_endpoint_id": preferred_eid,
                "request_priority": t.get("request_priority", 0),
                "min_traffic_percent": t.get("min_traffic_percent", 0),
            })
        return JSONResponse(safe)

    @app.post("/admin/api/tokens", dependencies=[Depends(require_admin)])
    async def create_token(request: Request, payload: ClientTokenCreate):
        if payload.incoming_block_id is not None:
            ib = await asyncio.to_thread(store.get_incoming_block, payload.incoming_block_id)
            if not ib:
                raise HTTPException(404, "incoming block not found")
        if payload.preferred_endpoint_id is not None:
            ep = await asyncio.to_thread(store.get_endpoint, payload.preferred_endpoint_id)
            if not ep:
                raise HTTPException(404, "preferred endpoint not found")
        try:
            tok = await asyncio.to_thread(
                store.create_client_token,
                payload.name, payload.token, payload.incoming_block_id,
                payload.preferred_endpoint_id, payload.request_priority, payload.min_traffic_percent,
            )
            if tok and (payload.pos_x != 80 or payload.pos_y != 100):
                await asyncio.to_thread(store.update_client_token, tok["id"], {"pos_x": payload.pos_x, "pos_y": payload.pos_y})
                tok["pos_x"] = payload.pos_x
                tok["pos_y"] = payload.pos_y
        except sqlite3.IntegrityError:
            raise HTTPException(409, "token already exists")
        audit.log("token_created", getattr(request.state, "request_id", ""), {"name": payload.name, "id": tok.get("id")})
        return JSONResponse(tok)

    @app.patch("/admin/api/tokens/{tid}", dependencies=[Depends(require_admin)])
    async def patch_token(tid: int, request: Request, payload: ClientTokenPatch):
        u = payload.model_dump(exclude_none=True)
        if not u:
            raise HTTPException(400, "nothing to update")
        if u.get("incoming_block_id") is not None:
            ib = await asyncio.to_thread(store.get_incoming_block, int(u["incoming_block_id"]))
            if not ib:
                raise HTTPException(404, "incoming block not found")
        if u.get("preferred_endpoint_id") is not None:
            ep = await asyncio.to_thread(store.get_endpoint, int(u["preferred_endpoint_id"]))
            if not ep:
                raise HTTPException(404, "preferred endpoint not found")
        result = await asyncio.to_thread(store.update_client_token, tid, u)
        if not result:
            raise HTTPException(404, "token not found")
        return JSONResponse(result)

    @app.delete("/admin/api/tokens/{tid}", dependencies=[Depends(require_admin)])
    async def delete_token(tid: int, request: Request):
        if not await asyncio.to_thread(store.delete_client_token, tid):
            raise HTTPException(404, "token not found")
        audit.log("token_deleted", getattr(request.state, "request_id", ""), {"id": tid})
        return JSONResponse({"ok": True})

    # Incoming blocks
    @app.get("/admin/api/incoming-blocks", dependencies=[Depends(require_admin)])
    async def list_incoming_blocks():
        blocks = await asyncio.to_thread(store.list_incoming_blocks)
        return JSONResponse(blocks)

    @app.post("/admin/api/incoming-blocks", dependencies=[Depends(require_admin)])
    async def create_incoming_block(payload: IncomingBlockCreate):
        block = await asyncio.to_thread(store.create_incoming_block, payload.name.strip(), payload.pos_x, payload.pos_y)
        return JSONResponse(block)

    @app.patch("/admin/api/incoming-blocks/{bid}", dependencies=[Depends(require_admin)])
    async def patch_incoming_block(bid: int, payload: IncomingBlockPatch):
        updates = payload.model_dump(exclude_none=True)
        if "name" in updates:
            updates["name"] = str(updates["name"]).strip()
        if not updates:
            raise HTTPException(400, "nothing to update")
        block = await asyncio.to_thread(store.update_incoming_block, bid, updates)
        if not block:
            raise HTTPException(404, "incoming block not found")
        return JSONResponse(block)

    @app.delete("/admin/api/incoming-blocks/{bid}", dependencies=[Depends(require_admin)])
    async def delete_incoming_block(bid: int):
        try:
            ok = await asyncio.to_thread(store.delete_incoming_block, bid)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if not ok:
            raise HTTPException(404, "incoming block not found")
        return JSONResponse({"ok": True})

    @app.patch("/admin/api/incoming-pos", dependencies=[Depends(require_admin)])
    async def patch_incoming_pos(request: Request):
        body = await request.json()
        blocks = await asyncio.to_thread(store.list_incoming_blocks)
        if not blocks:
            created = await asyncio.to_thread(store.create_incoming_block, "Incoming 1", 256.0, 300.0)
            blocks = [created]
        first_id = int(blocks[0]["id"])
        updates: dict[str, Any] = {}
        if "x" in body:
            x = float(body["x"])
            updates["pos_x"] = x
            await asyncio.to_thread(store.set_setting, "incoming_pos_x", str(x))
        if "y" in body:
            y = float(body["y"])
            updates["pos_y"] = y
            await asyncio.to_thread(store.set_setting, "incoming_pos_y", str(y))
        if updates:
            await asyncio.to_thread(store.update_incoming_block, first_id, updates)
        return JSONResponse({"ok": True, "incoming_block_id": first_id})

    # Routes: proxy
    @app.post("/v1/chat/completions", dependencies=[Depends(require_client)])
    async def chat(request: Request):
        return await proxy_chat(request)

    @app.post("/chat/completions", dependencies=[Depends(require_client)])
    async def chat_legacy(request: Request):
        return await proxy_chat(request)

    @app.post("/v1/{pool_name}/chat/completions", dependencies=[Depends(require_client)])
    async def chat_pool(pool_name: str, request: Request):
        return await proxy_chat(request)

    @app.get("/v1/models", dependencies=[Depends(require_client)])
    async def models(request: Request):
        return await proxy_models(request)

    @app.get("/models", dependencies=[Depends(require_client)])
    async def models_legacy(request: Request):
        return await proxy_models(request)

    @app.get("/v1/{pool_name}/models", dependencies=[Depends(require_client)])
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
        access_log=True,
    )
