"""Loopback FastAPI surface for cached tourism AI insights."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Literal

import duckdb
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from westbusan.river_regulation.heritage import (
    HeritageCriteriaCatalogue,
    HeritageProject,
    load_heritage_catalogue,
    unavailable_heritage_decision,
)
from westbusan.river_regulation.parcel import (
    NakdongParcelCatalogue,
    catalogue_unavailable_review,
    load_nakdong_parcel_catalogue,
    pnu_required_review,
)
from westbusan.river_regulation.vworld import (
    VWorldRegulationClient,
    unavailable_review,
)
from westbusan.tourism_ai.cache import (
    ClientCooldownExceeded,
    DailyLimitExceeded,
    InsightCache,
)
from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.models import (
    ComprehensiveReportRequest,
    EvidenceMetric,
    InsightRequest,
    MapSelection,
    ModelInsight,
    ParcelGeocodeRequest,
    ParcelGeocodeResponse,
    VacantAddressAnalysisRequest,
    VacantAddressAnalysisResponse,
)
from westbusan.tourism_ai.openai_client import OpenAIResponsesClient
from westbusan.tourism_ai.report_metrics import (
    ReportEvidenceCatalogue,
    load_report_evidence,
)
from westbusan.tourism_ai.report_models import ModelComprehensiveReport
from westbusan.tourism_ai.report_service import (
    ComprehensiveReportGenerator,
    ComprehensiveReportService,
)
from westbusan.tourism_ai.service import InsightGenerator, InsightService
from westbusan.tourism_ai.vworld_proxy import (
    VWorldBasemapError,
    VWorldBasemapProxy,
    VWorldGeocodeProxy,
    VWorldTileProxy,
)
from westbusan.vacant_house.address_analysis import (
    AddressAnalysisCatalogue,
    ResolvedParcel,
    analyse_address,
    load_address_catalogue,
)
from westbusan.vacant_house.cadastral import VWorldCadastralClient


class _Generator(InsightGenerator):
    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: MapSelection | None,
    ) -> ModelInsight:
        raise NotImplementedError


class _ReportGenerator(ComprehensiveReportGenerator):
    def generate_report(
        self, catalogue: ReportEvidenceCatalogue
    ) -> ModelComprehensiveReport:
        del catalogue
        raise NotImplementedError


def create_app(
    settings: TourismAISettings,
    *,
    generator: InsightGenerator | None = None,
    report_generator: ComprehensiveReportGenerator | None = None,
    report_catalogue: ReportEvidenceCatalogue | None = None,
    vworld_client: httpx.Client | None = None,
    vacant_catalogue: AddressAnalysisCatalogue | None = None,
    heritage_catalogue: HeritageCriteriaCatalogue | None = None,
    parcel_catalogue: NakdongParcelCatalogue | None = None,
) -> FastAPI:
    """Create the isolated application with explicit dependencies."""

    openai_client: OpenAIResponsesClient | None = None
    if generator is None:
        if settings.openai_api_key is None:
            generator = _Generator()
        else:
            openai_client = OpenAIResponsesClient(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.tourism_ai_model,
                max_output_tokens=settings.tourism_ai_max_output_tokens,
            )
            generator = openai_client
    if report_generator is None:
        if openai_client is not None:
            report_generator = openai_client
        elif isinstance(generator, OpenAIResponsesClient):
            report_generator = generator
        else:
            report_generator = _ReportGenerator()
    service = InsightService(
        data_path=settings.tourism_ai_data_path,
        generator=generator,
        model=settings.tourism_ai_model,
        prompt_version=settings.tourism_ai_prompt_version,
    )
    if report_catalogue is None:
        report_catalogue = load_report_evidence(
            data_path=settings.tourism_ai_data_path,
            db_path=(
                settings.tourism_ai_report_db_path
                or settings.tourism_ai_vacant_db_path
            ),
        )
    report_service = ComprehensiveReportService(
        generator=report_generator,
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
    vworld: VWorldBasemapProxy | None = None
    vworld_tiles: VWorldTileProxy | None = None
    vworld_geocoder: VWorldGeocodeProxy | None = None
    cadastral: VWorldCadastralClient | None = None
    regulations: VWorldRegulationClient | None = None
    if vacant_catalogue is None and settings.tourism_ai_vacant_db_path is not None:
        with duckdb.connect(
            str(settings.tourism_ai_vacant_db_path), read_only=True
        ) as connection:
            vacant_catalogue = load_address_catalogue(connection)
    regulation_db_path = (
        settings.tourism_ai_regulation_db_path
        or settings.tourism_ai_report_db_path
        or settings.tourism_ai_vacant_db_path
    )
    if heritage_catalogue is None and regulation_db_path is not None:
        try:
            with duckdb.connect(str(regulation_db_path), read_only=True) as connection:
                heritage_catalogue = load_heritage_catalogue(connection)
        except duckdb.Error:
            heritage_catalogue = None
    if parcel_catalogue is None and regulation_db_path is not None:
        try:
            with duckdb.connect(str(regulation_db_path), read_only=True) as connection:
                parcel_catalogue = load_nakdong_parcel_catalogue(connection)
        except duckdb.Error:
            parcel_catalogue = None
    if settings.vworld_api_key is not None:
        upstream = vworld_client or httpx.Client(timeout=15.0)
        vworld = VWorldBasemapProxy(
            api_key=settings.vworld_api_key.get_secret_value(),
            client=upstream,
        )
        vworld_tiles = VWorldTileProxy(
            api_key=settings.vworld_api_key.get_secret_value(),
            client=upstream,
        )
        vworld_geocoder = VWorldGeocodeProxy(
            api_key=settings.vworld_api_key.get_secret_value(),
            client=upstream,
        )
        cadastral = VWorldCadastralClient(
            api_key=settings.vworld_api_key.get_secret_value(),
            domain=settings.tourism_ai_vworld_domain,
            client=upstream,
        )
        regulations = VWorldRegulationClient(
            api_key=settings.vworld_api_key.get_secret_value(),
            domain=settings.tourism_ai_vworld_domain,
            client=upstream,
            max_workers=8,
        )

    @app.middleware("http")
    async def enforce_request_boundary(request: Request, call_next: Any) -> Any:
        if request.method == "POST" and request.url.path in {
            "/insights",
            "/report",
            "/vworld/geocode",
            "/vacant/address-analysis",
        }:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse(status_code=415, content={"detail": "json_required"})
            body = await request.body()
            if len(body) > 2048:
                return JSONResponse(status_code=413, content={"detail": "body_too_large"})
        return await call_next(request)

    @app.post("/vworld/geocode")
    def vworld_geocode(payload: ParcelGeocodeRequest) -> dict[str, object]:
        if vworld_geocoder is None:
            raise HTTPException(status_code=503, detail="vworld_unavailable")
        result = vworld_geocoder.resolve(payload.address)
        return ParcelGeocodeResponse(
            status=result.status,  # type: ignore[arg-type]
            longitude=result.longitude,
            latitude=result.latitude,
            district=result.district,
            crs=result.crs,
            pnu=result.pnu,
        ).model_dump(mode="json")

    @app.post("/vacant/address-analysis")
    def vacant_address_analysis(
        payload: VacantAddressAnalysisRequest,
    ) -> dict[str, object]:
        if vacant_catalogue is None:
            raise HTTPException(status_code=503, detail="vacant_catalogue_unavailable")
        if vworld_geocoder is None or cadastral is None:
            raise HTTPException(status_code=503, detail="vworld_unavailable")
        geocode = vworld_geocoder.resolve(payload.address)
        parcel: ResolvedParcel | None = None
        if geocode.status == "matched" and geocode.pnu is not None:
            evidence = cadastral.fetch(geocode.pnu)
            if evidence.status == "matched" and evidence.geometry is not None:
                parcel = ResolvedParcel(geocode.pnu, evidence.geometry)
        result = analyse_address(
            vacant_catalogue,
            address=payload.address,
            parcel=parcel,
        )
        return VacantAddressAnalysisResponse(
            **asdict(result),
        ).model_dump(mode="json")

    @app.get("/healthz")
    def health() -> dict[str, bool | str]:
        return {
            "status": "ok",
            "data_ready": settings.tourism_ai_data_path.is_file(),
        }

    @app.get("/regulations/point")
    def regulation_point(
        longitude: float,
        latitude: float,
        activity: str,
        river_zone: str,
        height_m: float | None = None,
        roof_type: Literal["flat", "sloped", "unknown"] = "unknown",
        pnu: str | None = None,
    ) -> JSONResponse:
        """Return a cumulative, non-legal point screen for fixed official layers."""
        if pnu is not None and re.fullmatch(r"\d{19}", pnu) is None:
            raise HTTPException(status_code=422, detail="invalid_pnu")
        try:
            if regulations is None:
                review = unavailable_review(
                    activity=activity,
                    river_zone=river_zone,
                )
            else:
                review = regulations.review_point(
                    longitude=longitude,
                    latitude=latitude,
                    activity=activity,
                    river_zone=river_zone,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            heritage = (
                heritage_catalogue.review_point(
                    longitude=longitude,
                    latitude=latitude,
                    project=HeritageProject(
                        activity=activity,
                        height_m=height_m,
                        roof_type=roof_type,
                    ),
                )
                if heritage_catalogue is not None
                else unavailable_heritage_decision()
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if pnu is None:
            parcel_planning = pnu_required_review()
        elif parcel_catalogue is None:
            parcel_planning = catalogue_unavailable_review(pnu)
        else:
            try:
                parcel_planning = parcel_catalogue.review_pnu(
                    pnu=pnu,
                    activity=activity,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        content = review.as_public_dict()
        heritage_public = heritage.as_public_dict()
        content["heritage_criteria"] = heritage_public
        content["parcel_planning"] = parcel_planning.as_public_dict()
        feature_collection = content.get("feature_collection")
        heritage_collection = heritage_public.get("feature_collection")
        if isinstance(feature_collection, dict) and isinstance(
            heritage_collection, dict
        ):
            features = feature_collection.get("features")
            heritage_features = heritage_collection.get("features")
            if isinstance(features, list) and isinstance(heritage_features, list):
                features.extend(heritage_features)
        return JSONResponse(
            content=content,
            headers={"Cache-Control": "private, max-age=300"},
        )

    @app.get("/vworld/base.png", response_class=Response)
    def vworld_basemap() -> Response:
        if vworld is None:
            raise HTTPException(status_code=503, detail="vworld_unavailable")
        try:
            payload = vworld.fetch()
        except VWorldBasemapError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return Response(
            content=payload,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get(
        "/vworld/tiles/{zoom}/{column}/{row}.png",
        response_class=Response,
    )
    def vworld_tile(zoom: int, column: int, row: int) -> Response:
        if vworld_tiles is None:
            raise HTTPException(status_code=503, detail="vworld_unavailable")
        try:
            payload = vworld_tiles.fetch(zoom=zoom, column=column, row=row)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except VWorldBasemapError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return Response(
            content=payload,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

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

    @app.post("/report")
    def report(
        payload: ComprehensiveReportRequest, request: Request
    ) -> dict[str, object]:
        del payload
        client_id = request.client.host if request.client else "unknown"
        try:
            response = cache.get_or_generate_report(
                publication_identity=dict(report_catalogue.publication_identity),
                model=settings.tourism_ai_model,
                prompt_version=settings.tourism_ai_prompt_version,
                client_id=client_id,
                generate=lambda: report_service.generate(report_catalogue),
            )
        except ClientCooldownExceeded as error:
            raise HTTPException(status_code=429, detail="generation_cooldown") from error
        except DailyLimitExceeded:
            response = report_service.fallback(report_catalogue)
        return response.model_dump(mode="json")

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn factory that reads systemd-provided environment variables."""

    return create_app(TourismAISettings())
