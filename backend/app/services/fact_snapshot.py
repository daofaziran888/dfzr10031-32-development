"""数据集市事实快照加工。

把带订单号、行号、金额的事实记录，按集市表声明的行粒度归并修订、关联维度，
生成带来源追溯的事实表快照与待核对记录。

设计约束（对应业务口径）：

1. 一个已确认的订单行在一份可用快照中只能贡献一次金额——快照事实表按行粒度
   （订单号+行号）唯一，归并后每个粒度键至多一条正式事实。
2. 修订只能以「整条记录」为单位生效，禁止把两份修订的字段混取成源端从未提交
   过的订单行。
3. 同一订单行的两份修订业务生效时刻相同即构成版本冲突：两份修订整体挂入待核对，
   正式快照暂留原确认行（可追溯规则下没有任何一条完整修订可被采纳）。
4. 维表匹配不唯一或未匹配时，事实与候选关联都进待核对，金额不进正式合计、
   但仍然计入待核对总额，不会悄悄退出总体。
5. 快照一经发布即冻结，后续加工只生成新版本快照，旧快照原样保留，可复现当时
   的地区汇总并沿来源版本复核当时的待核对金额。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.store import store

# ---- 内存仓库中的逻辑表名 -------------------------------------------------
DIM_TABLE = "fact_dimensions"     # 维表登记（客户维、地区维）
FACT_TABLE = "fact_records"       # 接收到的原始事实记录（确认 / 修订）
SNAP_TABLE = "fact_snapshots"     # 已发布的不可变事实表快照

# ---- 事实记录类型与待核对原因 ---------------------------------------------
RECORD_BASELINE = "确认"
RECORD_REVISION = "修订"

REASON_VERSION_CONFLICT = "版本冲突"
REASON_DIM_AMBIGUOUS = "维表匹配不唯一"
REASON_DIM_NOT_FOUND = "维表未匹配"

RECONCILE_PENDING = "待核对"
SNAPSHOT_PUBLISHED = "已发布"

UNMATCHED_REGION = "未匹配地区"


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _grain_key(record: dict[str, Any], grain_fields: list[str]) -> tuple[str, ...]:
    return tuple(str(record.get(field) or "") for field in grain_fields)


class FactSnapshotService:
    """事实登记、维度关联、修订归并、快照发布与 OLAP 汇总的业务规则入口。"""

    # ---- 维表登记 ---------------------------------------------------------
    def register_dimension(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        dim_type = str(values.get("维度类型") or "").strip()
        dim_key = str(values.get("维度键") or "").strip()
        dim_name = str(values.get("维度名称") or "").strip()
        if not dim_type or not dim_key or not dim_name:
            return None, "维表登记缺少必填字段：维度类型、维度键、维度名称"
        rows = store.rows(DIM_TABLE)
        # 同一维度键允许多条记录（这正是「维表匹配不唯一」的来源），
        # 只拦维度键与名称完全相同的重复登记，避免脏数据。
        if any(
            str(row.get("维度类型")) == dim_type
            and str(row.get("维度键")) == dim_key
            and str(row.get("维度名称")) == dim_name
            for row in rows
        ):
            return None, f"{dim_type}维中已存在「{dim_key}/{dim_name}」，不能重复登记"
        entry = {
            "id": _next_id(rows),
            "维度类型": dim_type,       # 客户 / 地区
            "维度键": dim_key,
            "维度名称": dim_name,
            "所属地区键": str(values.get("所属地区键") or "").strip(),
            "所属地区名称": str(values.get("所属地区名称") or "").strip(),
        }
        rows.append(entry)
        return entry, "维表记录已登记"

    def list_dimensions(self, dim_type: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(DIM_TABLE)
        if dim_type:
            rows = [row for row in rows if str(row.get("维度类型")) == dim_type]
        return rows

    def _customer_candidates(self, customer_key: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(DIM_TABLE)
            if str(row.get("维度类型")) == "客户" and str(row.get("维度键")) == customer_key
        ]

    def _region_name(self, region_key: str) -> str:
        for row in store.rows(DIM_TABLE):
            if str(row.get("维度类型")) == "地区" and str(row.get("维度键")) == region_key:
                return str(row.get("维度名称"))
        return region_key or UNMATCHED_REGION

    def _match_customer(self, record: dict[str, Any]) -> dict[str, Any]:
        """按客户键关联客户维表，返回匹配结论与候选关联。"""
        customer_key = str(record.get("客户键") or "").strip()
        candidates = self._customer_candidates(customer_key)
        candidate_brief = [
            {
                "维度id": row["id"],
                "维度键": row["维度键"],
                "维度名称": row["维度名称"],
                "所属地区键": row.get("所属地区键", ""),
                "所属地区名称": row.get("所属地区名称", ""),
            }
            for row in candidates
        ]
        if len(candidates) == 1:
            dim = candidates[0]
            region_key = str(dim.get("所属地区键") or "")
            return {
                "matched": True,
                "ambiguous": False,
                "reason": "",
                "客户键": customer_key,
                "客户名称": dim.get("维度名称", ""),
                "地区键": region_key,
                "地区名称": dim.get("所属地区名称") or self._region_name(region_key),
                "候选关联": candidate_brief,
            }
        if len(candidates) > 1:
            return {
                "matched": False,
                "ambiguous": True,
                "reason": REASON_DIM_AMBIGUOUS,
                "客户键": customer_key,
                "客户名称": "",
                "地区键": "",
                "地区名称": UNMATCHED_REGION,
                "候选关联": candidate_brief,
            }
        return {
            "matched": False,
            "ambiguous": False,
            "reason": REASON_DIM_NOT_FOUND,
            "客户键": customer_key,
            "客户名称": "",
            "地区键": "",
            "地区名称": UNMATCHED_REGION,
            "候选关联": [],
        }

    # ---- 事实记录登记 -----------------------------------------------------
    def ingest_fact(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        order_no = str(values.get("订单号") or "").strip()
        line_no = str(values.get("行号") or "").strip()
        if not order_no or not line_no:
            return None, "事实记录缺少行粒度字段：订单号、行号"
        amount_raw = values.get("金额")
        try:
            amount = round(float(amount_raw), 2)
        except (TypeError, ValueError):
            return None, "事实记录的金额必须是数字"
        record_type = str(values.get("记录类型") or RECORD_BASELINE).strip()
        if record_type not in (RECORD_BASELINE, RECORD_REVISION):
            return None, "记录类型只允许「确认」或「修订」"
        source = str(values.get("来源标识") or "订单系统").strip()
        source_version = str(values.get("来源版本") or "").strip()
        if not source_version:
            return None, "事实记录缺少来源版本，无法做来源追溯"
        rows = store.rows(FACT_TABLE)
        if any(str(row.get("来源版本")) == source_version for row in rows):
            return None, f"来源版本「{source_version}」已登记，不能重复接收同一版本"
        entry = {
            "id": _next_id(rows),
            "订单号": order_no,
            "行号": line_no,
            "金额": amount,
            "客户键": str(values.get("客户键") or "").strip(),
            "记录类型": record_type,
            "来源标识": source,
            "来源版本": source_version,
            "业务生效时刻": str(values.get("业务生效时刻") or "").strip(),
            # 接收序号用于同一生效时刻的稳定排序；同刻冲突不依赖它选主，只做追溯定位。
            "接收序号": len(rows) + 1,
            "挂起": False,
            "挂起原因": "",
        }
        rows.append(entry)
        return entry, f"事实记录已接收（{record_type}，来源版本 {source_version}）"

    def list_facts(self) -> list[dict[str, Any]]:
        return store.rows(FACT_TABLE)

    # ---- 修订归并 + 维度关联 + 快照生成 -----------------------------------
    def build_snapshot(self, mart_id: int, mart_name: str, grain_fields: list[str]) -> dict[str, Any]:
        """按声明的行粒度归并事实记录，生成一份事实表快照（含待核对记录）。"""
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for record in store.rows(FACT_TABLE):
            groups[_grain_key(record, grain_fields)].append(record)

        fact_rows: list[dict[str, Any]] = []
        reconciliations: list[dict[str, Any]] = []

        for key, records in groups.items():
            baselines = [r for r in records if r["记录类型"] == RECORD_BASELINE]
            revisions = [r for r in records if r["记录类型"] == RECORD_REVISION]
            baseline = baselines[0] if baselines else None

            # 同一业务生效时刻出现多份修订 => 版本冲突，整组挂起，不采纳任何一条。
            conflict_versions: set[str] = set()
            effective_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for rev in revisions:
                effective_groups[str(rev.get("业务生效时刻") or "")].append(rev)
            for effective_at, same_time_revs in effective_groups.items():
                if effective_at and len(same_time_revs) > 1:
                    for rev in same_time_revs:
                        conflict_versions.add(str(rev["来源版本"]))

            # 正式候选：有冲突时暂留原确认行；否则取业务生效时刻最新的完整修订，
            # 修订整体替换确认行，绝不跨记录拼字段。
            official: dict[str, Any] | None = None
            if conflict_versions:
                official = baseline
            elif revisions:
                latest = sorted(
                    revisions,
                    key=lambda r: (str(r.get("业务生效时刻") or ""), int(r.get("接收序号", 0))),
                )[-1]
                official = latest
            else:
                official = baseline

            # 挂起的修订逐条进入待核对，保留完整来源与金额：同刻冲突的修订标明
            # 版本冲突；该行其余未被采纳的修订同样挂起，避免任何记录悄悄退出总体。
            adopted_id = official.get("id") if official is not None else None
            for rev in revisions:
                if rev.get("id") == adopted_id:
                    continue
                if str(rev["来源版本"]) in conflict_versions:
                    reconciliations.append(self._recon_row(
                        rev, grain_fields, REASON_VERSION_CONFLICT,
                        note="同一业务生效时刻存在多份修订，两份修订整体挂起，正式快照暂留原确认行",
                    ))
                else:
                    reconciliations.append(self._recon_row(
                        rev, grain_fields, REASON_VERSION_CONFLICT,
                        note="该行存在未解决的同刻版本冲突，本修订一并挂起，冲突解决前不得采纳",
                    ))

            if official is None:
                continue

            match = self._match_customer(official)
            if match["matched"]:
                fact_rows.append(self._fact_row(official, grain_fields, match))
            else:
                # 维度关联不上（未匹配/不唯一）：金额不进正式合计，但进待核对总额。
                reconciliations.append(
                    self._recon_row(official, grain_fields, match["reason"], match=match)
                )

        # 一个已确认订单行在一份快照中只能贡献一次金额：粒度键唯一。
        seen_keys = [tuple(row["粒度键"]) for row in fact_rows]
        if len(seen_keys) != len(set(seen_keys)):
            raise RuntimeError("快照事实表出现重复粒度键，违反一行一金额约束")

        snapshot_no = _next_id(store.rows(SNAP_TABLE))
        snapshot = {
            "id": snapshot_no,
            "快照编号": f"FSNAP-{snapshot_no:04d}",
            "集市表id": mart_id,
            "集市表名称": mart_name,
            "事实粒度": list(grain_fields),
            "状态": SNAPSHOT_PUBLISHED,
            "事实行数": len(fact_rows),
            "待核对条数": len(reconciliations),
            "正式金额合计": round(sum(row["金额"] for row in fact_rows), 2),
            "待核对金额合计": round(sum(row["金额"] for row in reconciliations), 2),
            "事实行": fact_rows,
            "待核对记录": reconciliations,
        }
        return snapshot

    def publish_snapshot(
        self,
        mart_id: int,
        grain_fields: list[str] | None = None,
        mart_name: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        mart = store.find("data_mart", mart_id)
        if mart is None:
            return None, f"集市表 {mart_id} 不存在或已归档"
        fields = grain_fields or self._declared_grain_fields(mart)
        if not fields:
            return None, "集市表尚未登记事实粒度，无法按行粒度归并事实记录"
        name = mart_name or str(mart.get("表名称") or "")
        snapshot = self.build_snapshot(mart_id, name, fields)
        store.rows(SNAP_TABLE).append(snapshot)
        return snapshot, f"事实表快照 {snapshot['快照编号']} 已发布并冻结，旧快照保持不变"

    def _declared_grain_fields(self, mart: dict[str, Any]) -> list[str]:
        raw = str(mart.get("事实粒度") or "").strip()
        if not raw:
            return []
        for sep in ("，", ",", "、", "|", "/"):
            raw = raw.replace(sep, ",")
        fields = [part.strip() for part in raw.split(",") if part.strip()]
        # 登记的是自然语言粒度描述时，回退到订单行标准粒度。
        known = {"订单号", "行号"}
        filtered = [f for f in fields if f in known]
        return filtered or ["订单号", "行号"]

    def list_snapshots(self, mart_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SNAP_TABLE)
        if mart_id is not None:
            rows = [row for row in rows if int(row.get("集市表id", 0)) == mart_id]
        return rows

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(SNAP_TABLE, snapshot_id)

    # ---- OLAP：基于快照计算地区金额与待核对总额 ---------------------------
    def olap_by_region(self, snapshot_id: int) -> dict[str, Any] | None:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return None
        region_map: dict[str, dict[str, float]] = defaultdict(lambda: {"正式金额": 0.0, "待核对金额": 0.0})
        for row in snapshot["事实行"]:
            region_map[str(row["地区名称"])]["正式金额"] += float(row["金额"])
        for row in snapshot["待核对记录"]:
            region = str(row.get("地区名称") or UNMATCHED_REGION)
            region_map[region]["待核对金额"] += float(row["金额"])

        regions = [
            {
                "地区名称": region,
                "正式金额": round(amounts["正式金额"], 2),
                "待核对金额": round(amounts["待核对金额"], 2),
                "总体金额": round(amounts["正式金额"] + amounts["待核对金额"], 2),
            }
            for region, amounts in sorted(region_map.items())
        ]
        official_total = round(sum(item["正式金额"] for item in regions), 2)
        pending_total = round(sum(item["待核对金额"] for item in regions), 2)
        return {
            "快照编号": snapshot["快照编号"],
            "集市表名称": snapshot["集市表名称"],
            "状态": snapshot["状态"],
            "地区汇总": regions,
            "正式合计": official_total,
            "待核对总额": pending_total,
            "总体金额": round(official_total + pending_total, 2),
            "口径说明": "待核对金额不计入正式合计，但保留在总体金额中，不会悄悄退出总体",
        }

    # ---- 行结构拼装 -------------------------------------------------------
    def _fact_row(self, record: dict[str, Any], grain_fields: list[str], match: dict[str, Any]) -> dict[str, Any]:
        return {
            "粒度键": [str(record.get(field) or "") for field in grain_fields],
            "订单号": record["订单号"],
            "行号": record["行号"],
            "金额": round(float(record["金额"]), 2),
            "客户键": match["客户键"],
            "客户名称": match["客户名称"],
            "地区键": match["地区键"],
            "地区名称": match["地区名称"],
            "采用记录类型": record["记录类型"],
            "来源标识": record["来源标识"],
            "来源版本": record["来源版本"],
            "业务生效时刻": record.get("业务生效时刻", ""),
            "事实记录id": record["id"],
        }

    def _recon_row(
        self,
        record: dict[str, Any],
        grain_fields: list[str],
        reason: str,
        *,
        match: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        match = match or self._match_customer(record)
        return {
            "核对状态": RECONCILE_PENDING,
            "核对原因": reason,
            "粒度键": [str(record.get(field) or "") for field in grain_fields],
            "订单号": record["订单号"],
            "行号": record["行号"],
            "金额": round(float(record["金额"]), 2),
            "客户键": match["客户键"],
            "地区名称": match["地区名称"],
            "候选关联": match["候选关联"],
            "来源标识": record["来源标识"],
            "来源版本": record["来源版本"],
            "业务生效时刻": record.get("业务生效时刻", ""),
            "事实记录id": record["id"],
            "说明": note,
        }


fact_snapshot_service = FactSnapshotService()
