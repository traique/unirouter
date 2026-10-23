from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from . import config, http
from .admin import router as admin_router
from .api import error_body
from .api import router as api_router
from .providers import codex_auth, gemini_web_auth
from .state import AppState


def create_app(state: AppState | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.app.usage.start()
        app.state.app.keepalive.start()
        yield
        await app.state.app.keepalive.stop()
        await app.state.app.usage.stop()
        await http.close()
        await codex_auth.close()
        await gemini_web_auth.close()

    app = FastAPI(title="UniRouter", version=config.VERSION, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.app = state or AppState()

    def fmt_of(request: Request) -> str:
        return "anthropic" if request.url.path.endswith("/messages") else "chat"

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/admin"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return JSONResponse(error_body(fmt_of(request), exc.status_code, str(exc.detail)), status_code=exc.status_code,
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(error_body(fmt_of(request), 400, str(exc.errors())[:500]), status_code=400)

    @app.get("/")
    async def root():
        return RedirectResponse("/admin")

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        st = app.state.app
        return {"ok": True, "version": config.VERSION,
                "providers": sum(1 for p in st.store.providers.values()
                                 if p["enabled"] and st.store.provider_connections(p["id"]))}

    app.include_router(api_router)
    app.include_router(admin_router)
    return app
