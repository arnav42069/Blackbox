from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .api.auth import Auth, SecurityMiddleware, error_response
from .core.agent import Agent
from .core.ollama_client import OllamaClient, OllamaError
from .settings import Preferences, Settings
from .storage.db import Database, now, uid
from .storage import images
from .storage.retention import prune
from .storage.files import clean_text
from .core.scope import AuthorizationScope, approve_scope
from .tools.ssh import public_profiles


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=160)


class Pair(BaseModel):
    token: str = Field(max_length=128)


class Approval(BaseModel):
    approve: bool


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings()
    settings.prepare()
    db = Database(settings.data_dir / "blackbox.db")
    prune(settings, db)
    auth = Auth()
    ollama = OllamaClient(settings, db, transport)
    agent = Agent(settings, db, ollama)
    from .dependencies import detect
    dependencies = detect()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await agent.shutdown()
        await ollama.close()
        db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.auth = settings, db, auth
    app.state.ollama, app.state.agent = ollama, agent
    app.state.dependencies = dependencies
    app.add_middleware(SecurityMiddleware, auth=auth, port=settings.port)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response({400: "VALIDATION", 403: "SCOPE_DENIED", 404: "NOT_FOUND", 409: "RUN_ACTIVE", 413: "IMAGE_TOO_LARGE", 429: "BUDGET", 503: "OLLAMA_UNAVAILABLE"}.get(exc.status_code, "REQUEST_FAILED"), str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response("VALIDATION", "; ".join(str(e["msg"]) for e in exc.errors()), 400)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return error_response("VALIDATION", str(exc), 400)

    @app.exception_handler(OllamaError)
    async def ollama_error(request, exc):
        return error_response("OLLAMA_UNAVAILABLE", str(exc), 503)

    def session(sid):
        result = db.session(sid)
        if not result:
            raise HTTPException(404, "Session not found")
        return result

    def idle(sid):
        session(sid)
        if agent.active(sid):
            raise HTTPException(409, "Run already active; stop it before changing this session")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.1.0"}

    @app.post("/api/pair")
    async def pair(body: Pair):
        token = auth.exchange(body.token)
        if not token:
            raise HTTPException(401, "Pairing link expired or already used. Restart BlackBox for a new link.")
        return {"token": token}

    @app.get("/api/config")
    async def config():
        return settings.prefs.model_dump()

    @app.patch("/api/config")
    async def update_config(body: Preferences):
        if any(agent.active(sid) for sid in agent.tasks):
            raise HTTPException(409, "Stop active runs before changing settings")
        settings.save_preferences(body)
        prune(settings, db)
        return body.model_dump()

    @app.get("/api/preflight")
    async def preflight():
        try:
            models = await ollama.models()
            state = {"connected": True, "models": len(models), "message": "Connected" if models else "No models installed. Run ollama pull qwen3.5:4b"}
        except OllamaError as e:
            state = {"connected": False, "message": str(e)}
        return {"conversation_protocol": 1, "ollama": state, "dependencies": dependencies, "binaries": {name: bool(shutil.which(name)) for name in ("ping", "nmap", "curl", "whatweb", "ffuf", "ssh", "grim", "wl-paste")}, "ssh_agent": bool(__import__("os").environ.get("SSH_AUTH_SOCK")), "storage": "private", "port": settings.port}

    @app.get("/api/models")
    async def models():
        return await ollama.models(refresh=True)

    @app.post("/api/models/{model:path}/probe")
    async def probe(model: str):
        return await ollama.probe(model)

    @app.post("/api/sessions")
    async def new_session(body: NewSession):
        return db.create_session(Preferences(model=body.model or settings.prefs.model).model)

    @app.get("/api/sessions")
    async def sessions():
        return db.rows("SELECT id,title,model,status,updated_at FROM sessions ORDER BY updated_at DESC LIMIT 200")

    @app.get("/api/sessions/{sid}")
    async def get_session(sid: str):
        from .core.goals import GoalController
        goal = GoalController.load(db, sid)
        return {**session(sid), 'task_goal':goal.model_dump() if goal else None, "messages": db.messages(sid), "events": db.events(sid)}

    @app.patch("/api/sessions/{sid}")
    async def update_session(sid: str, body: NewSession):
        idle(sid)
        prefs = Preferences(model=body.model or settings.prefs.model)
        db.execute("UPDATE sessions SET model=? WHERE id=?", (prefs.model, sid))
        return session(sid)

    @app.get("/api/ssh-profiles")
    async def ssh_profiles():
        try:
            return public_profiles(settings)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/sessions/{sid}/scope")
    async def set_scope(sid: str, body: AuthorizationScope):
        idle(sid)
        try:
            approved = await approve_scope(body)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        idle(sid)
        previous=session(sid).get('scope') or {}
        db.execute("UPDATE sessions SET scope_json=? WHERE id=?", (json.dumps(approved), sid))
        if previous and previous!=approved:
            from .core.goals import GoalController
            goal=GoalController.load(db,sid)
            db.event(sid,None,'target_context_changed',{'previous_targets':previous.get('allowed_targets',[]),'targets':approved['allowed_targets'],'reason':'Authorization changed. Prior findings remain historical and must not be assumed current.'})
            if goal:
                goal.status='needs_access'
                goal.result=None
                goal.pending=None
                goal.pending_steps=[]
                goal.attempts={}
                goal.hypotheses=[]
                goal.verified_findings=[]
                goal.candidates=[]
                goal.remote_files=[]
                goal.scope_key=''
                goal.checkpoint={'reason':'Scope changed; confirm the exact target before continuing.','at':now()}
                db.execute('UPDATE task_goals SET payload_json=?,updated_at=? WHERE session_id=?',(goal.model_dump_json(),now(),sid))
        return session(sid)

    @app.post("/api/sessions/{sid}/approvals/{tid}")
    async def approve(sid: str, tid: str, body: Approval):
        item = agent.approvals.get(tid)
        if not item or item[0] != sid or item[1].done():
            raise HTTPException(404, "Approval expired")
        item[1].set_result(body.approve)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/messages")
    async def send_message(sid: str, request: Request):
        idle(sid)
        normalized = []
        async with request.form(max_files=4, max_fields=4, max_part_size=9 * 1024 * 1024) as form:
            text = clean_text(str(form.get("text", ""))).strip()
            uploads = form.getlist("images")
            if len(uploads) > 4:
                raise HTTPException(400, "Maximum four images per message")
            if (not text and not uploads) or len(text) > 16000:
                raise HTTPException(400, "Provide a message or images (maximum 16000 characters)")
            cap = await ollama.capabilities(session(sid)["model"])
            if uploads:
                try:
                    await ollama.vision_model(session(sid)['model'])
                except OllamaError as e:
                    raise HTTPException(400, str(e))
            for upload in uploads:
                if not hasattr(upload, 'read'):
                    raise HTTPException(400, "Images must be multipart file uploads")
                data = await upload.read(images.MAX_BYTES + 1)
                if len(data) > images.MAX_BYTES:
                    raise HTTPException(413, "Image exceeds 8 MiB")
                try:
                    normalized.append(await asyncio.to_thread(images.normalize, data))
                except ValueError as e:
                    raise HTTPException(400, str(e))
        idle(sid)  # Recheck after all network and upload awaits to avoid concurrent sends.
        rid, stored = uid(), []
        try:
            with db.transaction():
                for image in normalized:
                    stored.append(images.store(settings, db, sid, image))
                mid = db.message(sid, "user", text, attachments=[a[0] for a in stored])
                db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')", (rid, sid, now()))
                db.event(sid, rid, 'user_message', {'message_id':mid, 'content':text, 'attachment_ids':[a[0] for a in stored], 'metadata':{}})
                db.execute("UPDATE sessions SET title=CASE WHEN title='New session' THEN ? ELSE title END,status='running' WHERE id=?", ((text or 'Image analysis')[:64], sid))
        except BaseException:
            for _, path in stored:
                path.unlink(missing_ok=True)
            raise
        agent.start(sid, rid)
        return {"run_id": rid}

    @app.post("/api/sessions/{sid}/retry")
    async def retry(sid: str):
        idle(sid)
        users = [m for m in db.messages(sid, 1000) if m['role']=='user']
        if not users:
            raise HTTPException(400, 'No message to retry')
        last = users[-1]
        cap = await ollama.capabilities(session(sid)['model'])
        if last['attachment_ids']:
            try:
                await ollama.vision_model(session(sid)['model'])
            except OllamaError as e:
                raise HTTPException(400, str(e))
        idle(sid)
        rid = uid()
        with db.transaction():
            mid = db.message(sid, 'user', last['content'], attachments=last['attachment_ids'])
            db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')", (rid, sid, now()))
            db.event(sid, rid, 'user_message', {'message_id':mid, 'content':last['content'], 'attachment_ids':last['attachment_ids'], 'metadata':{}})
            db.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
        agent.start(sid, rid)
        return {'run_id': rid}

    @app.get("/api/sessions/{sid}/attachments/{aid}")
    async def attachment(sid: str, aid: str):
        session(sid)
        row = db.one('SELECT * FROM attachments WHERE session_id=? AND id=?', (sid, aid))
        if not row or not Path(row['path']).is_file():
            raise HTTPException(404, 'Attachment not found')
        return FileResponse(row['path'], media_type=row['mime'])

    @app.get("/api/sessions/{sid}/export")
    async def export(sid: str):
        from .storage.files import clean_value
        s = session(sid)
        return clean_value({'title': s['title'], 'model': s['model'], 'messages': [{k: m[k] for k in ('role', 'content', 'created_at', 'attachment_ids')} for m in db.messages(sid, 10000)]})

    @app.get("/api/sessions/{sid}/events")
    async def events(sid: str, request: Request, after: int = 0):
        session(sid)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor")
        async def stream():
            cursor = after
            earliest = db.one('SELECT MIN(id) AS first FROM event_journal WHERE session_id=?', (sid,))['first']
            if after and earliest and after < earliest - 1:
                # A retained snapshot repairs clients older than the bounded journal.
                yield 'data: ' + json.dumps({'type': 'resync_required', 'event_id': earliest - 1}) + '\n\n'
            while not await request.is_disconnected():
                for event in db.events(sid, cursor):
                    cursor = event["event_id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str):
        session(sid)
        await agent.cancel(sid)
        return {"status": db.session(sid)["status"]}

    @app.get("/api/sessions/{sid}/tool-events")
    async def tool_events(sid: str):
        session(sid)
        return db.rows("SELECT t.* FROM tool_events t JOIN runs r ON t.run_id=r.id WHERE r.session_id=? ORDER BY t.started_at LIMIT 1000", (sid,))

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        session(sid)
        await agent.cancel(sid)
        for attachment in db.rows("SELECT path FROM attachments WHERE session_id=?", (sid,)):
            Path(attachment["path"]).unlink(missing_ok=True)
        shutil.rmtree(settings.data_dir / "artifacts" / sid, ignore_errors=True)
        db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return {"ok": True}

    if (settings.frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="assets")

    @app.get("/")
    async def index():
        index_path = settings.frontend_dir / "index.html"
        if not index_path.exists():
            return error_response("UI_NOT_BUILT", "Run: cd frontend && npm ci && npm run build", 503)
        return FileResponse(index_path)

    return app
