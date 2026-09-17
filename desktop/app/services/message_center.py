# -*- coding: utf-8 -*-
"""
消息中心服务 —— 负责消息的创建、查询和管理。

所有提醒（查重、错误、系统通知）均通过此服务写入数据库，
界面通过查询此服务的未读计数来更新徽标。
"""

import json
import logging
from typing import Optional

from app.db.engine import DatabaseManager
from app.db.queries import (
    create_message, get_unread_messages, get_all_messages,
    mark_message_read, mark_all_messages_read, dismiss_message,
    get_unread_message_count,
)
from app.db.models import Message


def _msg_to_dict(msg: Message) -> dict:
    """将 ORM Message 对象转为 dict，避免 detached session 问题。"""
    return {
        "id": msg.id,
        "msg_type": msg.msg_type,
        "title": msg.title,
        "body": msg.body,
        "is_read": msg.is_read,
        "is_dismissed": msg.is_dismissed,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "action_data": msg.action_data,
    }

logger = logging.getLogger(__name__)


class MessageCenter:
    """消息中心 —— 提醒消息的创建和查询入口。

    用法:
        MessageCenter.create_warning("标题", "正文")
        count = MessageCenter.get_unread_count()
        messages = MessageCenter.get_unread()      # list[dict]，不是 ORM 对象

    注意：查询方法返回 **dict**（见 `_msg_to_dict`），界面请按键取值
    （`msg["is_read"]`），不要再按 ORM 属性访问 —— 对话框曾因此抛
    AttributeError 而打不开。
    """

    # ============================================================
    # 消息创建
    # ============================================================

    @staticmethod
    def create_info(title: str, body: str = "",
                    related_unit_id: Optional[int] = None) -> Optional[Message]:
        """创建一条普通通知消息。"""
        try:
            with DatabaseManager.session() as session:
                return create_message(
                    session,
                    msg_type="info",
                    title=title,
                    body=body,
                    related_unit_id=related_unit_id,
                )
        except Exception as e:
            logger.error(f"创建消息失败: {e}")
            return None

    @staticmethod
    def create_warning(title: str, body: str = "",
                       related_unit_id: Optional[int] = None) -> Optional[Message]:
        """创建一条警告消息。"""
        try:
            with DatabaseManager.session() as session:
                return create_message(
                    session,
                    msg_type="warning",
                    title=title,
                    body=body,
                    related_unit_id=related_unit_id,
                )
        except Exception as e:
            logger.error(f"创建警告失败: {e}")
            return None

    @staticmethod
    def create_error(title: str, body: str = "",
                     related_unit_id: Optional[int] = None) -> Optional[Message]:
        """创建一条错误消息。"""
        try:
            with DatabaseManager.session() as session:
                return create_message(
                    session,
                    msg_type="error",
                    title=title,
                    body=body,
                    related_unit_id=related_unit_id,
                )
        except Exception as e:
            logger.error(f"创建错误消息失败: {e}")
            return None

    @staticmethod
    def create_dedup_alert(
        unit_a_name: str, unit_b_name: str,
        similarity: float, match_count: int,
        dedup_result_id: int,
        related_unit_id: Optional[int] = None,
    ) -> Optional[Message]:
        """创建一条查重提醒消息。

        参数:
            unit_a_name: 资源单元 A 的名称。
            unit_b_name: 资源单元 B 的名称。
            similarity: 杰卡德相似度。
            match_count: 匹配文件对数。
            dedup_result_id: 查重结果数据库 ID。
            related_unit_id: 关联的资源单元 ID。
        """
        title = f"发现重复单元: {unit_a_name} ⟷ {unit_b_name}"
        body = (
            f"两个资源单元的相似度为 {similarity * 100:.1f}%，"
            f"共发现 {match_count} 对匹配文件。"
        )
        action_data = json.dumps({
            "action": "view_dedup",
            "dedup_result_id": dedup_result_id,
            "unit_a": unit_a_name,
            "unit_b": unit_b_name,
        }, ensure_ascii=False)

        try:
            with DatabaseManager.session() as session:
                return create_message(
                    session,
                    msg_type="dedup_alert",
                    title=title,
                    body=body,
                    related_unit_id=related_unit_id,
                    action_data=action_data,
                )
        except Exception as e:
            logger.error(f"创建查重提醒失败: {e}")
            return None

    @staticmethod
    def create_related_digest(
        pairs: list[tuple],
        face_pairs: Optional[list[tuple]] = None,
        top_n: int = 8,
    ) -> Optional[Message]:
        """创建一条"疑似相关 / 同演员"汇总提醒。

        参数:
            pairs: [(unit_a_name, unit_b_name, file_match_count, similarity, types), ...]
                —— 有**文件级证据**的疑似相关（部分重叠）。
            face_pairs: [(unit_a_name, unit_b_name, hint_count), ...]
                —— 只有人脸线索的同演员对（零文件重叠）。
            top_n: 正文里列出证据最强的多少对。

        说明:
            实测本库 166 个单元会产生 200+ 对疑似相关（同场景/部分重叠）
            加 370+ 对同演员线索，逐条发消息会把消息中心刷屏，因此合并成一条摘要。
            两者必须分开陈述：同演员线索的杰卡德恒为 0（人脸不计入判定），
            混在一起用户会以为"这些也有文件重叠"。逐条明细都在 dedup_results。
        """
        face_pairs = face_pairs or []
        if not pairs and not face_pairs:
            return None

        ordered = sorted(pairs, key=lambda p: -p[2])
        lines = []
        for name_a, name_b, evidence, similarity, types in ordered[:top_n]:
            lines.append(
                f"• {name_a} ⟷ {name_b}（{evidence} 个文件重叠，"
                f"杰卡德 {similarity * 100:.1f}%: {types}）"
            )
        more = len(ordered) - top_n
        if more > 0:
            lines.append(f"…另有 {more} 对，详见查重结果")

        title = f"发现 {len(ordered)} 对疑似相关单元（不建议删除）"
        if face_pairs:
            title += f"，另有 {len(face_pairs)} 对同演员线索"

        body_parts = []
        if ordered:
            body_parts.append(
                "这些单元有同场景 / 部分文件重叠的迹象。"
                "它们不是重复，仅供你了解，不建议删除：\n" + "\n".join(lines)
            )
        if face_pairs:
            body_parts.append(
                f"另有 {len(face_pairs)} 对只在人脸维度相似（同一演员的不同作品，"
                f"0 个文件重叠）。它们不是重复，已单独归入查重页的「同演员」，"
                f"不计入查重结论。"
            )
        strongest = sorted(face_pairs, key=lambda p: -p[2])[:3]
        if strongest:
            body_parts.append("人脸线索最强的几对：\n" + "\n".join(
                f"• {a} ⟷ {b}（{n} 处人脸线索）" for a, b, n in strongest
            ))
        body = "\n\n".join(body_parts)

        action_data = json.dumps({
            "action": "view_related",
            "level": "related",
            "count": len(ordered),
            "face_only_count": len(face_pairs),
        }, ensure_ascii=False)

        try:
            with DatabaseManager.session() as session:
                return create_message(
                    session,
                    msg_type="dedup_alert",
                    title=title,
                    body=body,
                    action_data=action_data,
                )
        except Exception as e:
            logger.error(f"创建疑似相关提醒失败: {e}")
            return None

    # ============================================================
    # 消息查询
    # ============================================================

    @staticmethod
    def get_unread(limit: int = 50) -> list[dict]:
        """获取未读消息列表（返回 dict 而非 ORM 对象，避免 detached session 问题）。"""
        try:
            with DatabaseManager.session() as session:
                msgs = get_unread_messages(session, limit=limit)
                return [_msg_to_dict(m) for m in msgs]
        except Exception as e:
            logger.error(f"获取未读消息失败: {e}")
            return []

    @staticmethod
    def get_all(limit: int = 200) -> list[dict]:
        """获取所有消息列表（返回 dict 而非 ORM 对象）。"""
        try:
            with DatabaseManager.session() as session:
                msgs = get_all_messages(session, limit=limit)
                return [_msg_to_dict(m) for m in msgs]
        except Exception as e:
            logger.error(f"获取消息列表失败: {e}")
            return []

    @staticmethod
    def get_unread_count() -> int:
        """获取未读消息数量。"""
        try:
            with DatabaseManager.session() as session:
                return get_unread_message_count(session)
        except Exception as e:
            logger.error(f"获取未读计数失败: {e}")
            return 0

    # ============================================================
    # 消息操作
    # ============================================================

    @staticmethod
    def mark_read(msg_id: int) -> bool:
        """将单条消息标记为已读。"""
        try:
            with DatabaseManager.session() as session:
                mark_message_read(session, msg_id)
            return True
        except Exception as e:
            logger.error(f"标记已读失败: {e}")
            return False

    @staticmethod
    def mark_all_read() -> bool:
        """将所有消息标记为已读。"""
        try:
            with DatabaseManager.session() as session:
                mark_all_messages_read(session)
            return True
        except Exception as e:
            logger.error(f"全部已读失败: {e}")
            return False

    @staticmethod
    def dismiss(msg_id: int) -> bool:
        """忽略一条消息。"""
        try:
            with DatabaseManager.session() as session:
                dismiss_message(session, msg_id)
            return True
        except Exception as e:
            logger.error(f"忽略消息失败: {e}")
            return False
