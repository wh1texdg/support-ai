import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api.routes import router
from app.core.config import settings
from app.core.logging import configure_logging
from app.database.session import Session, engine
from app.services.runtime import create_runtime

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    configure_logging(settings().log_level)
    app.state.runtime = create_runtime()
    yield
    await app.state.runtime.close()
    await engine.dispose()


app = FastAPI(title="SupportAI", version="0.1.0", lifespan=lifespan)
app.include_router(router)
static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = uuid.uuid4().hex
    start = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    log.info(
        "http_request id=%s method=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        response.status_code,
        round((time.monotonic() - start) * 1000),
    )
    return response


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    log.warning("database_constraint_conflict")
    return JSONResponse(status_code=409, content={"detail": "Resource conflict"})


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    log.error("database_unavailable")
    return JSONResponse(
        status_code=503, content={"detail": "База данных временно недоступна. Повторите запрос."}
    )


@app.exception_handler(OpenAIError)
async def provider_error(request, exc):
    log.error("provider_unavailable")
    return JSONResponse(
        status_code=503, content={"detail": "LLM/embeddings provider unavailable; changes rolled back"}
    )


@app.get("/", include_in_schema=False)
async def panel():
    return FileResponse(static / "index.html")


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request):
    try:
        async with Session() as session:
            await session.execute(text("SELECT 1 FROM users LIMIT 1"))
        await request.app.state.runtime.redis.ping()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {
        "status": "ok",
        "ai_configured": bool(
            settings().polza_ai_api_key.get_secret_value() or settings().openai_api_key.get_secret_value()
        ),
    }
