# AUDIT.md - how a third party verifies mcp-resource-heavy, step by step

Offline, with only `python3` (>= 3.10; built on 3.13.5) and optionally `git`, `sha256sum`, `curl`.

**What this kit can and cannot show.** It shows that the server behaves as the cited MCP clauses and the design below say *as the
author reads them*, that automated tests, a black-box suite and a bounded model check exercise the behaviour, and that the tests are
not vacuous (mutation check). It is **not** independent certification; no third-party MCP client has been run against the
subscription/SSE code. The strongest extra check is to do exactly that (Step 7).

## Step 0 - integrity

```bash
sha256sum -c mcp-resource-heavy.tar.gz.sha256
tar xzf mcp-resource-heavy.tar.gz && cd mcp-resource-heavy
python3 tools/make_manifest.py --check          # == sha256sum -c MANIFEST.sha256
python3 tools/check_doc_refs.py                 # every file/test/check named in the docs exists
```

## Step 1 - stdlib-only / originality

```bash
python3 tools/check_stdlib_only.py              # AST only; PASS/FAIL; line counts; git history
```

## Step 2 - unit tests (205) and conformance (154 checks across stdio + http)

```bash
python3 -m unittest discover -v
python3 -m mcpres conformance --json /tmp/conf.json
```

## Step 3 - pagination by hand

```bash
export MCP_FORGE_HOME=$(mktemp -d); export MCP_TOKEN=$(python3 -m mcpres mint-token)
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"1"}}}'
printf '%s\n' "$INIT" '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"resources/list"}' | python3 -m mcpres serve --page-size 5 2>/dev/null | tail -1 | python3 -m json.tool | grep -E '"uri"|nextCursor'
# take the nextCursor value and send {"method":"resources/list","params":{"cursor":"<value>"}}; a cursor with a character changed -> -32602
```

## Step 4 - subscriptions over stdio (the notification follows the response)

```bash
printf '%s\n' "$INIT" '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"resources/subscribe","params":{"uri":"dyn://counter"}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"counter_bump","arguments":{"by":1}}}' \
  | python3 -m mcpres serve 2>/dev/null | tail -3
# expected: result id 3, then {"method":"notifications/resources/updated","params":{"uri":"dyn://counter"}}
```

## Step 5 - SSE over HTTP

```bash
python3 -m mcpres serve --transport http --port 8768 --issuer http://127.0.0.1:8768 &
TOKEN=$(python3 -m mcpres mint-token --issuer http://127.0.0.1:8768)
curl -si -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d "$INIT" http://127.0.0.1:8768/mcp | grep -i 'mcp-session-id'            # -> SID
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8768/mcp                     # 406 (no Accept)
curl -N -H "Authorization: Bearer $TOKEN" -H 'Accept: text/event-stream' -H "Mcp-Session-Id: $SID" http://127.0.0.1:8768/mcp &   # stream
# POST initialized, resources/subscribe dyn://counter, tools/call counter_bump with the same Mcp-Session-Id -> "event: message" with ...updated
curl -X DELETE -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SID" http://127.0.0.1:8768/mcp                     # the stream ends
kill %1
```

(`demo.py` section 7 performs exactly this with raw sockets; `tests/test_subscriptions.py::HttpSse` automates it.)

## Step 6 - model check, mutation check, evidence

```bash
python3 tools/model_check.py --depth 6   # 2 sessions x 2 URIs, 13 operations: ~5.2M nodes, a few minutes; expect violations=0
python3 tools/mutation_sanity.py         # 27 injected bugs; every one must be KILLED
tools/build_evidence.sh                  # regenerate evidence/
```

## Step 7 - independent client (recommended; not done by the author)

Point any MCP client that supports `resources/subscribe` over stdio, or an SSE client, at the server and compare with the map below.

## Formal-verification hooks (honest strength)

| Hook | Where | Strength |
|---|---|---|
| Runtime contracts `SUB-1` (notify only subscribers), `SUB-2`, `SUB-3` (no duplicate pending), `SUB-5` (bounded subscriptions), `MCP-4` (capabilities honest), `PAGE-1` (pages sorted, unique, bounded) | `mcpres/hub.py`, `mcpres/server.py`, `mcpres/paging.py` | assertions on real traffic; `ContractViolation` inside the Hub is also caught by the model checker as a second oracle |
| Bounded model check of the subscription/notification state machine | `mcpres/modelcheck.py`, `tools/model_check.py`, `evidence/model_check.txt` | exhaustive **up to the depth given** (4 in unit tests, 6 in evidence) |
| Hash-chained audit log | `mcpres/audit.py` | tamper-evident |
| Evidence generators | `tools/build_evidence.sh`, `tools/gen_originality.py` | reproducible logs |

## Requirement -> evidence map

| Requirement | Implementation | Tests / checks |
|---|---|---|
| Static resources | `mcpres/resources.py` | `tests/test_resources.py::test_static`, `check:RSX-001` |
| Templated resources | `mcpres/resources.py` | `tests/test_resources.py::test_templated`, `tests/test_resources.py::test_templates_cover_the_documented_kinds`, `check:RSX-002` |
| Dynamic resources | `mcpres/resources.py`, `mcpres/userdata.py` | `tests/test_resources.py::test_dynamic_counter_changes_and_persists`, `tests/test_resources.py::test_dynamic_clock_differs_between_reads`, `tests/test_resources.py::test_user_glossary_is_dynamic_resource`, `check:RSX-003` |
| Binary resources | `mcpres/datasets.py`, `mcpres/resources.py` | `tests/test_resources.py::test_binary_blob_roundtrip_matches_independent_digest`, `tests/test_datasets.py::test_png_is_structurally_valid`, `check:RSX-004` |
| Every listed resource readable | `mcpres/resources.py` | `tests/test_resources.py::test_every_listed_resource_is_readable`, `check:RSX-005` |
| Self-authored datasets | `mcpres/datasets.py` | `tests/test_datasets.py::test_countries`, `tests/test_datasets.py::test_elements`, `tests/test_datasets.py::test_glossary`, `tests/test_datasets.py::test_units_round_trip_and_known_values` |
| Cursor pagination | `mcpres/paging.py` | `tests/test_paging.py::test_walk_is_complete_and_duplicate_free`, `tests/test_paging.py::test_keyset_stable_under_insert_and_delete_between_pages`, `tests/test_paging.py::test_cursor_from_another_list_or_garbage_is_invalid_params`, `check:PAG-001`, `check:PAG-002`, `check:PAG-003` |
| Subscriptions (Hub semantics) | `mcpres/hub.py` | `tests/test_subscriptions.py::test_basic_delivery_and_coalescing`, `tests/test_subscriptions.py::test_unsubscribe_drops_pending`, `tests/test_subscriptions.py::test_subscription_bound`, `check:SUB-002` |
| `list_changed` | `mcpres/server.py`, `mcpres/hub.py` | `tests/test_subscriptions.py::test_list_changed_goes_to_everyone_once`, `tests/test_subscriptions.py::test_glossary_replace_updates_without_list_change_and_remove_changes_list`, `check:SUB-004` |
| Capabilities honest | `mcpres/server.py` | `tests/test_subscriptions.py::test_capabilities_are_honest`, `check:SUB-001` |
| Nothing pushed before `initialized` | `mcpres/server.py` | `tests/test_subscriptions.py::test_nothing_is_sent_before_initialized_notification` |
| stdio notifications | `mcpres/transport_stdio.py` | `tests/test_subscriptions.py::test_notifications_follow_the_response_on_stdout`, `check:SUB-006` |
| HTTP SSE | `mcpres/transport_http.py` | `tests/test_subscriptions.py::test_sse_delivers_updated_and_list_changed`, `tests/test_subscriptions.py::test_stream_rules`, `tests/test_subscriptions.py::test_delete_session_ends_stream_and_clears_subscriptions`, `tests/test_subscriptions.py::test_keepalive_comment_is_sent`, `check:HTP-001`, `check:SUB-005` |
| Subscribe validation (-32002 / -32602) | `mcpres/server.py` | `tests/test_subscriptions.py::test_subscribe_errors`, `tests/test_subscriptions.py::test_subscription_limit_is_32602`, `check:SUB-003` |
| Multi-message prompts with embedded resources | `mcpres/prompts.py` | `tests/test_prompts_completion.py::test_country_briefing_is_multi_message_with_embedded_resources`, `tests/test_prompts_completion.py::test_overview_contains_text_resources_and_an_image`, `check:PRM-004` |
| Prompt argument validation | `mcpres/prompts.py` | `tests/test_prompts_completion.py::test_country_arg_forms_and_validation`, `tests/test_prompts_completion.py::test_unknown_prompt`, `check:PRM-005` |
| `completion/complete` | `mcpres/prompts.py` | `tests/test_prompts_completion.py::test_prompt_argument_completion`, `tests/test_prompts_completion.py::test_template_completion`, `tests/test_prompts_completion.py::test_invalid_requests_are_32602`, `check:CMP-001`, `check:CMP-002` |
| Dataset query tools | `mcpres/catalog.py` | `tests/test_resources.py::test_country_tools`, `tests/test_resources.py::test_unit_tools`, `tests/test_resources.py::test_glossary_tools`, `check:TLS-008` |
| `resource_link` only on 2025-06-18 | `mcpres/catalog.py`, `mcpres/server.py` | `tests/test_resources.py::test_resource_links_only_in_2025_06_18` |
| Scopes (data/memory/audit) | `mcpres/server.py`, `mcpres/auth.py` | `tests/test_resources.py::test_data_scope_gates_dataset_resources_and_tools`, `tests/test_resources.py::test_write_scope_needed_to_mutate`, `tests/test_resources.py::test_audit_resource_needs_audit_scope` |
| Model check detects injected bugs | `mcpres/modelcheck.py` | `tests/test_subscriptions.py::test_injected_bug_missing_coalescing_is_caught`, `tests/test_subscriptions.py::test_injected_bug_unsubscribe_keeps_pending_is_caught`, `tests/test_subscriptions.py::test_injected_bug_detach_leaves_subscriptions_is_caught`, `tests/test_subscriptions.py::test_depth4_exhaustive_passes` |
| JSON-RPC / lifecycle / auth / HTTP | `mcpres/jsonrpc.py`, `mcpres/server.py`, `mcpres/auth.py`, `mcpres/transport_http.py` | `tests/test_jsonrpc.py`, `tests/test_protocol.py`, `tests/test_auth.py`, `tests/test_http_e2e.py`, `check:RPC-001`, `check:AUT-007` |
| stdlib only | `tools/check_stdlib_only.py` | `tests/test_llm_and_tools.py::test_stdlib_only_checker_catches_violations` |
