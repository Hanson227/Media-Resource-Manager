# -*- coding: utf-8 -*-
"""
消息路由 —— /api/messages 真实数据库查询端点。
"""

import logging

from fastapi import APIRouter, Query, HTTPException

from app.api.schemas import MessageItem, MessageListResponse, StatusResponse
from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/messages", tags=["消息"])


@router.get("", response_model=MessageListResponse)
def list_messages(
    unread_only: bool = Query(False, description="仅显示未读"),
    limit: int = Query(50, ge=1, le=200),
):
    """列出消息。"""
    try:
        with DatabaseManager.session() as session:
            if unread_only:
                msgs = q.get_unread_messages(session, limit=limit)
            else:
                msgs = q.get_all_messages(session, limit=limit)

            unread = q.get_unread_message_count(session)

            items = [
                MessageItem(
                    id=m.id, msg_type=m.msg_type, title=m.title,
                    body=m.body, is_read=m.is_read, created_at=m.created_at,
                )
                for m in msgs
            ]
            return MessageListResponse(messages=items, unread_count=unread)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/unread-count")
def get_unread_count():
    """获取未读消息数量。"""
    try:
        with DatabaseManager.session() as session:
            count = q.get_unread_message_count(session)
            return {"unread_count": count}
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/{msg_id}/read", response_model=StatusResponse)
def mark_read(msg_id: int):
    """标记消息为已读。"""
    try:
        with DatabaseManager.session() as session:
            q.mark_message_read(session, msg_id)
            return StatusResponse(success=True, message=f"消息 {msg_id} 已标记为已读")
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/read-all", response_model=StatusResponse)
def mark_all_read():
    """标记所有消息为已读。"""
    try:
        with DatabaseManager.session() as session:
            q.mark_all_messages_read(session)
            return StatusResponse(success=True, message="所有消息已标记为已读")
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.delete("/{msg_id}", response_model=StatusResponse)
def dismiss_message(msg_id: int):
    """忽略/关闭一条消息。"""
    try:
        with DatabaseManager.session() as session:
            q.dismiss_message(session, msg_id)
            return StatusResponse(success=True, message=f"消息 {msg_id} 已忽略")
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
