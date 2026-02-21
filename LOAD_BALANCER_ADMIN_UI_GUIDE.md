# Load Balancer Admin UI Guide

This guide documents the admin interface at `http://localhost:8090/admin`, with focus on the new `?` help entries, routing controls, and token/incoming organization.

## UI Design

- Monochrome base (black/white) with structured semantic accents for state.
- No icon-only controls: critical actions are text-labeled.
- Every major admin section includes a `?` button with detailed contextual guidance.

## Header Controls

### Admin Token

- Stored in browser local storage (`lb_admin_token`).
- Sent as `X-Admin-Token` for all `/admin/api/*` calls.
- Required for all admin mutations and reads.

### Routing

- `Percentage`: distributes traffic by endpoint quota percentage.
- `Priority`: uses endpoint priority order and optional prioritized token input.

### Prioritized Input

- Only visible in `Priority` mode.
- Select a token that should receive preferential handling.
- Select `(kein priorisierter Eingang)` to disable prioritization.

### Refresh / Stats Reset / Theme

- `Aktualisieren`: pulls full state from `/admin/api/state`.
- `Stats Reset`: resets throughput and request counters globally.
- `Theme`: toggles dark/light monochrome theme.

## Sidebar Sections

### Info (`?`)

Explains node operations:

- Select endpoint to edit details.
- Drag nodes to reposition.
- Draw wire from incoming output port to endpoint input port.

### Endpoint hinzufuegen (`?`)

Key fields:

- `Name`: logical display name.
- `Base URL`: upstream OpenAI-compatible endpoint.
- `API Key`: optional upstream key.
- `Quota %`: used in percentage mode.
- `Prioritaet`: used in priority mode.
- `Max Parallel`: endpoint concurrency limit.
- `Timeout`: per-endpoint timeout seconds.
- `TLS pruefen`: cert validation for HTTPS.
- `Sofort verbinden`: connect directly after create.

### Incoming-Bloecke (`?`)

- Incoming blocks group client tokens.
- Token assignment is block-based.
- Deleting a block reassigns tokens to a remaining block.
- Incoming block dropdowns are alphabetically sorted.

### Client-Zugang (`?`)

- Manage client tokens used for proxy access.
- Configure token-specific routing overrides:
  - incoming block
  - preferred endpoint
  - request priority
  - minimum traffic percent
- Token and endpoint dropdowns are alphabetically sorted.

## Endpoint Detail Panel

- Shows mutable endpoint config and runtime counters.
- API key is write-only: leaving key field empty keeps old key.
- Save applies PATCH, Delete removes endpoint permanently.

## Keyboard Shortcuts

- `R`: refresh state
- `T`: toggle theme
- `?`: open shortcut overlay
- `Esc`: close overlay/help modal or clear endpoint selection
- `/`: focus token search

## Sorting Rules

The UI enforces deterministic ordering for easier operations:

- Prioritized input token select: sorted by token name.
- Incoming block lists/selects: sorted by block name.
- Token cards: sorted by token name.
- Token routing endpoint select: sorted by endpoint name.

## Troubleshooting

### Cannot load state

- Verify admin token in header.
- Check `/health` and browser network requests to `/admin/api/state`.

### Token copy/save actions not updating

- Ensure browser allows clipboard access.
- Verify token still exists and was not deleted by another admin session.

### Routing changes have no effect

- Confirm endpoint is both enabled and connected.
- In priority mode, ensure priority orders are configured correctly.
