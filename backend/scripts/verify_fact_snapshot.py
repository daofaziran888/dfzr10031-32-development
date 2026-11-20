"""事实快照加工端到端核对脚本（纯标准库，不依赖 pytest）。

覆盖题目场景：
- 已确认订单行 SO-1/L1 金额 100，客户 C-OLD（华东）；
- 同一业务时刻 2026-11-20T10:00 两份修订：同客户 120、未匹配新客户 C-NEW 90；
- 另构造一条维表匹配不唯一的事实，验证候选关联可追溯；
- 发布快照后再次加工生成新版本，旧快照保持不变且可复现地区汇总。

运行：cd backend && python3 -I scripts/verify_fact_snapshot.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.fact_snapshot import (
    DIM_TABLE,
    FACT_TABLE,
    SNAP_TABLE,
    RECORD_BASELINE,
    RECORD_REVISION,
    REASON_DIM_AMBIGUOUS,
    REASON_VERSION_CONFLICT,
    fact_snapshot_service as svc,
)
from app.store import store

# 与种子数据隔离：清空事实加工相关逻辑表，保证脚本可重复执行。
for _table in (DIM_TABLE, FACT_TABLE, SNAP_TABLE):
    store.rows(_table).clear()

GRAIN = ["订单号", "行号"]
MART_ID = 3  # seed 中已发布的集市表 DATA-0003
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def ingest(**values: object) -> dict:
    entry, message = svc.ingest_fact(values)  # type: ignore[arg-type]
    assert entry is not None, message
    return entry


# ---- 1. 维表登记：客户维 + 地区维 ------------------------------------------
print("== 1. 登记维表 ==")
svc.register_dimension({"维度类型": "地区", "维度键": "R-EAST", "维度名称": "华东"})
svc.register_dimension({"维度类型": "地区", "维度键": "R-NORTH", "维度名称": "华北"})
svc.register_dimension({
    "维度类型": "客户", "维度键": "C-OLD", "维度名称": "老客户甲",
    "所属地区键": "R-EAST", "所属地区名称": "华东",
})
# C-DUP 在客户维中登记两条，制造「维表匹配不唯一」。
svc.register_dimension({
    "维度类型": "客户", "维度键": "C-DUP", "维度名称": "重名客户A",
    "所属地区键": "R-EAST", "所属地区名称": "华东",
})
svc.register_dimension({
    "维度类型": "客户", "维度键": "C-DUP", "维度名称": "重名客户B",
    "所属地区键": "R-NORTH", "所属地区名称": "华北",
})
dup, msg = svc.register_dimension({"维度类型": "客户", "维度键": "C-OLD", "维度名称": "老客户甲"})
check("完全相同的维表记录被拒绝", dup is None, msg)

# ---- 2. 接收事实记录 -------------------------------------------------------
print("\n== 2. 接收确认行与同刻两份修订 ==")
ingest(订单号="SO-1", 行号="L1", 金额=100, 客户键="C-OLD",
       记录类型=RECORD_BASELINE, 来源标识="订单系统", 来源版本="V1",
       业务生效时刻="2026-11-19T09:00")
ingest(订单号="SO-1", 行号="L1", 金额=120, 客户键="C-OLD",
       记录类型=RECORD_REVISION, 来源标识="订单系统", 来源版本="V2",
       业务生效时刻="2026-11-20T10:00")
ingest(订单号="SO-1", 行号="L1", 金额=90, 客户键="C-NEW",
       记录类型=RECORD_REVISION, 来源标识="订单系统", 来源版本="V3",
       业务生效时刻="2026-11-20T10:00")
# 维表匹配不唯一的另一条事实
ingest(订单号="SO-2", 行号="L1", 金额=55, 客户键="C-DUP",
       记录类型=RECORD_BASELINE, 来源标识="订单系统", 来源版本="V4",
       业务生效时刻="2026-11-19T09:00")
retry, retry_msg = svc.ingest_fact({
    "订单号": "SO-1", "行号": "L1", "金额": 120, "客户键": "C-OLD",
    "记录类型": RECORD_REVISION, "来源版本": "V2",
})
check("重复来源版本被拒绝", retry is None, retry_msg)

# ---- 3. 发布第一份快照 -----------------------------------------------------
print("\n== 3. 发布快照 F1 ==")
snap1, msg = svc.publish_snapshot(MART_ID, grain_fields=GRAIN, mart_name="订单行事实表")
assert snap1 is not None, msg
print(f"快照 {snap1['快照编号']}：正式 {snap1['正式金额合计']} / 待核对 {snap1['待核对金额合计']}")

# 3.1 冲突行：正式快照暂留原确认 100，两份修订整体挂起
so1_facts = [r for r in snap1["事实行"] if r["订单号"] == "SO-1" and r["行号"] == "L1"]
check("SO-1/L1 在正式事实表只贡献一次金额", len(so1_facts) == 1)
check("正式快照暂留原确认金额 100", len(so1_facts) == 1 and so1_facts[0]["金额"] == 100.0)
check("正式行带来源版本 V1", so1_facts and so1_facts[0]["来源版本"] == "V1")
check("正式行关联到华东", so1_facts and so1_facts[0]["地区名称"] == "华东")

so1_recon = [r for r in snap1["待核对记录"] if r["订单号"] == "SO-1" and r["行号"] == "L1"]
check("两份同刻修订都挂入待核对", len(so1_recon) == 2)
check("挂起原因均为版本冲突", all(r["核对原因"] == REASON_VERSION_CONFLICT for r in so1_recon))
check("待核对保留 120 与 90 两笔完整金额",
      sorted(r["金额"] for r in so1_recon) == [90.0, 120.0])
check("两份挂起修订各自保留完整来源版本",
      sorted(r["来源版本"] for r in so1_recon) == ["V2", "V3"])

# 3.2 禁止拼出源端从未提交过的 120 元 + 未匹配新客户
forbidden = [
    r for r in snap1["事实行"] + snap1["待核对记录"]
    if r["金额"] == 120.0 and r.get("客户键") == "C-NEW"
]
check("任何取数都拼不出 120 元的未匹配新客户", not forbidden)
# 90 元修订必须作为完整记录（C-NEW + 90）存在，未被拆字段
v3 = next((r for r in so1_recon if r["来源版本"] == "V3"), None)
check("90 元修订保持完整记录（C-NEW+90）", v3 is not None and v3["客户键"] == "C-NEW" and v3["金额"] == 90.0)

# 3.3 维表匹配不唯一：事实可找到，候选关联一并挂出
dup_recon = [r for r in snap1["待核对记录"] if r["订单号"] == "SO-2"]
check("维表匹配不唯一进入待核对", len(dup_recon) == 1 and dup_recon[0]["核对原因"] == REASON_DIM_AMBIGUOUS)
check("不唯一时可找到对应事实及两个候选关联",
      len(dup_recon) == 1 and len(dup_recon[0]["候选关联"]) == 2)
check("候选关联含华东/华北两个地区",
      {c["所属地区名称"] for c in dup_recon[0]["候选关联"]} == {"华东", "华北"})

# ---- 4. OLAP 地区汇总 ------------------------------------------------------
print("\n== 4. OLAP 地区汇总（基于 F1）==")
olap1 = svc.olap_by_region(snap1["id"])
assert olap1 is not None
for region in olap1["地区汇总"]:
    print(region)
east = next(r for r in olap1["地区汇总"] if r["地区名称"] == "华东")
check("华东正式合计采用 100 元", east["正式金额"] == 100.0)
check("待核对总额 = 120 + 90 + 55 = 265", olap1["待核对总额"] == 265.0)
check("正式合计 = 100", olap1["正式合计"] == 100.0)
check("总体金额 = 正式 + 待核对 = 365，未匹配金额未退出总体", olap1["总体金额"] == 365.0)

# ---- 5. 再发一份新快照，旧快照必须原样保留且可复现 -------------------------
print("\n== 5. 快照版本保留与复现 ==")
# 新增一笔更晚生效、维度可唯一匹配的修订，验证非冲突时采纳完整修订
ingest(订单号="SO-3", 行号="L1", 金额=200, 客户键="C-OLD",
       记录类型=RECORD_BASELINE, 来源标识="订单系统", 来源版本="V5",
       业务生效时刻="2026-11-19T09:00")
snap2, msg = svc.publish_snapshot(MART_ID, grain_fields=GRAIN, mart_name="订单行事实表")
assert snap2 is not None, msg
check("生成新版本快照", snap2["快照编号"] != snap1["快照编号"])
check("旧快照仍保留且状态为已发布",
      svc.get_snapshot(snap1["id"]) is not None and svc.get_snapshot(snap1["id"])["状态"] == "已发布")

olap1_again = svc.olap_by_region(snap1["id"])
check("重开旧快照地区汇总与首次取数一致", olap1_again == olap1)
old_v3 = next((r for r in svc.get_snapshot(snap1["id"])["待核对记录"] if r["来源版本"] == "V3"), None)
check("旧快照仍可沿来源版本 V3 复核当时待核对的 90 元",
      old_v3 is not None and old_v3["金额"] == 90.0)

so3 = next(r for r in snap2["事实行"] if r["订单号"] == "SO-3")
check("新快照包含新事实行 200 元", so3["金额"] == 200.0 and so3["地区名称"] == "华东")
olap2 = svc.olap_by_region(snap2["id"])
east2 = next(r for r in olap2["地区汇总"] if r["地区名称"] == "华东")
check("新快照华东正式合计 = 100 + 200 = 300", east2["正式金额"] == 300.0)

# ---- 6. 非冲突的后续修订：整条采纳，不混字段 -------------------------------
print("\n== 6. 更晚时刻的单一修订整体替换确认行 ==")
ingest(订单号="SO-3", 行号="L1", 金额=180, 客户键="C-OLD",
       记录类型=RECORD_REVISION, 来源标识="订单系统", 来源版本="V6",
       业务生效时刻="2026-11-21T08:00")
snap3, msg = svc.publish_snapshot(MART_ID, grain_fields=GRAIN, mart_name="订单行事实表")
assert snap3 is not None
so3_v2 = next(r for r in snap3["事实行"] if r["订单号"] == "SO-3")
check("采纳完整修订 V6 金额 180", so3_v2["金额"] == 180.0 and so3_v2["来源版本"] == "V6")
check("修订行仍是一条完整事实（客户未被改写）", so3_v2["客户键"] == "C-OLD")

# ---- 7. 冲突行上的其他修订也不得悄悄退出总体 -------------------------------
print("\n== 7. 同刻冲突叠加另一时刻修订：全部未采纳修订均挂起 ==")
ingest(订单号="SO-4", 行号="L1", 金额=40, 客户键="C-OLD",
       记录类型=RECORD_BASELINE, 来源标识="订单系统", 来源版本="V7",
       业务生效时刻="2026-11-19T09:00")
ingest(订单号="SO-4", 行号="L1", 金额=45, 客户键="C-OLD",
       记录类型=RECORD_REVISION, 来源版本="V8",
       业务生效时刻="2026-11-20T10:00")
ingest(订单号="SO-4", 行号="L1", 金额=46, 客户键="C-OLD",
       记录类型=RECORD_REVISION, 来源版本="V9",
       业务生效时刻="2026-11-20T10:00")
ingest(订单号="SO-4", 行号="L1", 金额=47, 客户键="C-OLD",
       记录类型=RECORD_REVISION, 来源版本="V10",
       业务生效时刻="2026-11-21T08:00")
snap4, msg = svc.publish_snapshot(MART_ID, grain_fields=GRAIN, mart_name="订单行事实表")
assert snap4 is not None
so4_fact = [r for r in snap4["事实行"] if r["订单号"] == "SO-4"]
so4_recon = [r for r in snap4["待核对记录"] if r["订单号"] == "SO-4"]
check("SO-4 正式行暂留确认 40", len(so4_fact) == 1 and so4_fact[0]["金额"] == 40.0)
check("三份修订全部进入待核对（45/46/47 不丢账）",
      sorted(r["金额"] for r in so4_recon) == [45.0, 46.0, 47.0])
check("SO-4 总体守恒 40+45+46+47=178",
      so4_fact[0]["金额"] + sum(r["金额"] for r in so4_recon) == 178.0)

print("\n" + ("全部核对通过 ✅" if not failures else f"存在 {len(failures)} 项失败 ❌：{failures}"))
sys.exit(1 if failures else 0)
