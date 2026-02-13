# Changelog

All notable changes to the LLM Load Balancer project are documented in this file.

---

## [3.0.0] — Enterprise Overhaul

Complete enterprise-grade rewrite across 5 commits with 100+ improvements.

### Commit 1 — Core Robustness & Observability (`a459d2a`)

**Load Balancer (`load_balancer.py`)**

> 794 insertions, 599 deletions — full rewrite from v2 to v3.

#### Architecture & Code Quality
1. Extracted `AppSettings` dataclass with 16 typed fields + validators
2. Introduced `LoadBalancerError` hierarchy with structured error codes
3. Added `SlidingWindow` class for time-bucketed event counting
4. Added `TTLCache` class with max-size eviction and atomic operations
5. Added `RateLimiter` class (sliding-window, configurable RPM)
6. Added `AuditLogger` for all admin mutations (JSON audit trail)
7. App factory pattern (`create_app()`) for testability
8. All SQL queries use parameterized bindings (no string interpolation)
9. Constants extracted: `DB_SCHEMA_VERSION`, `DEFAULT_*`, `MAX_*`
10. Type hints on all function signatures

#### Security
11. Request body size enforcement middleware (`LB_MAX_REQUEST_BODY_BYTES`)
12. CORS with configurable origins (`LB_CORS_ORIGINS`)
13. Security headers on every response (`X-Content-Type-Options`, `X-Frame-Options`)
14. `X-Request-ID` injection (preserved if sent by client, generated otherwise)
15. Admin API rate limiting (429 on exceed)
16. Admin token length warning at startup
17. API key masking in all admin responses (`api_key_preview`)

#### Observability
18. Prometheus-compatible `/metrics` endpoint
19. `HEAD /health` for lightweight probes
20. Exponential moving average latency per endpoint
21. `X-LB-Version` response header
22. Structured JSON logging with configurable level

#### Resilience
23. Graceful shutdown with connection draining (`LB_DRAIN_TIMEOUT_SECONDS`)
24. Signal handler for SIGTERM/SIGINT
25. Database WAL mode + PRAGMA optimizations (journal_size_limit, synchronous, mmap)
26. Connection pool tuning (`LB_MAX_CONNECTIONS`, `LB_MAX_KEEPALIVE`)

#### Endpoints
27. `POST /admin/api/cache/clear` — clear response cache
28. `POST /admin/api/db/vacuum` — compact SQLite database
29. `GET /metrics` — Prometheus metrics export
30. `HEAD /health` — lightweight health probe
31. Root `/` redirects to `/admin`

#### Input Validation (Pydantic v2)
32. `base_url` validated as proper URL (scheme + host)
33. `token_quota_percent` clamped to 0.0–100.0
34. `priority_order` minimum 0
35. `max_concurrent` minimum 1
36. `timeout` minimum 1.0
37. `name` stripped and length-validated

---

### Commit 2 — Admin UI Enterprise (`1a1d493`)

**Admin Dashboard (`load_balancer_admin.html`)**

> 643 insertions, 207 deletions — 25 UI improvements.

#### UX Improvements
38. Toast notification system (replaced all `alert()` calls)
39. Styled confirmation modals (replaced all `confirm()` dialogs)
40. Dark/light theme toggle with `localStorage` persistence
41. Keyboard shortcuts: `R` (refresh), `T` (theme), `?` (help), `Esc` (close)
42. Collapsible sidebar sections (Endpoints, Tokens, Incoming Blocks)
43. Token search/filter with debounced input
44. Loading overlay spinner during initial load
45. Auto-retry on connection loss with exponential backoff

#### Visual Design
46. Connection status dot with pulse animation
47. Version badge fetched from `/health` endpoint
48. Canvas zoom controls (+/- buttons)
49. Color-coded footer metrics (green/amber/red thresholds)
50. Node fade-in animation on creation
51. CSS custom properties for purple wires (n8n-inspired)
52. Custom scrollbar styling
53. Improved empty-state messages

#### Accessibility & Security
54. ARIA roles and labels on interactive elements
55. `focus-visible` styles for keyboard navigation
56. XSS-hardened `esc()` utility (backtick + template literal escaping)
57. SVG favicon (inline, no external request)
58. Better responsive breakpoints for mobile/tablet

#### Integration
59. Improved API error display in toasts with status codes
60. Sidebar auto-refreshes on endpoint changes
61. Drag-and-drop wire drawing with visual feedback
62. Canvas pan with mouse drag (background)

---

### Commit 3 — Test Suite Enhancement (`2add472`)

**Test Suite (`tests/test_load_balancer.py`)**

> 469 insertions, 385 deletions — expanded from 15 to 35 tests.

#### New Tests
63. `test_health_endpoint` — GET /health returns all required fields
64. `test_health_head` — HEAD /health returns 200 with empty body
65. `test_metrics_endpoint` — Prometheus metrics format and content
66. `test_security_headers` — X-Content-Type-Options, X-Frame-Options present
67. `test_request_id_tracking` — X-Request-ID preserved when sent, generated when not
68. `test_root_redirects_to_admin` — GET / returns 307 to /admin
69. `test_admin_html_page` — GET /admin returns HTML with expected markers
70. `test_cache_clear` — POST /admin/api/cache/clear succeeds
71. `test_db_vacuum` — POST /admin/api/db/vacuum succeeds
72. `test_reset_stats` — POST /admin/api/reset-stats clears counters
73. `test_404_unknown_path` — Unknown routes return 404
74. `test_sliding_window_basic` — SlidingWindow add + count
75. `test_sliding_window_expiry` — SlidingWindow time-based expiry
76. `test_ttl_cache_set_get` — TTLCache basic set/get/eviction
77. `test_ttl_cache_ttl_expiry` — TTLCache TTL-based expiry
78. `test_rate_limiter` — RateLimiter allows under limit, blocks over
79. `test_endpoint_input_validation` — Pydantic validation rejects invalid input
80. `test_no_endpoints_returns_503` — Empty routing returns 503
81. `test_version_constant` — `__version__` matches semantic versioning
82. `test_incoming_blocks_crud` — Full CRUD lifecycle for incoming blocks

#### Improvements
83. Shared `ok_handler` / `ok_transport` helpers (DRY setup)
84. Replaced deprecated `asyncio.get_event_loop()` with `asyncio.run()`
85. Consistent test naming convention (`test_<feature>`)
86. All tests fully isolated (temp DB per test)

---

### Commit 4 — Docker & Operations (`cf78afe`)

**Infrastructure hardening across 7 files.**

#### Dockerfile.load-balancer
87. Multi-stage build (builder + runtime stages)
88. Non-root user (`appuser`, UID 1000)
89. `HEALTHCHECK` instruction for orchestration
90. OCI labels (maintainer, description, version)
91. `PYTHONDONTWRITEBYTECODE=1` + `PYTHONUNBUFFERED=1`

#### Dockerfile.ws-proxy
92. Non-root user (`appuser`)
93. `HEALTHCHECK` instruction
94. OCI labels and Python env vars

#### docker-compose.yml
95. Resource limits (512 MB memory per container)
96. JSON logging driver with rotation (10 MB max, 3 files)
97. `security_opt: no-new-privileges`
98. New env vars: `LB_CORS_ORIGINS`, `LB_CACHE_TTL_SECONDS`
99. Fixed healthcheck to accept `status < 500` (allows degraded)

#### nginx.conf
100. Gzip compression (text, JSON, JS, CSS, XML, SVG)
101. Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
102. Explicit `client_max_body_size 10m`

#### ws_proxy.py
103. `__version__` constant (1.1.0)
104. Graceful shutdown via SIGTERM/SIGINT handlers
105. Connection metrics tracking (active, total, requests)
106. Proper HTTP client cleanup on shutdown
107. Shutdown summary logging

#### New Files
108. `.dockerignore` — excludes .git, __pycache__, tests, data, IDE files
109. `requirements-dev.txt` — added httpx, anyio as explicit dev dependencies

---

### Commit 5 — Documentation & Final Polish

**Documentation overhaul.**

#### README.md (complete rewrite)
110. Table of contents with anchor links
111. Architecture diagram (ASCII)
112. Full API reference (proxy + admin + system endpoints)
113. Complete configuration reference table (18 env vars)
114. Monitoring & observability section (Prometheus, audit log, request tracing)
115. Docker & deployment section (multi-stage builds, security hardening)
116. Security best practices section
117. Testing section with category breakdown table
118. Troubleshooting guide with common issues and solutions
119. Load Producer quick-start integration guide

#### CHANGELOG.md (new)
120. Full changelog documenting all 5 commits
121. Numbered list of all 125+ improvements
122. Categorized by commit and feature area

#### ANFORDERUNGSLISTE.md
123. Updated test count from 9 to 35
124. Added new environment variables (LB_CORS_ORIGINS, LB_CACHE_*, LB_ADMIN_RATE_LIMIT_RPM, LB_MAX_REQUEST_BODY_BYTES, LB_DRAIN_TIMEOUT_SECONDS)
125. Added new NFRs for caching, rate limiting, Prometheus metrics, graceful shutdown

---

## [2.0.0] — Visual Flow Editor

- Endpoint model replacing pool/target model
- Visual Flow Editor (n8n-style) with SVG Bezier wires
- Percentage and Priority routing modes
- Token throughput tracking
- SQLite persistence
- 15 automated tests

## [1.0.0] — Initial Release

- Basic load tester with priority and advanced stats
- Benchmark presets and saved profiles
