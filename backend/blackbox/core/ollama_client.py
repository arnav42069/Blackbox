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


class _ProviderHTTPError(OllamaError):
    def __init__(self, status_code, reason):
        super().__init__(f'Ollama HTTP {status_code}: {reason}')
        self.status_code = status_code
        self.reason = reason


def _provider_http_error(response, *, limit=400):
    try:
        body = response.json()
    except ValueError:
        body = None
    reason = body.get('error', body.get('message')) if isinstance(body, dict) else None
    if isinstance(reason, dict):
        reason = reason.get('message')
    if not isinstance(reason, str) or not reason:
        reason = response.text or 'Ollama rejected request'
    return _ProviderHTTPError(response.status_code, clean_text(reason)[:limit])


def _rejects_think_option(error, model):
    if error.status_code not in {400, 422}:
        return False
    flag = r"""['"`]?think(?:ing)?['"`]?"""
    field = r'(?:option|parameter|field|argument)'
    option = rf'(?:(?:the )?{field} )?{flag}'
    quoted_model = re.escape(json.dumps(model, ensure_ascii=False))
    subject = rf'(?:(?:the |this )?model|(?-i:{quoted_model}|{re.escape(model)}))'
    return re.fullmatch(
        rf"(?:{subject} )?(?:does not|doesn't) support {option}"
        rf'|{option}(?: {field})? (?:is )?(?:not supported|unsupported)(?: (?:by|for) (?:this|the) model)?'
        rf'|(?:unknown|unrecognized|unsupported|unexpected) (?:request )?{field}(?: supplied)?\s*:?\s*{flag}',
        error.reason.strip().rstrip('.!'), re.IGNORECASE,
    ) is not None


class OllamaClient:
    def __init__(self, settings: Settings, db, transport=None):
        self.settings, self.db = settings, db
        self.http = httpx.AsyncClient(base_url=settings.ollama_url, timeout=httpx.Timeout(180, connect=3), trust_env=False, transport=transport)
        self.cache = None
        self.cache_at = 0
        self.models_without_think: dict[str, str | None] = {}

    async def request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
            if response.is_error:
                raise _provider_http_error(response, limit=200)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as e:
            raise OllamaError(f'Ollama timeout while waiting for {path}: {type(e).__name__}') from e
        except httpx.ConnectError as e:
            raise OllamaError(f'Ollama unavailable: connection failed for {path}: {str(e)[:240]}') from e
        except (httpx.HTTPError, ValueError) as e:
            raise OllamaError(f"Ollama unavailable or rejected request. Run `ollama serve`; check installed model with `ollama list`. {str(e)[:240]}") from e

    async def models(self, refresh=False, *, check_identity=False):
        cached = self.cache
        cache_fresh = cached is not None and time.monotonic() - self.cache_at < 30
        if cache_fresh and not refresh and not check_identity:
            return cached
        tags = (await self.request("GET", "/api/tags")).get("models", [])
        if cache_fresh and not refresh and cached is self.cache:
            live_ids = {(tag.get('name', tag.get('model')), tag.get('digest')) for tag in tags}
            cached_ids = {(entry['name'], entry['digest']) for entry in cached}
            if live_ids == cached_ids:
                return cached
        models = []
        for tag in tags:
            name = tag.get("name", tag.get("model"))
            digest = tag.get("digest")
            if not digest:
                raise OllamaError(f"Ollama did not provide a digest for {name}; capability verification requires an exact model identity")
            saved = self.db.one("SELECT * FROM model_capabilities WHERE model_digest=?", (digest,))
            metadata = await self.request("POST", "/api/show", json={"model": name})
            if saved:
                probes = json.loads(saved["probe_json"])
                self.db.execute("UPDATE model_capabilities SET metadata_json=? WHERE model_digest=?", (json.dumps({"capabilities":metadata.get("capabilities", []), "details":metadata.get("details", {})}), digest))
            else:
                probes = {}
                self.db.execute("INSERT OR REPLACE INTO model_capabilities VALUES (?,?,?,?,?)", (digest, name, json.dumps({"capabilities": metadata.get("capabilities", []), "details": metadata.get("details", {})}), "{}", time.time()))
            caps = metadata.get("capabilities", [])
            models.append({"name": name, "digest": digest, "size": tag.get("size"), "vision": "vision" in caps and probes.get("vision") is True, "vision_advertised": "vision" in caps, "tools": "tools" in caps and probes.get("tools") is True, "tools_advertised": "tools" in caps, "thinking": "thinking" in caps, "source": "metadata + probes" if probes else "metadata (unprobed)", "probes": probes})
        self.cache, self.cache_at = models, time.monotonic()
        return models

    @staticmethod
    def _find_model(model, installed):
        for m in installed:
            if m["name"] == model:
                return m
        alternatives=', '.join(m['name'] for m in installed) or '(none installed)'
        raise OllamaError(f"Model {model} is not installed. Installed alternatives: {alternatives}. Select one explicitly or run `ollama pull {model}` (downloads require your action).")

    async def capabilities(self, model, *, check_identity=False):
        return self._find_model(model, await self.models(check_identity=check_identity))

    async def selection_status(self, model):
        """Restoring history remains possible when the provider is unavailable."""
        try:
            installed=await self.models(check_identity=True)
        except OllamaError as exc:
            return {'status':'unknown','message':str(exc),'alternatives':[]}
        names=[m['name'] for m in installed]
        return {'status':'available' if model in names else 'missing',
                'message':'' if model in names else f'Saved model {model} is unavailable. Select an installed alternative explicitly.',
                'alternatives':names}

    async def vision_model(self, selected):
        installed = await self.models(check_identity=True)
        capability = self._find_model(selected, installed)
        if capability['vision']:
            return selected
        fallback = self.settings.prefs.vision_model
        if fallback and self._find_model(fallback, installed)['vision']:
            return fallback
        diagnostic=capability.get('probes',{}).get('diagnostics',{}).get('vision',{})
        if capability['vision_advertised'] and diagnostic:
            raise OllamaError(f"Vision probe failed ({diagnostic.get('code','unknown')}): {diagnostic.get('message','No successful image probe')}. This does not confirm lack of vision support. Retry Probe capabilities in Settings.")
        raise OllamaError('Vision is advertised but unverified; select Probe capabilities in Settings.' if capability['vision_advertised'] else 'Ollama metadata does not advertise vision for the selected model.')

    def _think_digest(self, model):
        return next((entry['digest'] for entry in self.cache or [] if entry['name'] == model), None)

    async def chat(self, model, messages, tools=None):
        p = self.settings.prefs
        digest = self._think_digest(model)
        body = {"model": model, "messages": messages, "stream": False, "options": {"num_ctx": p.num_ctx, "temperature": p.temperature}, "keep_alive": p.keep_alive}
        if tools:
            body["tools"] = tools
        if model not in self.models_without_think or self.models_without_think[model] != digest:
            body["think"] = p.think
        try:
            return await self.request("POST", "/api/chat", json=body)
        except _ProviderHTTPError as e:
            if "think" in body and _rejects_think_option(e, model):
                self.models_without_think[model] = digest
                body.pop("think")
                return await self.request("POST", "/api/chat", json=body)
            raise

    async def chat_stream(self, model, messages, tools=None, on_content=None):
        """Consume Ollama NDJSON, keeping private thinking out of the public stream.

        Some proxies return a single JSON chat response even with stream=true.
        Retry transport failures only before any output or action has been received.
        """
        p = self.settings.prefs
        digest = self._think_digest(model)
        body = {"model": model, "messages": messages, "stream": True,
                "options": {"num_ctx": p.num_ctx, "temperature": p.temperature},
                "keep_alive": p.keep_alive}
        if tools:
            body['tools'] = tools
        if model not in self.models_without_think or self.models_without_think[model] != digest:
            body['think'] = p.think
        transport_retried = False
        # At most two option fallbacks and one transport retry before output.
        for _ in range(4):
            raw, calls, metrics, emitted = '', [], {}, ''
            thinking_chars, content_chunks = 0, 0
            received = False
            indexed_calls = {}
            try:
                async with self.http.stream('POST', '/api/chat', json=body) as response:
                    if response.is_error:
                        await response.aread()
                        error = _provider_http_error(response)
                        if 'think' in body and _rejects_think_option(error, model):
                            self.models_without_think[model] = digest
                            body.pop('think')
                            continue
                        if 'tools' in body and 'support' in error.reason.lower() and 'tool' in error.reason.lower():
                            body.pop('tools')
                            continue
                        raise error
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
                if not received and not transport_retried:
                    transport_retried = True
                    await asyncio.sleep(0)
                    continue
                raise OllamaError('Ollama connection failed or timed out; partial output retained. Check `ollama serve` and retry.') from exc
            except (ValueError, TypeError) as exc:
                raise OllamaError('Malformed Ollama streaming response; partial output retained') from exc
        raise OllamaError('Selected model rejected supported request options; select or probe another model')

    async def probe(self, model):
        cap = await self.capabilities(model, check_identity=True)
        probes = {'vision': False, 'tools': False, 'diagnostics': {}}
        def failure(kind, code, message):
            probes['diagnostics'][kind] = {'code': code, 'message': str(message)[:500]}
        def classify(error):
            text = str(error).lower()
            if 'timeout' in text or 'timed out' in text: return 'timeout'
            if 'memory' in text or 'out of memory' in text: return 'memory_exhausted'
            if 'loading' in text or 'load model' in text: return 'model_loading'
            if 'connect' in text or 'unavailable' in text: return 'connection_failed'
            return 'provider_error'
        if cap['tools_advertised']:
            tool = {"type":"function","function":{"name":"echo_test","description":"Echo a test value","parameters":{"type":"object","properties":{"value":{"type":"string"}},"required":["value"]}}}
            try:
                response = await self.chat(model,[{"role":"user","content":"Call echo_test with value blackbox-probe. Do not answer in text."}],[tool])
                calls = response.get('message',{}).get('tool_calls',[])
                for call in calls if isinstance(calls,list) else []:
                    f=call.get('function',{}) if isinstance(call,dict) else {}
                    args=f.get('arguments',{}) if isinstance(f,dict) else {}
                    if isinstance(args,str):
                        try: args=json.loads(args)
                        except ValueError: args={}
                    if len(calls)==1 and f.get('name')=='echo_test' and args=={'value':'blackbox-probe'}:
                        probes['tools']=True
                if not probes['tools']: failure('tools','invalid_response','No valid structured echo_test tool call was returned.')
                else:
                    probes['tools']=False
                    history=[{'role':'user','content':'Call echo_test with value blackbox-probe.'},
                             response['message'],
                             {'role':'tool','tool_name':'echo_test','content':'blackbox-probe'},
                             {'role':'user','content':'Now call echo_test again with value blackbox-second.'}]
                    second=await self.chat(model,history,[tool])
                    from .agent import parse_calls, visible_content
                    second_calls=parse_calls(second.get('message'))
                    if len(second_calls)!=1 or second_calls[0]['function']!={'name':'echo_test','arguments':{'value':'blackbox-second'}}:
                        failure('tools','multi_turn_failed','Model did not return the requested second structured tool call after a tool result.')
                    else:
                        history.extend([second['message'],{'role':'tool','tool_name':'echo_test','content':'blackbox-second'},
                                        {'role':'user','content':'Both tool results are available. Answer exactly blackbox-probe-complete, without calling tools.'}])
                        final=await self.chat(model,history)
                        message=final.get('message',{})
                        probes['tools']=visible_content(message.get('content',''))=='blackbox-probe-complete' and not parse_calls(message)
                        if not probes['tools']:
                            failure('tools','final_answer_failed','Model failed to generate the expected final answer after successive tool results.')
            except (OllamaError, AttributeError, TypeError, ValueError) as e:
                failure('tools',classify(e),e)
        else:
            failure('tools','unsupported','Ollama metadata does not advertise tools.')
        if cap['vision_advertised']:
            image=Image.new('RGB',(64,64),'white')
            ImageDraw.Draw(image).rectangle((12,12,52,52),fill='red')
            output=io.BytesIO(); image.save(output,format='PNG')
            try:
                answer=await self.chat(model,[{"role":"user","content":"Name the color and shape in this image, using two English words.","images":[base64.b64encode(output.getvalue()).decode()]}])
                content = answer.get('message', {}).get('content', '')
                if not isinstance(content, str):
                    failure('vision', 'invalid_response', 'Image probe did not return a text answer.')
                else:
                    normalized = ' '.join(content.casefold().strip().strip('"`\'_*!.').split())
                    probes['vision'] = bool(re.fullmatch(r'(?:a )?(?:solid )?red (?:square|rectangle)', normalized))
                    if not probes['vision']:
                        failure('vision', 'invalid_response',
                                f'Image probe expected a short affirmative description of the red square: {content[:250] or "empty response"}')
            except (OllamaError, AttributeError, TypeError, ValueError) as e:
                failure('vision',classify(e),e)
        else:
            failure('vision','unsupported','Ollama metadata does not advertise vision.')
        # Confirm the tag still identifies the same model after inference.
        tags=(await self.request('GET','/api/tags')).get('models',[])
        if not any(t.get('name',t.get('model'))==model and t.get('digest')==cap['digest'] for t in tags):
            self.cache=None
            raise OllamaError('Model identity changed during probing; results were not saved. Retry the probe.')
        self.db.execute("UPDATE model_capabilities SET probe_json=?,tested_at=? WHERE model_digest=?",(json.dumps(probes),time.time(),cap['digest']))
        self.cache=None
        return probes

    async def close(self):
        await self.http.aclose()
