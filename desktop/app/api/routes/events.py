# -*- coding: utf-8 -*-
"""
事件路由 —— /api/events 未读事件简报（手机端首页角标用）。
"""

import logging

from fastapi import APIRouter, HTTPException

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import Message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["事件"])


@router.get("/unread")
async def get_unread_events():
    """未读事件简报（用于手机端首页角标）。"""
    try:
        with DatabaseManager.session() as session:
            unread = q.get_unread_message_count(session)
            has_dedup = session.query(Message).filter(
                Message.msg_type == "dedup_alert",
                Message.is_read == False,
                Message.is_dismissed == False,
            ).count()

            latest_unread = q.get_unread_messages(session, limit=1)
            latest = None
            if latest_unread:
                m = latest_unread[0]
                latest = {"id": m.id, "msg_type": m.msg_type, "title": m.title}

            return {
                "unread_count": unread,
                "has_dedup_alerts": has_dedup > 0,
                "latest": latest,
            }
    except Exception as e:
        logger.error(f"获取未读事件简报失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
