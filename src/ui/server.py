"""SAGE UI — FastAPI WebSocket Server.

Endpoints:
    GET  /                      — Serve UI HTML
    WS   /ws                    — Live event stream
    POST /api/chat              — Send message to Friday
    GET  /api/tasks             — List recent tasks (from StateStore)
    GET  /api/task/{id}         — Task detail + steps
    POST /api/task              — Submit a new task to Supervisor
    POST /api/task/{id}/cancel  — Cancel running task
    GET  /api/approvals         — List pending approvals
    POST /api/approval/{id}/approve — Approve (via TrustCore)
    POST /api/approval/{id}/reject  — Reject (via TrustCore)
    GET  /api/status            — System health snapshot

Security:
    - Localhost-only binding (host=127.0.0.1).
    - Simple session token validated on every request (shared secret in env/config).
    - Approval decisions always go through ApprovalStore — never accepted from frontend claims.
    - Sensitive fields (tokens, keys) are never emitted in events or API responses.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.ui.events import EventType, ExecutionEvent, get_event_bus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    user_id: str = "ui-user"
    conversation_id: str = "ui-session"


class TaskRequest(BaseModel):
    title: str
    goal: Optional[str] = None


class ApprovalDecision(BaseModel):
    actor: str = "ui-user"


class RejectionDecision(BaseModel):
    actor: str = "ui-user"
    reason: str = ""


# ---------------------------------------------------------------------------
# App and shared state (injected by create_app)
# ---------------------------------------------------------------------------

app: Optional[FastAPI] = None
_sage_app: Any = None   # SageApplication instance
_session_token: str = ""


def create_app(sage_application: Any, session_token: Optional[str] = None) -> FastAPI:
    """Create and configure the FastAPI app bound to a live SageApplication.

    Args:
        sage_application: Running SageApplication instance.
        session_token: Optional pre-shared token; generated if None.
    """
    global app, _sage_app, _session_token

    _sage_app = sage_application
    _session_token = session_token or os.environ.get("SAGE_UI_TOKEN") or secrets.token_urlsafe(32)
    logger.info("SAGE UI session token: %s", _session_token)

    app = FastAPI(title="SAGE Live Dashboard", docs_url=None, redoc_url=None)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # -----------------------------------------------------------------------
    # Authentication helper
    # -----------------------------------------------------------------------

    def _check_auth(request: Request) -> None:
        """Validate session token from header or query param."""
        token = request.headers.get("X-SAGE-Token") or request.query_params.get("token")
        if token != _session_token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui(request: Request, token: str = Query(default="")):
        # For the UI we accept token in URL for browser convenience
        if token != _session_token:
            return HTMLResponse(
                "<html><body><h2>SAGE UI</h2>"
                "<p>Token required. Append <code>?token=YOUR_TOKEN</code></p></body></html>",
                status_code=401,
            )
        index = Path(__file__).parent / "static" / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return HTMLResponse("<h1>SAGE UI static files missing.</h1>", status_code=500)

    # -----------------------------------------------------------------------
    # Server-Sent Events (SSE) live event stream (100% offline & standard HTTP)
    # -----------------------------------------------------------------------

    @app.get("/api/events")
    async def sse_events_endpoint(request: Request, token: str = Query(default="")):
        if token != _session_token:
            raise HTTPException(status_code=401, detail="Unauthorized")

        bus = get_event_bus()
        q = bus.subscribe()

        async def event_generator():
            init_ev = ExecutionEvent(
                event_type=EventType.RUNTIME_STARTED,
                message="SAGE UI connected",
                component="ui",
                status="connected",
            ).to_dict()
            yield f"data: {json.dumps(init_ev)}\n\n"

            import asyncio
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = q.get_nowait()
                        yield f"data: {json.dumps(payload)}\n\n"
                    except Exception:
                        await asyncio.sleep(0.05)
            except Exception:
                pass
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -----------------------------------------------------------------------
    # WebSocket live event stream (optional fallback if websockets installed)
    # -----------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
        if token != _session_token:
            await ws.close(code=4001)
            return

        await ws.accept()
        bus = get_event_bus()
        q = bus.subscribe()

        # Emit a "connected" ping so the frontend knows it's live
        await ws.send_text(
            json.dumps(
                ExecutionEvent(
                    event_type=EventType.RUNTIME_STARTED,
                    message="SAGE UI connected",
                    component="ui",
                    status="connected",
                ).to_dict()
            )
        )

        try:
            while True:
                try:
                    payload = q.get_nowait()
                except Exception:
                    # Wait for next event
                    import asyncio
                    await asyncio.sleep(0.05)
                    continue
                await ws.send_text(json.dumps(payload))
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(q)

    # -----------------------------------------------------------------------
    # Chat
    # -----------------------------------------------------------------------

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        _check_auth(request)
        if not body.message.strip():
            raise HTTPException(status_code=400, detail="Empty message")

        try:
            from src.communication.domain import (
                ChannelPermissionLevel,
                ChannelPlatform,
                CommunicationContext,
                IncomingMessage,
            )

            ctx = CommunicationContext(
                channel=ChannelPlatform.DESKTOP,
                sender_id=body.user_id,
                conversation_id=body.conversation_id,
                user_id=body.user_id,
                display_name="UI User",
                permissions=[ChannelPermissionLevel.ADMIN],
                session_id=body.conversation_id,
            )
            from uuid import uuid4

            msg = IncomingMessage(
                message_id=str(uuid4()),
                channel=ChannelPlatform.DESKTOP,
                sender_id=body.user_id,
                conversation_id=body.conversation_id,
                text=body.message,
                is_authenticated=True,
            )
            reply = _sage_app.friday_handler.handle_message(ctx, msg)

            bus = get_event_bus()
            bus.emit(
                EventType.CHAT_RESPONSE,
                message=reply,
                component="friday",
                status="ok",
            )
            return {"reply": reply}
        except Exception as exc:
            logger.exception("Chat error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    # -----------------------------------------------------------------------
    # Tasks
    # -----------------------------------------------------------------------

    @app.post("/api/task")
    async def submit_task(body: TaskRequest, request: Request):
        _check_auth(request)
        try:
            from uuid import uuid4
            task_id = str(uuid4())
            rec = _sage_app._state_store.create_task(
                title=body.title or body.goal or "Unnamed task",
                task_id=task_id,
            )
            bus = get_event_bus()
            bus.emit(
                EventType.TASK_CREATED,
                message=f"Task submitted: {rec.title}",
                task_id=rec.task_id,
                component="supervisor",
                status="SUBMITTED",
            )
            return {"task_id": rec.task_id, "status": rec.state.value if hasattr(rec.state, "value") else str(rec.state)}
        except Exception as exc:
            logger.exception("Task submit error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    @app.get("/api/tasks")
    async def list_tasks(request: Request, limit: int = Query(default=50)):
        _check_auth(request)
        try:
            tasks = _sage_app._state_store.list_tasks()
            # Apply limit client-side since StateStore doesn't accept it
            tasks = tasks[:limit]
            return {
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "state": t.state.value if hasattr(t.state, "value") else str(t.state),
                        "created_at": t.created_at,
                        "updated_at": t.updated_at,
                    }
                    for t in tasks
                ]
            }
        except Exception as exc:
            logger.exception("List tasks error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/task/{task_id}")
    async def get_task(task_id: str, request: Request):
        _check_auth(request)
        try:
            t = _sage_app._state_store.get_task(task_id)
            if not t:
                raise HTTPException(status_code=404, detail="Task not found")
            steps = _sage_app._state_store.list_task_steps(task_id)
            return {
                "task": {
                    "task_id": t.task_id,
                    "title": t.title,
                    "state": t.state.value if hasattr(t.state, "value") else str(t.state),
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                },
                "steps": [
                    {
                        "step_id": s.step_id,
                        "name": s.name,
                        "state": s.state.value if hasattr(s.state, "value") else str(s.state),
                        "tool_name": getattr(s, "tool_name", None),
                        "output_summary": getattr(s, "output_summary", None),
                    }
                    for s in steps
                ],
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Get task error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    @app.post("/api/task/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        _check_auth(request)
        try:
            from src.manager.models import TaskState
            _sage_app._state_store.transition_task_state(task_id, TaskState.CANCELLED)
            bus = get_event_bus()
            bus.emit(
                EventType.TASK_CANCELLED,
                message=f"Task {task_id} cancelled by UI user",
                task_id=task_id,
                component="ui",
                status="CANCELLED",
            )
            return {"status": "CANCELLED"}
        except Exception as exc:
            logger.exception("Cancel task error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    # -----------------------------------------------------------------------
    # Approvals — always validated through ApprovalStore (TrustCore backend)
    # -----------------------------------------------------------------------

    @app.get("/api/approvals")
    async def list_approvals(request: Request):
        _check_auth(request)
        pending = _sage_app.approval_store.list_pending()
        return {
            "approvals": [
                {
                    "request_id": a.request_id,
                    "task_id": a.task_id,
                    "action_name": a.action_name,
                    "action_risk_level": a.action_risk_level,
                    "target_resource": a.target_resource,
                    "parameters_summary": a.parameters_summary,
                    "expires_at": a.expires_at,
                }
                for a in pending
            ]
        }

    @app.post("/api/approval/{request_id}/approve")
    async def approve(request_id: str, body: ApprovalDecision, request: Request):
        _check_auth(request)
        try:
            rec = _sage_app.approval_store.approve_request(request_id, actor=body.actor)
            bus = get_event_bus()
            bus.emit(
                EventType.APPROVAL_GRANTED,
                message=f"Approval granted for {rec.action_name}",
                task_id=rec.task_id,
                component="trustcore",
                status="APPROVED",
                request_id=request_id,
            )
            return {"status": rec.status}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/approval/{request_id}/reject")
    async def reject(request_id: str, body: RejectionDecision, request: Request):
        _check_auth(request)
        try:
            rec = _sage_app.approval_store.reject_request(
                request_id, actor=body.actor, reason=body.reason
            )
            bus = get_event_bus()
            bus.emit(
                EventType.APPROVAL_REJECTED,
                message=f"Approval rejected for {rec.action_name}",
                task_id=rec.task_id,
                component="trustcore",
                status="REJECTED",
                request_id=request_id,
            )
            return {"status": rec.status}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # -----------------------------------------------------------------------
    # System status
    # -----------------------------------------------------------------------

    @app.get("/api/status")
    async def status(request: Request):
        _check_auth(request)
        return {
            "runtime": "running" if _sage_app.is_running else "stopped",
            "components": {
                "trustcore": "ok",
                "supervisor": "ok",
                "model_gateway": "ok" if _sage_app.model_gateway else "unavailable",
                "memory_engine": "ok" if _sage_app.memory_engine else "unavailable",
                "worker_manager": "ok" if _sage_app.worker_manager else "unavailable",
                "communication_gateway": "ok" if _sage_app.communication_gateway else "unavailable",
            },
        }

    return app
