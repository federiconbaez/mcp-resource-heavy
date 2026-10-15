# SECURITY.md - mcp-resource-heavy

Inherits the threat model and controls of `mcp-base` (local/lab server, simplified HS256 bearer tokens, strict schemas, size
limits, loopback-only HTTP with Origin/Host checks, hash-chained audit log). What the resource-centric design adds:

## Added controls

* **Scope-filtered resource space:** `data:read` for datasets, `memory:read` for `memory://`, `audit:read` for `audit://chain`; listings and
  templates only show what the token may read; reads outside scope -> `-32003`. Mutating tools need `data:write` / `memory:write`.
* **Subscriptions are bounded and validated:** <= 100 per session (`-32602` beyond that), URIs must exist (`-32002`) and be canonical
  (`-32602` with a hint, so aliases cannot create duplicate state); notifications are coalesced per (method, URI), so a session's queue
  is bounded; nothing is delivered before `notifications/initialized`; sessions only receive what they subscribed to (`SUB-1`).
* **SSE:** requires a valid bearer token and a known session; the session is bound to the token subject (other subject -> 403); one stream
  per session (409); keep-alive comments; the stream ends on DELETE/eviction/expiry; server threads are daemonised and connection resets are swallowed.
* **Cursors are opaque, typed and validated:** wrong list kind, wrong alphabet, wrong shape, over 1 KiB -> `-32602`; they contain only a
  list kind and the last key (no secrets, no offsets into user data).
* **User glossary terms are data, not instructions:** they are never embedded into prompts (`tests/test_prompts_completion.py::test_embedded_user_terms_in_quiz_are_never_included`).
* **Argument validation for prompts and completion:** unknown/extra/missing arguments -> `-32602` with an `errors` list; completion capped at 100 values.

## Known weaknesses / not done

* **Resource exhaustion:** the `glossary_add` / `memory_store` / `counter_bump` tools have size limits but no per-subject quotas; a holder of
  `data:write` can grow `userdata` and trigger `list_changed` storms (coalescing bounds each session's queue, not the CPU spent on publishing).
* **SSE hygiene:** one thread per open stream; no connection cap besides the 256-session limit inherited from mcp-base; no resumability.
* **Information exposure:** `dyn://clock`, the counter, and `audit://chain` (with `audit:read`) reveal server state by design.
  The `memory://` resources expose stored text to any token with `memory:read`.
* **Datasets are not authoritative** and are not security-relevant data; they must not be treated as facts.
* HS256 shared secret in `oidc_secret`; no rotation (see `mcp-auth-hardened`); no TLS; Linux/CPython 3.13 only; no third-party review.
