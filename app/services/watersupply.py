"""枯水期供水保障：申请 → 审核 → 配水 → 完成 四态闭环 + 库容扣减与预警回写。

角色与状态机
    水库管理员 apply        → applied（待审核，同一水库开口计划单幂等归并）
    调度员     review        → approved（审核通过，校验可供库容）
    乡镇联络员 execute       → executing（确认各乡镇优先级，按优先级×可用库容配水）
    乡镇联络员 complete      → completed（按实际供水扣减库容，回写水库工况与枯水预警）

完成闭环联动：
  - 水库工况回写：current_storage 按实际供水量扣减、current_level 经库容曲线
    反算更新（实时监测/联合调度视图随之反映枯水期供水后的运用工况）；
  - 预警回写：以 run_id=NULL / disposal_id=NULL 的台账写入 warning_records
    （kind=water_supply 库容偏低 / kind=supply_shortage 异常欠供），与预报
    预警同表展示；预报重跑只维护 run_id 关联台账，不会清除本类记录，
    run_id 为 NULL 的历史遗留记录同样原样保留（兼容原有处置记录）；
  - 异常欠供：实际供水 < 计划分配（shortage > 0）时，物资管理员可围绕计划单
    追加应急物资（WaterSupplyEmergency），从既有 supplies 库存增量出库；
    出库校验计入防汛处置单的库存预占（available = stock − 预占），与原有
    应急资源协同口径兼容，不干扰处置单自身的出库/释放流程。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (Reservoir, Supply, WaterSupplyEmergency, WaterSupplyItem,
                        WaterSupplyPlan, WarningRecord)
from app.services import resources as resource_svc

# 状态 → 下一状态、可操作角色、操作人字段、落库时间字段
TRANSITIONS = {
    "review": {"from": "applied", "to": "approved", "roles": ("dispatcher",),
               "actor": "reviewed_by", "at": "reviewed_at"},
    "execute": {"from": "approved", "to": "executing", "roles": ("township",),
                "actor": "executed_by", "at": "executed_at"},
    "complete": {"from": "executing", "to": "completed", "roles": ("township",),
                 "actor": "completed_by", "at": "completed_at"},
}
STATUS_TEXT = {"applied": "待审核", "approved": "待配水",
               "executing": "配水执行中", "completed": "已完成"}
ROLE_TEXT = {"reservoir_manager": "水库管理员", "dispatcher": "调度员",
             "township": "乡镇联络员", "supply_manager": "物资管理员"}
ITEM_STATUS_TEXT = {"pending": "待供水", "supplied": "已足额", "short": "欠供"}

# 同一水库的供水计划操作串行化（申请归并 / 审核 / 配水 / 完成扣减库容）
_plan_locks_guard = threading.Lock()
_plan_locks: Dict[int, threading.Lock] = {}


def _reservoir_lock(reservoir_id: int) -> threading.Lock:
    with _plan_locks_guard:
        return _plan_locks.setdefault(reservoir_id, threading.Lock())


# 开口（未闭环）计划单状态：同一水库同一时刻至多一张
OPEN_STATUSES = ("applied", "approved", "executing")


def _dead_storage(res: Reservoir) -> float:
    """死库容：库容曲线起点的蓄水量（供水不可动用部分）。"""
    curve = sorted(res.storage_curve or [], key=lambda p: p[0])
    return curve[0][1] if curve else 0.0


def _available_storage(res: Reservoir) -> float:
    """可供水量 = 当前库容 − 死库容（不为负）。"""
    return max((res.current_storage or 0.0) - _dead_storage(res), 0.0)


def serialize_plan(db: Session, plan: WaterSupplyPlan) -> dict:
    """计划单序列化（含水库冗余信息、乡镇明细与应急物资追加记录）。"""
    res = db.get(Reservoir, plan.reservoir_id)
    items = (db.query(WaterSupplyItem)
             .filter(WaterSupplyItem.plan_id == plan.id)
             .order_by(WaterSupplyItem.priority, WaterSupplyItem.id).all())
    emergencies = (db.query(WaterSupplyEmergency)
                   .filter(WaterSupplyEmergency.plan_id == plan.id).all())
    supply_by_id = {s.id: s for s in db.query(Supply).all()}
    return {
        "id": plan.id,
        "reservoir_id": plan.reservoir_id,
        "reservoir_name": res.name if res else "（水库已删除）",
        "title": plan.title,
        "status": plan.status,
        "status_text": STATUS_TEXT.get(plan.status, plan.status),
        "period": plan.period,
        "total_demand": round(plan.total_demand or 0.0, 1),
        "planned_supply": round(plan.planned_supply or 0.0, 1),
        "allocated_supply": round(plan.allocated_supply or 0.0, 1),
        "actual_supply": round(plan.actual_supply or 0.0, 1),
        "shortage": round(plan.shortage or 0.0, 1),
        "undersupplied": (plan.shortage or 0.0) > 0,
        "snapshot": plan.snapshot or {},
        "remark": plan.remark,
        "items": [{
            "id": it.id, "township": it.township,
            "demand": round(it.demand or 0.0, 1),
            "priority": it.priority,
            "allocated": round(it.allocated or 0.0, 1),
            "actual": round(it.actual or 0.0, 1),
            "status": it.status,
            "status_text": ITEM_STATUS_TEXT.get(it.status, it.status),
        } for it in items],
        "emergencies": [{
            "id": em.id, "supply_id": em.supply_id,
            "supply_name": supply_by_id[em.supply_id].name
                           if supply_by_id.get(em.supply_id) else "（已删除）",
            "unit": supply_by_id[em.supply_id].unit
                    if supply_by_id.get(em.supply_id) else "",
            "quantity": em.quantity,
            "issued_quantity": em.issued_quantity or 0,
            "reason": em.reason, "created_by": em.created_by,
            "created_at": em.created_at.isoformat() if em.created_at else None,
        } for em in emergencies],
        "applied_by": plan.applied_by, "reviewed_by": plan.reviewed_by,
        "executed_by": plan.executed_by, "completed_by": plan.completed_by,
        "applied_at": plan.applied_at.isoformat() if plan.applied_at else None,
        "reviewed_at": plan.reviewed_at.isoformat() if plan.reviewed_at else None,
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
        "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


def _check_role(role: str, action: str) -> None:
    roles = TRANSITIONS[action]["roles"]
    if role not in roles:
        need = "、".join(ROLE_TEXT[r] for r in roles)
        raise HTTPException(403, f"该操作需由{need}执行（当前角色：{ROLE_TEXT.get(role, role)}）")


def _get_plan_for(db: Session, plan_id: int, action: str) -> WaterSupplyPlan:
    plan = db.get(WaterSupplyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, f"供水计划 #{plan_id} 不存在")
    allowed = TRANSITIONS[action]["from"]
    allowed = (allowed,) if isinstance(allowed, str) else allowed
    if plan.status not in allowed:
        need = "、".join(STATUS_TEXT[s] for s in allowed)
        raise HTTPException(
            409, f"供水计划当前为「{STATUS_TEXT.get(plan.status, plan.status)}」，"
                 f"不能执行该操作（须为「{need}」）")
    return plan


def apply_plan(db: Session, reservoir_id: int, operator: str, role: str,
               title: str = "", period: str = "", items: list | None = None,
               remark: str = "") -> dict:
    """水库管理员提交枯水期供水计划（同一水库开口计划单幂等归并）。"""
    if role != "reservoir_manager":
        raise HTTPException(403, f"仅水库管理员可提交供水计划（当前角色："
                                f"{ROLE_TEXT.get(role, role)}）")
    res = db.get(Reservoir, reservoir_id)
    if res is None:
        raise HTTPException(404, f"水库 #{reservoir_id} 不存在")

    # 乡镇需水明细校验：名称非空、需水为正、同乡镇归并
    demand_by_town: Dict[str, float] = {}
    for raw in items or []:
        town = str((raw or {}).get("township") or "").strip()
        demand = float((raw or {}).get("demand") or 0.0)
        if not town:
            raise HTTPException(422, "乡镇名称不能为空")
        if demand <= 0:
            raise HTTPException(422, f"乡镇「{town}」需水量须大于 0")
        demand_by_town[town] = demand_by_town.get(town, 0.0) + demand
    if not demand_by_town:
        raise HTTPException(422, "供水计划至少包含一个乡镇的需水明细")
    total = round(sum(demand_by_town.values()), 1)

    with _reservoir_lock(reservoir_id):
        existing = (db.query(WaterSupplyPlan)
                    .filter(WaterSupplyPlan.reservoir_id == reservoir_id,
                            WaterSupplyPlan.status.in_(OPEN_STATUSES))
                    .first())
        if existing is not None:
            return serialize_plan(db, existing)  # 重复申请：归并到开口计划单

        available = _available_storage(res)
        if total > available:
            raise HTTPException(
                422, f"计划供水 {total} 万m³ 超出水库可供水量 "
                     f"{round(available, 1)} 万m³（当前库容 {round(res.current_storage or 0, 1)}，"
                     f"死库容 {round(_dead_storage(res), 1)}），请调减乡镇需水")

        snapshot = {
            "level": round(res.current_level or 0.0, 2),
            "storage": round(res.current_storage or 0.0, 1),
            "dead_storage": round(_dead_storage(res), 1),
            "available": round(available, 1),
        }
        plan = WaterSupplyPlan(
            reservoir_id=reservoir_id,
            title=(title or "").strip() or f"{res.name}枯水期供水保障计划",
            period=(period or "").strip(),
            total_demand=total, planned_supply=total,
            snapshot=snapshot, remark=(remark or "").strip(),
            applied_by=(operator or "").strip() or "水库管理员",
            status="applied")
        db.add(plan)
        db.flush()
        for town, demand in demand_by_town.items():
            db.add(WaterSupplyItem(plan_id=plan.id, township=town,
                                   demand=demand, priority=3))
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def review_plan(db: Session, plan_id: int, operator: str, role: str,
                opinion: str = "") -> dict:
    """调度员审核：复核可供库容后通过（库容较申请时下降可能被拒）。"""
    _check_role(role, "review")
    plan = _get_plan_for(db, plan_id, "review")

    with _reservoir_lock(plan.reservoir_id):
        res = db.get(Reservoir, plan.reservoir_id)
        available = _available_storage(res)
        if (plan.planned_supply or 0.0) > available:
            raise HTTPException(
                409, f"水库当前可供水量 {round(available, 1)} 万m³ 已不足计划供水 "
                     f"{round(plan.planned_supply or 0, 1)} 万m³，"
                     f"请退回计划由水库管理员调减后重新申请")

        plan.status = "approved"
        plan.reviewed_by = (operator or "").strip() or "值班调度员"
        plan.reviewed_at = datetime.now()
        if opinion.strip():
            plan.remark = (plan.remark + f"\n[审核意见] {opinion.strip()}").strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def _parse_num_map(raw: dict | None, cast, field: str) -> dict:
    """解析 {明细id: 数值} 请求参数（JSON 键为字符串），非法值抛 422。"""
    result = {}
    for k, v in (raw or {}).items():
        try:
            result[int(k)] = cast(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{field}参数格式非法：{k}={v}")
    return result


def execute_plan(db: Session, plan_id: int, operator: str, role: str,
                 priorities: dict | None = None, note: str = "") -> dict:
    """乡镇联络员确认各乡镇配水优先级并启动配水。

    按「优先级（1 最高）→ 需水量」顺序，以当前可供库容逐镇分配：
    足额分配至库容耗尽，排在后面的乡镇可能只能分到部分水量（配水缺口
    在计划单与明细上如实记录，完成时将计入欠供）。
    """
    _check_role(role, "execute")
    plan = _get_plan_for(db, plan_id, "execute")

    with _reservoir_lock(plan.reservoir_id):
        items = (db.query(WaterSupplyItem)
                 .filter(WaterSupplyItem.plan_id == plan.id).all())
        prio = _parse_num_map(priorities, int, "优先级")
        for it in items:
            if it.id in prio:
                if prio[it.id] < 1:
                    raise HTTPException(422, f"乡镇「{it.township}」优先级须为 ≥1 的整数")
                it.priority = prio[it.id]

        res = db.get(Reservoir, plan.reservoir_id)
        remaining = _available_storage(res)
        ordered = sorted(items, key=lambda it: (it.priority, -(it.demand or 0), it.id))
        gaps = []
        for it in ordered:
            it.allocated = round(min(it.demand or 0.0, remaining), 1)
            remaining = round(remaining - it.allocated, 1)
            if it.allocated < (it.demand or 0.0) - 1e-9:
                gaps.append(f"{it.township} 缺口 {round((it.demand or 0) - it.allocated, 1)} 万m³")

        plan.allocated_supply = round(sum(it.allocated for it in items), 1)
        plan.status = "executing"
        plan.executed_by = (operator or "").strip() or "乡镇联络员"
        plan.executed_at = datetime.now()
        line = (f"[配水执行] {plan.executed_by} 确认优先级并启动配水："
                f"计划分配 {plan.allocated_supply} 万m³"
                f"（可供库容 {round(_available_storage(res), 1)} 万m³）")
        if gaps:
            line += "；" + "；".join(gaps)
        if note.strip():
            line += f"。说明：{note.strip()}"
        plan.remark = (plan.remark + "\n" + line).strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def _upsert_supply_warning(db: Session, data: dict) -> None:
    """以 run_id=NULL 台账形式回写枯水预警（与预报预警同表，重跑预报不清除）。

    按 (run_id IS NULL, target_type, target_id, kind) 幂等：同一水库同一类
    枯水预警只保留一条并刷新内容，避免重复完成/重试产生重复台账。
    """
    rec = (db.query(WarningRecord)
           .filter(WarningRecord.run_id.is_(None),
                   WarningRecord.target_type == data["target_type"],
                   WarningRecord.target_id == data["target_id"],
                   WarningRecord.kind == data["kind"])
           .first())
    if rec is None:
        db.add(WarningRecord(run_id=None, disposal_id=None, **data))
        return
    for field in ("target_name", "level", "value", "threshold", "message"):
        setattr(rec, field, data[field])


def _storage_warning_level(ratio: float) -> str:
    if ratio < 0.4:
        return "red"
    if ratio < 0.6:
        return "orange"
    if ratio < 0.8:
        return "yellow"
    return "blue"


def complete_plan(db: Session, plan_id: int, operator: str, role: str,
                  actuals: dict | None = None, summary: str = "") -> dict:
    """乡镇联络员确认完成：按实际供水扣减库容，回写水库工况与枯水预警。

    actuals 为 {明细id: 实际供水量}，缺省按计划分配量足额供水；
    实际 < 分配 的差额记为欠供（shortage > 0 即异常欠供，可追加应急物资）。
    """
    _check_role(role, "complete")
    plan = _get_plan_for(db, plan_id, "complete")

    with _reservoir_lock(plan.reservoir_id):
        items = (db.query(WaterSupplyItem)
                 .filter(WaterSupplyItem.plan_id == plan.id).all())
        actual_by_id = _parse_num_map(actuals, float, "实际供水量")
        for it in items:
            actual = actual_by_id.get(it.id, it.allocated or 0.0)
            if actual < 0:
                raise HTTPException(422, f"乡镇「{it.township}」实际供水量不能为负")
            if actual > (it.allocated or 0.0) + 1e-9:
                raise HTTPException(
                    422, f"乡镇「{it.township}」实际供水 {actual} 万m³ 超过计划分配 "
                         f"{round(it.allocated or 0, 1)} 万m³")
            it.actual = round(actual, 1)
            it.status = "supplied" if it.actual >= (it.allocated or 0.0) - 1e-9 else "short"

        res = db.get(Reservoir, plan.reservoir_id)
        actual_total = round(sum(it.actual for it in items), 1)
        shortage = round(sum((it.allocated or 0.0) - it.actual for it in items), 1)

        # ---- 回写 1：扣减库容（可供水量不足时按可交付量扣减，差额并入欠供）----
        available = _available_storage(res)
        delivered = round(min(actual_total, available), 1)
        if delivered < actual_total:
            shortage = round(shortage + (actual_total - delivered), 1)
            actual_total = delivered
        new_storage = round((res.current_storage or 0.0) - actual_total, 1)
        res.current_storage = new_storage
        res.current_level = round(res.level_at(new_storage), 2)

        plan.actual_supply = actual_total
        plan.shortage = shortage
        snap = dict(plan.snapshot or {})
        snap["writeback"] = {
            "actual_supply": actual_total, "shortage": shortage,
            "final_level": res.current_level, "final_storage": res.current_storage,
        }
        plan.snapshot = snap

        # ---- 回写 2：枯水预警台账（run_id=NULL，兼容预报重跑与历史记录）----
        normal_storage = res.storage_at(res.normal_level)
        ratio = (new_storage / normal_storage) if normal_storage else 1.0
        if ratio < 1.0:
            level = _storage_warning_level(ratio)
            _upsert_supply_warning(db, {
                "target_type": "reservoir", "target_id": res.id,
                "target_name": res.name, "kind": "water_supply", "level": level,
                "value": round(new_storage, 1),
                "threshold": round(normal_storage, 1),
                "message": f"{res.name}枯水期供水 {actual_total} 万m³ 后库容降至 "
                           f"{new_storage} 万m³（正常蓄水的 {round(ratio * 100)}%），"
                           f"注意蓄水保供"})
        else:
            # 库容已恢复至正常蓄水以上：既有库容偏低预警同步销警（保留台账）
            stale = (db.query(WarningRecord)
                     .filter(WarningRecord.run_id.is_(None),
                             WarningRecord.target_type == "reservoir",
                             WarningRecord.target_id == res.id,
                             WarningRecord.kind == "water_supply",
                             WarningRecord.status == "active").first())
            if stale is not None:
                stale.status = "cleared"
        if shortage > 0:
            short_towns = "、".join(it.township for it in items if it.status == "short")
            ratio_s = shortage / plan.allocated_supply if plan.allocated_supply else 1.0
            level = "red" if ratio_s > 0.3 else ("orange" if ratio_s > 0.1 else "yellow")
            _upsert_supply_warning(db, {
                "target_type": "reservoir", "target_id": res.id,
                "target_name": res.name, "kind": "supply_shortage", "level": level,
                "value": shortage,
                "threshold": round(plan.allocated_supply or 0.0, 1),
                "message": f"供水计划 #{plan.id} 异常欠供 {shortage} 万m³"
                           f"（{short_towns or '部分乡镇'}），可追加应急物资保障"})

        plan.status = "completed"
        plan.completed_by = (operator or "").strip() or "乡镇联络员"
        plan.completed_at = datetime.now()
        line = (f"[完成回写] {plan.completed_by} 确认供水完成：实际供水 {actual_total} 万m³，"
                f"库容扣减至 {new_storage} 万m³（水位 {res.current_level} m）")
        if shortage > 0:
            line += f"；异常欠供 {shortage} 万m³，已联动枯水预警"
        if summary.strip():
            line += f"。小结：{summary.strip()}"
        plan.remark = (plan.remark + "\n" + line).strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def append_emergency(db: Session, plan_id: int, operator: str, role: str,
                     supply_id: int, quantity: int, reason: str = "") -> dict:
    """物资管理员为异常欠供计划追加应急物资（库存增量出库，重复追加幂等）。

    出库校验与防汛处置协同同一口径：可用量 = 当前库存 − 处置单预占，
    不会挤占防汛处置单已预占/待出库的份额（兼容原有处置记录）。
    """
    if role != "supply_manager":
        raise HTTPException(403, f"应急物资由物资管理员追加（当前角色："
                                f"{ROLE_TEXT.get(role, role)}）")
    plan = db.get(WaterSupplyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, f"供水计划 #{plan_id} 不存在")
    if plan.status != "completed" or (plan.shortage or 0.0) <= 0:
        raise HTTPException(409, "仅异常欠供的已完成供水计划可追加应急物资")
    quantity = int(quantity or 0)
    if quantity <= 0:
        raise HTTPException(422, "追加数量须大于 0")

    # 与防汛物资分配/调度令出库共用同一把物资锁，串行化库存扣减
    with resource_svc._resource_lock("supply", int(supply_id)):
        supply = db.get(Supply, int(supply_id))
        if supply is None or not supply.active:
            raise HTTPException(404, "物资不存在或已停用")

        rec = (db.query(WaterSupplyEmergency)
               .filter(WaterSupplyEmergency.plan_id == plan.id,
                       WaterSupplyEmergency.supply_id == supply.id).first())
        delta = quantity - (rec.issued_quantity or 0) if rec else quantity
        if delta < 0:
            raise HTTPException(422, f"追加数量不能少于已出库 {rec.issued_quantity}{supply.unit}")
        if delta > 0:
            # 可用量口径：库存 − 防汛处置单预占（不挤占原有处置记录份额）
            committed = resource_svc._supply_committed(db)
            available = supply.stock - committed.get(supply.id, 0)
            if delta > available:
                raise HTTPException(
                    409, f"物资「{supply.name}」可用库存不足：可出库 {available}{supply.unit}"
                         f"（库存 {supply.stock} − 处置单预占 {committed.get(supply.id, 0)}），"
                         f"本次需出库 {delta}{supply.unit}")
            supply.stock -= delta
        if rec is None:
            rec = WaterSupplyEmergency(plan_id=plan.id, supply_id=supply.id)
            db.add(rec)
        rec.quantity = quantity
        rec.issued_quantity = quantity
        rec.reason = (reason or "").strip() or f"异常欠供 {round(plan.shortage or 0, 1)} 万m³"
        rec.created_by = (operator or "").strip() or "物资管理员"
        plan.remark = (plan.remark +
                       f"\n[应急物资] {rec.created_by} 追加「{supply.name}」"
                       f"{quantity}{supply.unit}（{rec.reason}）").strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def list_plans(db: Session, limit: int = 50) -> list:
    rows = (db.query(WaterSupplyPlan)
            .order_by(WaterSupplyPlan.id.desc()).limit(limit).all())
    return [serialize_plan(db, p) for p in rows]


def get_plan(db: Session, plan_id: int) -> dict:
    plan = db.get(WaterSupplyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, f"供水计划 #{plan_id} 不存在")
    return serialize_plan(db, plan)
