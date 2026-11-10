from __future__ import annotations

import asyncio
import re
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
            models.append({"name": name, "digest": digest, "size": tag.get("size"), "vision": "vision" in caps and probes.get("vision", not modified), "vision_advertised": "vision" in caps, "tools": probes.get("tools", "tools" in caps and not modified), "thinking": "thinking" in caps, "source": "metadata + probes" if probes else "metadata (unprobed)", "probes": probes})
        self.cache, self.cache_at = models, time.monotonic()
        return models

    async def capabilities(self, model):
        for m in await self.models():
            if m["name"] == model:
                return m
        raise OllamaError(f"Model {model} is not installed. Run `ollama pull {model}` (downloads require your action).")

    async def vision_model(self, selected):
        if (await self.capabilities(selected))['vision']:
            return selected
        fallback = self.settings.prefs.vision_model
        if fallback and (await self.capabilities(fallback))['vision']:
            return fallback
        raise OllamaError('Vision unsupported or unverified. Select/probe a vision-capable model or configure vision_model; images cannot be read by the selected text-only model.')

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

    async def chat_stream(self, model, messages, tools=None, on_content=None):
        """Consume Ollama NDJSON, keeping private thinking out of the public stream.

        Some proxies return a single JSON chat response even with stream=true.
        Retry transport failures only before any output or action has been received.
        """
        p = self.settings.prefs
        body = {"model": model, "messages": messages, "stream": True,
                "options": {"num_ctx": p.num_ctx, "temperature": p.temperature},
                "keep_alive": p.keep_alive}
        if tools:
            body['tools'] = tools
        if not self.omit_think:
            body['think'] = p.think
        for attempt in range(3):
            raw, calls, metrics, emitted = '', [], {}, ''
            thinking_chars, content_chunks = 0, 0
            received = False
            indexed_calls = {}
            try:
                async with self.http.stream('POST', '/api/chat', json=body) as response:
                    if response.is_error:
                        data = await response.aread()
                        reason = clean_text(data.decode(errors='replace'))[:400]
                        if 'think' in reason.lower() and 'think' in body:
                            self.omit_think = True
                            body.pop('think')
                            continue
                        if 'tools' in body and 'support' in reason.lower() and 'tool' in reason.lower():
                            body.pop('tools')
                            continue
                        raise OllamaError(f'Ollama HTTP {response.status_code}: {reason}')
                    done = False
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if not isinstance(chunk, dict) or chunk.get('error'):
                            raise OllamaError('Ollama returned an error or malformed stream')
                        message = chunk.get('message', {})
                        if not isinstance(message, dict) or (message.get('content') is not None and not isinstance(message.get('content'), str)):
                            raise OllamaError('Malformed Ollama assistant message')
                        part = message.get('content') or ''
                        thinking = message.get('thinking') or ''
                        if not isinstance(thinking, str):
                            raise OllamaError('Malformed Ollama thinking field')
                        # Counts diagnose reasoning-only responses without retaining private text.
                        thinking_chars += len(thinking)
                        content_chunks += bool(part)
                        tool_calls = message.get('tool_calls', [])
                        if not isinstance(tool_calls, list):
                            raise OllamaError('Malformed Ollama tool calls')
                        received = received or bool(part or tool_calls)
                        raw += part
                        if len(raw) > 128000 or len(calls) + len(tool_calls) > 20:
                            raise OllamaError('Ollama response size limit reached')
                        for call in tool_calls:
                            function = call.get('function', {}) if isinstance(call, dict) else {}
                            index = call.get('index', function.get('index')) if isinstance(call, dict) and isinstance(function, dict) else None
                            if index is None:
                                calls.append(call)
                                continue
                            if not isinstance(index, int) or not isinstance(function, dict):
                                raise OllamaError('Malformed Ollama tool-call index')
                            if index not in indexed_calls:
                                indexed_calls[index] = {'function': dict(function)}
                                calls.append(indexed_calls[index])
                            else:
                                prior = indexed_calls[index]['function']
                                if function.get('name'):
                                    if prior.get('name') and prior['name'] != function['name']:
                                        raise OllamaError('Malformed conflicting Ollama tool names')
                                    prior['name'] = function['name']
                                args = function.get('arguments')
                                if args is not None:
                                    old = prior.get('arguments', '' if isinstance(args, str) else {})
                                    if isinstance(old, str) and isinstance(args, str):
                                        prior['arguments'] = old + args
                                    elif isinstance(old, dict) and isinstance(args, dict):
                                        prior['arguments'] = {**old, **args}
                                    else:
                                        raise OllamaError('Malformed Ollama tool arguments')
                        # Separate message.thinking is intentionally never returned or persisted.
                        public = re.sub(r'<think>.*?(?:</think>|$)', '', raw, flags=re.S | re.I)
                        # An opening tag may straddle transport chunks.
                        public = re.sub(r'<(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$', '', public, flags=re.I)
                        public = clean_text(public)[:32000]
                        if public != emitted and on_content:
                            on_content(public)
                        emitted = public
                        metrics.update({k: chunk[k] for k in ('total_duration', 'load_duration', 'prompt_eval_count', 'eval_count') if k in chunk})
                        done = done or chunk.get('done') is True
                        # JSON-only adapters do not have a done field.
                        if 'done' not in chunk and 'message' in chunk:
                            done = True
                        if done and chunk.get('done_reason') in {'length','max_tokens'}:
                            raise OllamaError('Ollama output budget exhausted; partial response retained. Narrow the request or adjust model limits before retrying.')
                        if done:
                            break
                    if not done:
                        raise OllamaError('Ollama stream ended before completion; partial output retained')
                    return {**metrics, 'thinking_chars': thinking_chars, 'content_chunks': content_chunks,
                            'tool_fallback': bool(tools and 'tools' not in body),
                            'message': {'role': 'assistant', 'content': emitted.strip(), 'tool_calls': calls}}
            except (httpx.HTTPError, TimeoutError) as exc:
                if not received and attempt == 0:
                    await asyncio.sleep(0)
                    continue
                raise OllamaError('Ollama connection failed or timed out; partial output retained. Check `ollama serve` and retry.') from exc
            except (ValueError, TypeError) as exc:
                raise OllamaError('Malformed Ollama streaming response; partial output retained') from exc
        raise OllamaError('Selected model rejected supported request options; select or probe another model')

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
            if not cap.get('vision_advertised'):
                raise OllamaError('Model metadata does not advertise vision; a guessed probe answer cannot establish image support')
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
