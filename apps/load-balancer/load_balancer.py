"""LLM Load Balancer v4 – Enterprise Visual Flow Router.

A high-performance reverse proxy for LLM API endpoints with:
- Percentage-based and priority-based routing
- Real-time token throughput tracking
- SQLite persistence with WAL mode
- Response caching with TTL + LRU eviction
- Streaming (SSE) passthrough
- Client token management with per-token routing
- Incoming block grouping for visual flow
- Prometheus-compatible metrics
- RFC 7807 error responses
- Security hardening (headers, rate limiting, constant-time auth)
- Graceful shutdown with request draining
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

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

LOG = logging.getLogger("load-balancer")
__version__ = "4.0.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


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
    max_request_body_bytes: int = 10 * 1024 * 1024
    admin_rate_limit_rpm: int = 300
    drain_timeout_seconds: float = 30.0
    sliding_window_seconds: float = 60.0
    sliding_window_max_entries: int = 100_000


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_request_id() -> str:
    return str(uuid.uuid4())


def _bearer(raw: Optional[str]) -> str:
    if not raw:
        return ""
    parts = raw.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _ct_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _normalize_url(val: str) -> str:
    url = (val or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return url


def _join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _is_success(status: int) -> bool:
    return not (status >= 500 or status in (401, 403, 429))


def _mask(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return f"{s[:2]}***"
    return f"{s[:4]}...{s[-4:]}"


def _sanitize_ep(ep: dict[str, Any]) -> dict[str, Any]:
    out = dict(ep)
    raw_key = str(out.pop("api_key", "") or "")
    out["api_key_preview"] = _mask(raw_key)
    out["has_api_key"] = bool(raw_key.strip())
    return out


def _extract_usage(body: bytes) -> tuple[int, int]:
    try:
        data = json.loads(body)
        usage = data.get("usage") or {}
        return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except Exception:
        return 0, 0


def _upstream_headers(request: Request, ep: dict[str, Any]) -> dict[str, str]:
    blocked = {
        "host", "connection", "content-length", "authorization",
        "accept-encoding", "x-admin-token", "transfer-encoding",
    }
    hdrs: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in blocked or k.lower().startswith("x-lb-"):
            continue
        hdrs[k] = v
    api_key = ep.get("api_key", "")
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    if "content-type" not in {h.lower() for h in hdrs}:
        hdrs["Content-Type"] = "application/json"
    rid = getattr(request.state, "request_id", "")
    if rid:
        hdrs["X-Request-ID"] = rid
    return hdrs


def _filter_upstream_headers(
    headers: httpx.Headers, ep_name: str, request_id: str = "",
) -> dict[str, str]:
    keep = {"content-type", "cache-control", "retry-after"}
    out: dict[str, str] = {}
    for k, v in headers.items():
        lo = k.lower()
        if lo in keep or lo.startswith("x-ratelimit") or lo.startswith("x-request-id"):
            out[k] = v
    out["X-LB-Endpoint"] = ep_name
    out["X-LB-Version"] = __version__
    if request_id:
        out["X-Request-ID"] = request_id
    return out


def _problem(
    status: int, detail: str, error_type: str = "about:blank",
    request_id: str = "", **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"type": error_type, "status": status, "detail": detail}
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return JSONResponse(body, status_code=status)


# ---------------------------------------------------------------------------
# SlidingWindow – bounded, thread-safe rate tracker
# ---------------------------------------------------------------------------


class SlidingWindow:
    def __init__(self, window_seconds: float = 60.0, max_entries: int = 100_000) -> None:
        self._window = window_seconds
        self._max = max_entries
        self._entries: list[tuple[float, float]] = []

    def add(self, value: float = 1.0) -> None:
        self._entries.append((time.time(), value))
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._entries = [(t, v) for t, v in self._entries if t > cutoff]

    def sum(self) -> float:
        self._trim()
        return sum(v for _, v in self._entries)

    def count(self) -> int:
        self._trim()
        return len(self._entries)

    def rate_per_second(self) -> float:
        s = self.sum()
        return round(s / self._window, 1) if s else 0.0

    def count_per_second(self) -> float:
        c = self.count()
        return round(c / self._window, 1) if c else 0.0

    def clear(self) -> None:
        self._entries.clear()


# ---------------------------------------------------------------------------
# TTLCache – LRU with time-to-live
# ---------------------------------------------------------------------------


class TTLCache:
    def __init__(self, max_entries: int = 5000, ttl_seconds: float = 300.0) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            ts, val = item
            if time.time() - ts > self._ttl:
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
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# RateLimiter – sliding-window RPM
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, rpm: int = 300) -> None:
        self._rpm = rpm
        self._win = SlidingWindow(window_seconds=60.0, max_entries=rpm * 2)

    def check(self) -> bool:
        if self._rpm <= 0:
            return True
        if self._win.count() >= self._rpm:
            return False
        self._win.add(1.0)
        return True

    @property
    def remaining(self) -> int:
        return max(0, self._rpm - self._win.count())


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class _AuditLogger:
    def __init__(self) -> None:
        self._log = logging.getLogger("load-balancer.audit")

    def log(self, action: str, request_id: str = "", details: Optional[dict[str, Any]] = None) -> None:
        entry: dict[str, Any] = {"timestamp": _utcnow_iso(), "action": action, "request_id": request_id}
        if details:
            entry["details"] = details
        self._log.info(json.dumps(entry))


# ---------------------------------------------------------------------------
# SQLiteStore – persistent data layer
# ---------------------------------------------------------------------------


class _SQLiteStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lk = threading.Lock()
        with self._lk:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA cache_size=-8000")
        self._create_tables()

    # -- schema --

    def _create_tables(self) -> None:
        with self._lk:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS endpoints (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                   TEXT    NOT NULL UNIQUE,
                    base_url               TEXT    NOT NULL,
                    api_key                TEXT    NOT NULL DEFAULT '',
                    timeout_seconds        REAL    NOT NULL DEFAULT 120,
                    verify_tls             INTEGER NOT NULL DEFAULT 1,
                    enabled                INTEGER NOT NULL DEFAULT 1,
                    connected              INTEGER NOT NULL DEFAULT 0,
                    role                   TEXT    NOT NULL DEFAULT 'percentage',
                    token_quota_percent    REAL    NOT NULL DEFAULT 0,
                    priority_order         INTEGER NOT NULL DEFAULT 10,
                    max_concurrent         INTEGER NOT NULL DEFAULT 0,
                    cooldown_until         REAL    NOT NULL DEFAULT 0,
                    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
                    total_requests         INTEGER NOT NULL DEFAULT 0,
                    total_success          INTEGER NOT NULL DEFAULT 0,
                    total_failures         INTEGER NOT NULL DEFAULT 0,
                    avg_latency_ms         REAL    NOT NULL DEFAULT 0,
                    prompt_tokens_total    INTEGER NOT NULL DEFAULT 0,
                    completion_tokens_total INTEGER NOT NULL DEFAULT 0,
                    pos_x                  REAL    NOT NULL DEFAULT 500,
                    pos_y                  REAL    NOT NULL DEFAULT 200,
                    created_at             TEXT    NOT NULL,
                    updated_at             TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_tokens (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                  TEXT    NOT NULL,
                    token                 TEXT    NOT NULL UNIQUE,
                    incoming_block_id     INTEGER,
                    preferred_endpoint_id INTEGER,
                    request_priority      INTEGER NOT NULL DEFAULT 0,
                    min_traffic_percent   REAL    NOT NULL DEFAULT 0,
                    pos_x                 REAL    NOT NULL DEFAULT 80,
                    pos_y                 REAL    NOT NULL DEFAULT 100,
                    total_requests        INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens_total   INTEGER NOT NULL DEFAULT 0,
                    completion_tokens_total INTEGER NOT NULL DEFAULT 0,
                    created_at            TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incoming_blocks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT    NOT NULL,
                    pos_x      REAL    NOT NULL DEFAULT 256,
                    pos_y      REAL    NOT NULL DEFAULT 300,
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ep_conn ON endpoints(connected, enabled, cooldown_until);
                CREATE INDEX IF NOT EXISTS idx_ct_tok  ON client_tokens(token);
            """)
            self._conn.commit()
            # ensure at least one incoming block exists
            cnt = self._conn.execute("SELECT COUNT(*) AS c FROM incoming_blocks").fetchone()
            if int(cnt["c"]) == 0:
                now = _utcnow_iso()
                self._conn.execute(
                    "INSERT INTO incoming_blocks(name,pos_x,pos_y,created_at,updated_at) VALUES(?,?,?,?,?)",
                    ("Incoming 1", 256.0, 300.0, now, now),
                )
            # assign orphan tokens to first block
            first_blk = self._conn.execute("SELECT id FROM incoming_blocks ORDER BY id LIMIT 1").fetchone()
            if first_blk:
                self._conn.execute(
                    "UPDATE client_tokens SET incoming_block_id=? WHERE incoming_block_id IS NULL",
                    (int(first_blk["id"]),),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lk:
            try:
                self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            self._conn.close()

    @staticmethod
    def _boolify(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for f in ("enabled", "verify_tls", "connected"):
            if f in d:
                d[f] = bool(d[f])
        return d

    # -- settings --

    def set_setting(self, key: str, value: str) -> None:
        with self._lk:
            self._conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _utcnow_iso()),
            )
            self._conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        with self._lk:
            r = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(r["value"]) if r else None

    # -- endpoints --

    def create_endpoint(self, d: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow_iso()
        with self._lk:
            cur = self._conn.execute(
                """INSERT INTO endpoints(
                    name,base_url,api_key,timeout_seconds,verify_tls,enabled,connected,
                    role,token_quota_percent,priority_order,max_concurrent,
                    pos_x,pos_y,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    d["name"], d["base_url"], d.get("api_key", ""),
                    float(d.get("timeout_seconds", 120)), int(bool(d.get("verify_tls", True))),
                    int(bool(d.get("enabled", True))), int(bool(d.get("connected", False))),
                    d.get("role", "percentage"), float(d.get("token_quota_percent", 0)),
                    int(d.get("priority_order", 10)), int(d.get("max_concurrent", 0)),
                    float(d.get("pos_x", 500)), float(d.get("pos_y", 200)), now, now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM endpoints WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._boolify(row)

    def get_endpoint(self, eid: int) -> Optional[dict[str, Any]]:
        with self._lk:
            r = self._conn.execute("SELECT * FROM endpoints WHERE id=?", (eid,)).fetchone()
        return self._boolify(r) if r else None

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self._lk:
            rows = self._conn.execute("SELECT * FROM endpoints ORDER BY id").fetchall()
        return [self._boolify(r) for r in rows]

    def list_connected_healthy(self, now_epoch: float) -> list[dict[str, Any]]:
        with self._lk:
            rows = self._conn.execute(
                "SELECT * FROM endpoints WHERE connected=1 AND enabled=1 AND cooldown_until<=? ORDER BY id",
                (now_epoch,),
            ).fetchall()
        return [self._boolify(r) for r in rows]

    def list_connected_enabled(self) -> list[dict[str, Any]]:
        with self._lk:
            rows = self._conn.execute(
                "SELECT * FROM endpoints WHERE connected=1 AND enabled=1 ORDER BY id",
            ).fetchall()
        return [self._boolify(r) for r in rows]

    def update_endpoint(self, eid: int, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not updates:
            return self.get_endpoint(eid)
        allowed = {
            "name", "base_url", "api_key", "timeout_seconds", "verify_tls",
            "enabled", "connected", "role", "token_quota_percent",
            "priority_order", "max_concurrent", "cooldown_until",
            "consecutive_failures", "total_requests", "total_success",
            "total_failures", "avg_latency_ms", "prompt_tokens_total",
            "completion_tokens_total", "pos_x", "pos_y",
        }
        cols, vals = [], []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in ("enabled", "verify_tls", "connected"):
                v = int(bool(v))
            cols.append(f"{k}=?")
            vals.append(v)
        if not cols:
            return self.get_endpoint(eid)
        vals += [_utcnow_iso(), eid]
        with self._lk:
            self._conn.execute(f"UPDATE endpoints SET {','.join(cols)},updated_at=? WHERE id=?", vals)
            self._conn.commit()
            r = self._conn.execute("SELECT * FROM endpoints WHERE id=?", (eid,)).fetchone()
        return self._boolify(r) if r else None

    def delete_endpoint(self, eid: int) -> bool:
        with self._lk:
            c = self._conn.execute("DELETE FROM endpoints WHERE id=?", (eid,))
            self._conn.commit()
        return c.rowcount > 0

    def record_result(
        self, eid: int, success: bool, latency_ms: float,
        fail_threshold: int, cooldown_seconds: float,
        prompt_tokens: int = 0, completion_tokens: int = 0,
    ) -> Optional[dict[str, Any]]:
        ep = self.get_endpoint(eid)
        if not ep:
            return None
        tr = int(ep["total_requests"]) + 1
        ts = int(ep["total_success"]) + (1 if success else 0)
        tf = int(ep["total_failures"]) + (0 if success else 1)
        prev_avg = float(ep["avg_latency_ms"])
        avg = latency_ms if prev_avg == 0 else 0.1 * latency_ms + 0.9 * prev_avg
        upd: dict[str, Any] = {
            "total_requests": tr, "total_success": ts, "total_failures": tf,
            "avg_latency_ms": round(avg, 2),
            "prompt_tokens_total": int(ep["prompt_tokens_total"]) + prompt_tokens,
            "completion_tokens_total": int(ep["completion_tokens_total"]) + completion_tokens,
        }
        if success:
            upd["consecutive_failures"] = 0
            upd["cooldown_until"] = 0
        else:
            cf = int(ep["consecutive_failures"]) + 1
            upd["consecutive_failures"] = cf
            if cf >= fail_threshold:
                upd["cooldown_until"] = time.time() + cooldown_seconds
                LOG.warning("Endpoint %s cooldown after %d failures", ep.get("name", eid), cf)
        return self.update_endpoint(eid, upd)

    def reset_all_stats(self) -> None:
        with self._lk:
            self._conn.execute(
                "UPDATE endpoints SET total_requests=0,total_success=0,total_failures=0,"
                "avg_latency_ms=0,prompt_tokens_total=0,completion_tokens_total=0,"
                "cooldown_until=0,consecutive_failures=0"
            )
            self._conn.commit()

    # -- incoming blocks --

    def list_incoming_blocks(self) -> list[dict[str, Any]]:
        with self._lk:
            rows = self._conn.execute("SELECT * FROM incoming_blocks ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_incoming_block(self, bid: int) -> Optional[dict[str, Any]]:
        with self._lk:
            r = self._conn.execute("SELECT * FROM incoming_blocks WHERE id=?", (bid,)).fetchone()
        return dict(r) if r else None

    def create_incoming_block(self, name: str, px: float, py: float) -> dict[str, Any]:
        now = _utcnow_iso()
        with self._lk:
            cur = self._conn.execute(
                "INSERT INTO incoming_blocks(name,pos_x,pos_y,created_at,updated_at) VALUES(?,?,?,?,?)",
                (name, px, py, now, now),
            )
            self._conn.commit()
            r = self._conn.execute("SELECT * FROM incoming_blocks WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)

    def update_incoming_block(self, bid: int, u: dict[str, Any]) -> Optional[dict[str, Any]]:
        allowed = {"name", "pos_x", "pos_y"}
        cols, vals = [], []
        for k, v in u.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return self.get_incoming_block(bid)
        vals += [_utcnow_iso(), bid]
        with self._lk:
            self._conn.execute(f"UPDATE incoming_blocks SET {','.join(cols)},updated_at=? WHERE id=?", vals)
            self._conn.commit()
            r = self._conn.execute("SELECT * FROM incoming_blocks WHERE id=?", (bid,)).fetchone()
        return dict(r) if r else None

    def delete_incoming_block(self, bid: int) -> bool:
        with self._lk:
            rows = self._conn.execute("SELECT id FROM incoming_blocks ORDER BY id").fetchall()
            ids = [int(r["id"]) for r in rows]
            if bid not in ids:
                return False
            if len(ids) <= 1:
                raise ValueError("cannot delete last incoming block")
            fallback = next(i for i in ids if i != bid)
            self._conn.execute("UPDATE client_tokens SET incoming_block_id=? WHERE incoming_block_id=?", (fallback, bid))
            c = self._conn.execute("DELETE FROM incoming_blocks WHERE id=?", (bid,))
            self._conn.commit()
        return c.rowcount > 0

    # -- client tokens --

    def create_client_token(
        self, name: str, token: Optional[str] = None,
        incoming_block_id: Optional[int] = None,
        preferred_endpoint_id: Optional[int] = None,
        request_priority: int = 0, min_traffic_percent: float = 0,
    ) -> dict[str, Any]:
        tok = (token or "").strip() or ("ct-" + secrets.token_urlsafe(32))
        now = _utcnow_iso()
        with self._lk:
            ibid = incoming_block_id
            if ibid is None:
                blk = self._conn.execute("SELECT id FROM incoming_blocks ORDER BY id LIMIT 1").fetchone()
                ibid = int(blk["id"]) if blk else None
            cur = self._conn.execute(
                "INSERT INTO client_tokens(name,token,incoming_block_id,preferred_endpoint_id,"
                "request_priority,min_traffic_percent,created_at) VALUES(?,?,?,?,?,?,?)",
                (name, tok, ibid, preferred_endpoint_id, int(request_priority), float(min_traffic_percent), now),
            )
            self._conn.commit()
            r = self._conn.execute("SELECT * FROM client_tokens WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)

    def list_client_tokens(self) -> list[dict[str, Any]]:
        with self._lk:
            rows = self._conn.execute("SELECT * FROM client_tokens ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def find_client_token_by_value(self, token: str) -> Optional[dict[str, Any]]:
        with self._lk:
            r = self._conn.execute("SELECT * FROM client_tokens WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None

    def client_token_exists(self, token: str) -> bool:
        with self._lk:
            r = self._conn.execute("SELECT 1 FROM client_tokens WHERE token=?", (token,)).fetchone()
        return r is not None

    def update_client_token(self, tid: int, u: dict[str, Any]) -> Optional[dict[str, Any]]:
        allowed = {"name", "pos_x", "pos_y", "incoming_block_id", "preferred_endpoint_id", "request_priority", "min_traffic_percent"}
        cols, vals = [], []
        for k, v in u.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return None
        vals.append(tid)
        with self._lk:
            self._conn.execute(f"UPDATE client_tokens SET {','.join(cols)} WHERE id=?", vals)
            self._conn.commit()
            r = self._conn.execute("SELECT * FROM client_tokens WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def delete_client_token(self, tid: int) -> bool:
        with self._lk:
            c = self._conn.execute("DELETE FROM client_tokens WHERE id=?", (tid,))
            self._conn.commit()
        return c.rowcount > 0

    def clear_client_token_endpoint_mapping(self, eid: int) -> None:
        with self._lk:
            self._conn.execute("UPDATE client_tokens SET preferred_endpoint_id=NULL WHERE preferred_endpoint_id=?", (eid,))
            self._conn.commit()

    def record_client_token_usage(self, tid: int, prompt_t: int, completion_t: int) -> None:
        with self._lk:
            self._conn.execute(
                "UPDATE client_tokens SET total_requests=total_requests+1,"
                "prompt_tokens_total=prompt_tokens_total+?,completion_tokens_total=completion_tokens_total+? WHERE id=?",
                (prompt_t, completion_t, tid),
            )
            self._conn.commit()

    def get_client_token_traffic_stats(self, tid: int) -> tuple[int, int]:
        with self._lk:
            total_r = self._conn.execute("SELECT COALESCE(SUM(total_requests),0) AS t FROM client_tokens").fetchone()
            tok_r = self._conn.execute("SELECT total_requests FROM client_tokens WHERE id=?", (tid,)).fetchone()
        return (int(total_r["t"]) if total_r else 0, int(tok_r["total_requests"]) if tok_r else 0)

    def reset_client_token_stats(self) -> None:
        with self._lk:
            self._conn.execute("UPDATE client_tokens SET total_requests=0,prompt_tokens_total=0,completion_tokens_total=0")
            self._conn.commit()

    def vacuum(self) -> None:
        with self._lk:
            self._conn.execute("VACUUM")


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


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
    pos_x: float = 500
    pos_y: float = 200

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if not re.match(r"^[\w\-. ]+$", v):
            raise ValueError("name contains invalid characters")
        return v

    @field_validator("role")
    @classmethod
    def _role_ok(cls, v: str) -> str:
        if v not in ("percentage", "primary", "overflow"):
            raise ValueError("role must be percentage|primary|overflow")
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
    def _mode_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("percentage", "priority"):
            raise ValueError("routing_mode must be percentage|priority")
        return v


class ClientTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    token: Optional[str] = Field(default=None, max_length=500)
    pos_x: float = 80
    pos_y: float = 100
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
    pos_x: float = 256
    pos_y: float = 300


class IncomingBlockPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None


# ---------------------------------------------------------------------------
# Runtime per-endpoint state (in-memory only)
# ---------------------------------------------------------------------------


@dataclass
class _RuntimeState:
    inflight: int = 0
    served: int = 0


# ---------------------------------------------------------------------------
# LoadBalancerEngine – routing + connection pooling
# ---------------------------------------------------------------------------


class _Engine:
    def __init__(self, store: _SQLiteStore, cfg: AppSettings, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.store = store
        self.cfg = cfg
        self._transport = transport
        self._rt: dict[int, _RuntimeState] = {}
        self._lock = asyncio.Lock()
        self._verified: Optional[httpx.AsyncClient] = None
        self._insecure: Optional[httpx.AsyncClient] = None
        self._tok_win = SlidingWindow(cfg.sliding_window_seconds, cfg.sliding_window_max_entries)
        self._req_win = SlidingWindow(cfg.sliding_window_seconds, cfg.sliding_window_max_entries)
        self._client_wins: dict[int, SlidingWindow] = {}
        self._total_req = 0
        self._total_err = 0
        self._start = time.time()

    def _make_client(self, verify: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=verify, http2=True, timeout=None, transport=self._transport,
            limits=httpx.Limits(max_connections=self.cfg.max_connections, max_keepalive_connections=self.cfg.max_keepalive),
        )

    async def startup(self) -> None:
        self._verified = self._make_client(True)
        self._insecure = self._make_client(False)
        self._start = time.time()

    async def shutdown(self) -> None:
        deadline = time.time() + self.cfg.drain_timeout_seconds
        while time.time() < deadline:
            async with self._lock:
                total = sum(r.inflight for r in self._rt.values())
            if total == 0:
                break
            await asyncio.sleep(0.5)
        for c in (self._verified, self._insecure):
            if c and not c.is_closed:
                await c.aclose()

    def http_client(self, ep: dict[str, Any]) -> httpx.AsyncClient:
        c = self._verified if ep.get("verify_tls", True) else self._insecure
        if not c:
            raise RuntimeError("HTTP client not initialised")
        return c

    @property
    def routing_mode(self) -> str:
        return self.store.get_setting("routing_mode") or "percentage"

    async def acquire(self, preferred_eid: Optional[int] = None, prefer_non_primary: bool = False) -> dict[str, Any]:
        now = time.time()
        healthy = await asyncio.to_thread(self.store.list_connected_healthy, now)
        if not healthy:
            connected = await asyncio.to_thread(self.store.list_connected_enabled)
            if not connected:
                raise HTTPException(503, "no connected endpoints available")
            raise HTTPException(503, "all connected endpoints in cooldown")

        if preferred_eid is not None:
            chosen = next((e for e in healthy if int(e["id"]) == int(preferred_eid)), None)
            if not chosen:
                connected = await asyncio.to_thread(self.store.list_connected_enabled)
                if any(int(e["id"]) == int(preferred_eid) for e in connected):
                    raise HTTPException(503, "mapped endpoint currently in cooldown")
                raise HTTPException(503, "mapped endpoint not connected")
        elif self.routing_mode == "priority":
            chosen = await self._pick_priority(healthy, prefer_non_primary)
        else:
            chosen = await self._pick_percentage(healthy)

        async with self._lock:
            rt = self._rt.setdefault(int(chosen["id"]), _RuntimeState())
            rt.inflight += 1
            rt.served += 1
            self._req_win.add(1.0)
            self._total_req += 1
        return chosen

    async def _pick_percentage(self, eps: list[dict[str, Any]]) -> dict[str, Any]:
        total_tok = sum(int(e.get("completion_tokens_total", 0)) for e in eps)
        if total_tok == 0:
            async with self._lock:
                return min(eps, key=lambda e: self._rt.get(int(e["id"]), _RuntimeState()).inflight)
        total_quota = sum(float(e.get("token_quota_percent", 0)) for e in eps)
        best, best_d = eps[0], -1e18
        for e in eps:
            q = float(e.get("token_quota_percent", 0))
            if total_quota <= 0:
                q = 100.0 / len(eps)
            actual = int(e.get("completion_tokens_total", 0)) / total_tok * 100
            deficit = q - actual
            async with self._lock:
                inf = self._rt.get(int(e["id"]), _RuntimeState()).inflight
            adj = deficit - inf * 0.01
            if adj > best_d:
                best, best_d = e, adj
        return best

    async def _pick_priority(self, eps: list[dict[str, Any]], prefer_non_primary: bool = False) -> dict[str, Any]:
        def sort_key(e: dict[str, Any]) -> tuple:
            return (
                int(e.get("priority_order", 10)),
                int(e.get("consecutive_failures", 0)),
                int(e.get("total_failures", 0)),
                float(e.get("avg_latency_ms", 0)) if int(e.get("total_requests", 0)) > 0 else 0.0,
                int(e.get("id", 0)),
            )
        ordered = sorted(eps, key=sort_key)
        if prefer_non_primary and len(ordered) > 1:
            ordered = ordered[1:] + ordered[:1]
        async with self._lock:
            for e in ordered:
                mc = int(e.get("max_concurrent", 0))
                inf = self._rt.get(int(e["id"]), _RuntimeState()).inflight
                if mc > 0 and inf >= mc:
                    continue
                return e
            return min(ordered, key=lambda e: self._rt.get(int(e["id"]), _RuntimeState()).inflight)

    async def release(self, eid: int) -> None:
        async with self._lock:
            rt = self._rt.setdefault(eid, _RuntimeState())
            rt.inflight = max(0, rt.inflight - 1)

    def note_tokens(self, n: int) -> None:
        if n > 0:
            self._tok_win.add(float(n))

    def note_error(self) -> None:
        self._total_err += 1

    def note_client_request(self, tid: int) -> None:
        if tid not in self._client_wins:
            self._client_wins[tid] = SlidingWindow(60.0, 10_000)
        self._client_wins[tid].add(1.0)

    def client_stats(self) -> dict[int, dict[str, Any]]:
        return {tid: {"requests_per_second": w.count_per_second()} for tid, w in self._client_wins.items()}

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            inf = {eid: rt.inflight for eid, rt in self._rt.items()}
            total_inf = sum(rt.inflight for rt in self._rt.values())
        return {
            "tokens_per_second": self._tok_win.rate_per_second(),
            "requests_per_second": self._req_win.count_per_second(),
            "total_inflight": total_inf,
            "inflight_per_endpoint": inf,
            "total_requests_served": self._total_req,
            "total_errors": self._total_err,
            "uptime_seconds": round(time.time() - self._start, 1),
        }

    async def prometheus_metrics(self) -> str:
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
            n = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(f'lb_endpoint_requests_total{{endpoint="{n}"}} {ep.get("total_requests", 0)}')
        lines += [
            "", "# HELP lb_endpoint_failures_total Failures per endpoint",
            "# TYPE lb_endpoint_failures_total counter",
        ]
        for ep in eps:
            n = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(f'lb_endpoint_failures_total{{endpoint="{n}"}} {ep.get("total_failures", 0)}')
        lines += [
            "", "# HELP lb_endpoint_latency_ms Average latency per endpoint",
            "# TYPE lb_endpoint_latency_ms gauge",
        ]
        for ep in eps:
            n = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(f'lb_endpoint_latency_ms{{endpoint="{n}"}} {ep.get("avg_latency_ms", 0)}')
        lines += [
            "", "# HELP lb_endpoint_inflight In-flight per endpoint",
            "# TYPE lb_endpoint_inflight gauge",
        ]
        for ep in eps:
            n = ep.get("name", "unknown").replace('"', '\\"')
            lines.append(f'lb_endpoint_inflight{{endpoint="{n}"}} {st["inflight_per_endpoint"].get(int(ep["id"]), 0)}')
        lines += [
            "", "# HELP lb_endpoint_tokens_total Tokens per endpoint",
            "# TYPE lb_endpoint_tokens_total counter",
        ]
        for ep in eps:
            n = ep.get("name", "unknown").replace('"', '\\"')
            t = int(ep.get("prompt_tokens_total", 0)) + int(ep.get("completion_tokens_total", 0))
            lines.append(f'lb_endpoint_tokens_total{{endpoint="{n}"}} {t}')
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------


def load_settings() -> AppSettings:
    ct_raw = os.getenv("LB_CLIENT_TOKENS", "")
    ct = {t.strip() for t in ct_raw.split(",") if t.strip()}
    cors_raw = os.getenv("LB_CORS_ORIGINS", "*")
    cors = [o.strip() for o in cors_raw.split(",") if o.strip()]
    s = AppSettings(
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
    if s.admin_token == "change-me-admin-token":
        LOG.warning("LB_ADMIN_TOKEN uses the default – set a strong token!")
    return s


def create_app(
    settings: Optional[AppSettings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    cfg = settings or load_settings()

    fmt = (
        "%(asctime)s %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s"
        if cfg.log_level == "DEBUG"
        else "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    logging.basicConfig(level=cfg.log_level, format=fmt, stream=sys.stdout)

    store = _SQLiteStore(cfg.db_path)
    engine = _Engine(store, cfg, transport)
    cache = TTLCache(cfg.cache_max_entries, cfg.cache_ttl_seconds)
    rate_limiter = RateLimiter(cfg.admin_rate_limit_rpm)
    audit = _AuditLogger()

    def _cache_key(ctid: int, mapped_eid: Optional[int], body: bytes) -> str:
        h = hashlib.sha256()
        h.update(str(ctid).encode())
        h.update(b"|")
        h.update(str(mapped_eid).encode() if mapped_eid is not None else b"-")
        h.update(b"|")
        h.update(body)
        return h.hexdigest()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.startup()
        LOG.info("Load Balancer v%s ready on port %s", __version__, cfg.port)
        try:
            yield
        finally:
            await engine.shutdown()
            store.close()

    app = FastAPI(title="LLM Load Balancer", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url="/redoc")
    app.state.settings = cfg
    app.state.store = store
    app.state.engine = engine

    app.add_middleware(
        CORSMiddleware, allow_origins=cfg.cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
        expose_headers=["X-LB-Endpoint", "X-LB-Cache", "X-LB-Version", "X-Request-ID"],
    )

    # -- middleware --

    @app.middleware("http")
    async def _security(request: Request, call_next):
        rid = request.headers.get("x-request-id", "") or _make_request_id()
        request.state.request_id = rid
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-Request-ID"] = rid
        resp.headers["X-LB-Version"] = __version__
        return resp

    @app.middleware("http")
    async def _body_limit(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > cfg.max_request_body_bytes:
            return _problem(413, f"Request body too large (max {cfg.max_request_body_bytes} bytes)", "request_too_large")
        return await call_next(request)

    # -- exception handlers --

    @app.exception_handler(HTTPException)
    async def _http_err(request: Request, exc: HTTPException):
        return _problem(exc.status_code, str(exc.detail), request_id=getattr(request.state, "request_id", ""))

    @app.exception_handler(Exception)
    async def _generic_err(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "")
        LOG.exception("Unhandled error: %s [req=%s]", exc, rid)
        return _problem(500, "internal server error", "internal_error", rid)

    # -- auth deps --

    def _prio_input_tid() -> Optional[int]:
        raw = store.get_setting("priority_input_token_id")
        if not raw:
            return None
        try:
            val = int(raw)
        except Exception:
            return None
        return val if val > 0 else None

    def require_admin(request: Request, authorization: Optional[str] = Header(None), x_admin_token: Optional[str] = Header(None)):
        if not rate_limiter.check():
            raise HTTPException(429, "admin API rate limit exceeded")
        if not cfg.admin_token or cfg.admin_token == "change-me-admin-token":
            return
        tok = (x_admin_token or "").strip() or _bearer(authorization)
        if not _ct_compare(tok, cfg.admin_token):
            raise HTTPException(401, "admin authorization failed")

    def require_client(request: Request, authorization: Optional[str] = Header(None)):
        request.state.client_token_id = None
        request.state.client_token = None
        has_env = bool(cfg.client_tokens)
        has_db = bool(store.list_client_tokens())
        if not has_env and not has_db:
            return
        tok = _bearer(authorization)
        if not tok:
            raise HTTPException(401, "client token required")
        if any(_ct_compare(tok, ct) for ct in cfg.client_tokens):
            return
        rec = store.find_client_token_by_value(tok)
        if rec:
            request.state.client_token_id = rec["id"]
            request.state.client_token = rec
            return
        raise HTTPException(401, "client token invalid")

    # -- internal helpers --

    async def _finish(eid: int, status: int, lat: float, pt: int = 0, ct: int = 0, ctid: Optional[int] = None):
        ok = _is_success(status)
        if not ok:
            engine.note_error()
        await asyncio.to_thread(store.record_result, eid, ok, lat, cfg.fail_threshold, cfg.cooldown_seconds, pt, ct)
        engine.note_tokens(pt + ct)
        if ctid is not None:
            await asyncio.to_thread(store.record_client_token_usage, ctid, pt, ct)
            engine.note_client_request(ctid)

    async def _fail(eid: int, lat: float):
        engine.note_error()
        await asyncio.to_thread(store.record_result, eid, False, lat, cfg.fail_threshold, cfg.cooldown_seconds)

    # -- proxy: chat completions --

    async def _proxy_chat(request: Request) -> Response:
        rid = getattr(request.state, "request_id", "")
        ctid: Optional[int] = getattr(request.state, "client_token_id", None)
        ct_rec: Optional[dict] = getattr(request.state, "client_token", None)
        mapped_eid: Optional[int] = None
        if ct_rec and ct_rec.get("preferred_endpoint_id") is not None:
            mapped_eid = int(ct_rec["preferred_endpoint_id"])

        prio_tid = _prio_input_tid() if engine.routing_mode == "priority" else None
        prefer_non = (
            mapped_eid is None and engine.routing_mode == "priority"
            and prio_tid is not None
            and (ctid is None or int(ctid) != prio_tid)
        )

        raw = await request.body()
        if len(raw) > cfg.max_request_body_bytes:
            raise HTTPException(413, "request body too large")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")

        if ct_rec and ct_rec.get("request_priority") is not None and body.get("priority") is None:
            body["priority"] = int(ct_rec["request_priority"])
            raw = json.dumps(body).encode()

        stream = bool(body.get("stream", False))

        # quota / cache logic
        min_share = float(ct_rec.get("min_traffic_percent", 0)) if ct_rec else 0
        cache_key: Optional[str] = None
        cache_only = False
        if ctid is not None and min_share > 0:
            total_req, tok_req = await asyncio.to_thread(store.get_client_token_traffic_stats, ctid)
            share = (tok_req / total_req * 100) if total_req > 0 else 0.0
            cache_only = total_req > 0 and share >= min_share
            if stream and cache_only:
                raise HTTPException(409, "cache-only traffic requires non-stream requests")
            if not stream:
                cache_key = _cache_key(ctid, mapped_eid, raw)

        if cache_only and cache_key:
            cached = await cache.get(cache_key)
            if not cached:
                raise HTTPException(503, "cache miss for over-quota token traffic")
            c_status, c_body, c_media, c_ep = cached
            pt, cpt = _extract_usage(c_body)
            if ctid is not None:
                await asyncio.to_thread(store.record_client_token_usage, ctid, pt, cpt)
                engine.note_client_request(ctid)
            engine.note_tokens(pt + cpt)
            engine._req_win.add(1.0)
            return Response(content=c_body, status_code=c_status, headers={"X-LB-Cache": "HIT", "X-LB-Endpoint": c_ep, "X-Request-ID": rid}, media_type=c_media)

        ep = await engine.acquire(mapped_eid, prefer_non_primary=prefer_non)
        eid = int(ep["id"])
        ep_name = str(ep["name"])
        hdrs = _upstream_headers(request, ep)
        url = _join(str(ep["base_url"]), "/chat/completions")
        timeout = float(ep.get("timeout_seconds") or cfg.default_timeout_seconds)
        hclient = engine.http_client(ep)
        t0 = time.perf_counter()

        if not stream:
            try:
                up = await hclient.post(url, content=raw, headers=hdrs, timeout=timeout)
                lat = (time.perf_counter() - t0) * 1000
                pt, cpt = _extract_usage(up.content)
                await _finish(eid, up.status_code, lat, pt, cpt, ctid)
                if cache_key and 200 <= up.status_code < 300 and up.headers.get("content-type"):
                    await cache.put(cache_key, (up.status_code, bytes(up.content), up.headers.get("content-type", "application/json"), ep_name))
                rh = _filter_upstream_headers(up.headers, ep_name, rid)
                rh["X-LB-Cache"] = "MISS"
                return Response(content=up.content, status_code=up.status_code, headers=rh, media_type=up.headers.get("content-type", "application/json"))
            except HTTPException:
                raise
            except Exception as exc:
                lat = (time.perf_counter() - t0) * 1000
                await _fail(eid, lat)
                raise HTTPException(502, f"upstream error: {type(exc).__name__}: {exc}")
            finally:
                await engine.release(eid)

        # streaming
        ctx = hclient.stream("POST", url, content=raw, headers=hdrs, timeout=timeout)
        try:
            up = await ctx.__aenter__()
        except Exception as exc:
            lat = (time.perf_counter() - t0) * 1000
            await _fail(eid, lat)
            await engine.release(eid)
            raise HTTPException(502, f"upstream stream failed: {type(exc).__name__}: {exc}")

        status = int(up.status_code)
        rh = _filter_upstream_headers(up.headers, ep_name, rid)

        async def _gen():
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
                    lat = (time.perf_counter() - t0) * 1000
                    if failed:
                        await _fail(eid, lat)
                    else:
                        pt, cpt = 0, 0
                        try:
                            for line in last_data.decode("utf-8", errors="ignore").split("\n"):
                                s = line.strip()
                                if s.startswith("data:") and s != "data: [DONE]":
                                    j = json.loads(s[5:].strip())
                                    u = j.get("usage") or {}
                                    if u.get("completion_tokens"):
                                        pt = int(u.get("prompt_tokens", 0))
                                        cpt = int(u.get("completion_tokens", 0))
                        except Exception:
                            pass
                        await _finish(eid, status, lat, pt, cpt, ctid)
                    await engine.release(eid)

        return StreamingResponse(_gen(), status_code=status, headers=rh, media_type=up.headers.get("content-type", "text/event-stream"))

    # -- proxy: models --

    async def _proxy_models(request: Request) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        ct_rec: Optional[dict] = getattr(request.state, "client_token", None)
        mapped_eid: Optional[int] = None
        if ct_rec and ct_rec.get("preferred_endpoint_id") is not None:
            mapped_eid = int(ct_rec["preferred_endpoint_id"])
        endpoints = await asyncio.to_thread(store.list_connected_enabled)
        if mapped_eid is not None:
            endpoints = [e for e in endpoints if int(e["id"]) == int(mapped_eid)]
            if not endpoints:
                raise HTTPException(503, "mapped endpoint not connected")
        if not endpoints:
            raise HTTPException(503, "no connected endpoints")

        async def _fetch(ep: dict[str, Any]) -> list[dict[str, Any]]:
            eid = int(ep["id"])
            url = _join(str(ep["base_url"]), "/models")
            h = _upstream_headers(request, ep)
            to = float(ep.get("timeout_seconds") or cfg.default_timeout_seconds)
            hc = engine.http_client(ep)
            t0 = time.perf_counter()
            try:
                r = await hc.get(url, headers=h, timeout=to)
                lat = (time.perf_counter() - t0) * 1000
                await _finish(eid, r.status_code, lat)
                if 200 <= r.status_code < 300:
                    return r.json().get("data", [])
            except Exception:
                await _fail(eid, (time.perf_counter() - t0) * 1000)
            return []

        results = await asyncio.gather(*(_fetch(ep) for ep in endpoints), return_exceptions=True)
        combined: dict[str, dict[str, Any]] = {}
        for res in results:
            if isinstance(res, Exception):
                continue
            for m in res:
                mid = m.get("id")
                if mid:
                    combined[mid] = m
        if not combined:
            raise HTTPException(502, "no models returned from any endpoint")
        return JSONResponse({"object": "list", "data": [combined[k] for k in sorted(combined)]}, headers={"X-Request-ID": rid})

    # ===== ROUTES =====

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin")

    @app.get("/health")
    async def health():
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        connected = sum(1 for e in eps if e.get("connected"))
        ok = connected > 0
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "version": __version__, "endpoints": len(eps), "connected": connected,
             "routing_mode": engine.routing_mode, "priority_input_token_id": _prio_input_tid(), "cache_size": cache.size, **st},
            status_code=200 if ok else 503,
        )

    @app.head("/health")
    async def health_head():
        eps = await asyncio.to_thread(store.list_endpoints)
        connected = sum(1 for e in eps if e.get("connected"))
        return Response(status_code=200 if connected > 0 else 503)

    @app.get("/metrics")
    async def metrics():
        text = await engine.prometheus_metrics()
        return Response(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/admin/api/config")
    async def public_config():
        return JSONResponse({"supabase_url": os.getenv("SUPABASE_URL", ""), "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", "")})

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_ui():
        p = Path(__file__).with_name("load_balancer_admin.html")
        if not p.exists():
            raise HTTPException(500, "admin UI missing")
        return HTMLResponse(p.read_text(encoding="utf-8"))

    # -- admin: state --

    @app.get("/admin/api/state", dependencies=[Depends(require_admin)])
    async def get_state():
        eps = await asyncio.to_thread(store.list_endpoints)
        st = await engine.stats()
        inf = st.get("inflight_per_endpoint", {})
        safe_eps = []
        for e in eps:
            eid = int(e["id"])
            enriched = dict(e)
            enriched["inflight"] = inf.get(eid, 0)
            enriched["cooldown_remaining_s"] = max(0.0, float(e.get("cooldown_until", 0)) - time.time())
            safe_eps.append(_sanitize_ep(enriched))
        blocks = await asyncio.to_thread(store.list_incoming_blocks)
        if not blocks:
            created = await asyncio.to_thread(store.create_incoming_block, "Incoming 1", 256.0, 300.0)
            blocks = [created]
        inc_safe = [{"id": int(b["id"]), "name": str(b["name"]), "pos_x": float(b.get("pos_x", 256)), "pos_y": float(b.get("pos_y", 300))} for b in blocks]
        tokens_raw = await asyncio.to_thread(store.list_client_tokens)
        cstats = engine.client_stats()
        ep_names = {int(e["id"]): str(e["name"]) for e in eps}
        inc_ids = {int(b["id"]) for b in blocks}
        default_ibid = int(blocks[0]["id"])
        tokens_safe = []
        for t in tokens_raw:
            tid = t["id"]
            cs = cstats.get(tid, {})
            peid = t.get("preferred_endpoint_id")
            ibid = t.get("incoming_block_id")
            if ibid is None or int(ibid) not in inc_ids:
                ibid = default_ibid
            tokens_safe.append({
                "id": tid, "name": t["name"], "token_preview": _mask(t["token"]),
                "created_at": t["created_at"], "pos_x": t.get("pos_x", 80), "pos_y": t.get("pos_y", 100),
                "incoming_block_id": int(ibid), "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "requests_per_second": cs.get("requests_per_second", 0),
                "preferred_endpoint_id": peid,
                "preferred_endpoint_name": ep_names.get(int(peid)) if peid is not None else None,
                "request_priority": t.get("request_priority", 0),
                "min_traffic_percent": t.get("min_traffic_percent", 0),
            })
        return JSONResponse({
            "routing_mode": engine.routing_mode,
            "priority_input_token_id": _prio_input_tid(),
            "endpoints": safe_eps,
            "incoming_blocks": inc_safe,
            "incoming_pos": {"x": inc_safe[0]["pos_x"], "y": inc_safe[0]["pos_y"]},
            "stats": st, "client_tokens": tokens_safe, "version": __version__,
        })

    # -- admin: endpoints --

    @app.post("/admin/api/endpoints", dependencies=[Depends(require_admin)])
    async def create_ep(request: Request, payload: EndpointCreate):
        d = payload.model_dump()
        try:
            d["base_url"] = _normalize_url(d["base_url"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        try:
            ep = await asyncio.to_thread(store.create_endpoint, d)
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        audit.log("endpoint_created", getattr(request.state, "request_id", ""), {"name": d["name"], "id": ep.get("id")})
        return JSONResponse(_sanitize_ep(ep))

    @app.patch("/admin/api/endpoints/{eid}", dependencies=[Depends(require_admin)])
    async def patch_ep(eid: int, request: Request, payload: EndpointPatch):
        u = payload.model_dump(exclude_none=True)
        if "base_url" in u:
            try:
                u["base_url"] = _normalize_url(u["base_url"])
            except ValueError as e:
                raise HTTPException(422, str(e))
        if "name" in u:
            u["name"] = u["name"].strip()
        try:
            ep = await asyncio.to_thread(store.update_endpoint, eid, u)
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        if not ep:
            raise HTTPException(404, "endpoint not found")
        audit.log("endpoint_updated", getattr(request.state, "request_id", ""), {"id": eid, "fields": list(u.keys())})
        return JSONResponse(_sanitize_ep(ep))

    @app.delete("/admin/api/endpoints/{eid}", dependencies=[Depends(require_admin)])
    async def delete_ep(eid: int, request: Request):
        await asyncio.to_thread(store.clear_client_token_endpoint_mapping, eid)
        if not await asyncio.to_thread(store.delete_endpoint, eid):
            raise HTTPException(404, "endpoint not found")
        audit.log("endpoint_deleted", getattr(request.state, "request_id", ""), {"id": eid})
        return JSONResponse({"ok": True})

    # -- admin: routing --

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
        audit.log("routing_updated", getattr(request.state, "request_id", ""))
        return JSONResponse({"routing_mode": engine.routing_mode, "priority_input_token_id": _prio_input_tid()})

    # -- admin: stats / cache / vacuum --

    @app.post("/admin/api/reset-stats", dependencies=[Depends(require_admin)])
    async def reset_stats(request: Request):
        await asyncio.to_thread(store.reset_all_stats)
        await asyncio.to_thread(store.reset_client_token_stats)
        engine._tok_win.clear()
        engine._req_win.clear()
        engine._client_wins.clear()
        engine._total_req = 0
        engine._total_err = 0
        await cache.clear()
        audit.log("stats_reset", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True})

    @app.post("/admin/api/cache/clear", dependencies=[Depends(require_admin)])
    async def clear_cache(request: Request):
        await cache.clear()
        audit.log("cache_cleared", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True, "message": "cache cleared"})

    @app.post("/admin/api/db/vacuum", dependencies=[Depends(require_admin)])
    async def vacuum_db(request: Request):
        await asyncio.to_thread(store.vacuum)
        audit.log("db_vacuum", getattr(request.state, "request_id", ""))
        return JSONResponse({"ok": True, "message": "database vacuumed"})

    # -- admin: tokens --

    @app.get("/admin/api/tokens", dependencies=[Depends(require_admin)])
    async def list_tokens():
        tokens = await asyncio.to_thread(store.list_client_tokens)
        blocks = await asyncio.to_thread(store.list_incoming_blocks)
        inc_ids = {int(b["id"]) for b in blocks}
        default_ibid = int(blocks[0]["id"]) if blocks else 1
        safe = []
        for t in tokens:
            ibid = t.get("incoming_block_id")
            if ibid is None or int(ibid) not in inc_ids:
                ibid = default_ibid
            safe.append({
                "id": t["id"], "name": t["name"], "token_preview": _mask(t["token"]), "token": t["token"],
                "created_at": t["created_at"], "pos_x": t.get("pos_x", 80), "pos_y": t.get("pos_y", 100),
                "incoming_block_id": int(ibid), "total_requests": t.get("total_requests", 0),
                "prompt_tokens_total": t.get("prompt_tokens_total", 0),
                "completion_tokens_total": t.get("completion_tokens_total", 0),
                "preferred_endpoint_id": t.get("preferred_endpoint_id"),
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
                store.create_client_token, payload.name, payload.token,
                payload.incoming_block_id, payload.preferred_endpoint_id,
                payload.request_priority, payload.min_traffic_percent,
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

    # -- admin: incoming blocks --

    @app.get("/admin/api/incoming-blocks", dependencies=[Depends(require_admin)])
    async def list_inc_blocks():
        return JSONResponse(await asyncio.to_thread(store.list_incoming_blocks))

    @app.post("/admin/api/incoming-blocks", dependencies=[Depends(require_admin)])
    async def create_inc_block(payload: IncomingBlockCreate):
        b = await asyncio.to_thread(store.create_incoming_block, payload.name.strip(), payload.pos_x, payload.pos_y)
        return JSONResponse(b)

    @app.patch("/admin/api/incoming-blocks/{bid}", dependencies=[Depends(require_admin)])
    async def patch_inc_block(bid: int, payload: IncomingBlockPatch):
        u = payload.model_dump(exclude_none=True)
        if "name" in u:
            u["name"] = str(u["name"]).strip()
        if not u:
            raise HTTPException(400, "nothing to update")
        b = await asyncio.to_thread(store.update_incoming_block, bid, u)
        if not b:
            raise HTTPException(404, "incoming block not found")
        return JSONResponse(b)

    @app.delete("/admin/api/incoming-blocks/{bid}", dependencies=[Depends(require_admin)])
    async def delete_inc_block(bid: int):
        try:
            ok = await asyncio.to_thread(store.delete_incoming_block, bid)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if not ok:
            raise HTTPException(404, "incoming block not found")
        return JSONResponse({"ok": True})

    @app.patch("/admin/api/incoming-pos", dependencies=[Depends(require_admin)])
    async def patch_inc_pos(request: Request):
        body = await request.json()
        blocks = await asyncio.to_thread(store.list_incoming_blocks)
        if not blocks:
            created = await asyncio.to_thread(store.create_incoming_block, "Incoming 1", 256.0, 300.0)
            blocks = [created]
        fid = int(blocks[0]["id"])
        updates: dict[str, Any] = {}
        if "x" in body:
            updates["pos_x"] = float(body["x"])
            await asyncio.to_thread(store.set_setting, "incoming_pos_x", str(body["x"]))
        if "y" in body:
            updates["pos_y"] = float(body["y"])
            await asyncio.to_thread(store.set_setting, "incoming_pos_y", str(body["y"]))
        if updates:
            await asyncio.to_thread(store.update_incoming_block, fid, updates)
        return JSONResponse({"ok": True, "incoming_block_id": fid})

    # -- proxy routes --

    @app.post("/v1/chat/completions", dependencies=[Depends(require_client)])
    async def chat(request: Request):
        return await _proxy_chat(request)

    @app.post("/chat/completions", dependencies=[Depends(require_client)])
    async def chat_legacy(request: Request):
        return await _proxy_chat(request)

    @app.post("/v1/{pool_name}/chat/completions", dependencies=[Depends(require_client)])
    async def chat_pool(pool_name: str, request: Request):
        return await _proxy_chat(request)

    @app.get("/v1/models", dependencies=[Depends(require_client)])
    async def models(request: Request):
        return await _proxy_models(request)

    @app.get("/models", dependencies=[Depends(require_client)])
    async def models_legacy(request: Request):
        return await _proxy_models(request)

    @app.get("/v1/{pool_name}/models", dependencies=[Depends(require_client)])
    async def models_pool(pool_name: str, request: Request):
        return await _proxy_models(request)

    return app


# ---------------------------------------------------------------------------
# Module-level app + entry point
# ---------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("load_balancer:app", host="0.0.0.0", port=s.port, reload=False, log_level=s.log_level.lower(), access_log=True)
