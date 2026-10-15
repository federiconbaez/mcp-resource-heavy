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
### AST stdlib-only check (live run, exit code 0)

```
STDLIB-ONLY CHECK (ast-based; nothing was imported or executed)
python 3.13.5, 290 stdlib modules known to this interpreter

file                                 code  total  imports (stdlib/local)
demo.py                               295    352  base64,hashlib,http,json,os,shutil,signal,socket,subprocess,sys,tempfile,threading,time,urllib
mcpres/__init__.py                      6     11  
mcpres/__main__.py                      3      5  sys  | local: .cli
mcpres/audit.py                       132    153  fcntl,hashlib,json,os,threading,time
mcpres/auth.py                        222    264  base64,binascii,hashlib,hmac,json,math,os,re,secrets,time
mcpres/catalog.py                     244    279  base64,hashlib,json,sqlite3,urllib  | local: .,.llm,.memory,.prompts,.resources,.userdata
mcpres/cli.py                         142    174  argparse,json,os,signal,sys,threading  | local: .,.audit,.auth,.conformance,.llm,.server,.transport_http,.transport_stdio
mcpres/conformance.py                1064   1344  base64,hashlib,http,json,os,queue,shutil,socket,subprocess,sys,tempfile,threading,time,urllib  | local: .,.audit
mcpres/contracts.py                    73    100  functools,os,threading
mcpres/datasets.py                    201    238  hashlib,struct,zlib
mcpres/fuzz.py                        164    193  json,random
mcpres/hub.py                         130    168  collections,threading  | local: .
mcpres/jsonrpc.py                     208    255  collections,json  | local: .
mcpres/llm.py                          44     52  json,os,urllib
mcpres/memory.py                      127    152  contextlib,json,os,re,sqlite3,time,unicodedata
mcpres/modelcheck.py                  151    175  copy  | local: .contracts,.hub
mcpres/paging.py                       47     59  base64,binascii,json
mcpres/prompts.py                     277    321  base64,json,random  | local: .,.memory,.resources,.userdata
mcpres/resources.py                   185    207  base64,datetime,json,re,time,urllib  | local: .,.userdata
mcpres/schema.py                      204    241  re
mcpres/server.py                      392    445  json,os,secrets,threading,time,urllib  | local: .,.audit,.auth,.catalog,.hub,.jsonrpc,.memory,.paging,.prompts,.resources,.userdata
mcpres/transport_http.py              310    354  http,json,socket,sys,threading,time,urllib  | local: .,.auth,.jsonrpc
mcpres/transport_stdio.py              45     54  sys  | local: .jsonrpc
mcpres/userdata.py                     80     99  contextlib,os,sqlite3,threading,time  | local: .
tests/__init__.py                       0      0  
tests/helpers.py                       90    122  json,os,shutil,subprocess,sys,tempfile,threading,unittest  | local: mcpres.auth,mcpres.server,mcpres.transport_http
tests/test_auth.py                    154    192  base64,inspect,json,os,tempfile,unittest  | local: mcpres,mcpres.auth,tests
tests/test_datasets.py                 95    109  base64,struct,unittest,zlib  | local: mcpres
tests/test_fuzz.py                     99    112  json,random,unittest  | local: mcpres,mcpres.auth,tests,tests.helpers
tests/test_http_e2e.py                218    249  base64,http,json,os,subprocess,sys,unittest,urllib  | local: mcpres.auth,tests,tests.helpers
tests/test_jsonrpc.py                 129    158  json,unittest  | local: mcpres,mcpres.jsonrpc,tests
tests/test_llm_and_tools.py           123    142  http,json,os,shutil,subprocess,sys,tempfile,threading,unittest  | local: mcpres.llm,tests,tests.helpers
tests/test_paging.py                  109    127  json,unittest  | local: mcpres,tests.helpers
tests/test_persistence_audit.py       180    208  json,os,threading,unittest  | local: mcpres.audit,mcpres.memory,tests,tests.helpers
tests/test_prompts_completion.py      159    187  base64,json,unittest  | local: mcpres,tests.helpers
tests/test_protocol.py                286    330  itertools,json,os,unittest  | local: mcpres.auth,tests,tests.helpers
tests/test_resources.py               213    246  base64,hashlib,json,unittest  | local: mcpres,tests.helpers
tests/test_schema.py                   87    102  tempfile,unittest  | local: mcpres,mcpres.catalog,mcpres.memory,mcpres.prompts,mcpres.resources,mcpres.userdata,tests
tests/test_stdio_e2e.py                72     84  json,unittest  | local: tests,tests.helpers
tests/test_subscriptions.py           410    461  http,json,threading,time,unittest  | local: mcpres,mcpres.hub,mcpres.transport_http,tests,tests.helpers
tools/adaptation_report.py             47     58  difflib,os,sys
tools/check_doc_refs.py                51     59  glob,os,re,sys
tools/check_stdlib_only.py            142    161  argparse,ast,json,os,subprocess,sys
tools/gen_originality.py               72     88  hashlib,os,re,subprocess,sys
tools/make_manifest.py                 60     73  argparse,hashlib,os,sys
tools/model_check.py                   28     34  argparse,json,os,sys,time  | local: mcpres
tools/mutation_sanity.py               78     91  argparse,concurrent,os,shutil,subprocess,sys,tempfile

files: 47   lines: total=9088 code=7648 comment=107 blank=1333
stdlib modules used (39): argparse, ast, base64, binascii, collections, concurrent, contextlib, copy, datetime, difflib, fcntl, functools, glob, hashlib, hmac, http, inspect, itertools, json, math, os, queue, random, re, secrets, shutil, signal, socket, sqlite3, struct, subprocess, sys, tempfile, threading, time, unicodedata, unittest, urllib, zlib

GIT HISTORY (oldest first):
cf481db|Grok Bot (mcp-family build)|2026-10-15T02:47:10Z|Import mcp-forge base (adapted: package renamed, project-specific audience/paths); variant work follows

 34 files changed, 5745 insertions(+)
dacac24|Grok Bot (mcp-family build)|2026-10-15T03:28:57Z|Resource-heavy: datasets, resource space (static/templated/dynamic/binary), keyset pagination, subscription hub, SSE + stdio notifications, completion, rich prompts, dataset tools, model check, tests, conformance

 27 files changed, 3295 insertions(+), 249 deletions(-)
c5ea90f|Grok Bot (mcp-family build)|2026-10-15T03:36:04Z|Resource-heavy: end-to-end demo (29 steps)

 1 file changed, 239 insertions(+), 129 deletions(-)
a21a951|Grok Bot (mcp-family build)|2026-10-15T03:49:40Z|Resource-heavy: README/AUDIT/ORIGINALITY/SECURITY, mutation suite (27 mutants), evidence tooling

 11 files changed, 500 insertions(+), 16 deletions(-)

RESULT: PASS - zero third-party imports, no dynamic imports/exec/eval, no dependency manifests
```

### Lines inherited from mcp-base / mcp-forge, per file (`evidence/adaptation_report.txt`)

```
file                                lines  inherited      %
mcpres/__init__.py                      8          7    88%
mcpres/__main__.py                      3          3   100%
mcpres/audit.py                       132        132   100%
mcpres/auth.py                        227        222    98%
mcpres/catalog.py                     246         66    27%
mcpres/cli.py                         142        137    96%
mcpres/conformance.py                1101        791    72%
mcpres/contracts.py                    73         73   100%
mcpres/datasets.py                    209          0     0%
mcpres/fuzz.py                        166        166   100%
mcpres/hub.py                         134          0     0%
mcpres/jsonrpc.py                     211        211   100%
mcpres/llm.py                          44         44   100%
mcpres/memory.py                      129        129   100%
mcpres/modelcheck.py                  151          0     0%
mcpres/paging.py                       48          0     0%
mcpres/prompts.py                     281          0     0%
mcpres/resources.py                   189          0     0%
mcpres/schema.py                      205        205   100%
mcpres/server.py                      399        225    56%
mcpres/transport_http.py              318        236    74%
mcpres/transport_stdio.py              45         32    71%
mcpres/userdata.py                     82          0     0%
TOTAL package code                   4543       2679    59%
```

### Git history (4 commits at generation time; later commits only touch docs/evidence)

```
cf481db  Grok Bot (mcp-family build)  2026-10-15T02:47:10Z  Import mcp-forge base (adapted: package renamed, project-specific audience/paths); variant work follows
dacac24  Grok Bot (mcp-family build)  2026-10-15T03:28:57Z  Resource-heavy: datasets, resource space (static/templated/dynamic/binary), keyset pagination, subscription hub, SSE + stdio notifications, completion, rich prompts, dataset tools, model check, tests, conformance
c5ea90f  Grok Bot (mcp-family build)  2026-10-15T03:36:04Z  Resource-heavy: end-to-end demo (29 steps)
a21a951  Grok Bot (mcp-family build)  2026-10-15T03:49:40Z  Resource-heavy: README/AUDIT/ORIGINALITY/SECURITY, mutation suite (27 mutants), evidence tooling
```

### SDK-identifier scan of source files (`modelcontextprotocol|fastmcp|from mcp|import mcp`)

```
tools/adaptation_report.py:2: """Per-file report: how many lines of each variant file are inherited (unchanged) from mcp-base (= t
tools/gen_originality.py:51: parts.append("### Lines inherited from mcp-base / mcp-forge, per file (`evidence/adaptation_report.t
tools/gen_originality.py:55: pat = re.compile(r"modelcontextprotocol|fastmcp|from mcp\b|import mcp\b", re.I)
tools/gen_originality.py:61: parts.append("### SDK-identifier scan of source files (`modelcontextprotocol|fastmcp|from mcp|import
```

### Source sha256 manifest (49 files, 9121 lines incl. blanks/comments/tests)

```
5e1798b10814c09350a9645cdc4d19672548aa54d6301317e0c86de340cb8667  demo.py
c88fbd3f827ef1c7ccedfc9841f7a376f5c3c469a4427716cbb2e18006b2efe9  demo.sh
076572a068b0c2748a0df36f6736061264ce55fea254defbc7b2eab1665cd8cc  mcpres/__init__.py
13a1a5b340cdcfc1902b62be90e508c7c71886000d5bf087e7854aadf09fb35e  mcpres/__main__.py
63d10d1acb3d9fe1f8b55ecf5a1957d950bc10c1199352f350b890f5029bed7b  mcpres/audit.py
3bab6a48238e8aeb74fdf53f0ba4ffc704255daa153e1dbb3358c4545b7bcffe  mcpres/auth.py
e62f2e1693f2209ead20f5087b3b76440f98448b87d907bb1a003a480278f661  mcpres/catalog.py
dc75d652d2b6b6baa2f4ef0d1eaca3660cb7d4cd76769d789407f6830693bd63  mcpres/cli.py
7a21c966e4a7a29b1e58fc9f042b68760e4f8db09a99fc9bc92dc8060b2fe2a6  mcpres/conformance.py
9f072dbea531000f70687390911bee0af3a2b28fe27950eaf9a8c9a6fde1b7a3  mcpres/contracts.py
6add05208b74893f2583c94b316edb5f9129b9c3a4004e6cd879d69c0581e575  mcpres/datasets.py
87a0ea1907c1434ff9c6d64dc2f8b94da3ce4e641631f0b0820c351978e40995  mcpres/fuzz.py
063a18c27a3b9194298491ad245e17930abd73ecb1776e2fec93e1dcd4217d7a  mcpres/hub.py
35352448a4ba83985f5f84ff18f1e0decc05096222f5ee506964ea305dc7873a  mcpres/jsonrpc.py
030bdd0f0eed180c2248318ca4af571cb3154d376529014f703cf243ce4e51b2  mcpres/llm.py
07328559e4e01eebddbbbc558ff90003efc06bc2c140ab00bbf3ac4737401f1d  mcpres/memory.py
6a1d7c695166e4c8559bb4a1715b02411c13168a402acf4949390425f7a28fdc  mcpres/modelcheck.py
c8d406d1b219b27d9e911d7c9dd59851ffb7a23e4fac0b73cccb64bae5029a00  mcpres/paging.py
b849b14e5d36791883e593556e4c5a05ace7c4bc0771f1b537557681f50e21f4  mcpres/prompts.py
0b6659b41327c85bf4d6e33ff00e36d9ec09fe20cc726cd15d565f5d86b8b2aa  mcpres/resources.py
6522db8460cbd73973d0eeb6e71fa96908606bff53000bd99dd3850f37d6fd8a  mcpres/schema.py
de2ae16f13c6a359300891817836ee2d8d10755f2e7676eb975bd81452c5f5ad  mcpres/server.py
39dac28b171a18f0e8df8664c6ff64de3198b39c4ca1191a78e16bf876068a95  mcpres/transport_http.py
791e9c09ae277f47d77a571f63e3a3e719ab88a0c1330fa8bb583ab33aaea693  mcpres/transport_stdio.py
f11b89854f988d99a5185b848d3ea38c38f1ec64b11954a2a50cfc8a4d79ee14  mcpres/userdata.py
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  tests/__init__.py
3fedafba9590210e04b1b6966f0fcf435c09c0032c3929168e953dcfca867dd9  tests/helpers.py
5d13d43b41b5ce49bd3f6b666d06bd7e4e9d183f1d18fc02b780cd74c87fc03e  tests/test_auth.py
94729174df9be105fbb3fff7231fea2ba6e89e9bc9322cb8bb0a3ec549cc734f  tests/test_datasets.py
418dfab87886b3a33f21101f00bc870997c9ff96b8940e93d0a7fe01b596eb3a  tests/test_fuzz.py
e4b049066e0a0cc3d1cf7fa0ded9fe5a81809a4608f3d08f6f038747bbc84ec1  tests/test_http_e2e.py
3940cf2d4872a0a7b542d05095e3e217241b815e9566a7c0e530a16858ed507b  tests/test_jsonrpc.py
c74592642eeb4de1e98e1370c72f95e88c980fb8abe72c1df9302ce6435d79d0  tests/test_llm_and_tools.py
e50f0203643fe00118926c809f53b7641c58221cb10f9fdd96f4924a8c87f5ac  tests/test_paging.py
aa14c37ad60c5d11d225407ebdcce1cabb61c7b550131c071366cd9c2f77ce83  tests/test_persistence_audit.py
c44e44a11c6a53b718d6617e6fed35475740d63cf6d22a78f21269d96b65e840  tests/test_prompts_completion.py
e8bd692983210c6454312f00fdf664399dfc0e9f752cd6212ab1ec9839d2e3bc  tests/test_protocol.py
a651da3b43efdf9ef2e0683c98fdf877b5e2d24a97223c916f3d704b2a6f41a6  tests/test_resources.py
a6d899e5327ac12d914e6b105d2017f67e15db5479ca97c68530575b7c9adcfd  tests/test_schema.py
8c10fd6880fd92e0075166e9b94f0ce54155477eea78edcedc78885bfcc38478  tests/test_stdio_e2e.py
8e633101b2aa4a8f83c9181eed445759ff666a05ec82bf655c64164c4c902a2c  tests/test_subscriptions.py
89e8b7dd57c23907d7fab1305e2eedcf3056ed2dc30ab767912d04d1a644751c  tools/adaptation_report.py
c778503bfc3c104eec59d093c8d1687510662ce5defd529bfc7327bcfbf2f936  tools/build_evidence.sh
b2d2145aae29a6f08cc2f3ac1ab3bfd6ba0485b560c8033d93480e26f91e81cb  tools/check_doc_refs.py
d6df799b230aa4e01f927e19ae9d4a202894265b47c1a0b10369f2e9c5da5fa8  tools/check_stdlib_only.py
65074c2678b2e041b9cc1e426dcb0549c95b4e42cca1c43b198b40f8efd2fc7d  tools/gen_originality.py
e9f8dccddf1a82f43c746d0eb10d1efe89722acded7b5e5bcf032080b0bc76d7  tools/make_manifest.py
8a9ad4e29dec669b6fce0b65e9f2ea6b473de5439b1584bfd11edae79792f36a  tools/model_check.py
62c22dc6f57032ca07ca72d82369dc30ab7e191704f9899ce54138e46a11c73d  tools/mutation_sanity.py
```

<!-- GENERATED:END -->
