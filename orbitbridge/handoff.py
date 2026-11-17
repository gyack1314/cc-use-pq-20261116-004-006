"""从已提交观测生成交接项，并负责有期限的领取、确认与接班再分派。"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import catalog, store
from .normalize import Observation, ObservationError, iso_utc, parse_time

DEFAULT_CLAIM_TTL_SECONDS = 4 * 3600
MIN_CLAIM_TTL_SECONDS = 1
MAX_CLAIM_TTL_SECONDS = 7 * 24 * 3600
CLAIM_STATUSES = ("active", "expired", "superseded", "acknowledged")
REASSIGN_OPERATOR = "system"


class HandoffError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _moment(value: str | None) -> str:
    try:
        return iso_utc(parse_time(value or catalog.utc_now(), 1, "when"))
    except ObservationError as exc:
        raise HandoffError("when", f"时间无效：{exc}") from exc


def _deadline(now: str, ttl_seconds: int) -> str:
    return iso_utc(parse_time(now, 1, "when") + timedelta(seconds=ttl_seconds))


def _operator(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise HandoffError("operator", "操作人无效")
    return value.strip()


def _ttl(value: int | None) -> int:
    if value is None:
        return DEFAULT_CLAIM_TTL_SECONDS
    if type(value) is not int or not MIN_CLAIM_TTL_SECONDS <= value <= MAX_CLAIM_TTL_SECONDS:
        raise HandoffError("ttl", f"领取期限须为 {MIN_CLAIM_TTL_SECONDS} 到 {MAX_CLAIM_TTL_SECONDS} 秒")
    return value


def _page(limit: int, after_id: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HandoffError("limit", "查询上限须在 1 到 1000 之间")
    if type(after_id) is not int or after_id < 0:
        raise HandoffError("after_id", "游标无效")


def route_observation(connection: sqlite3.Connection, observation_id: int, observation: Observation, when: str) -> int:
    matches = catalog.matching_subscriptions(connection, observation.region, observation.severity, observation.confidence)
    for subscription in matches:
        cursor = connection.execute(
            "INSERT INTO handoffs(observation_id,subscription_id,status,created_at) VALUES(?,?,'pending',?)",
            (observation_id, subscription["subscription_id"], when),
        )
        handoff_id = int(cursor.lastrowid)
        store.record_activity(connection, "handoff_created", "handoff", str(handoff_id), when,
                              {"observation_id": observation_id, "subscription_id": subscription["subscription_id"]})
    return len(matches)


def _create_claim(connection: sqlite3.Connection, handoff_id: int, group_id: str, operator: str,
                  now: str, ttl_seconds: int, reason: str, previous_claim_id: int | None) -> int:
    sequence = int(connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM claims WHERE handoff_id=?",
                                      (handoff_id,)).fetchone()[0])
    deadline = _deadline(now, ttl_seconds)
    cursor = connection.execute(
        "INSERT INTO claims(handoff_id,group_id,sequence,operator,status,claimed_at,deadline) "
        "VALUES(?,?,?,?,'active',?,?)", (handoff_id, group_id, sequence, operator, now, deadline),
    )
    claim_id = int(cursor.lastrowid)
    store.record_activity(connection, "claim_created", "claim", str(claim_id), now,
                          {"handoff_id": handoff_id, "group_id": group_id, "operator": operator,
                           "sequence": sequence, "deadline": deadline, "reason": reason,
                           "previous_claim_id": previous_claim_id})
    return claim_id


def _reassign(connection: sqlite3.Connection, previous_claim: dict[str, Any], now: str, reason: str) -> dict[str, Any]:
    handoff_id = previous_claim["handoff_id"]
    item = store.get_handoff(connection, handoff_id)
    if item is None or item["status"] != "pending":
        return {"handoff_id": handoff_id, "outcome": "closed", "previous_claim_id": previous_claim["claim_id"]}
    next_group = catalog.next_eligible_group(connection, item["subscription_id"], previous_claim["group_id"])
    if next_group is None:
        store.record_activity(connection, "handoff_unassigned", "handoff", str(handoff_id), now,
                              {"reason": reason, "previous_claim_id": previous_claim["claim_id"],
                               "previous_group_id": previous_claim["group_id"]})
        return {"handoff_id": handoff_id, "outcome": "unassigned", "previous_claim_id": previous_claim["claim_id"]}
    claim_id = _create_claim(connection, handoff_id, next_group, REASSIGN_OPERATOR, now,
                             DEFAULT_CLAIM_TTL_SECONDS, reason, previous_claim["claim_id"])
    return {"handoff_id": handoff_id, "outcome": "reassigned", "group_id": next_group,
            "previous_claim_id": previous_claim["claim_id"], "claim_id": claim_id}


def expire_due(connection: sqlite3.Connection, now: str) -> list[dict[str, Any]]:
    """处理所有已到期的领取并按接班顺序再分派；调用方须已持有事务。"""
    due = store.many(connection,
                     "SELECT c.* FROM claims c JOIN handoffs h USING(handoff_id) "
                     "WHERE c.status='active' AND c.deadline<? AND h.status='pending' ORDER BY c.claim_id", (now,))
    outcomes = []
    for claim in due:
        changed = connection.execute(
            "UPDATE claims SET status='expired',resolved_at=? WHERE claim_id=? AND status='active'",
            (now, claim["claim_id"])).rowcount
        if changed != 1:
            raise HandoffError("claim_conflict", "领取状态冲突")
        store.record_activity(connection, "claim_expired", "claim", str(claim["claim_id"]), now,
                              {"handoff_id": claim["handoff_id"], "group_id": claim["group_id"],
                               "deadline": claim["deadline"]})
        outcomes.append({**_reassign(connection, claim, now, "expired"), "expired_claim_id": claim["claim_id"]})
    return outcomes


def sweep(connection: sqlite3.Connection, *, when: str | None = None) -> dict[str, Any]:
    timestamp = _moment(when)
    with store.transaction(connection):
        outcomes = expire_due(connection, timestamp)
    return {"when": timestamp, "expired": len(outcomes),
            "reassigned": sum(1 for outcome in outcomes if outcome["outcome"] == "reassigned"),
            "unassigned": sum(1 for outcome in outcomes if outcome["outcome"] == "unassigned"),
            "results": outcomes}


def _eligible(connection: sqlite3.Connection, subscription_id: str, group_id: str) -> bool:
    row = store.one(connection, "SELECT r.active AS roster_active,g.active AS group_active FROM subscription_roster r "
                    "JOIN duty_groups g USING(group_id) WHERE r.subscription_id=? AND r.group_id=?",
                    (subscription_id, group_id))
    return bool(row and row["roster_active"] and row["group_active"])


def claim_handoff(connection: sqlite3.Connection, handoff_id: int, group_id: str, operator: str,
                  *, ttl_seconds: int | None = None, when: str | None = None) -> dict[str, Any]:
    if type(handoff_id) is not int or handoff_id < 1:
        raise HandoffError("handoff_id", "交接编号无效")
    name = _operator(operator)
    ttl = _ttl(ttl_seconds)
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
        item = store.get_handoff(connection, handoff_id)
        if item is None:
            raise HandoffError("handoff_missing", "交接项不存在")
        if item["status"] != "pending":
            raise HandoffError("handoff_acknowledged", "交接项已确认，无法领取")
        if catalog.group(connection, group_id) is None:
            raise HandoffError("group_missing", "班组不存在")
        if not _eligible(connection, item["subscription_id"], group_id):
            raise HandoffError("group_ineligible", "班组不在该订阅的接班序列或已停用")
        if store.one(connection, "SELECT 1 FROM claims WHERE handoff_id=? AND status='active'", (handoff_id,)):
            raise HandoffError("claim_active", "交接项已有进行中的领取")
        claim_id = _create_claim(connection, handoff_id, group_id, name, timestamp, ttl, "manual", None)
    return store.get_claim(connection, claim_id)


def acknowledge(connection: sqlite3.Connection, handoff_id: int, operator: str, *,
                claim_id: int, when: str | None = None) -> dict[str, Any]:
    if type(handoff_id) is not int or handoff_id < 1:
        raise HandoffError("handoff_id", "交接编号无效")
    name = _operator(operator)
    if type(claim_id) is not int or claim_id < 1:
        raise HandoffError("claim_id", "领取编号无效")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
        item = store.get_handoff(connection, handoff_id)
        if item is None:
            raise HandoffError("handoff_missing", "交接项不存在")
        claim = store.get_claim(connection, claim_id)
        if claim is None:
            raise HandoffError("claim_missing", "领取记录不存在")
        if claim["handoff_id"] != handoff_id:
            raise HandoffError("claim_mismatch", "领取不属于该交接项")
        if claim["status"] == "acknowledged" and item["operator"] == name:
            return {**item, "claim_id": claim_id, "status": "duplicate"}
        if claim["status"] != "active":
            cursor = connection.execute(
                "INSERT INTO disputes(handoff_id,claim_id,operator,reason,attempted_at) VALUES(?,?,?,?,?)",
                (handoff_id, claim_id, name, claim["status"], timestamp))
            dispute_id = int(cursor.lastrowid)
            store.record_activity(connection, "ack_disputed", "dispute", str(dispute_id), timestamp,
                                  {"handoff_id": handoff_id, "claim_id": claim_id, "operator": name,
                                   "reason": claim["status"]})
            return {"status": "disputed", "dispute_id": dispute_id, "handoff_id": handoff_id,
                    "claim_id": claim_id, "reason": claim["status"]}
        changed = connection.execute(
            "UPDATE handoffs SET status='acknowledged',acknowledged_at=?,operator=? "
            "WHERE handoff_id=? AND status='pending'", (timestamp, name, handoff_id),
        ).rowcount
        if changed != 1:
            raise HandoffError("acknowledge_conflict", "确认状态冲突")
        changed = connection.execute(
            "UPDATE claims SET status='acknowledged',resolved_at=? WHERE claim_id=? AND status='active'",
            (timestamp, claim_id)).rowcount
        if changed != 1:
            raise HandoffError("acknowledge_conflict", "确认状态冲突")
        store.record_activity(connection, "handoff_acknowledged", "handoff", str(handoff_id), timestamp,
                              {"operator": name, "claim_id": claim_id, "group_id": claim["group_id"]})
    return {"status": "acknowledged", "claim_id": claim_id, **store.get_handoff(connection, handoff_id)}


def _supersede(connection: sqlite3.Connection, claim: dict[str, Any], now: str, reason: str) -> None:
    changed = connection.execute(
        "UPDATE claims SET status='superseded',resolved_at=? WHERE claim_id=? AND status='active'",
        (now, claim["claim_id"])).rowcount
    if changed != 1:
        raise HandoffError("claim_conflict", "领取状态冲突")
    store.record_activity(connection, "claim_superseded", "claim", str(claim["claim_id"]), now,
                          {"handoff_id": claim["handoff_id"], "group_id": claim["group_id"], "reason": reason})


def _held_claims(connection: sqlite3.Connection, group_id: str, subscription_id: str | None) -> list[dict[str, Any]]:
    query = ("SELECT c.* FROM claims c JOIN handoffs h USING(handoff_id) "
             "WHERE c.group_id=? AND c.status='active' AND h.status='pending'")
    args: list[Any] = [group_id]
    if subscription_id is not None:
        query += " AND h.subscription_id=?"
        args.append(subscription_id)
    query += " ORDER BY c.claim_id"
    return store.many(connection, query, tuple(args))


def apply_group_state(connection: sqlite3.Connection, group_id: str, active: bool, *,
                      when: str | None = None) -> dict[str, Any]:
    """变更班组状态；停用时其已领取待办立即按接班政策再分派，与新待办同一责任政策。"""
    if type(active) is not bool:
        raise HandoffError("active_type", "班组状态必须为布尔值")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
        if catalog.group(connection, group_id) is None:
            raise HandoffError("group_missing", "班组不存在")
        connection.execute("UPDATE duty_groups SET active=? WHERE group_id=?", (int(active), group_id))
        store.record_activity(connection, "group_state", "group", group_id, timestamp, {"active": active})
        outcomes = []
        if not active:
            for claim in _held_claims(connection, group_id, None):
                _supersede(connection, claim, timestamp, "group_deactivated")
                outcomes.append(_reassign(connection, claim, timestamp, "group_deactivated"))
    return {"group": catalog.group(connection, group_id), "reassigned": outcomes}


def apply_roster_state(connection: sqlite3.Connection, subscription_id: str, group_id: str, active: bool, *,
                       when: str | None = None) -> dict[str, Any]:
    """变更订阅内接班资格；资格停用时该班组在该订阅的已领取待办立即再分派。"""
    if type(active) is not bool:
        raise HandoffError("active_type", "资格状态必须为布尔值")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
        if catalog.subscription(connection, subscription_id) is None:
            raise HandoffError("subscription_missing", "订阅不存在")
        if catalog.roster_entry(connection, subscription_id, group_id) is None:
            raise HandoffError("roster_missing", "班组不在该订阅的接班序列中")
        connection.execute("UPDATE subscription_roster SET active=? WHERE subscription_id=? AND group_id=?",
                           (int(active), subscription_id, group_id))
        store.record_activity(connection, "roster_state", "subscription", subscription_id, timestamp,
                              {"group_id": group_id, "active": active})
        outcomes = []
        if not active:
            for claim in _held_claims(connection, group_id, subscription_id):
                _supersede(connection, claim, timestamp, "eligibility_removed")
                outcomes.append(_reassign(connection, claim, timestamp, "eligibility_removed"))
    return {"roster": catalog.roster_entry(connection, subscription_id, group_id), "reassigned": outcomes}


def list_handoffs(connection: sqlite3.Connection, *, subscription_id: str | None = None,
                  group_id: str | None = None, status: str | None = None, limit: int = 100,
                  after_id: int = 0, when: str | None = None) -> list[dict[str, Any]]:
    _page(limit, after_id)
    if status is not None and status not in {"pending", "acknowledged"}:
        raise HandoffError("status", "交接状态无效")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
    query = ("SELECT h.*,o.station_id,o.external_id,o.observed_at,o.region,o.latitude,o.longitude,"
             "o.confidence,o.severity,o.source,o.batch_id,b.sequence,b.packet_id,"
             "c.claim_id AS active_claim_id,c.group_id AS active_group_id,c.operator AS active_claim_operator,"
             "c.claimed_at AS active_claimed_at,c.deadline AS active_claim_deadline "
             "FROM handoffs h JOIN observations o USING(observation_id) "
             "JOIN batches b ON b.batch_id=o.batch_id "
             "LEFT JOIN claims c ON c.handoff_id=h.handoff_id AND c.status='active' WHERE h.handoff_id>?")
    args: list[Any] = [after_id]
    if subscription_id is not None:
        query += " AND h.subscription_id=?"
        args.append(subscription_id)
    if group_id is not None:
        query += " AND c.group_id=?"
        args.append(group_id)
    if status is not None:
        query += " AND h.status=?"
        args.append(status)
    query += " ORDER BY h.handoff_id LIMIT ?"
    args.append(limit)
    return store.many(connection, query, tuple(args))


def list_claims(connection: sqlite3.Connection, *, handoff_id: int | None = None, group_id: str | None = None,
                status: str | None = None, limit: int = 100, after_id: int = 0,
                when: str | None = None) -> list[dict[str, Any]]:
    _page(limit, after_id)
    if handoff_id is not None and (type(handoff_id) is not int or handoff_id < 1):
        raise HandoffError("handoff_id", "交接编号无效")
    if status is not None and status not in CLAIM_STATUSES:
        raise HandoffError("status", "领取状态无效")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
    query = "SELECT * FROM claims WHERE claim_id>?"
    args: list[Any] = [after_id]
    if handoff_id is not None:
        query += " AND handoff_id=?"
        args.append(handoff_id)
    if group_id is not None:
        query += " AND group_id=?"
        args.append(group_id)
    if status is not None:
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY claim_id LIMIT ?"
    args.append(limit)
    return store.many(connection, query, tuple(args))


def list_disputes(connection: sqlite3.Connection, *, handoff_id: int | None = None,
                  limit: int = 100, after_id: int = 0) -> list[dict[str, Any]]:
    _page(limit, after_id)
    if handoff_id is not None and (type(handoff_id) is not int or handoff_id < 1):
        raise HandoffError("handoff_id", "交接编号无效")
    query = ("SELECT d.*,c.group_id,c.sequence AS claim_sequence FROM disputes d "
             "JOIN claims c USING(claim_id) WHERE d.dispute_id>?")
    args: list[Any] = [after_id]
    if handoff_id is not None:
        query += " AND d.handoff_id=?"
        args.append(handoff_id)
    query += " ORDER BY d.dispute_id LIMIT ?"
    args.append(limit)
    return store.many(connection, query, tuple(args))


def handoff_detail(connection: sqlite3.Connection, handoff_id: int) -> dict[str, Any] | None:
    rows = store.many(connection,
        "SELECT h.*,o.station_id,o.external_id,o.observed_at,o.region,o.latitude,o.longitude,o.confidence,"
        "o.severity,o.source,o.fingerprint,o.batch_id,b.sequence,b.packet_id,b.digest,b.source_name "
        "FROM handoffs h JOIN observations o USING(observation_id) JOIN batches b ON b.batch_id=o.batch_id "
        "WHERE h.handoff_id=?", (handoff_id,))
    return rows[0] if rows else None


def subscription_summary(connection: sqlite3.Connection, subscription_id: str, *,
                         when: str | None = None) -> dict[str, Any]:
    subscription = catalog.subscription(connection, subscription_id)
    if not subscription:
        raise HandoffError("subscription_missing", "订阅不存在")
    timestamp = _moment(when)
    with store.transaction(connection):
        expire_due(connection, timestamp)
    rows = store.many(connection, "SELECT status,COUNT(*) AS amount FROM handoffs WHERE subscription_id=? GROUP BY status",
                      (subscription_id,))
    counts = {row["status"]: row["amount"] for row in rows}
    pending = counts.get("pending", 0)
    claimed = int(connection.execute(
        "SELECT COUNT(*) FROM handoffs h WHERE h.subscription_id=? AND h.status='pending' "
        "AND EXISTS(SELECT 1 FROM claims c WHERE c.handoff_id=h.handoff_id AND c.status='active')",
        (subscription_id,)).fetchone()[0])
    return {"subscription": subscription, "pending": pending, "acknowledged": counts.get("acknowledged", 0),
            "claimed": claimed, "unclaimed": pending - claimed}
