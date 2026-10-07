"""存量库迁移：枯水期供水保障。

在既有库基础上做两件事（脚本幂等，可重复执行）：

1. 建立供水保障表 water_supply_plans / water_supply_items /
   water_supply_emergencies（Base.metadata.create_all 自动补建，
   不动任何历史数据）；
2. 建立乡镇明细与应急物资的唯一索引（幂等键由数据库兜底）。

说明：枯水预警以 run_id=NULL / disposal_id=NULL 台账写入既有
warning_records，无需改表；历史预警/转移/处置单记录原样保留。

用法：python scripts/migrate_supply.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import Base, engine

# 供水保障表通过模型元数据补建（表已存在时 create_all 自动跳过）
IMPORTED = True  # noqa: F841 —— 导入即注册到 Base.metadata
from app import models  # noqa: E402,F401

UNIQUE_INDEXES = [
    ("uq_supply_item_plan_town", "water_supply_items", "plan_id, township"),
    ("uq_supply_emergency", "water_supply_emergencies", "plan_id, supply_id"),
]


def main():
    # 1. 补建供水保障表（新表，不影响存量数据）
    Base.metadata.create_all(bind=engine)
    print("[1/2] 供水保障表 water_supply_plans / water_supply_items / "
          "water_supply_emergencies 已就绪")

    with engine.begin() as conn:
        print("[2/2] 建立唯一索引（幂等键由数据库兜底）...")
        for name, table, cols in UNIQUE_INDEXES:
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
            print(f"  + {name} ON {table}({cols})")

    print("迁移完成：枯水期供水保障（申请/审核/配水/完成 + 库容扣减与预警回写）已启用。")


if __name__ == "__main__":
    main()
