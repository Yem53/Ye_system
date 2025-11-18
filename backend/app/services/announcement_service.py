from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import httpx
from dateutil import parser
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.announcement import Announcement
from app.models.enums import AnnouncementStatus


@dataclass
class NormalizedAnnouncement:
    """整理后的公告结构，方便后续统一写入数据库。"""

    source_id: str
    source: str
    title: str
    content: str
    symbol: str | None
    listing_time: datetime | None
    timezone_label: str | None
    url: str | None
    metadata: dict


class AnnouncementFetcher:
    """负责请求币安公告接口，并转为 NormalizedAnnouncement。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        proxy = None
        if self.settings.http_proxy:
            proxy = self.settings.http_proxy
        # 使用真实浏览器的User-Agent和请求头，避免被币安API拒绝
        self.client = httpx.Client(
            timeout=self.settings.binance_http_timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Content-Type": "application/json",
                "Origin": "https://www.binance.com",
                "Referer": "https://www.binance.com/",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "cache-control": "no-cache",
            },
            proxy=proxy,
            follow_redirects=True,
        )

    def fetch(self) -> list[NormalizedAnnouncement]:
        items: list[NormalizedAnnouncement] = []
        # 构建要抓取的 URL 列表（币安主站）
        # 注意：优先尝试futures，通常更稳定；如果某个API端点返回404，系统会跳过并继续尝试其他端点
        urls_to_fetch = [
            ("futures", self.settings.futures_feed_url),  # 优先尝试futures
            ("alpha", self.settings.alpha_feed_url),
        ]
        # 如果配置了日本站点，也添加到列表中（可选）
        if self.settings.alpha_feed_url_jp:
            urls_to_fetch.append(("alpha_jp", self.settings.alpha_feed_url_jp))
        if self.settings.futures_feed_url_jp:
            urls_to_fetch.append(("futures_jp", self.settings.futures_feed_url_jp))
        
        for source, url in urls_to_fetch:
            try:
                logger.debug("正在获取 {} 公告: {}", source, url)
                # 构建请求参数
                params = {"pageNo": 1, "pageSize": 20}
                # 如果是日本站点，可以添加语言参数（如果API支持）
                if source.endswith("_jp"):
                    params["language"] = "ja"  # 日语
                # 发送请求，使用更长的超时时间
                response = self.client.get(url, params=params, timeout=30.0)
                response.raise_for_status()
                
                # 检查响应内容类型
                content_type = response.headers.get("content-type", "").lower()
                if "application/json" not in content_type:
                    logger.warning("{} API返回非JSON格式: {}，跳过", source, content_type)
                    continue
                
                payload = response.json()
                
                # 检查响应结构
                if not isinstance(payload, dict):
                    logger.warning("{} API返回格式异常: 期望dict，实际{}，跳过", source, type(payload))
                    continue
                
                logger.debug("成功获取 {} 公告，响应大小: {}", source, len(str(payload)))
            except Exception as exc:  # pragma: no cover - network failure logging
                error_msg = str(exc)
                logger.warning("无法获取{}公告 (URL: {}): {}", source, url, error_msg)
                
                # 检查是否是HTTP错误
                if hasattr(exc, 'response'):
                    status_code = getattr(exc.response, 'status_code', None)
                    if status_code:
                        logger.error("HTTP错误码: {}", status_code)
                        if status_code == 404:
                            logger.error("API端点不存在，可能需要更新URL配置 / API endpoint not found, may need to update URL config")
                        elif status_code == 403:
                            logger.error("访问被禁止，可能是代理问题或需要更新请求头 / Access forbidden, may be proxy issue or need to update headers")
                
                # 如果是代理相关错误，提供更详细的提示
                if "proxy" in error_msg.lower() or "connection" in error_msg.lower():
                    if not self.settings.http_proxy:
                        logger.warning("提示: 如果无法连接，请在 .env 中配置 HTTP_PROXY 代理")
                    else:
                        logger.warning("提示: 当前代理配置为 {}，请检查代理是否可用", self.settings.http_proxy)
                continue

            # 根据 source 类型选择解析函数
            if source.startswith("alpha"):
                parser_fn = self._parse_alpha
            else:
                parser_fn = self._parse_futures
            
            parsed = parser_fn(payload)
            # 为日本站点的公告添加标识
            if source.endswith("_jp"):
                parsed = [
                    NormalizedAnnouncement(
                        source_id=item.source_id,
                        source=item.source + "_jp",
                        title=item.title,
                        content=item.content,
                        symbol=item.symbol,
                        listing_time=item.listing_time,
                        timezone_label=item.timezone_label,
                        url=item.url,
                        metadata=item.metadata,
                    )
                    for item in parsed
                ]
            items.extend(parsed)
            logger.info("从 {} 获取到 {} 条公告", source, len(parsed))
        return items

    def _parse_alpha(self, payload: dict) -> list[NormalizedAnnouncement]:
        data = payload.get("data") or {}
        items = data.get("items") or data.get("catalogs") or []
        normalized: list[NormalizedAnnouncement] = []
        for raw in items:
            source_id = str(raw.get("id") or raw.get("noticeId") or raw.get("code"))
            title = raw.get("title") or ""
            content = raw.get("content") or raw.get("body") or ""
            symbol = self._extract_symbol(title, content)
            announce_time = raw.get("releaseTime") or raw.get("publishTime")
            listing_time = self._safe_parse_time(announce_time)
            url = raw.get("url") or raw.get("link")
            normalized.append(
                NormalizedAnnouncement(
                    source_id=source_id,
                    source="alpha",
                    title=title,
                    content=content,
                    symbol=symbol,
                    listing_time=listing_time,
                    timezone_label="UTC+8",
                    url=url,
                    extra_metadata=raw,
                )
            )
        return normalized

    def _parse_futures(self, payload: dict) -> list[NormalizedAnnouncement]:
        data = payload.get("data") or {}
        items: Iterable = data.get("rows") or data.get("items") or []
        normalized: list[NormalizedAnnouncement] = []
        for raw in items:
            source_id = str(raw.get("id") or raw.get("noticeId") or raw.get("code"))
            title = raw.get("title") or ""
            content = raw.get("content") or raw.get("message") or ""
            symbol = self._extract_symbol(title, content)
            schedule_info = raw.get("startTime") or raw.get("goLiveTime")
            listing_time = self._safe_parse_time(schedule_info)
            url = raw.get("url") or raw.get("link")
            normalized.append(
                NormalizedAnnouncement(
                    source_id=source_id,
                    source="futures",
                    title=title,
                    content=content,
                    symbol=symbol,
                    listing_time=listing_time,
                    timezone_label=raw.get("timeZone") or "UTC+8",
                    url=url,
                    extra_metadata=raw,
                )
            )
        return normalized

    def _extract_symbol(self, title: str, content: str) -> str | None:
        candidates = re.findall(r"([A-Z]{3,10})USDT", title.upper() + content.upper())
        if candidates:
            return candidates[0]
        match = re.search(r"\(([A-Z0-9]{3,10})\)", title)
        if match:
            return match.group(1)
        return None

    def _safe_parse_time(self, value: str | int | None) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            if isinstance(value, (int, float)):
                dt = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
            else:
                dt = parser.parse(str(value))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:  # pragma: no cover - parsing safety
            return None


class AnnouncementService:
    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.fetcher = AnnouncementFetcher(self.settings)

    def sync_from_sources(self) -> list[Announcement]:
        """从接口抓取公告，避免重复写入，保持待审核状态。"""
        new_records: list[Announcement] = []
        fetched_items = []
        
        try:
            fetched_items = self.fetcher.fetch()
            logger.debug("从币安API抓取到 {} 条原始公告", len(fetched_items))
        except Exception as exc:
            logger.error("抓取公告失败: {}", exc, exc_info=True)
            return new_records
        
        for item in fetched_items:
            if not item.source_id:
                logger.debug("跳过无source_id的公告项")
                continue
            existing = self.db.scalar(select(Announcement).where(Announcement.source_id == item.source_id))
            if existing:
                logger.debug("公告 {} 已存在，跳过", item.source_id)
                continue
            status = AnnouncementStatus.PENDING_REVIEW if self.settings.approval_required else AnnouncementStatus.APPROVED
            if not self.settings.approval_required:
                status = AnnouncementStatus.APPROVED
            announcement = Announcement(
                source_id=item.source_id,
                source=item.source,
                title=item.title,
                content=item.content,
                symbol=item.symbol,
                listing_time=item.listing_time,
                timezone_label=item.timezone_label,
                status=status,
                url=item.url,
                extra_metadata=item.metadata,
            )
            self.db.add(announcement)
            new_records.append(announcement)
            logger.debug("添加新公告: {} (交易对: {})", item.title[:50], item.symbol)
        
        if new_records:
            try:
                self.db.commit()
                logger.info("成功保存 {} 条新公告到数据库", len(new_records))
            except Exception as exc:
                logger.error("保存公告到数据库失败: {}", exc, exc_info=True)
                self.db.rollback()
                return []
        else:
            logger.debug("本次抓取没有新公告需要保存")
        
        return new_records

    def list_pending(self) -> list[Announcement]:
        """列出尚未审核的公告（仪表盘展示使用）。"""

        stmt = (
            select(Announcement)
            .where(Announcement.status.in_([AnnouncementStatus.NEW, AnnouncementStatus.PENDING_REVIEW]))
            .order_by(Announcement.created_at.desc())
        )
        return list(self.db.scalars(stmt))

    def update_status(self, announcement_id: str, status: AnnouncementStatus) -> Announcement:
        announcement = self.db.get(Announcement, announcement_id)
        if not announcement:
            raise ValueError("announcement not found")
        announcement.status = status
        self.db.commit()
        self.db.refresh(announcement)
        return announcement
