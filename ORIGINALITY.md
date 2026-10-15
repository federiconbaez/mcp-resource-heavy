# ORIGINALITY.md - provenance of mcp-resource-heavy (and what the evidence cannot prove)

## What was done

* Written by an AI assistant (Grok Bot) for Federico N. Báez Rizzo. The repository starts from a **copy of the user's own `mcp-forge`**
  (commit "Import mcp-forge base", i.e. `mcp-base`) and adds the resource-centric design on top.
* **Adapted from mcp-forge (stated plainly):** JSON-RPC engine (`mcpres/jsonrpc.py`), schema validator (`mcpres/schema.py`), audit log
  (`mcpres/audit.py`), contracts registry (`mcpres/contracts.py`), fuzzer (`mcpres/fuzz.py`), LLM hook (`mcpres/llm.py`), the SQLite memory
  store (`mcpres/memory.py`, unchanged but for the package name), HS256 authenticator (`mcpres/auth.py`), a large part of
  `mcpres/server.py` / `mcpres/transport_http.py` / `mcpres/transport_stdio.py` (extended with subscriptions, SSE, notifications), the
  CLI skeleton and the conformance *harness* with its ~120 protocol checks. Per-file overlap is measured below (`tools/adaptation_report.py`).
* **New in this repo (not in mcp-forge):** `mcpres/datasets.py` (all data hand-typed by the author: countries, elements, units, glossary,
  PNG generator), `mcpres/resources.py` (resource space), `mcpres/paging.py`, `mcpres/hub.py` (subscription hub + outbox), `mcpres/userdata.py`,
  `mcpres/prompts.py` (prompt book + completer), `mcpres/modelcheck.py`, most of `mcpres/catalog.py` (dataset tools), the SSE and
  subscription parts of the server and transports, conformance checks `PAG-`, `RSX-`, `SUB-`, `CMP-`, `PRM-004/5`, `TLS-008`, and the tests
  `test_datasets`, `test_paging`, `test_resources`, `test_prompts_completion`, `test_subscriptions`.
* **Data provenance:** facts such as element symbols/atomic numbers, ISO country codes, capitals and unit definitions are public
  knowledge typed from the author's memory; numbers are rounded and may be off (`static://dataset-notice` says so). Glossary
  sentences are original. No dataset was downloaded or copied from a file.
* No code was copied from any SDK/library, no package was installed, no web search/fetch was performed.
* Dependencies: **zero** (the PNG is produced with `zlib`/`struct`).

## What the evidence below can and cannot show

| Evidence | Shows | Does NOT show |
|---|---|---|
| `tools/check_stdlib_only.py` (AST) | no third-party import, no `exec/eval/__import__/importlib`, no dependency manifest or vendored dir | that the code is dissimilar to anyone else's |
| SDK-identifier scan | no `modelcontextprotocol`/`fastmcp`/`from mcp` in source | the same |
| Line-overlap report vs mcp-base | how much is inherited from the user's own earlier project | anything about third parties |
| git history | incremental construction | immutability (unsigned history) |
| Source sha256 manifest + `MANIFEST.sha256` | byte-identity with what was verified | authorship |
| Mutation check | tests catch 27 injected bugs | that they catch all |

**The evidence cannot prove dissimilarity to code you have not shown me.** Only a third-party similarity tool against the corpus you
care about can say something about that; none was run, and no negative result is claimed. The claim is limited to *provenance*.

## Disclosure

Produced by an AI assistant. "Originality" means provenance and absence of third-party code, not novelty of MCP resource
semantics, pagination, SSE or pub/sub patterns, which are standard.

## Mechanical evidence (live at generation time)

<!-- GENERATED:BEGIN -->
<!-- GENERATED:END -->
