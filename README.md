# mcp-resource-heavy

> **ES:** Servidor MCP centrado en resources y prompts en Python 3 stdlib: resources estáticos, plantillas, dinámicos y binarios; suscripciones y list_changed por stdio y SSE; paginación por cursor; prompts con resources embebidos y completion/complete.
> Probar: `python3 -m unittest discover -s tests -t .`
>
> **EN:** MCP server built around resources and prompts in Python 3 stdlib: static, templated, dynamic and binary resources; subscriptions and list_changed over stdio and SSE; cursor pagination; prompts with embedded resources and completion/complete.
> Test: `python3 -m unittest discover -s tests -t .`
>
> **Estado / Status:** 205 tests en verde en la box (Python 3.13.5, Linux 6.12, x86_64) el 2026-10-22; **no auditado** por un tercero / **not independently audited**. Implementación didáctica, no un IdP de producción / teaching implementation, not a production IdP. Licencia / License: Apache-2.0.


An **MCP server built around resources and prompts**, pure Python 3, standard library only: static, templated, dynamic and
binary resources over self-authored datasets; **subscriptions and `list_changed` notifications on stdio and on HTTP SSE** (with
capabilities that are advertised only because they really work); keyset **cursor pagination**; multi-message **prompts with
embedded resources and argument validation**; **`completion/complete`**; and dataset query tools. Sibling of `mcp-base`,
`mcp-memory-first`, `mcp-auth-hardened` and `mcp-crosscheck` in the *mcp-family*.

> Honest scope: a reference implementation plus a self-consistent verification kit; not certified; CPython 3.13.5 on Linux only.
> The datasets are **hand-authored, approximate and illustrative** (see `static://dataset-notice`).

## What is different from mcp-base

| Feature | Behaviour | Code |
|---|---|---|
| **Static resources** | `static://about`, `static://dataset-notice`, `static://schema/country` (stable bytes) | `mcpres/resources.py` |
| **Templated resources** (7 templates) | `country://{iso2}`, `element://{symbol-or-Z}`, `unit://{category}`, `glossary://term/{term}`, `memory://entry/{id}`, `memory://tag/{tag}`, `data://{countries\|elements\|units\|glossary}` | `mcpres/resources.py` |
| **Dynamic resources** | `dyn://counter` (persistent, changes with `counter_bump`), `dyn://clock` (differs per read), `glossary://user` (user terms), `glossary://all`, `memory://index`, `audit://chain` | `mcpres/resources.py`, `mcpres/userdata.py` |
| **Binary resources** | `blob://sample.png` (valid PNG, built with `zlib`/`struct`), `blob://random.bin` (deterministic); `blob` base64 with listed size and mime; digest verifiable through the `resource_digest` tool | `mcpres/datasets.py`, `mcpres/resources.py` |
| **Datasets** (original, approximate) | 45 countries, 36 elements, 9 unit categories (convert with exact definitions), 40-term glossary | `mcpres/datasets.py` |
| **Pagination** | all four list methods paginate (default page 25, `--page-size`); cursor = opaque base64url JSON `{"v":1,"k":<list kind>,"a":<last key>}`; keyset, stable under inserts/deletes between pages; a cursor from another list or garbage -> `-32602` | `mcpres/paging.py`, `mcpres/server.py` |
| **Subscriptions** | `resources/subscribe` / `resources/unsubscribe`; `notifications/resources/updated` only for subscribers (coalesced per URI); `notifications/resources/list_changed` to everyone; max 100 subscriptions per session; nothing is sent before `notifications/initialized` | `mcpres/hub.py`, `mcpres/server.py` |
| **stdio push** | the notification is written right after the response that caused it | `mcpres/transport_stdio.py` |
| **HTTP SSE** | `GET /mcp` with `Accept: text/event-stream` and `Mcp-Session-Id`: 406 / 400 / 404 / 403 (other subject) / 409 (second stream); 15 s keep-alive comments; ends on `DELETE`, eviction or expiry. POST replies stay plain JSON (no resumability, no `Last-Event-ID`) | `mcpres/transport_http.py` |
| **Honest capabilities** | `resources: {subscribe: true, listChanged: true}`, `completions: {}` (not advertised for 2024-11-05), tools/prompts `listChanged: false` because they never change; contract `MCP-4` | `mcpres/server.py` |
| **Prompts** (7) | `country_briefing`, `element_study_guide`, `unit_conversion_tutor`, `glossary_quiz`, `dataset_overview`, `recall_and_summarize`, `memory_digest`; multi-message, embedded `resource` parts (text and an image), argument validation with an `errors` list | `mcpres/prompts.py` |
| **Completion** | `completion/complete` for prompt arguments (with `context.arguments`, comma lists) and resource templates; <= 100 values | `mcpres/prompts.py` (`Completer`) |
| **Tools** (16) | queries (`data:read`): `country_lookup`, `country_search`, `element_lookup`, `element_search`, `unit_convert`, `unit_list`, `glossary_lookup`, `glossary_search`, `resource_digest`; mutating (`data:write`): `glossary_add`, `glossary_remove`, `counter_bump`; memory: `memory_store`, `memory_recall`, `memory_list`, `memory_forget`; results carry `resource_link` content only on 2025-06-18 | `mcpres/catalog.py` |
| **Formal-verification hooks** | contracts `SUB-1/2/3/5`, `MCP-4`, `PAGE-1`; bounded model check of the subscription state machine (`mcpres/modelcheck.py`, `tools/model_check.py`); hash-chained audit log | see [AUDIT.md](AUDIT.md) |

Scopes: `mcp`, `data:read`, `data:write`, `memory:read`, `memory:write`, `audit:read` (listings are scope-filtered).
Default audience: `mcp-resource-heavy`. Clients: `demo-client` (all), `readonly-client` (`mcp data:read memory:read`).

## Quickstart

```bash
./demo.sh                            # 29-step demo (pagination, 4 resource kinds, completion, prompts, stdio + SSE notifications, restart)
python3 -m unittest discover         # 205 tests
python3 -m mcpres conformance        # 154 black-box checks (stdio + http); PAG-, RSX-, SUB-, CMP-, PRM-004/5, TLS-008 are new
python3 tools/model_check.py --depth 6   # bounded model check (~5.2M nodes, a few minutes)
python3 tools/check_stdlib_only.py   # AST proof: stdlib only
```

```bash
export MCP_FORGE_HOME=$PWD/.mcpres
export MCP_TOKEN=$(python3 -m mcpres mint-token)
python3 -m mcpres serve --page-size 10                      # stdio
python3 -m mcpres serve --transport http --port 8768        # HTTP + SSE (token: mint-token --issuer http://127.0.0.1:8768)
curl -N -H "Authorization: Bearer $TOKEN" -H 'Accept: text/event-stream' -H "Mcp-Session-Id: $SID" http://127.0.0.1:8768/mcp
```

## Limitations

* SSE has **no resumability** (`Last-Event-ID` is ignored); notifications missed while no stream is open are queued (coalesced) and
  delivered on the next `GET`, but the queue is in memory (bounded by coalescing: at most one pending message per (method, URI), i.e. <= 101); a restart loses it. POST responses never use SSE.
* One SSE stream per session (a second one gets 409).
* `list_changed` is emitted only for resources (memory entries, glossary terms); the tool and prompt lists are static by design.
* Datasets are small, hand-typed, **approximate** (countries rounded, circa 2023); never use them as facts. Unit conversions
  use exact SI definitions where they exist.
* Subscription model check is bounded (2 sessions x 2 URIs, 13 operations, depth 4 in the test-suite, depth 6 for the evidence).
* `completion/complete` is not *advertised* for protocol 2024-11-05 (which predates it) but is still answered if a client calls it (lenient, documented).
* Tests, suite and server share one author; no third-party MCP client has been run against the SSE/subscription code.

## Resumen en español

`mcp-resource-heavy` es un servidor MCP en Python puro (solo biblioteca estándar) centrado en recursos: recursos estáticos, con
plantillas, dinámicos y binarios sobre conjuntos de datos escritos a mano (aproximados e ilustrativos); suscripciones y
`list_changed` por stdio y por SSE sobre HTTP (las capacidades anunciadas son reales); paginación por cursor; *prompts* de varios
mensajes con recursos incrustados y validación de argumentos; `completion/complete`; y herramientas de consulta. Verificación:
`./demo.sh`, `python3 -m unittest discover`, `python3 -m mcpres conformance`, `python3 tools/model_check.py --depth 6`. Pasos para
un auditor externo en `AUDIT.md`; evidencia de originalidad (y sus límites) en `ORIGINALITY.md`. No es una implementación certificada.
