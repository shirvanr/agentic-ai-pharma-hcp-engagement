"""
llm.py - one function, chat(), behind a multi-provider failover chain.

Order: OpenAI -> Groq -> Google Gemini -> OpenRouter -> deterministic rule-based
fallback.

Nothing is downloaded and no SDK is installed: every provider is a plain HTTPS
call with `requests`. OpenAI, Groq and OpenRouter share one code path because all
three speak the OpenAI chat-completions schema; Gemini gets a small adapter.

Model ids are not blindly hardcoded. Each provider has a preference list and
resolves it against the provider's live /models endpoint on first use, so a
retired model id degrades to the next choice instead of 404-ing the demo.
"""

import json
import os
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

TIMEOUT = 20
MAX_TOKENS = 700          # answers are meant to be under 250 words; this keeps latency down


class LLMResult(object):
    def __init__(self, text="", tool_calls=None, provider="none", model="",
                 latency_ms=0, prompt_tokens=0, completion_tokens=0,
                 ok=False, error="", attempts=None):
        self.text = text or ""
        self.tool_calls = tool_calls or []      # [{id, name, arguments(dict)}]
        self.provider = provider
        self.model = model
        self.latency_ms = latency_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.ok = ok
        self.error = error
        self.attempts = attempts or []          # [(provider, model, status)]

    @property
    def label(self):
        if not self.ok:
            return "rule-based fallback (no LLM)"
        return "%s / %s" % (self.provider, self.model)


# ===========================================================================
# Provider: anything speaking the OpenAI chat-completions schema
# ===========================================================================
class OpenAICompatProvider(object):
    def __init__(self, name, base_url, key_env, model_prefs, extra_headers=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.model_prefs = model_prefs
        self.extra_headers = extra_headers or {}
        self._model = None
        self._resolved = False

    @property
    def key(self):
        return os.environ.get(self.key_env, "").strip()

    def available(self):
        return bool(self.key)

    def _headers(self):
        h = {"Authorization": "Bearer %s" % self.key,
             "Content-Type": "application/json"}
        h.update(self.extra_headers)
        return h

    def resolve_model(self):
        """Ask the provider which models actually exist right now."""
        if self._resolved:
            return self._model
        self._resolved = True
        override = os.environ.get("%s_MODEL" % self.name.upper(), "").strip()
        if override:
            self._model = override
            return self._model
        live = set()
        try:
            r = requests.get(self.base_url + "/models", headers=self._headers(),
                             timeout=TIMEOUT)
            if r.status_code == 200:
                live = {m.get("id") for m in r.json().get("data", []) if m.get("id")}
        except Exception:
            live = set()
        for m in self.model_prefs:
            if not live or m in live:
                self._model = m
                return self._model
        # nothing from the preference list survived - take any free tool-capable id
        for m in sorted(live):
            if m.endswith(":free") or "llama" in m or "instruct" in m:
                self._model = m
                return self._model
        self._model = self.model_prefs[0]
        return self._model

    def chat(self, messages, tools=None, temperature=0.2):
        model = self.resolve_model()
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": MAX_TOKENS}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        t0 = time.time()
        r = requests.post(self.base_url + "/chat/completions",
                          headers=self._headers(), data=json.dumps(payload),
                          timeout=TIMEOUT)
        latency = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            raise RuntimeError("%s HTTP %s: %s" % (self.name, r.status_code,
                                                   r.text[:180]))
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            calls.append({"id": tc.get("id") or "call_%d" % len(calls),
                          "name": fn.get("name"), "arguments": args or {}})
        usage = data.get("usage") or {}
        return LLMResult(text=msg.get("content") or "", tool_calls=calls,
                         provider=self.name, model=model, latency_ms=latency,
                         prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                         completion_tokens=usage.get("completion_tokens", 0) or 0,
                         ok=True)


# ===========================================================================
# Provider: Google Gemini (its own request/response shape)
# ===========================================================================
class GeminiProvider(object):
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model_prefs):
        self.model_prefs = model_prefs
        self._model = None
        self._resolved = False

    @property
    def key(self):
        return os.environ.get("GEMINI_API_KEY", "").strip()

    def available(self):
        return bool(self.key)

    def resolve_model(self):
        if self._resolved:
            return self._model
        self._resolved = True
        override = os.environ.get("GEMINI_MODEL", "").strip()
        if override:
            self._model = override
            return self._model
        live = set()
        try:
            r = requests.get("%s/models?key=%s" % (self.BASE, self.key), timeout=TIMEOUT)
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    nm = (m.get("name") or "").replace("models/", "")
                    if "generateContent" in (m.get("supportedGenerationMethods") or []):
                        live.add(nm)
        except Exception:
            live = set()
        for m in self.model_prefs:
            if not live or m in live:
                self._model = m
                return self._model
        for m in sorted(live):
            if "flash" in m:
                self._model = m
                return self._model
        self._model = self.model_prefs[0]
        return self._model

    @staticmethod
    def _to_gemini(messages, tools):
        system = "\n\n".join(m["content"] for m in messages
                             if m.get("role") == "system" and m.get("content"))
        contents = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            if role == "tool":
                contents.append({"role": "user", "parts": [{"text":
                    "TOOL RESULT (%s):\n%s" % (m.get("name", "tool"), m.get("content", ""))}]})
                continue
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except Exception:
                        args = {}
                parts.append({"functionCall": {"name": fn.get("name"), "args": args or {}}})
            if not parts:
                continue
            contents.append({"role": "model" if role == "assistant" else "user",
                             "parts": parts})
        body = {"contents": contents,
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": MAX_TOKENS}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            decls = []
            for t in tools:
                f = t.get("function", t)
                params = json.loads(json.dumps(f.get("parameters") or
                                               {"type": "object", "properties": {}}))
                decls.append({"name": f["name"], "description": f.get("description", ""),
                              "parameters": params})
            body["tools"] = [{"functionDeclarations": decls}]
        return body

    def chat(self, messages, tools=None, temperature=0.2):
        model = self.resolve_model()
        url = "%s/models/%s:generateContent?key=%s" % (self.BASE, model, self.key)
        body = self._to_gemini(messages, tools)
        body["generationConfig"]["temperature"] = temperature
        t0 = time.time()
        r = requests.post(url, data=json.dumps(body),
                          headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
        latency = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            raise RuntimeError("gemini HTTP %s: %s" % (r.status_code, r.text[:180]))
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts") or [])
        text, calls = "", []
        for p in parts:
            if "text" in p:
                text += p["text"]
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append({"id": "call_%d" % len(calls), "name": fc.get("name"),
                              "arguments": fc.get("args") or {}})
        um = data.get("usageMetadata") or {}
        return LLMResult(text=text, tool_calls=calls, provider=self.name, model=model,
                         latency_ms=latency,
                         prompt_tokens=um.get("promptTokenCount", 0) or 0,
                         completion_tokens=um.get("candidatesTokenCount", 0) or 0,
                         ok=True)


# ===========================================================================
# The chain
# ===========================================================================
PROVIDERS = [
    OpenAICompatProvider(
        "openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
        ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1-nano"]),
    OpenAICompatProvider(
        "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "openai/gpt-oss-120b",
         "llama-3.1-8b-instant"]),
    GeminiProvider(
        ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash-lite",
         "gemini-3.5-flash"]),
    OpenAICompatProvider(
        "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        ["z-ai/glm-5.2:free", "google/gemma-4-31b-it:free",
         "minimax/minimax-m2.7:free", "nvidia/nemotron-3-super-120b-a12b:free",
         "meta-llama/llama-3.3-70b-instruct:free"],
        extra_headers={"HTTP-Referer": "https://localhost",
                       "X-Title": "HCP Engagement Assistant Demo"}),
]


def configured_providers():
    return [p.name for p in PROVIDERS if p.available()]


def any_key_present():
    return bool(configured_providers())


def chat(messages, tools=None, temperature=0.2):
    """
    Try each configured provider in order. Never raises.
    Returns an LLMResult; ok=False means every provider failed or none is
    configured, and the caller must use the deterministic fallback path.
    """
    attempts = []
    for p in PROVIDERS:
        if not p.available():
            attempts.append((p.name, "-", "no key"))
            continue
        try:
            res = p.chat(messages, tools=tools, temperature=temperature)
            res.attempts = attempts + [(p.name, res.model, "ok")]
            return res
        except Exception as e:
            attempts.append((p.name, getattr(p, "_model", "") or "?",
                             str(e)[:120]))
            continue
    return LLMResult(provider="fallback", model="rule-based", ok=False,
                     error="no provider answered", attempts=attempts)


def status_line():
    """Short human-readable description of the model layer, for the UI header."""
    names = configured_providers()
    if not names:
        return "FALLBACK MODE - no LLM key configured (rule-based answers)"
    p = [x for x in PROVIDERS if x.name == names[0]][0]
    try:
        m = p.resolve_model()
    except Exception:
        m = "?"
    chain = " -> ".join(names + ["rule-based fallback"])
    return "%s / %s   (failover: %s)" % (names[0], m, chain)


if __name__ == "__main__":
    print("Configured providers:", configured_providers() or "none")
    print("Status:", status_line())
    r = chat([{"role": "system", "content": "You are terse."},
              {"role": "user", "content": "Reply with the single word: ready"}])
    print("ok=%s provider=%s model=%s latency=%sms tokens=%s" %
          (r.ok, r.provider, r.model, r.latency_ms, r.total_tokens))
    print("text:", (r.text or "")[:200])
    print("attempts:", r.attempts)
