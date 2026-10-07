"""枯水期供水保障四态闭环测试。

覆盖：
- 申请/审核/配水/完成四态闭环与角色权限（越权 403、跳态 409）
- 申请幂等：同一水库开口计划单归并；超出可供水量 422
- 审核复核可供库容（申请后库容下降被拒 409）
- 乡镇确认优先级：按优先级×可用库容配水，缺口如实记录
- 完成扣减库容并回写水库工况与枯水预警（run_id=NULL 台账）
- 异常欠供：追加应急物资（增量出库、幂等归并），并兼容防汛处置单预占
- 预报重跑不清除枯水预警，历史遗留台账共存
"""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, FloodZone, ForecastRun, RainfallEvent,
                        Reservoir, RiverNode, RiverReach, SubBasin, Supply,
                        SupplyAllocation, WarningRecord, WaterStation,
                        WaterSupplyEmergency, WaterSupplyItem, WaterSupplyPlan)
from app.services import watersupply
from app.services.forecast import run_forecast


def _seed(db):
    """自洽流域（沿用处置协同测试水文骨架）+ 供水物资。"""
    db.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="库址", kind="reservoir"),
        RiverNode(id=3, name="出口", kind="outlet"),
        RiverReach(id=1, name="源→库", from_node_id=1, to_node_id=2,
                   k_hr=1.0, x_coef=0.2),
        RiverReach(id=2, name="库→出口", from_node_id=2, to_node_id=3,
                   k_hr=1.0, x_coef=0.2),
        SubBasin(id=1, name="子流域", area_km2=120.0, cn=88.0, lag_hr=1.0,
                 outlet_node_id=1),
        # 死库容 100（10m），正常蓄水位 12m 对应 300 万m³；当前满蓄 300
        Reservoir(id=1, name="测试水库", node_id=2, normal_level=12.0,
                  flood_level=13.0, crest_level=16.0,
                  storage_curve=[[10, 100], [12, 300], [14, 600], [16, 1000], [18, 1500]],
                  discharge_curve=[[14, 0], [16, 200], [18, 600]],
                  gate_max=120.0, current_level=12.0, current_storage=300.0),
        WaterStation(id=1, name="出口水位站", node_id=3,
                     thresholds={"base_level": 5.0, "blue": 6.0, "yellow": 7.0,
                                 "orange": 8.0, "red": 9.0,
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        Supply(id=1, name="饮用水", unit="箱", stock=100, safety_stock=10),
        Supply(id=2, name="棉被", unit="床", stock=400, safety_stock=50),
    ])
    db.commit()


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    fac = sessionmaker(bind=engine)
    db = fac()
    _seed(db)
    db.close()
    yield fac
    engine.dispose()


def _apply(db, rid=1, towns=None):
    return watersupply.apply_plan(
        db, rid, "周库管", "reservoir_manager",
        title="枯水期供水计划", period="2026-01~2026-03",
        items=towns or [{"township": "白水渡镇", "demand": 80},
                        {"township": "龙潭镇", "demand": 60}])


def _to_executing(db, rid=1, priorities=None):
    p = _apply(db, rid)
    p = watersupply.review_plan(db, p["id"], "张调度", "dispatcher", opinion="同意")
    p = watersupply.execute_plan(db, p["id"], "吴乡镇", "township",
                                 priorities=priorities or {})
    return p


def _full_loop(db, rid=1, actuals=None):
    p = _to_executing(db, rid)
    return watersupply.complete_plan(db, p["id"], "吴乡镇", "township",
                                     actuals=actuals or {}, summary="供水完成")


# ---------------- 四态闭环与回写 ----------------
def test_full_loop_deducts_storage_and_writes_back(factory):
    db = factory()
    p = _full_loop(db)
    assert p["status"] == "completed"
    assert (p["applied_by"], p["reviewed_by"], p["executed_by"], p["completed_by"]) == \
           ("周库管", "张调度", "吴乡镇", "吴乡镇")
    assert p["planned_supply"] == 140 and p["allocated_supply"] == 140
    assert p["actual_supply"] == 140 and p["shortage"] == 0

    # 库容扣减 + 水位反算回写水库工况
    res = db.get(Reservoir, 1)
    assert res.current_storage == 160            # 300 - 140
    assert res.current_level == 10.6             # level_at(160)
    assert p["snapshot"]["writeback"]["final_storage"] == 160

    # 枯水预警回写：run_id=NULL 台账（160/300 ≈ 53% → 橙色）
    w = db.query(WarningRecord).filter(WarningRecord.kind == "water_supply").one()
    assert w.run_id is None and w.disposal_id is None
    assert w.target_type == "reservoir" and w.target_id == 1
    assert w.level == "orange" and w.value == 160 and w.threshold == 300
    # 足额供水无欠供预警
    assert db.query(WarningRecord).filter(
        WarningRecord.kind == "supply_shortage").count() == 0
    # 明细全部足额
    assert all(it.status == "supplied" for it in db.query(WaterSupplyItem).all())
    db.close()


def test_role_enforcement(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 1, "x", "dispatcher",
                               items=[{"township": "A镇", "demand": 10}])
    assert ei.value.status_code == 403

    p = _apply(db)
    for bad in ("reservoir_manager", "township", "supply_manager"):
        with pytest.raises(HTTPException) as ei:
            watersupply.review_plan(db, p["id"], "x", bad)
        assert ei.value.status_code == 403
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    for bad in ("reservoir_manager", "dispatcher"):
        with pytest.raises(HTTPException) as ei:
            watersupply.execute_plan(db, p["id"], "x", bad)
        assert ei.value.status_code == 403
    watersupply.execute_plan(db, p["id"], "吴乡镇", "township")
    with pytest.raises(HTTPException) as ei:
        watersupply.complete_plan(db, p["id"], "x", "dispatcher")
    assert ei.value.status_code == 403
    db.close()


def test_illegal_transitions_rejected(factory):
    db = factory()
    p = _apply(db)
    with pytest.raises(HTTPException) as ei:
        watersupply.execute_plan(db, p["id"], "吴乡镇", "township")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    assert ei.value.status_code == 409
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    with pytest.raises(HTTPException) as ei:
        watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    assert ei.value.status_code == 409
    watersupply.execute_plan(db, p["id"], "吴乡镇", "township")
    watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    with pytest.raises(HTTPException) as ei:
        watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    assert ei.value.status_code == 409
    db.close()


def test_plan_not_found_404(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        watersupply.review_plan(db, 999, "张调度", "dispatcher")
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 999, "周库管", "reservoir_manager",
                               items=[{"township": "A镇", "demand": 10}])
    assert ei.value.status_code == 404
    db.close()


# ---------------- 申请幂等与库容校验 ----------------
def test_apply_idempotent_merge_per_reservoir(factory):
    db = factory()
    p1 = _apply(db)
    p2 = _apply(db)  # 重复申请归并
    assert p1["id"] == p2["id"]
    assert db.query(WaterSupplyPlan).count() == 1
    # 闭环后可再次申请新计划
    watersupply.review_plan(db, p1["id"], "张调度", "dispatcher")
    watersupply.execute_plan(db, p1["id"], "吴乡镇", "township")
    watersupply.complete_plan(db, p1["id"], "吴乡镇", "township")
    p3 = _apply(db, towns=[{"township": "白水渡镇", "demand": 20}])
    assert p3["id"] != p1["id"]
    assert db.query(WaterSupplyPlan).count() == 2
    db.close()


def test_apply_merges_same_township_and_validates(factory):
    db = factory()
    p = watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "白水渡镇", "demand": 50},
                                      {"township": "白水渡镇", "demand": 30}])
    assert p["total_demand"] == 80 and len(p["items"]) == 1
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "", "demand": 10}])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "A镇", "demand": 0}])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 1, "周库管", "reservoir_manager", items=[])
    assert ei.value.status_code == 422
    db.close()


def test_apply_exceeds_available_storage_rejected(factory):
    db = factory()
    # 可供水量 = 300 - 100（死库容）= 200，申请 250 → 422
    with pytest.raises(HTTPException) as ei:
        watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "白水渡镇", "demand": 250}])
    assert ei.value.status_code == 422
    db.close()


def test_review_rechecks_available_storage(factory):
    db = factory()
    p = _apply(db)  # 计划 140，申请时可供 200
    # 申请后库容被其它运用耗至 150（可供 50）→ 审核复核不通过
    res = db.get(Reservoir, 1)
    res.current_storage = 150
    db.commit()
    with pytest.raises(HTTPException) as ei:
        watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()


def test_concurrent_apply_merges_into_single_plan(factory):
    ids, errors = [], []

    def worker():
        db = factory()
        try:
            p = _apply(db)
            ids.append(p["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 1
    db = factory()
    assert db.query(WaterSupplyPlan).count() == 1
    db.close()


# ---------------- 乡镇确认优先级与配水 ----------------
def test_execute_allocates_by_priority_when_storage_short(factory):
    db = factory()
    p = _apply(db)  # 白水渡 80 + 龙潭 60 = 140
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    # 审核后库容降至 220（可供 120）→ 配水按优先级分配，低优先级出缺口
    res = db.get(Reservoir, 1)
    res.current_storage = 220
    db.commit()
    items = {it["township"]: it["id"] for it in p["items"]}
    p = watersupply.execute_plan(db, p["id"], "吴乡镇", "township",
                                 priorities={items["白水渡镇"]: 1,
                                             items["龙潭镇"]: 2})
    got = {it["township"]: it["allocated"] for it in p["items"]}
    assert got["白水渡镇"] == 80          # 优先级 1 足额
    assert got["龙潭镇"] == 40            # 仅剩 40，缺口 20
    assert p["allocated_supply"] == 120
    assert "缺口" in p["remark"]
    db.close()


def test_execute_rejects_bad_priority(factory):
    db = factory()
    p = _apply(db)
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    iid = p["items"][0]["id"]
    with pytest.raises(HTTPException) as ei:
        watersupply.execute_plan(db, p["id"], "吴乡镇", "township",
                                 priorities={iid: 0})
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        watersupply.execute_plan(db, p["id"], "吴乡镇", "township",
                                 priorities={iid: "abc"})
    assert ei.value.status_code == 422
    db.close()


# ---------------- 完成扣减与异常欠供 ----------------
def test_complete_with_shortage_flags_undersupply(factory):
    db = factory()
    p = _to_executing(db)
    items = {it["township"]: it["id"] for it in p["items"]}
    # 白水渡分配 80 实供 50 → 欠供 30（30/140 ≈ 21% → 橙色欠供预警）
    p = watersupply.complete_plan(db, p["id"], "吴乡镇", "township",
                                  actuals={items["白水渡镇"]: 50})
    assert p["actual_supply"] == 110 and p["shortage"] == 30
    assert p["undersupplied"] is True
    got = {it["township"]: it for it in p["items"]}
    assert got["白水渡镇"]["status"] == "short"
    assert got["龙潭镇"]["status"] == "supplied"

    w = db.query(WarningRecord).filter(WarningRecord.kind == "supply_shortage").one()
    assert w.run_id is None and w.level == "orange" and w.value == 30
    assert "欠供" in w.message
    # 库容按实际供水 110 扣减
    assert db.get(Reservoir, 1).current_storage == 190
    db.close()


def test_complete_rejects_actual_over_allocated(factory):
    db = factory()
    p = _to_executing(db)
    iid = p["items"][0]["id"]
    with pytest.raises(HTTPException) as ei:
        watersupply.complete_plan(db, p["id"], "吴乡镇", "township",
                                  actuals={iid: 999})
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        watersupply.complete_plan(db, p["id"], "吴乡镇", "township",
                                  actuals={iid: -1})
    assert ei.value.status_code == 422
    db.close()


def test_complete_clamps_to_available_storage(factory):
    db = factory()
    p = _to_executing(db)  # 分配 140
    # 配水后库容骤降至 150（可供 50）→ 完成时按可交付量扣减，差额并入欠供
    res = db.get(Reservoir, 1)
    res.current_storage = 150
    db.commit()
    p = watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    assert p["actual_supply"] == 50 and p["shortage"] == 90
    res = db.get(Reservoir, 1)
    assert res.current_storage == 100      # 扣至死库容为止
    assert res.current_level == 10.0
    db.close()


def test_storage_warning_cleared_when_storage_recovered(factory):
    db = factory()
    _full_loop(db)  # 完成后库容 160 → 橙色库容偏低预警
    assert db.query(WarningRecord).filter(
        WarningRecord.kind == "water_supply",
        WarningRecord.status == "active").count() == 1
    # 汛期拦蓄回蓄至正常蓄水以上（如处置单审核回写工况）
    res = db.get(Reservoir, 1)
    res.current_storage = 400
    res.current_level = res.level_at(400)
    db.commit()
    # 第二张计划完成（供 50 后库容 350 ≥ 正常蓄水 300）→ 既有偏低预警销警
    p = watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "白水渡镇", "demand": 50}])
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    watersupply.execute_plan(db, p["id"], "吴乡镇", "township")
    watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    w = db.query(WarningRecord).filter(WarningRecord.kind == "water_supply").one()
    assert w.status == "cleared"
    db.close()


# ---------------- 异常欠供追加应急物资（兼容原有处置记录） ----------------
def _shortage_plan(db):
    p = _to_executing(db)
    items = {it["township"]: it["id"] for it in p["items"]}
    return watersupply.complete_plan(db, p["id"], "吴乡镇", "township",
                                     actuals={items["白水渡镇"]: 50})  # 欠供 30


def test_emergency_supply_appended_incrementally(factory):
    db = factory()
    p = _shortage_plan(db)
    # 非物资管理员追加 → 403
    with pytest.raises(HTTPException) as ei:
        watersupply.append_emergency(db, p["id"], "x", "township", 1, 20)
    assert ei.value.status_code == 403
    # 追加 20 箱饮用水：立即出库
    p = watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 20)
    assert db.get(Supply, 1).stock == 80
    assert p["emergencies"][0]["quantity"] == 20
    # 重复追加归并为同一条，只出增量（30 - 20 = 10）
    p = watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 30)
    assert len(p["emergencies"]) == 1
    assert db.get(Supply, 1).stock == 70
    assert db.query(WaterSupplyEmergency).count() == 1
    # 少于已出库 → 422
    with pytest.raises(HTTPException) as ei:
        watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 10)
    assert ei.value.status_code == 422
    db.close()


def test_emergency_requires_undersupplied_completed_plan(factory):
    db = factory()
    p = _to_executing(db)
    # 执行中（未完成）不可追加
    with pytest.raises(HTTPException) as ei:
        watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 10)
    assert ei.value.status_code == 409
    # 足额完成（无欠供）不可追加
    p = watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    assert p["shortage"] == 0
    with pytest.raises(HTTPException) as ei:
        watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 10)
    assert ei.value.status_code == 409
    db.close()


def test_emergency_respects_disposal_committed_stock(factory):
    """应急追加计入防汛处置单预占：可用 = 库存 − 预占，不挤占原有处置记录。"""
    db = factory()
    # 既有防汛处置单（approved）预占饮用水 60 箱
    run = ForecastRun(event_id=1, mode="natural", status="done")
    db.add(run)
    db.flush()
    order = DisposalOrder(run_id=run.id, title="防汛处置单", status="approved")
    db.add(order)
    db.flush()
    db.add(SupplyAllocation(disposal_id=order.id, evacuation_id=None,
                            supply_id=1, quantity=60))
    db.commit()

    p = _shortage_plan(db)
    # 库存 100 − 预占 60 = 可用 40，追加 50 → 409
    with pytest.raises(HTTPException) as ei:
        watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 50)
    assert ei.value.status_code == 409
    # 追加 40 成功：库存 60（处置单预占份额原样保留）
    watersupply.append_emergency(db, p["id"], "陈物资", "supply_manager", 1, 40)
    assert db.get(Supply, 1).stock == 60
    alloc = db.query(SupplyAllocation).one()
    assert alloc.quantity == 60 and alloc.issued_quantity == 0
    db.close()


# ---------------- 与预报/处置记录兼容 ----------------
def test_supply_warnings_survive_forecast_rerun(factory):
    """枯水预警为 run_id=NULL 台账：预报重跑只维护运行台账，不清除枯水预警。"""
    db = factory()
    _full_loop(db)
    assert db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).count() == 1

    r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    rid = r["run_id"]
    # 预报预警挂运行，枯水预警原样保留
    supply_w = db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).all()
    assert len(supply_w) == 1 and supply_w[0].kind == "water_supply"
    assert supply_w[0].status == "active"
    assert db.query(WarningRecord).filter(WarningRecord.run_id == rid).count() >= 1
    # 重跑预报依然不影响
    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    assert db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).count() == 1
    db.close()


def test_legacy_null_run_records_coexist_with_supply_plan(factory):
    """历史遗留台账（run_id/disposal_id 为 NULL）与供水回写记录共存互不影响。"""
    db = factory()
    db.add(WarningRecord(run_id=None, target_type="station", target_id=99,
                         target_name="历史站", kind="water_level", level="blue",
                         status="cleared"))
    db.commit()
    _full_loop(db)
    legacy = db.query(WarningRecord).filter(
        WarningRecord.target_type == "station").one()
    assert legacy.status == "cleared" and legacy.disposal_id is None
    # 供水回写记录独立成条
    mine = db.query(WarningRecord).filter(
        WarningRecord.target_type == "reservoir").one()
    assert mine.kind == "water_supply" and mine.status == "active"
    db.close()


def test_second_plan_refreshes_warning_in_place(factory):
    """同一水库多次供水：枯水预警按 (目标, 类型) 幂等刷新，不重复建账。"""
    db = factory()
    _full_loop(db)                       # 库容 300 → 160
    # 第二张计划再供 50（可供 = 160 − 100 = 60）
    p = watersupply.apply_plan(db, 1, "周库管", "reservoir_manager",
                               items=[{"township": "白水渡镇", "demand": 50}])
    watersupply.review_plan(db, p["id"], "张调度", "dispatcher")
    watersupply.execute_plan(db, p["id"], "吴乡镇", "township")
    watersupply.complete_plan(db, p["id"], "吴乡镇", "township")
    assert db.get(Reservoir, 1).current_storage == 110
    rows = db.query(WarningRecord).filter(WarningRecord.kind == "water_supply").all()
    assert len(rows) == 1
    assert rows[0].value == 110 and rows[0].level == "red"   # 110/300 ≈ 37% → 红色
    db.close()
