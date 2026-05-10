"""
app.main
--------
FastAPI entrypoint for the C&W Board Evaluation Platform.

Run:
    uvicorn app.main:app --reload

Database schema is not created at startup. Apply migrations first:
    alembic upgrade head
"""

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.reports import router as reports_router
from app.api.evaluations import router as evaluations_router
from app.api.questions import router as questions_router
from app.api.portal import router as portal_router
from app.api.assignments import router as assignments_router

try:
    from app.api.debug import router as debug_router
except Exception:
    debug_router = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Crest & Waterfalls Board Evaluation API",
        version="0.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(reports_router, prefix="/api/v1", tags=["reports"])
    app.include_router(evaluations_router, prefix="/api/v1", tags=["evaluations"])
    app.include_router(questions_router, prefix="/api/v1", tags=["questions"])
    app.include_router(portal_router, prefix="/api/v1", tags=["portal"])
    app.include_router(assignments_router, prefix="/api/v1", tags=["assignments"])


    if debug_router is not None:
        app.include_router(debug_router, prefix="/api/v1", tags=["debug"])

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok"}

    print("\n[ROUTES REGISTERED]")
    for r in app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        name = getattr(r, "name", None)
        if path:
            print(f" - {path} | {methods} | {name}")
    print("[/ROUTES REGISTERED]\n")

    return app


app = create_app()
