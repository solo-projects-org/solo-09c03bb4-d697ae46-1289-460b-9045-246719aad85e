"""FastAPI 接口层。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import Store, get_db_path
from .service import ChainService, ServiceError

ROAD_ID_PATTERN = r"^[A-Za-z0-9_\-一-鿿]+$"


class SegmentIn(BaseModel):
    start_m: float = Field(..., description="段起点实测米数")
    end_m: float = Field(..., description="段止点实测米数")
    start_station: float = Field(..., description="段首桩号（米）")


class RoadIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"id": "S12", "name": "示范公路"}]}
    )

    id: str = Field(..., pattern=ROAD_ID_PATTERN, description="公路编号")
    name: str = Field(..., min_length=1)


class VersionIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "label": "旧表（两段桩号重号）",
                    "activate": True,
                    "segments": [
                        {"start_m": 0, "end_m": 1000, "start_station": 0},
                        {"start_m": 1000, "end_m": 2000, "start_station": 0},
                    ],
                },
                {
                    "label": "新表（重新编排桩号，实距轴不变）",
                    "activate": True,
                    "segments": [
                        {"start_m": 0, "end_m": 800, "start_station": 10000},
                        {"start_m": 800, "end_m": 1500, "start_station": 10200},
                        {"start_m": 1500, "end_m": 2500, "start_station": 10700},
                    ],
                },
            ]
        }
    )

    segments: list[SegmentIn] = Field(..., min_length=1)
    label: Optional[str] = None
    activate: bool = Field(False, description="登记后立即生效（需通过覆盖校验）")


class LocateIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "damage_no": "D-001",
                    "old_version": 1,
                    "start_station": 1000,
                    "end_station": 500,
                    "measured_length": 500,
                }
            ]
        }
    )

    damage_no: str = Field(..., min_length=1)
    old_version: int = Field(..., ge=1, description="旧桩号表版本号")
    start_station: float = Field(..., description="一端桩号（米）")
    end_station: float = Field(..., description="另一端桩号（米）")
    measured_length: float = Field(..., gt=0, description="实测长度（米）")
    tolerance_m: float = Field(0.1, ge=0, description="长度吻合容差（米），默认 0.1")


class ConfirmIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"start_m": 1000, "end_m": 1500}]}
    )

    start_m: float = Field(..., description="所选候选区段的实距起点（米）")
    end_m: float = Field(..., description="所选候选区段的实距止点（米）")


def _store() -> Store:
    return Store(get_db_path())


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = _store()
    app.state.store = store
    yield
    store.close()


app = FastAPI(
    title="公路病害桩号定位与供数服务",
    version="1.0.0",
    description=(
        "桩号表按版登记、内容固定；各版共用同一条实测路距轴。\n\n"
        "**定位**：按旧表版号把两端桩号换算为实距（分段端点也参与换算），"
        "列出长度吻合的候选区段。无解 → `pending_survey`（待补测）；"
        "多候选 → `needs_selection`（须人工选定）。\n\n"
        "**确认**：只能从候选列表中选择，确认后固定实距区段。\n\n"
        "**供数**：沿现行表分段返回桩号，并注明所用表版号。\n\n"
        "**换版**：登记新版后激活；无论确认还是换版，生效后的现行表都必须完整"
        "覆盖全部已确认区段，有缺口返回 409 并列出病害号与缺失实距。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"code": "error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)


def _service(request: Request) -> ChainService:
    return ChainService(request.app.state.store)


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/roads", status_code=201, tags=["公路"])
def create_road(body: RoadIn, request: Request) -> dict[str, Any]:
    return _service(request).create_road(body.id, body.name)


@app.get("/roads", tags=["公路"])
def list_roads(request: Request) -> list[dict[str, Any]]:
    return _service(request).list_roads()


@app.get("/roads/{road_id}", tags=["公路"])
def get_road(road_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_road(road_id)


@app.post("/roads/{road_id}/versions", status_code=201, tags=["桩号表"])
def create_version(road_id: str, body: VersionIn, request: Request) -> dict[str, Any]:
    raw = [(seg.start_m, seg.end_m, seg.start_station) for seg in body.segments]
    return _service(request).create_version(road_id, raw, label=body.label, activate=body.activate)


@app.get("/roads/{road_id}/versions", tags=["桩号表"])
def list_versions(road_id: str, request: Request) -> list[dict[str, Any]]:
    return _service(request).list_versions(road_id)


@app.get("/roads/{road_id}/versions/{version_no}", tags=["桩号表"])
def get_version(road_id: str, version_no: int, request: Request) -> dict[str, Any]:
    return _service(request).get_version(road_id, version_no)


@app.post("/roads/{road_id}/versions/{version_no}/activate", tags=["桩号表"])
def activate_version(road_id: str, version_no: int, request: Request) -> dict[str, Any]:
    return _service(request).activate_version(road_id, version_no)


@app.get("/roads/{road_id}/current", tags=["桩号表"])
def current_version(road_id: str, request: Request) -> dict[str, Any]:
    return _service(request).current_version(road_id)


@app.post("/roads/{road_id}/damages", status_code=201, tags=["病害"])
def locate_damage(road_id: str, body: LocateIn, request: Request) -> dict[str, Any]:
    return _service(request).locate(
        road_id,
        body.damage_no,
        body.old_version,
        body.start_station,
        body.end_station,
        body.measured_length,
        body.tolerance_m,
    )


@app.get("/roads/{road_id}/damages", tags=["病害"])
def list_damages(road_id: str, request: Request) -> list[dict[str, Any]]:
    return _service(request).list_damages(road_id)


@app.get("/roads/{road_id}/damages/{damage_no}", tags=["病害"])
def get_damage(road_id: str, damage_no: str, request: Request) -> dict[str, Any]:
    return _service(request).get_damage(road_id, damage_no)


@app.post("/roads/{road_id}/damages/{damage_no}/confirm", tags=["病害"])
def confirm_damage(road_id: str, damage_no: str, body: ConfirmIn, request: Request) -> dict[str, Any]:
    return _service(request).confirm(road_id, damage_no, body.start_m, body.end_m)


@app.get("/roads/{road_id}/damages/{damage_no}/supply", tags=["病害"])
def supply_damage(road_id: str, damage_no: str, request: Request) -> dict[str, Any]:
    return _service(request).get_damage(road_id, damage_no)
