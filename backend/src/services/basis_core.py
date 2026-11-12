"""测算依据（basis）纯函数层：指纹、差异归因。

计划在创建 / 复检时记录一份「测算依据」：当时生效的策略版本与单价 / 闸门条件、
热度算法参数、访问日志汇总、表档案汇总、测算日期（as_of，热度窗口的右端点）。
任何一项变化都意味着上次算出的省额与风险不再是同一口径 —— 指纹不同即「依据失效」。

本模块只吃 dict，不 import Session，可被 pytest 直接单测：

  fingerprint(payload) = sha256(canonical_json(payload))
  diff(old, new)       = 人话差异列表（策略升版 / 补录访问 / 表档案变化 / 窗口前移）

`old` 传计划保存的 basis_snapshot（可能为空），`new` 传当前 basis。
"""
from __future__ import annotations

import hashlib
import json

# 参与指纹计算的字段：按顺序取，缺失视为空（兼容历史计划）
PAYLOAD_KEYS = ("policy", "heat", "access", "profiles", "as_of")


def payload_of(basis: dict) -> dict:
    """只保留参与指纹的字段，剔除 fingerprint / captured_at 等元数据。"""
    return {key: basis.get(key) for key in PAYLOAD_KEYS}


def canonical_json(payload: dict) -> str:
    """稳定序列化：键排序 + 无空格 + 保留中文，保证跨进程 / 跨请求指纹一致。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(basis: dict) -> str:
    """测算依据指纹（sha256 十六进制，64 字符）。"""
    return hashlib.sha256(canonical_json(payload_of(basis)).encode("utf-8")).hexdigest()


def diff(old: dict, new: dict) -> list[str]:
    """逐项比较上一次依据与当前依据，返回人话差异；无差异返回空列表。"""
    if not old:
        return ["计划尚未记录测算依据，请先风险复检"]

    reasons: list[str] = []
    old_policy = old.get("policy") or {}
    new_policy = new.get("policy") or {}
    if old_policy != new_policy:
        if old_policy.get("version") != new_policy.get("version"):
            reasons.append(
                f"策略版本 v{old_policy.get('version')} → v{new_policy.get('version')}"
            )
        else:
            reasons.append("策略单价或风险闸门条件已变更")

    if (old.get("heat") or {}) != (new.get("heat") or {}):
        reasons.append("热度算法口径已变更")

    old_as_of = old.get("as_of")
    new_as_of = new.get("as_of")
    if old_as_of != new_as_of:
        reasons.append(f"测算日期 {old_as_of or '—'} → {new_as_of or '—'}，热度窗口已前移")

    if (old.get("access") or {}) != (new.get("access") or {}):
        reasons.append("访问日志已变化（含回溯补录）")

    if (old.get("profiles") or {}) != (new.get("profiles") or {}):
        reasons.append("表档案（体积 / 分区 / 报表引用）已变化")

    return reasons
