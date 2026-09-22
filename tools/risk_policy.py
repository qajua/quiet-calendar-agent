#!/usr/bin/env python3
"""Deterministic risk gate for notification-triggered calendar requests."""

import re


HIGH_RISK = (
    "删除", "取消", "通知参会人", "邀请参会人", "发送消息",
    "付款", "支付", "转账", "购买", "下单", "密码", "验证码", "登录",
)
CALENDAR_MUTATION = ("改到", "改为", "调整到", "改期", "创建日程", "添加日程", "安排会议")
READ_ONLY = ("查询日程", "查看日程", "有什么安排", "什么时候有空", "空闲时间")


def classify_request(title, text):
    combined = re.sub(r"\s+", " ", f"{title or ''} {text or ''}").strip()
    high_matches = [word for word in HIGH_RISK if word in combined]
    if high_matches:
        return {
            "risk": "high",
            "decision": "blocked",
            "reason": f"包含高风险动作：{'、'.join(high_matches)}；当前版本禁止后台执行。",
            "supported": False,
            "executor": None,
        }
    if any(word in combined for word in CALENDAR_MUTATION):
        if any(word in combined for word in ("会议结束后", "会后", "紧接着", "会议纪要")):
            executor = "batch"
        elif any(word in combined for word in ("冲突", "空档", "提醒")):
            executor = "smart"
        else:
            executor = "notice"
        return {
            "risk": "medium",
            "decision": "needs_confirmation",
            "reason": "会修改手机日历；确认后通知正文会发送给配置的 OpenAI 规划器，必须在电脑控制台明确批准。",
            "supported": True,
            "executor": executor,
        }
    if any(word in combined for word in READ_ONLY):
        return {
            "risk": "low",
            "decision": "observed",
            "reason": "只读意图风险较低，但当前执行器尚未实现日程问答。",
            "supported": False,
            "executor": None,
        }
    return {
        "risk": "none",
        "decision": "ignored",
        "reason": "未识别为支持的日历请求，不会交给模型或执行器。",
        "supported": False,
        "executor": None,
    }
