from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cloud_agent.config import Settings, get_settings
from cloud_agent.db import init_db, make_engine, require_schema, sessions
from cloud_agent.queue import QueueError, TaskQueue


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Cloud Agent", version="0.1.0")
    engine = make_engine(settings)
    if settings.environment == "production":
        require_schema(engine)
    else:
        init_db(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions(engine)
    app.state.queue = TaskQueue(app.state.sessions, settings)

    from cloud_agent import api, auth

    app.include_router(auth.router)
    app.include_router(api.router)

    @app.exception_handler(QueueError)
    async def queue_error(request: Request, error: QueueError):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "application/json required"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 131_072:
                    return JSONResponse({"detail": "Request body exceeds limit"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static / "index.html")

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        forwarded_allow_ips=settings.trusted_proxies,
    )
