from __future__ import annotations

import base64
import io
import json
import time

import httpx
from PIL import Image, ImageDraw

from ..settings import Settings
from ..storage.files import clean_text


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, settings: Settings, db, transport=None):
        self.settings, self.db = settings, db
        self.http = httpx.AsyncClient(base_url=settings.ollama_url, timeout=httpx.Timeout(180, connect=3), trust_env=False, transport=transport)
        self.cache = None
        self.cache_at = 0
        self.omit_think = False

    async def request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
            if response.is_error:
                try:
                    reason = clean_text(str(response.json().get('error', 'Ollama rejected request')))[:200]
                except ValueError:
                    reason = 'Ollama rejected request'
                raise OllamaError(f'Ollama HTTP {response.status_code}: {reason}')
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OllamaError(f"Ollama unavailable or rejected request. Run `ollama serve`; check installed model with `ollama list`. {str(e)[:240]}") from e

    async def models(self, refresh=False):
        if self.cache is not None and not refresh and time.monotonic() - self.cache_at < 30:
            return self.cache
        tags = (await self.request("GET", "/api/tags")).get("models", [])
        models = []
        for tag in tags:
            name = tag.get("name", tag.get("model"))
            digest = tag.get("digest", name)
            saved = self.db.one("SELECT * FROM model_capabilities WHERE model_digest=?", (digest,))
            if saved:
                metadata, probes = json.loads(saved["metadata_json"]), json.loads(saved["probe_json"])
            else:
                metadata = await self.request("POST", "/api/show", json={"model": name})
                probes = {}
                self.db.execute("INSERT OR REPLACE INTO model_capabilities VALUES (?,?,?,?,?)", (digest, name, json.dumps({"capabilities": metadata.get("capabilities", []), "details": metadata.get("details", {})}), "{}", time.time()))
            caps = metadata.get("capabilities", [])
            modified = "abliterated" in (name + json.dumps(tag.get("details", {})) + json.dumps(metadata.get("details", {}))).lower()
            models.append({"name": name, "digest": digest, "size": tag.get("size"), "vision": probes.get("vision", "vision" in caps and not modified), "tools": probes.get("tools", "tools" in caps and not modified), "thinking": "thinking" in caps, "source": "metadata + probes" if probes else "metadata (unprobed)", "probes": probes})
        self.cache, self.cache_at = models, time.monotonic()
        return models

    async def capabilities(self, model):
        for m in await self.models():
            if m["name"] == model:
                return m
        raise OllamaError(f"Model {model} is not installed. Run `ollama pull {model}` (downloads require your action).")

    async def chat(self, model, messages, tools=None):
        p = self.settings.prefs
        body = {"model": model, "messages": messages, "stream": False, "options": {"num_ctx": p.num_ctx, "temperature": p.temperature}, "keep_alive": p.keep_alive}
        if tools:
            body["tools"] = tools
        if not self.omit_think:
            body["think"] = p.think
        try:
            return await self.request("POST", "/api/chat", json=body)
        except OllamaError as e:
            if "think" in str(e).lower() and "think" in body:
                self.omit_think = True
                body.pop("think")
                return await self.request("POST", "/api/chat", json=body)
            raise

    async def probe(self, model):
        cap = await self.capabilities(model)
        probes = {}
        tool = {"type": "function", "function": {"name": "echo_test", "description": "Echo a test value", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}
        try:
            response = await self.chat(model, [{"role": "user", "content": "Call echo_test with value blackbox-probe. Do not answer in text."}], [tool])
            calls = response.get("message", {}).get("tool_calls", [])
        except (OllamaError, AttributeError, TypeError):
            calls = []
        if not isinstance(calls, list):
            calls = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            f = call.get("function", {})
            args = f.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if f.get("name") == "echo_test" and args == {"value": "blackbox-probe"}:
                probes["tools"] = True
        probes.setdefault("tools", False)
        image = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(image).rectangle((12, 12, 52, 52), fill="red")
        output = io.BytesIO()
        image.save(output, format="PNG")
        try:
            answer = await self.chat(model, [{"role": "user", "content": "Name the color and shape in this image, using two English words.", "images": [base64.b64encode(output.getvalue()).decode()]}])
            content = answer.get("message", {}).get("content", "").lower()
            probes["vision"] = "red" in content and ("square" in content or "rectangle" in content)
        except (OllamaError, AttributeError, TypeError):
            probes["vision"] = False
        self.db.execute("UPDATE model_capabilities SET probe_json=?,tested_at=? WHERE model_digest=?", (json.dumps(probes), time.time(), cap["digest"]))
        self.cache = None
        return probes

    async def close(self):
        await self.http.aclose()
