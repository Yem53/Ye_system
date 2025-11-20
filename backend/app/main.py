from pathlib import Path

from datetime import datetime
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.scheduler import scheduler, start_scheduler
from app.db.init_db import init_db
from app.db.session import get_db
from app.models.enums import ManualPlanStatus
from app.models.manual_plan import ManualPlan
from app.services.manual_plan_service import ManualPlanService

settings = get_settings()

# FastAPI 既提供 API 也提供仪表盘页面
app = FastAPI(
    title=settings.app_name,
    description="量化交易新闻分析系统 / Quantitative Trading News Analysis System",
    version="1.0.0"
)
app.include_router(api_router)
# 通过绝对路径加载静态文件/模板，避免容器中找不到目录
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    logger.info("初始化数据库并启动调度器…")
    init_db()
    start_scheduler()
    
    # 启动WebSocket价格订阅服务（但不自动订阅任何交易对，按需订阅）
    if settings.websocket_price_enabled:
        try:
            from app.services.binance_websocket_service import get_websocket_price_service
            
            # 如果配置了初始订阅列表，则订阅；否则不订阅任何交易对，等待按需订阅
            symbols = None
            if settings.websocket_price_symbols:
                symbols = [s.strip().upper() for s in settings.websocket_price_symbols.split(",") if s.strip()]
            
            ws_service = get_websocket_price_service()
            ws_service.start(symbols=symbols)  # symbols=None时，不订阅任何交易对
            if symbols:
                logger.info("WebSocket价格订阅服务已启动，初始订阅 {} 个交易对", len(symbols))
            else:
                logger.info("WebSocket价格订阅服务已启动（按需订阅模式：交易前5分钟自动订阅）")
        except Exception as exc:
            logger.error("启动WebSocket价格订阅服务失败: {}", exc, exc_info=True)


@app.on_event("shutdown")
def on_shutdown() -> None:
    """优雅关闭调度器和WebSocket服务"""
    logger.info("正在关闭调度器和相关服务…")
    
    # 关闭调度器（先停止调度，再关闭线程池）
    if scheduler.running:
        try:
            scheduler.shutdown(wait=False)  # wait=False 不等待任务完成，避免阻塞
            logger.info("调度器已关闭")
        except Exception as exc:
            logger.warning("关闭调度器时出错: {}", exc)
    
    # 关闭线程池执行器（等待正在执行的任务完成）
    try:
        from app.core.scheduler import _monitor_executor, _sync_executor, _precision_threads
        
        # 等待监控和同步线程池关闭
        logger.info("正在关闭线程池执行器…")
        _monitor_executor.shutdown(wait=True)
        _sync_executor.shutdown(wait=True)
        logger.info("线程池执行器已关闭")
        
        # 等待精确执行线程完成（最多等待2秒）
        if _precision_threads:
            logger.info("正在等待 {} 个精确执行线程完成…", len(_precision_threads))
            for plan_id, thread in list(_precision_threads.items()):
                if thread.is_alive():
                    thread.join(timeout=2)
                    if thread.is_alive():
                        logger.warning("精确执行线程 {} 未能在2秒内完成，强制继续关闭", plan_id)
            logger.info("精确执行线程已清理")
    except Exception as exc:
        logger.warning("关闭线程池时出错: {}", exc)
    
    # 关闭WebSocket服务
    if settings.websocket_price_enabled:
        try:
            from app.services.binance_websocket_service import get_websocket_price_service
            ws_service = get_websocket_price_service()
            ws_service.stop()
            logger.info("WebSocket价格订阅服务已关闭")
        except Exception as exc:
            logger.warning("关闭WebSocket服务时出错: {}", exc)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """仪表盘首页，展示交易计划的实时状态。"""

    manual_plans = list(db.scalars(select(ManualPlan).order_by(ManualPlan.listing_time.asc())))
    
    # 获取系统配置的默认值，传递给模板（每次请求时重新获取，确保使用最新配置）
    current_settings = get_settings()
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "manual_plans": manual_plans,
            "settings": settings,
            # 传递系统配置默认值给手动计划表单
            "default_leverage": current_settings.leverage,
            "default_position_pct": current_settings.position_pct,
            "default_trailing_exit_pct": current_settings.trailing_exit_pct,
            "default_stop_loss_pct": current_settings.stop_loss_pct,
            "default_max_slippage_pct": current_settings.max_slippage_pct,
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    """历史操作记录页面 / Trading History Page"""
    current_settings = get_settings()
    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "settings": current_settings,
        },
    )


@app.post("/manual-plans")
def submit_manual_plan(
    symbol: str = Form(...),
    side: str = Form("BUY"),
    listing_time: str = Form(..., description="ISO 时间"),
    leverage: float = Form(None),  # 使用 None 作为默认值，然后从系统配置获取
    position_pct: float = Form(None),
    trailing_exit_pct: float = Form(None),
    stop_loss_pct: float = Form(None),
    max_slippage_pct: float = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    # 获取系统配置的默认值
    current_settings = get_settings()
    
    # 如果表单没有提供值，使用系统配置的默认值
    if leverage is None:
        leverage = current_settings.leverage
    if position_pct is None:
        position_pct = current_settings.position_pct
    if trailing_exit_pct is None:
        trailing_exit_pct = current_settings.trailing_exit_pct
    if stop_loss_pct is None:
        stop_loss_pct = current_settings.stop_loss_pct
    if max_slippage_pct is None:
        max_slippage_pct = current_settings.max_slippage_pct
    try:
        parsed_time = datetime.fromisoformat(listing_time)
        if parsed_time.tzinfo is None:
            from datetime import timezone

            parsed_time = parsed_time.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间格式错误: {exc}")

    service = ManualPlanService(db)
    service.create(
        {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "listing_time": parsed_time,
            "leverage": leverage,
            "position_pct": position_pct,
            "trailing_exit_pct": trailing_exit_pct,
            "stop_loss_pct": stop_loss_pct,
            "max_slippage_pct": max_slippage_pct,
            "notes": notes or None,
        }
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/manual-plans/{plan_id}/cancel")
def cancel_manual_plan(plan_id: str, db: Session = Depends(get_db)):
    """取消手动计划 / Cancel Manual Plan"""
    plan = db.get(ManualPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在 / Plan not found")
    
    plan.status = ManualPlanStatus.CANCELLED
    db.commit()
    
    return RedirectResponse(url="/", status_code=303)

