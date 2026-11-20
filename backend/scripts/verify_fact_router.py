"""无 pip 环境下的路由层冒烟测试。

用最小桩件模拟 fastapi/pydantic，让 app.routers.fact_snapshot 可导入，
然后直接调用路由函数，验证参数搬运与 HTTP 错误语义（业务判断仍在 service）。
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_stubs() -> None:
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str) -> None:
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object):
            return lambda fn: fn

        post = get

    def Query(default=None, **_kwargs: object):  # noqa: ANN001, N802
        return default

    class Request:  # noqa: D401
        pass

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Query = Query
    fastapi.Request = Request
    sys.modules["fastapi"] = fastapi

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, **_kwargs: object):  # noqa: ANN001
        return default

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    sys.modules["pydantic"] = pydantic


_install_stubs()

from app.routers import fact_snapshot as router  # noqa: E402
from app.services.fact_snapshot import (  # noqa: E402
    DIM_TABLE,
    FACT_TABLE,
    SNAP_TABLE,
    RECORD_BASELINE,
    RECORD_REVISION,
    fact_snapshot_service as svc,
)
from app.schemas import EntryPayload  # noqa: E402
from app.store import store  # noqa: E402

# 与种子数据隔离：清空事实加工相关逻辑表，保证脚本可重复执行。
for _table in (DIM_TABLE, FACT_TABLE, SNAP_TABLE):
    store.rows(_table).clear()

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# 维表登记走路由层
r = router.register_dimension(EntryPayload(values={
    "维度类型": "地区", "维度键": "R-EAST", "维度名称": "华东",
}))
check("路由：维表登记成功", r.ok and r.entry["维度键"] == "R-EAST", r.message)
r = router.register_dimension(EntryPayload(values={
    "维度类型": "客户", "维度键": "C1", "维度名称": "老客户甲",
    "所属地区键": "R-EAST", "所属地区名称": "华东",
}))
check("路由：客户维登记成功", r.ok and r.entry["维度键"] == "C1", r.message)
r = router.register_dimension(EntryPayload(values={"维度类型": "客户", "维度键": "C1"}))
check("路由：缺字段返回 ok=False", not r.ok)

# 事实接收走路由层
r = router.ingest_fact(EntryPayload(values={
    "订单号": "SO-1", "行号": "L1", "金额": 100, "客户键": "C1",
    "记录类型": RECORD_BASELINE, "来源版本": "V1", "业务生效时刻": "2026-11-19T09:00",
}))
check("路由：确认事实接收成功", r.ok and r.entry["金额"] == 100.0, r.message)
r = router.ingest_fact(EntryPayload(values={
    "订单号": "SO-1", "行号": "L1", "金额": 120, "客户键": "C1",
    "记录类型": RECORD_REVISION, "来源版本": "V2", "业务生效时刻": "2026-11-20T10:00",
}))
check("路由：修订 V2 接收成功", r.ok, r.message)
r = router.ingest_fact(EntryPayload(values={
    "订单号": "SO-1", "行号": "L1", "金额": 90, "客户键": "C-NEW",
    "记录类型": RECORD_REVISION, "来源版本": "V3", "业务生效时刻": "2026-11-20T10:00",
}))
check("路由：修订 V3 接收成功", r.ok, r.message)
r = router.ingest_fact(EntryPayload(values={"订单号": "SO-9", "行号": "L1"}))
check("路由：缺来源版本被拒", not r.ok)
r = router.ingest_fact(EntryPayload(values={
    "订单号": "SO-9", "行号": "L1", "金额": "abc", "来源版本": "VX",
}))
check("路由：非数字金额被拒", not r.ok)

# 快照发布：非法 id、未知集市、正常发布
r = router.publish_snapshot(EntryPayload(values={"集市表id": "x"}))
check("路由：非法集市表id被拒", not r.ok)
r = router.publish_snapshot(EntryPayload(values={"集市表id": 99999}))
check("路由：未知集市表被拒", not r.ok)
r = router.publish_snapshot(EntryPayload(values={
    "集市表id": 3, "事实粒度": ["订单号", "行号"], "集市表名称": "订单行事实表",
}))
check("路由：快照发布成功", r.ok and r.entry["快照编号"] == "FSNAP-0001", r.message)
snapshot_id = r.entry["id"]

# 快照详情与 OLAP
detail = router.get_snapshot(snapshot_id)
check("路由：可打开快照详情", detail["正式金额合计"] == 100.0)
try:
    router.get_snapshot(99999)
    check("路由：未知快照抛 404", False)
except Exception as exc:  # noqa: BLE001
    check("路由：未知快照抛 404", getattr(exc, "status_code", None) == 404)

olap = router.olap_by_region(snapshot_id)
check("路由：OLAP 正式合计 100", olap["正式合计"] == 100.0)
check("路由：OLAP 待核对总额 210（120+90）", olap["待核对总额"] == 210.0)
check("路由：OLAP 总体金额守恒 310", olap["总体金额"] == 310.0)

# 列表接口
check("路由：快照列表含 1 份", router.list_snapshots()["total"] == 1)
check("路由：事实记录列表含 3 条", router.list_fact_records()["total"] == 3)
check("路由：维表列表可按类型过滤", router.list_dimensions("客户")["total"] == 1)

# 再发一版，旧版保留
r2 = router.publish_snapshot(EntryPayload(values={
    "集市表id": 3, "事实粒度": ["订单号", "行号"], "集市表名称": "订单行事实表",
}))
check("路由：第二份快照发布成功", r2.ok and r2.entry["快照编号"] == "FSNAP-0002")
check("路由：旧快照仍可复现同一汇总",
      router.olap_by_region(snapshot_id)["正式合计"] == olap["正式合计"])

# 直接验证 service 内部状态与路由结果一致
check("服务层与路由层待核对金额一致",
      svc.get_snapshot(snapshot_id)["待核对金额合计"] == 210.0)

print("\n" + ("路由冒烟测试全部通过 ✅" if not failures else f"失败 {len(failures)} 项 ❌：{failures}"))
sys.exit(1 if failures else 0)
