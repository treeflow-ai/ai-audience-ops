from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine, init_db
from .schemas import ApprovalPayload, CreateRequestPayload
from .seed import seed_synthetic_data
from .services import AudienceService, MarketingSyncError

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _decorate(request_obj):
    request_obj.intent = json.loads(request_obj.intent_json)
    request_obj.policy_checks = json.loads(request_obj.policy_json)
    request_obj.funnel = json.loads(request_obj.funnel_json)
    request_obj.retrieved_policies = json.loads(request_obj.retrieved_policy_json)
    return request_obj


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = engine or build_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        with Session(engine) as session:
            # Seeding is idempotent when student data already exists. Calling it
            # unconditionally avoids treating a partially initialized database
            # (for example, courses created but students missing) as healthy.
            seed_synthetic_data(session, count=settings.synthetic_student_count)
        yield

    app = FastAPI(
        title="AI Audience Ops",
        version="0.1.2",
        description="Governed AI audience requests for LearnDash + marketing systems.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health")
    def health():
        return {"status": "ok", "llm_provider": settings.llm_provider}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with Session(engine) as session:
            rows = AudienceService(session, settings).list_requests()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"requests": rows, "settings": settings},
        )

    @app.post("/requests")
    def create_request_form(
        text: str = Form(...),
        requested_by: str = Form("Alex Rivera — Marketing"),
        marketing_provider: str = Form("mock_mailchimp"),
    ):
        try:
            payload = CreateRequestPayload(text=text, requested_by=requested_by, marketing_provider=marketing_provider)
            with Session(engine) as session:
                obj = AudienceService(session, settings).create_request(
                    payload.text, payload.requested_by, payload.marketing_provider
                )
                request_id = obj.id
            return RedirectResponse(url=f"/requests/{request_id}", status_code=303)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/requests/{request_id}", response_class=HTMLResponse)
    def request_detail(request: Request, request_id: int):
        try:
            with Session(engine) as session:
                obj = _decorate(AudienceService(session, settings).get_request(request_id))
            return TEMPLATES.TemplateResponse(
                request=request,
                name="detail.html",
                context={"item": obj, "settings": settings},
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/requests/{request_id}/approve")
    def approve_form(request_id: int, approver: str = Form("Jane Smith")):
        try:
            with Session(engine) as session:
                AudienceService(session, settings).approve(request_id, approver)
            return RedirectResponse(url=f"/requests/{request_id}", status_code=303)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/requests/{request_id}/sync")
    def sync_form(request_id: int):
        try:
            with Session(engine) as session:
                AudienceService(session, settings).sync(request_id)
            return RedirectResponse(url=f"/requests/{request_id}", status_code=303)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MarketingSyncError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/requests")
    def api_list_requests():
        with Session(engine) as session:
            rows = AudienceService(session, settings).list_requests()
            return [
                {
                    "id": r.id,
                    "request_key": r.request_key,
                    "status": r.status,
                    "risk_level": r.risk_level,
                    "eligible_count": r.eligible_count,
                    "marketing_provider": r.marketing_provider,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    @app.post("/api/requests", status_code=201)
    def api_create_request(payload: CreateRequestPayload):
        with Session(engine) as session:
            obj = AudienceService(session, settings).create_request(
                payload.text, payload.requested_by, payload.marketing_provider
            )
            return _api_request(obj)

    @app.get("/api/requests/{request_id}")
    def api_get_request(request_id: int):
        try:
            with Session(engine) as session:
                return _api_request(AudienceService(session, settings).get_request(request_id))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/requests/{request_id}/approve")
    def api_approve(request_id: int, payload: ApprovalPayload):
        try:
            with Session(engine) as session:
                return _api_request(AudienceService(session, settings).approve(request_id, payload.approver))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/requests/{request_id}/sync")
    def api_sync(request_id: int):
        try:
            with Session(engine) as session:
                return _api_request(AudienceService(session, settings).sync(request_id))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MarketingSyncError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def _api_request(obj):
    return {
        "id": obj.id,
        "request_key": obj.request_key,
        "raw_request": obj.raw_request,
        "requested_by": obj.requested_by,
        "manager": obj.manager,
        "marketing_provider": obj.marketing_provider,
        "status": obj.status,
        "risk_level": obj.risk_level,
        "confidence": obj.confidence,
        "eligible_count": obj.eligible_count,
        "intent": json.loads(obj.intent_json),
        "policy_checks": json.loads(obj.policy_json),
        "funnel": json.loads(obj.funnel_json),
        "retrieved_policies": json.loads(obj.retrieved_policy_json),
        "approved_by": obj.approved_by,
        "external_segment_id": obj.external_segment_id,
        "sync_detail": obj.sync_detail,
        "audit_events": [
            {
                "event_type": e.event_type,
                "actor": e.actor,
                "detail": e.detail,
                "created_at": e.created_at,
            }
            for e in obj.events
        ],
    }


app = create_app()
