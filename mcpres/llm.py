"""Optional LLM hook. Disabled unless MCP_FORGE_LLM_API_KEY is set; never required.

Speaks the widely used ``POST {base}/chat/completions`` JSON shape using only urllib.
Environment:
    MCP_FORGE_LLM_API_KEY   enables the hook (bearer token for the provider)
    MCP_FORGE_LLM_BASE_URL  default https://api.openai.com/v1  (any compatible endpoint)
    MCP_FORGE_LLM_MODEL     default gpt-4o-mini
    MCP_FORGE_LLM_TIMEOUT   seconds, default 30
The key is never logged, audited or returned in any result.
"""

import json
import os
import urllib.error
import urllib.request


class LlmError(Exception):
    pass


class LlmClient:
    def __init__(self, api_key, base_url, model, timeout=30.0):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("LLM base URL must be http(s)")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        key = env.get("MCP_FORGE_LLM_API_KEY")
        if not key:
            return None
        return cls(key, env.get("MCP_FORGE_LLM_BASE_URL", "https://api.openai.com/v1"),
                   env.get("MCP_FORGE_LLM_MODEL", "gpt-4o-mini"), float(env.get("MCP_FORGE_LLM_TIMEOUT", "30")))

    def complete(self, system, user, max_tokens=400):
        body = json.dumps({"model": self.model, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self._api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read(1_000_000).decode("utf-8"))
            return str(payload["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            raise LlmError("LLM endpoint returned HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError("LLM request failed: %s" % type(exc).__name__)
