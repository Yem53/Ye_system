from __future__ import annotations

from datetime import UTC, datetime

import httpx
from dateutil.relativedelta import relativedelta
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.announcement import Announcement
from app.models.enums import AnnouncementStatus
from app.services.announcement_service import AnnouncementFetcher


class AnnouncementBackfillService:
    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.fetcher = AnnouncementFetcher(self.settings)
        # 使用与 AnnouncementFetcher 相同的代理配置
        proxy = None
        if self.settings.http_proxy:
            proxy = self.settings.http_proxy
        self.client = httpx.Client(
            timeout=self.settings.binance_http_timeout,
            proxy=proxy,
        )

    def backfill(self, months: int = 6, max_pages: int = 100, languages: list[str] = None) -> int:
        """抓取历史公告
        
        Args:
            months: 抓取最近几个月的历史公告
            max_pages: 每个数据源最多抓取多少页
            languages: 语言列表，如 ["en", "zh"]，默认只抓取英文（原API默认语言）
        """
        if languages is None:
            languages = ["en"]  # 默认只抓英文
        
        cutoff = datetime.now(UTC) - relativedelta(months=months)
        total = 0
        
        # 为每种语言抓取公告
        for lang in languages:
            logger.info("开始抓取 %s 语言的历史公告", lang)
            for source, url in (("alpha", self.settings.alpha_feed_url), ("futures", self.settings.futures_feed_url)):
                for page in range(1, max_pages + 1):
                    items = self._fetch_page(source, url, page, language=lang)
                    if not items:
                        break
                    stop = False
                    for item in items:
                        if item.listing_time and item.listing_time < cutoff:
                            stop = True
                            break
                        # 为不同语言生成不同的 source_id，避免重复
                        lang_source_id = f"{item.source_id}_{lang}" if lang != "en" else item.source_id
                        if self._exists(lang_source_id):
                            continue
                        status = (
                            AnnouncementStatus.PENDING_REVIEW
                            if self.settings.approval_required
                            else AnnouncementStatus.APPROVED
                        )
                        # 在 metadata 中记录语言信息
                        metadata = item.metadata.copy() if item.metadata else {}
                        metadata["language"] = lang
                        ann = Announcement(
                            source_id=lang_source_id,
                            source=f"{item.source}_{lang}" if lang != "en" else item.source,
                            title=item.title,
                            content=item.content,
                            symbol=item.symbol,
                            listing_time=item.listing_time,
                            timezone_label=item.timezone_label,
                            status=status,
                            url=item.url,
                            extra_metadata=metadata,
                        )
                        self.db.add(ann)
                        total += 1
                    self.db.commit()
                if stop:
                    break
        logger.info("历史公告补抓完成，共新增 %s 条", total)
        return total

    def _fetch_page(self, source: str, url: str, page: int, language: str = "en"):
        params = {"pageNo": page, "pageSize": 20}
        # 添加语言参数（如果API支持）
        if language and language != "en":
            params["language"] = language
        try:
            resp = self.client.get(url, params=params)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover
            logger.warning("无法补抓%s公告 (语言: %s): %s", source, language, exc)
            return []
        payload = resp.json()
        parser_fn = self.fetcher._parse_alpha if source == "alpha" else self.fetcher._parse_futures
        return parser_fn(payload)

    def _exists(self, source_id: str) -> bool:
        stmt = select(Announcement).where(Announcement.source_id == source_id)
        return self.db.scalar(stmt) is not None
