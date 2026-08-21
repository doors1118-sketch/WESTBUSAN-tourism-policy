"""Loopback FastAPI surface for cached tourism AI insights."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from westbusan.tourism_ai.cache import (
    ClientCooldownExceeded,
    DailyLimitExceeded,
    InsightCache,
)
from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.models import EvidenceMetric, InsightRequest, ModelInsight
from westbusan.tourism_ai.openai_client import OpenAIResponsesClient
from westbusan.tourism_ai.service import InsightGenerator, InsightService


class _Generator(InsightGenerator):
    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
    ) -> ModelInsight:
        raise NotImplementedError


def create_app(
    settings: TourismAISettings,
    *,
    generator: InsightGenerator | None = None,
) -> FastAPI:
    """Create the isolated application with explicit dependencies."""

    if generator is None:
        generator = OpenAIResponsesClient(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.tourism_ai_model,
            max_output_tokens=settings.tourism_ai_max_output_tokens,
        )
    service = InsightService(
        data_path=settings.tourism_ai_data_path,
        generator=generator,
        model=settings.tourism_ai_model,
        prompt_version=settings.tourism_ai_prompt_version,
    )
    cache = InsightCache(
        root=settings.tourism_ai_cache_dir,
        daily_limit=settings.tourism_ai_daily_limit,
        cooldown_seconds=settings.tourism_ai_client_cooldown_seconds,
    )
    app = FastAPI(
        title="West Busan Tourism AI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def enforce_request_boundary(request: Request, call_next: Any) -> Any:
        if request.url.path == "/insights":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse(status_code=415, content={"detail": "json_required"})
            body = await request.body()
            if len(body) > 2048:
                return JSONResponse(status_code=413, content={"detail": "body_too_large"})
        return await call_next(request)

    @app.get("/healthz")
    def health() -> dict[str, bool | str]:
        return {
            "status": "ok",
            "data_ready": settings.tourism_ai_data_path.is_file(),
        }

    @app.post("/insights")
    def insights(payload: InsightRequest, request: Request) -> dict[str, object]:
        client_id = request.client.host if request.client else "unknown"
        try:
            response = cache.get_or_generate(
                request=payload,
                model=settings.tourism_ai_model,
                prompt_version=settings.tourism_ai_prompt_version,
                client_id=client_id,
                generate=lambda: service.generate(payload),
            )
        except ClientCooldownExceeded as error:
            raise HTTPException(status_code=429, detail="generation_cooldown") from error
        except DailyLimitExceeded:
            response = service.fallback(payload)
        return response.model_dump(mode="json")

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn factory that reads systemd-provided environment variables."""

    return create_app(TourismAISettings())
