"""数据集市事实快照接口。

挂在数据集市前缀下（/api/data_mart/fact/...），提供维表登记、事实记录接收、
快照发布与基于快照的 OLAP 地区汇总。路由层只做参数搬运，业务判断都在 service。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, EntryPayload
from app.services.fact_snapshot import fact_snapshot_service

router = APIRouter(prefix="/api/data_mart/fact", tags=["数据集市-事实快照"])


@router.get("/dimensions")
def list_dimensions(维度类型: str | None = None) -> dict[str, Any]:
    """列出已登记维表记录，可按客户/地区类型过滤。"""
    items = fact_snapshot_service.list_dimensions(维度类型)
    return {"items": items, "total": len(items)}


@router.post("/dimensions", response_model=ActionResult)
def register_dimension(payload: EntryPayload) -> ActionResult:
    """登记一条客户维或地区维记录；重复维度键会被拦下。"""
    entry, message = fact_snapshot_service.register_dimension(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/records")
def list_fact_records() -> dict[str, Any]:
    """列出接收到的全部原始事实记录（确认与修订），便于沿来源版本追溯。"""
    items = fact_snapshot_service.list_facts()
    return {"items": items, "total": len(items)}


@router.post("/records", response_model=ActionResult)
def ingest_fact(payload: EntryPayload) -> ActionResult:
    """接收一条带订单号、行号、金额的事实记录；缺粒度字段或来源版本时说明原因。"""
    entry, message = fact_snapshot_service.ingest_fact(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/snapshots")
def list_snapshots(集市表id: int | None = None) -> dict[str, Any]:
    """列出已发布快照；新版发布后旧快照仍在，可重新打开复核。"""
    items = fact_snapshot_service.list_snapshots(集市表id)
    return {"items": items, "total": len(items)}


@router.post("/snapshots", response_model=ActionResult)
def publish_snapshot(payload: EntryPayload) -> ActionResult:
    """按集市表声明的行粒度归并修订、关联维度，发布一份不可变事实表快照。"""
    raw_mart_id = payload.values.get("集市表id")
    try:
        mart_id = int(raw_mart_id)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="集市表id 缺失或不是整数")
    grain_fields = payload.values.get("事实粒度")
    if grain_fields is not None and not isinstance(grain_fields, list):
        return ActionResult(ok=False, message="事实粒度需以字符串数组传入，例如 ['订单号','行号']")
    snapshot, message = fact_snapshot_service.publish_snapshot(
        mart_id,
        grain_fields=grain_fields,
        mart_name=str(payload.values.get("集市表名称") or ""),
    )
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int) -> dict[str, Any]:
    """打开一份快照，查看正式事实行、待核对记录及其来源版本。"""
    snapshot = fact_snapshot_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"事实快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/snapshots/{snapshot_id}/olap")
def olap_by_region(snapshot_id: int) -> dict[str, Any]:
    """基于指定快照计算各地区正式金额与待核对总额；同一快照多次查询结果一致。"""
    result = fact_snapshot_service.olap_by_region(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"事实快照 {snapshot_id} 不存在")
    return result
