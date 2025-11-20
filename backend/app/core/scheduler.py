import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import ManualPlanStatus
from app.services.execution_service import ExecutionService
from app.services.manual_plan_service import ManualPlanService
from app.services.position_service import PositionService

# APScheduler 持续在后台触发公告抓取与日报发送任务
scheduler = BackgroundScheduler(timezone="UTC")

# 用于跟踪已启动精确执行线程的计划，避免重复启动
_precision_threads: dict[str, threading.Thread] = {}

# 动态线程池大小（根据CPU核心数优化资源利用）
CPU_COUNT = os.cpu_count() or 4
MONITOR_WORKERS = max(4, CPU_COUNT)  # 至少4个，最多等于CPU核心数（充分利用多核）
SYNC_WORKERS = max(2, CPU_COUNT // 2)  # 同步任务使用一半核心数

# 线程池执行器（用于异步执行监控任务，避免阻塞调度器）
_monitor_executor = ThreadPoolExecutor(
    max_workers=MONITOR_WORKERS, 
    thread_name_prefix="position-monitor"
)
_sync_executor = ThreadPoolExecutor(
    max_workers=SYNC_WORKERS, 
    thread_name_prefix="binance-sync"
)

logger.info("线程池配置: 监控任务={}个工作线程, 同步任务={}个工作线程 (CPU核心数={})", 
          MONITOR_WORKERS, SYNC_WORKERS, CPU_COUNT)

# 动态刷新频率状态（模块级别变量）
MIN_MANUAL_EXECUTOR_INTERVAL: float = 0.3  # 避免调度器堆积的最小间隔
MIN_POSITION_MONITOR_INTERVAL: float = 0.5  # 持仓监控的最小间隔（秒），保证高精度
MONITOR_TIMEOUT: float = 0.7  # 轻度告警阈值（秒），超过后记录warning但继续等待当前任务完成
MAX_MONITOR_RUNTIME: float = 3.0  # 硬性超时阈值（秒），超过后才强制重置
SYNC_TIMEOUT: float = 3.0  # 同步任务的轻度告警阈值（秒）
MAX_SYNC_RUNTIME: float = 12.0  # 同步任务的硬性超时阈值（秒）
_current_position_monitor_interval: float = MIN_POSITION_MONITOR_INTERVAL  # 初始值：使用最小间隔
_current_manual_executor_interval: float = MIN_MANUAL_EXECUTOR_INTERVAL  # 初始值：使用最小间隔
_last_binance_sync_ts: float = 0.0  # 上次同步币安持仓的时间戳
_monitor_positions_running: bool = False  # 标记 monitor_positions 是否正在执行
_monitor_start_time: float = 0.0  # 监控任务开始时间（用于超时检测）
_sync_positions_running: bool = False  # 标记 sync_positions_from_binance 是否正在执行
_sync_start_time: float = 0.0  # 同步任务开始时间
_manual_executor_running: bool = False  # 标记手动计划执行器是否正在运行
_manual_executor_start_time: float = 0.0  # 手动计划执行任务开始时间
MANUAL_EXECUTOR_TIMEOUT: float = 1.5  # 手动计划执行器超时阈值（秒）


def start_scheduler() -> None:
    """在 FastAPI 启动时调用，注册所有需要的定时任务。"""

    settings = get_settings()
    if not scheduler.running:
        scheduler.start()

    raw_manual_interval = settings.manual_plan_check_interval
    check_interval = max(raw_manual_interval, MIN_MANUAL_EXECUTOR_INTERVAL)
    if check_interval != raw_manual_interval:
        logger.warning(
            "配置 manual_plan_check_interval={:.3f} 秒过低，已自动提升到 {:.3f} 秒以避免调度器实例堆积",
            raw_manual_interval,
            check_interval,
        )


    def execute_manual_plans() -> None:
        """执行手动计划，支持精确模式（毫秒级精度）"""
        from datetime import datetime, timezone
        global _manual_executor_running, _manual_executor_start_time
        
        # 检查调度器是否还在运行，避免在关闭后执行
        if not scheduler.running:
            logger.debug("调度器已关闭，跳过手动计划执行任务")
            return
        
        if _manual_executor_running:
            elapsed = time.time() - _manual_executor_start_time
            if elapsed > MANUAL_EXECUTOR_TIMEOUT:
                logger.warning("手动计划执行任务超时（{:.2f}秒），强制重置", elapsed)
                _manual_executor_running = False
            else:
                logger.debug("手动计划执行任务仍在进行中，跳过本次调度")
                return
        
        _manual_executor_running = True
        _manual_executor_start_time = time.time()
        
        try:
            with SessionLocal() as db:
                service = ManualPlanService(db)
                executor = ExecutionService(db, settings)
                
                # 1. 先处理已到执行时间的计划（立即执行）
                # 注意：只处理状态为 PENDING 的计划，避免重复执行已失败的计划
                due_plans = service.due_plans()
                for plan in due_plans:
                    # 使用数据库级别的原子更新来防止并发执行
                    # 尝试将状态从 PENDING 更新为 EXECUTING
                    from sqlalchemy import update
                    from app.models.manual_plan import ManualPlan
                    
                    result = db.execute(
                        update(ManualPlan)
                        .where(ManualPlan.id == plan.id)
                        .where(ManualPlan.status == ManualPlanStatus.PENDING)
                        .values(status=ManualPlanStatus.EXECUTING)
                    )
                    db.commit()
                    
                    # 如果更新失败（返回0行），说明计划已被其他任务执行
                    if result.rowcount == 0:
                        logger.debug("计划 {} 已被其他任务执行，跳过", plan.id)
                        continue
                    
                    # 重新加载计划以获取最新状态
                    db.refresh(plan)
                    
                    try:
                        executor.execute_manual_plan(plan)
                        service.mark_status(plan, ManualPlanStatus.EXECUTED)
                        logger.info("计划 {} 立即执行完成", plan.id)
                        # 清理已完成的精确执行线程
                        if plan.id in _precision_threads:
                            del _precision_threads[plan.id]
                    except Exception as exc:
                        logger.error("手动计划 {} 执行失败: {}", plan.id, exc, exc_info=True)
                        # 标记为失败，避免重复执行
                        try:
                            service.mark_status(plan, ManualPlanStatus.FAILED)
                        except Exception as status_exc:
                            logger.error("标记计划 {} 状态失败: {}", plan.id, status_exc)
                        if plan.id in _precision_threads:
                            del _precision_threads[plan.id]
                
                # 2. 检查接近执行时间的计划，启动精确执行模式和WebSocket订阅
                if settings.manual_plan_precision_mode:
                    now = datetime.now(timezone.utc)
                    pending_plans = service.get_pending_plans()
                    
                    # 导入WebSocket服务
                    from app.services.binance_websocket_service import get_websocket_price_service
                    ws_service = get_websocket_price_service()
                    
                    for plan in pending_plans:
                        # 跳过已到时间的计划（上面已处理）
                        if plan.listing_time <= now:
                            continue
                        
                        time_diff = (plan.listing_time - now).total_seconds()
                        
                        # 确保symbol格式正确
                        symbol = plan.symbol.upper()
                        if not symbol.endswith("USDT"):
                            symbol = f"{symbol}USDT"
                        
                        # 在执行前N分钟开始订阅WebSocket（默认5分钟）
                        subscribe_before_seconds = settings.websocket_subscribe_before_minutes * 60
                        if 0 < time_diff <= subscribe_before_seconds:
                            # 提前订阅WebSocket，确保价格数据实时
                            if settings.websocket_price_enabled:
                                try:
                                    ws_service.subscribe_symbol(symbol)
                                    logger.debug("计划 {} 提前订阅WebSocket: {} (距离执行还有 {:.1f} 分钟)", 
                                               plan.id, symbol, time_diff / 60)
                                except Exception as exc:
                                    logger.warning("订阅WebSocket失败 ({}): {}", symbol, exc)
                        
                        # 如果计划在精确模式阈值内，且尚未启动精确执行线程
                        if 0 < time_diff <= settings.manual_plan_precision_threshold:
                            if plan.id not in _precision_threads or not _precision_threads[plan.id].is_alive():
                                logger.info("计划 {} 将在 {:.2f}秒后执行，启动精确执行模式", plan.id, time_diff)
                                
                                def precise_execute(plan_id: str, listing_time: datetime):
                                    """精确执行函数，在指定时间精确执行"""
                                    try:
                                        # 计算等待时间
                                        wait_time = (listing_time - datetime.now(timezone.utc)).total_seconds()
                                        
                                        # 如果还有较长时间，先等待大部分时间
                                        if wait_time > 0.1:
                                            # 提前50ms开始精确等待
                                            time.sleep(max(0, wait_time - 0.05))
                                        
                                        # 精确等待到执行时间（1毫秒检查一次）
                                        while True:
                                            now_check = datetime.now(timezone.utc)
                                            if now_check >= listing_time:
                                                break
                                            remaining = (listing_time - now_check).total_seconds()
                                            if remaining > 0.01:  # 如果还有10ms以上，等待5ms
                                                time.sleep(0.005)
                                            else:  # 最后10ms，1ms检查一次
                                                time.sleep(0.001)
                                        
                                        # 执行时间到达，立即执行
                                        actual_exec_time = datetime.now(timezone.utc)
                                        delay = (actual_exec_time - listing_time).total_seconds() * 1000  # 转换为毫秒
                                        
                                        with SessionLocal() as db_exec:
                                            from app.models.manual_plan import ManualPlan
                                            from sqlalchemy import update
                                            
                                            # 使用数据库级别的原子更新来防止并发执行
                                            result = db_exec.execute(
                                                update(ManualPlan)
                                                .where(ManualPlan.id == plan_id)
                                                .where(ManualPlan.status == ManualPlanStatus.PENDING)
                                                .values(status=ManualPlanStatus.EXECUTING)
                                            )
                                            db_exec.commit()
                                            
                                            # 如果更新失败（返回0行），说明计划已被其他任务执行
                                            if result.rowcount == 0:
                                                logger.debug("计划 %s 已被其他任务执行，跳过精确执行", plan_id)
                                                return
                                            
                                            # 重新加载计划以获取最新状态
                                            plan_check = db_exec.get(ManualPlan, plan_id)
                                            if not plan_check:
                                                logger.warning("计划 %s 不存在，跳过精确执行", plan_id)
                                                return
                                            
                                            service_exec = ManualPlanService(db_exec)
                                            executor_exec = ExecutionService(db_exec, settings)
                                            
                                            try:
                                                executor_exec.execute_manual_plan(plan_check)
                                                service_exec.mark_status(plan_check, ManualPlanStatus.EXECUTED)
                                                logger.info("计划 {} 精确执行完成，执行时间: {}，延迟: {:.2f}毫秒", 
                                                          plan_id, actual_exec_time.isoformat(), delay)
                                            except Exception as exc:
                                                logger.error("计划 %s 精确执行失败: %s", plan_id, exc, exc_info=True)
                                                service_exec.mark_status(plan_check, ManualPlanStatus.FAILED)
                                    except Exception as exc:
                                        logger.error("精确执行线程异常: %s", exc, exc_info=True)
                                    finally:
                                        # 清理线程记录
                                        if plan_id in _precision_threads:
                                            del _precision_threads[plan_id]
                                
                                # 启动精确执行线程
                                thread = threading.Thread(
                                    target=precise_execute,
                                    args=(plan.id, plan.listing_time),
                                    daemon=True,
                                    name=f"PreciseExecute-{plan.id[:8]}"
                                )
                                thread.start()
                                _precision_threads[plan.id] = thread
        except Exception as exc:
            if scheduler.running:
                logger.error("手动计划执行任务失败: {}", exc, exc_info=True)
            else:
                logger.debug("调度器关闭中，忽略手动计划执行错误")
        finally:
            _manual_executor_running = False

    if not scheduler.get_job("manual-executor"):
        scheduler.add_job(
            execute_manual_plans,
            "interval",
            seconds=check_interval,
            id="manual-executor",
            replace_existing=True,
            # 不设置 max_instances，使用默认值，通过数据库级别的原子更新防止并发问题
            coalesce=True,  # 合并错过的执行
            misfire_grace_time=1,  # 错过的任务在1秒内仍执行
            max_instances=3,  # 允许并发启动，但内部有自定义互斥逻辑
        )
        logger.info(
            "手动计划执行器已启动，检查间隔: {:.3f}秒（{}毫秒）",
            check_interval,
            int(check_interval * 1000),
        )

    # 动态刷新频率配置 - 分离监控和同步任务，保证高精度监控
    HIGH_FREQ_INTERVAL = 0.5  # 有持仓时：500ms（高精度监控，不包含同步操作）
    NORMAL_FREQ_INTERVAL = 2.0  # 无持仓时：2秒（减少不必要的检查）
    BINANCE_SYNC_INTERVAL = 5.0  # 同步币安持仓的间隔（秒），独立任务，不阻塞监控
    
    # 初始化手动执行器间隔
    global _current_manual_executor_interval, _last_binance_sync_ts, _monitor_positions_running, _sync_positions_running
    _current_manual_executor_interval = check_interval
    _last_binance_sync_ts = 0.0
    _monitor_positions_running = False
    _sync_positions_running = False

    def sync_positions_from_binance() -> None:
        """独立的任务：同步币安持仓（低频，不阻塞监控，异步执行）"""
        global _last_binance_sync_ts, _sync_positions_running, _sync_start_time
        
        if not scheduler.running:
            return
        
        # 检查上次同步是否超时（超过10秒认为超时）
        if _sync_positions_running:
            elapsed = time.time() - _sync_start_time
            if elapsed > MAX_SYNC_RUNTIME:
                logger.error("同步任务运行超过 {:.2f} 秒，强制重置", elapsed)
                _sync_positions_running = False
            else:
                if elapsed > SYNC_TIMEOUT:
                    logger.warning("同步任务执行时间过长: {:.2f}秒（等待当前任务完成）", elapsed)
                return
        
        _sync_positions_running = True
        _sync_start_time = time.time()
        
        def _execute_sync():
            global _sync_positions_running, _last_binance_sync_ts
            try:
                with SessionLocal() as db:
                    service = PositionService(db, settings)
                    sync_result = service.sync_positions_from_binance()
                    _last_binance_sync_ts = time.time()
                    if sync_result["created"] > 0 or sync_result["updated"] > 0 or sync_result["closed"] > 0:
                        logger.debug("同步币安持仓: 创建={} 更新={} 关闭={}", 
                                   sync_result["created"], sync_result["updated"], sync_result["closed"])
            except Exception as exc:
                if scheduler.running:
                    logger.warning("同步币安持仓失败: {}", exc)
            finally:
                elapsed = time.time() - _sync_start_time
                if elapsed > 5.0:
                    logger.warning("同步任务执行时间过长: {:.2f}秒", elapsed)
                _sync_positions_running = False
        
        # 异步提交到线程池，不阻塞调度器
        _sync_executor.submit(_execute_sync)

    def monitor_positions() -> None:
        """高频监控持仓并执行退出策略（异步执行，不阻塞调度器）"""
        global _current_position_monitor_interval, _current_manual_executor_interval
        global _monitor_positions_running, _monitor_start_time
        
        if not scheduler.running:
            return
        
        # 检查上次任务是否超时（超过400ms认为可能有问题）
        if _monitor_positions_running:
            elapsed = time.time() - _monitor_start_time
            if elapsed > MAX_MONITOR_RUNTIME:
                logger.error("监控任务运行超过 {:.2f} 秒，强制重置", elapsed)
                _monitor_positions_running = False
            else:
                if elapsed > MONITOR_TIMEOUT:
                    logger.warning("监控任务执行时间过长: {:.2f}秒（等待当前任务完成）", elapsed)
                # 正常执行中，跳过本次（避免堆积）
                return
        
        _monitor_positions_running = True
        _monitor_start_time = time.time()
        
        def _execute_monitor():
            global _monitor_positions_running, _current_position_monitor_interval
            try:
                with SessionLocal() as db:
                    service = PositionService(db, settings)
                    
                    # 只监控，不同步（同步由独立任务处理）
                    service.monitor_positions(sync_from_binance=False)
                    
                    # 获取活跃持仓数量（用于动态调整刷新频率）
                    active_positions = service.get_active_positions()
                    has_positions = len(active_positions) > 0
                    
                    # 动态调整刷新频率
                    base_interval = HIGH_FREQ_INTERVAL if has_positions else NORMAL_FREQ_INTERVAL
                    new_interval = max(base_interval, MIN_POSITION_MONITOR_INTERVAL)
                    
                    # 如果频率需要改变，更新任务
                    if new_interval != _current_position_monitor_interval:
                        _current_position_monitor_interval = new_interval
                        position_job = scheduler.get_job("position-monitor")
                        if position_job:
                            scheduler.reschedule_job(
                                "position-monitor",
                                trigger="interval",
                                seconds=new_interval
                            )
                            logger.info("持仓监控刷新频率已调整为: {:.3f}秒（{}毫秒） - {}", 
                                      new_interval, int(new_interval * 1000),
                                      "高频模式" if has_positions else "正常模式")
            except Exception as exc:
                if scheduler.running:
                    logger.error("持仓监控任务执行失败: {}", exc, exc_info=True)
            finally:
                elapsed = time.time() - _monitor_start_time
                if elapsed > MONITOR_TIMEOUT:
                    logger.warning("监控任务执行时间过长: {:.2f}秒（目标<{:.2f}秒）", elapsed, MONITOR_TIMEOUT)
                _monitor_positions_running = False
        
        # 异步提交到线程池，不阻塞调度器
        # 这样即使执行时间较长，也不会影响下一次调度
        _monitor_executor.submit(_execute_monitor)

    # 注册独立的币安持仓同步任务（低频，不阻塞监控）
    if not scheduler.get_job("binance-sync"):
        scheduler.add_job(
            sync_positions_from_binance,
            "interval",
            seconds=BINANCE_SYNC_INTERVAL,
            id="binance-sync",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=1,
            max_instances=3,
        )
        logger.info("币安持仓同步任务已启动，同步间隔: {:.1f}秒", BINANCE_SYNC_INTERVAL)
    
    # 注册高频持仓监控任务（快速执行，不包含同步操作）
    if not scheduler.get_job("position-monitor"):
        initial_interval = max(HIGH_FREQ_INTERVAL, MIN_POSITION_MONITOR_INTERVAL)
        scheduler.add_job(
            monitor_positions, 
            "interval", 
            seconds=initial_interval, 
            id="position-monitor", 
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=1,
            max_instances=3,
        )
        logger.info("持仓监控已启动，初始刷新频率: {:.3f}秒（{}毫秒）", 
                  initial_interval, int(initial_interval * 1000))
        
        # 系统启动时立即同步一次币安持仓，确保监控所有持仓（包括非系统下单的）
        try:
            with SessionLocal() as db:
                service = PositionService(db, settings)
                sync_result = service.sync_positions_from_binance()
                if sync_result["created"] > 0 or sync_result["updated"] > 0 or sync_result["closed"] > 0:
                    logger.info("系统启动时同步币安持仓: 创建={} 更新={} 关闭={}", 
                              sync_result["created"], sync_result["updated"], sync_result["closed"])
                else:
                    logger.debug("系统启动时同步币安持仓: 无变化")
                
                # 立即执行一次监控，确保所有持仓都被检查
                active_positions = service.get_active_positions()
                if active_positions:
                    logger.info("系统启动时检测到 %d 个活跃持仓，立即开始监控退出策略", len(active_positions))
                    service.monitor_positions(sync_from_binance=False)  # 刚同步过，不需要再同步
        except Exception as exc:
            logger.warning("系统启动时同步币安持仓失败（将继续监控）: {}", exc, exc_info=True)
