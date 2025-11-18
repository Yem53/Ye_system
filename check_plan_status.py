"""检查计划状态和错误信息"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv()

from app.db.session import SessionLocal
from app.models.manual_plan import ManualPlan
from app.models.execution_log import ExecutionLog
from sqlalchemy import select, desc

def check_plan_status():
    """检查最近的计划状态"""
    with SessionLocal() as db:
        # 获取最近的计划
        plans = list(db.scalars(
            select(ManualPlan)
            .order_by(desc(ManualPlan.created_at))
            .limit(5)
        ))
        
        print("=" * 80)
        print("最近的计划状态")
        print("=" * 80)
        print()
        
        for plan in plans:
            print(f"计划ID: {plan.id}")
            print(f"交易对: {plan.symbol}")
            print(f"状态: {plan.status.value}")
            print(f"执行时间: {plan.listing_time}")
            print(f"创建时间: {plan.created_at}")
            print()
            
            # 检查执行日志
            logs = list(db.scalars(
                select(ExecutionLog)
                .where(ExecutionLog.manual_plan_id == plan.id)
                .order_by(desc(ExecutionLog.created_at))
                .limit(10)
            ))
            
            if logs:
                print("执行日志:")
                for log in logs:
                    print(f"  - {log.event_type}: {log.status} (时间: {log.created_at})")
                    if log.payload:
                        import json
                        print(f"    详情: {json.dumps(log.payload, indent=2, default=str)}")
                print()
            else:
                print("  无执行日志")
                print()
            
            print("-" * 80)
            print()

if __name__ == "__main__":
    check_plan_status()

