import asyncio
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import ManualPlanStatus
from app.services.announcement_service import AnnouncementService
from app.services.analytics_service import HistoricalAnalyzer
from app.services.execution_service import ExecutionService
from app.services.manual_plan_service import ManualPlanService
from app.services.position_service import PositionService
from app.services.report_service import DailyReporter
from app.services.window_return_service import WindowReturnService

# APScheduler 持续在后台触发公告抓取与日报发送任务
scheduler = BackgroundScheduler(timezone="UTC")

# 用于跟踪已启动精确执行线程的计划，避免重复启动
_precision_threads: dict[str, threading.Thread] = {}

# 动态刷新频率状态（模块级别变量）
_current_position_monitor_interval: float = 1.0  # 初始值：1秒
_current_manual_executor_interval: float = 0.1  # 初始值：100ms


def start_scheduler() -> None:
    """在 FastAPI 启动时调用，注册所有需要的定时任务。"""

    settings = get_settings()
    if not scheduler.running:
        scheduler.start()

    def poll_announcements() -> None:
        """轮询币安公告，保存到数据库并等待人工审核。"""
        try:
            with SessionLocal() as db:
                service = AnnouncementService(db, settings)
                new_records = service.sync_from_sources()
                if new_records:
                    logger.info("新增公告 %s 条", len(new_records))
                else:
                    logger.debug("本次轮询未发现新公告")
        except Exception as exc:
            logger.error("公告轮询失败: %s", exc, exc_info=True)

    if not scheduler.get_job("announcement-poll"):
        scheduler.add_job(
            poll_announcements,
            "interval",
            seconds=settings.announcement_poll_interval,
            id="announcement-poll",
            replace_existing=True,
        )

    def send_daily_report() -> None:
        """每天 UTC 00:05 构建日报并发送到邮箱（若 SMTP 已配置）。"""

        with SessionLocal() as db:
            reporter = DailyReporter(db, settings)
            asyncio.run(reporter.build_and_send())

    if not scheduler.get_job("daily-report"):
        scheduler.add_job(send_daily_report, "cron", hour=0, minute=5, id="daily-report", replace_existing=True)

    def run_history_analysis() -> None:
        with SessionLocal() as db:
            analyzer = HistoricalAnalyzer(db, settings)
            analyzer.sync_pending()

    if not scheduler.get_job("analysis"):
        scheduler.add_job(run_history_analysis, "interval", minutes=30, id="analysis", replace_existing=True)

    def run_window_returns() -> None:
        with SessionLocal() as db:
            service = WindowReturnService(db, settings)
            service.run()

    if not scheduler.get_job("window-returns"):
        scheduler.add_job(run_window_returns, "interval", minutes=60, id="window-returns", replace_existing=True)

    def execute_manual_plans() -> None:
        """执行手动计划，支持精确模式（毫秒级精度）"""
        from datetime import datetime, timezone
        
        # 检查调度器是否还在运行，避免在关闭后执行
        if not scheduler.running:
            logger.debug("调度器已关闭，跳过手动计划执行任务")
            return
        
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
                                    logger.debug("计划 %s 提前订阅WebSocket: %s (距离执行还有 %.1f 分钟)", 
                                               plan.id, symbol, time_diff / 60)
                                except Exception as exc:
                                    logger.warning("订阅WebSocket失败 ({}): {}", symbol, exc)
                        
                        # 如果计划在精确模式阈值内，且尚未启动精确执行线程
                        if 0 < time_diff <= settings.manual_plan_precision_threshold:
                            if plan.id not in _precision_threads or not _precision_threads[plan.id].is_alive():
                                logger.info("计划 %s 将在 %.2f秒后执行，启动精确执行模式", plan.id, time_diff)
                                
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
                                                logger.info("计划 %s 精确执行完成，执行时间: %s，延迟: %.2f毫秒", 
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
            # 如果调度器正在关闭，忽略错误
            if scheduler.running:
                logger.error("手动计划执行任务失败: {}", exc, exc_info=True)
            else:
                logger.debug("调度器关闭中，忽略手动计划执行错误")

    if not scheduler.get_job("manual-executor"):
        check_interval = settings.manual_plan_check_interval
        scheduler.add_job(
            execute_manual_plans, 
            "interval", 
            seconds=check_interval, 
            id="manual-executor", 
            replace_existing=True,
            max_instances=3  # 允许最多3个并发实例，避免任务堆积
        )
        logger.info("手动计划执行器已启动，检查间隔: %.3f秒（%d毫秒）", check_interval, int(check_interval * 1000))

    # 动态刷新频率配置
    HIGH_FREQ_INTERVAL = 0.2  # 有持仓时：200ms
    NORMAL_FREQ_INTERVAL = 1.0  # 无持仓时：1秒
    
    # 初始化手动执行器间隔
    _current_manual_executor_interval = settings.manual_plan_check_interval

    def monitor_positions() -> None:
        """实时监控持仓并执行退出策略，动态调整刷新频率"""
        global _current_position_monitor_interval, _current_manual_executor_interval
        
        # 检查调度器是否还在运行，避免在关闭后执行
        if not scheduler.running:
            logger.debug("调度器已关闭，跳过持仓监控任务")
            return
        
        try:
            with SessionLocal() as db:
                service = PositionService(db, settings)
                
                # 执行监控（包含同步币安持仓，确保监控所有持仓包括非系统下单的）
                # 每次监控时同步一次，但频率不要太高（避免API限流）
                # 这里每次都会同步，但sync_positions_from_binance内部有错误处理，不会影响监控
                service.monitor_positions(sync_from_binance=True)
                
                # 获取活跃持仓数量（用于动态调整刷新频率）
                active_positions = service.get_active_positions()
                has_positions = len(active_positions) > 0
                
                # 动态调整刷新频率
                new_interval = HIGH_FREQ_INTERVAL if has_positions else NORMAL_FREQ_INTERVAL
                
                # 如果频率需要改变，更新所有相关任务
                if new_interval != _current_position_monitor_interval:
                    _current_position_monitor_interval = new_interval
                    
                    # 更新持仓监控任务
                    position_job = scheduler.get_job("position-monitor")
                    if position_job:
                        scheduler.reschedule_job(
                            "position-monitor",
                            trigger="interval",
                            seconds=new_interval
                        )
                        logger.info("持仓监控刷新频率已调整为: %.3f秒（%d毫秒） - %s", 
                                  new_interval, int(new_interval * 1000),
                                  "高频模式" if has_positions else "正常模式")
                    
                    # 更新手动计划执行器（有持仓时也提高频率）
                    manual_job = scheduler.get_job("manual-executor")
                    if manual_job:
                        new_manual_interval = HIGH_FREQ_INTERVAL if has_positions else settings.manual_plan_check_interval
                        if new_manual_interval != _current_manual_executor_interval:
                            _current_manual_executor_interval = new_manual_interval
                            scheduler.reschedule_job(
                                "manual-executor",
                                trigger="interval",
                                seconds=new_manual_interval
                            )
                            logger.info("手动计划执行器刷新频率已调整为: %.3f秒（%d毫秒）", 
                                      new_manual_interval, int(new_manual_interval * 1000))
        except Exception as exc:
            # 如果调度器正在关闭，忽略错误
            if scheduler.running:
                logger.error("持仓监控任务执行失败: {}", exc, exc_info=True)
            else:
                logger.debug("调度器关闭中，忽略持仓监控错误")

    if not scheduler.get_job("position-monitor"):
        scheduler.add_job(
            monitor_positions, 
            "interval", 
            seconds=_current_position_monitor_interval, 
            id="position-monitor", 
            replace_existing=True,
            max_instances=3  # 允许最多3个并发实例，避免任务堆积
        )
        logger.info("持仓监控已启动，初始刷新频率: %.3f秒（%d毫秒）", 
                  _current_position_monitor_interval, int(_current_position_monitor_interval * 1000))
        
        # 系统启动时立即同步一次币安持仓，确保监控所有持仓（包括非系统下单的）
        # 这样即使系统意外中断并重启，也能立即恢复监控
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
