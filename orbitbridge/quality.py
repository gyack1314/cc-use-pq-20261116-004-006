"""跨表业务不变量和数据质量诊断。"""
from __future__ import annotations

import sqlite3
from collections import Counter
from decimal import Decimal
from typing import Any

from . import store


def check_all(connection: sqlite3.Connection) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    def issue(code: str, entity: str, identifier: str, detail: str) -> None:
        issues.append({"code": code, "entity": entity, "id": identifier, "detail": detail})
    for item in store.integrity_check(connection):
        issue("storage_integrity", "database", "0", item)
    for station in connection.execute("SELECT station_id,committed_sequence,last_batch_id FROM station_cursors"):
        station_id, cursor, last_id = station
        batches = list(connection.execute("SELECT batch_id,sequence FROM batches WHERE station_id=? ORDER BY sequence",
                                          (station_id,)))
        if len(batches) != cursor:
            issue("sequence_gap", "station", station_id, f"游标 {cursor}，批次数 {len(batches)}")
        for expected, batch in enumerate(batches, 1):
            if batch["sequence"] != expected:
                issue("sequence_gap", "batch", str(batch["batch_id"]),
                      f"预期序号 {expected}，实际 {batch['sequence']}")
        if batches and batches[-1]["batch_id"] != last_id:
            issue("last_batch", "station", station_id, "游标末尾批次不匹配")
        if not batches and last_id is not None:
            issue("last_batch", "station", station_id, "空站点不应引用批次")
    for row in connection.execute("SELECT o.*,b.station_id AS batch_station FROM observations o "
                                  "JOIN batches b ON b.batch_id=o.batch_id"):
        identifier = str(row["observation_id"])
        if row["station_id"] != row["batch_station"]:
            issue("station_mismatch", "observation", identifier, "观测站点与批次站点不符")
        if not Decimal("0") <= Decimal(row["confidence"]) <= Decimal("1"):
            issue("confidence_range", "observation", identifier, "置信度越界")
        if not 1 <= row["severity"] <= 5:
            issue("severity_range", "observation", identifier, "火情等级越界")
    for row in connection.execute("SELECT h.*,o.region,o.confidence,o.severity,s.region AS subscription_region,"
                                  "s.minimum_confidence,s.minimum_severity,b.received_at FROM handoffs h "
                                  "JOIN observations o USING(observation_id) JOIN subscriptions s USING(subscription_id) "
                                  "JOIN batches b ON b.batch_id=o.batch_id"):
        identifier = str(row["handoff_id"])
        if row["region"] != row["subscription_region"]:
            issue("region_mismatch", "handoff", identifier, "交接区域与订阅区域不符")
        if row["severity"] < row["minimum_severity"] or Decimal(row["confidence"]) < Decimal(row["minimum_confidence"]):
            issue("threshold_mismatch", "handoff", identifier, "交接项不满足订阅阈值")
        if row["created_at"] < row["received_at"]:
            issue("created_before_batch", "handoff", identifier, "交接时间早于批次接收时间")
        if row["status"] == "acknowledged" and row["acknowledged_at"] < row["created_at"]:
            issue("ack_before_create", "handoff", identifier, "确认时间早于交接创建时间")
    for row in connection.execute("SELECT handoff_id,COUNT(*) AS amount FROM claims WHERE status='active' "
                                  "GROUP BY handoff_id HAVING amount>1"):
        issue("claim_active_multiple", "handoff", str(row["handoff_id"]), "同一交接项存在多个有效领取")
    sequences: dict[int, list[int]] = {}
    for row in connection.execute("SELECT c.*,h.status AS handoff_status,h.acknowledged_at AS handoff_acknowledged "
                                  "FROM claims c JOIN handoffs h USING(handoff_id) ORDER BY c.handoff_id,c.sequence"):
        identifier = str(row["claim_id"])
        sequences.setdefault(row["handoff_id"], []).append(row["sequence"])
        if row["deadline"] < row["claimed_at"]:
            issue("claim_deadline", "claim", identifier, "领取期限早于领取时间")
        if row["status"] == "active" and row["handoff_status"] != "pending":
            issue("claim_on_closed_handoff", "claim", identifier, "已结清交接项仍有有效领取")
        if row["status"] == "acknowledged" and (row["handoff_status"] != "acknowledged"
                                                or row["handoff_acknowledged"] != row["resolved_at"]):
            issue("claim_ack_mismatch", "claim", identifier, "领取确认与交接确认不一致")
    for handoff_id, order in sequences.items():
        if order != list(range(1, len(order) + 1)):
            issue("claim_sequence_gap", "handoff", str(handoff_id), "领取序号不连续")
    for row in connection.execute("SELECT handoff_id FROM handoffs WHERE status='acknowledged' AND NOT EXISTS "
                                  "(SELECT 1 FROM claims WHERE claims.handoff_id=handoffs.handoff_id "
                                  "AND claims.status='acknowledged')"):
        issue("ack_without_claim", "handoff", str(row["handoff_id"]), "已确认交接项缺少已确认领取")
    for row in connection.execute("SELECT d.dispute_id,d.handoff_id,c.handoff_id AS claim_handoff "
                                  "FROM disputes d JOIN claims c USING(claim_id)"):
        if row["handoff_id"] != row["claim_handoff"]:
            issue("dispute_handoff_mismatch", "dispute", str(row["dispute_id"]), "争议与领取不属于同一交接项")
    return {"ok": not issues, "issues": issues, "counts": store.counts(connection)}


def station_quality(connection: sqlite3.Connection, station_id: str) -> dict[str, Any]:
    cursor = store.station_cursor(connection, station_id)
    if cursor is None:
        raise ValueError("站点不存在")
    by_reason = Counter()
    for row in connection.execute("SELECT reason_code,attempts FROM quarantine WHERE station_id=?", (station_id,)):
        by_reason[row["reason_code"]] += row["attempts"]
    return {"station_id": station_id, "committed_sequence": cursor["committed_sequence"],
            "rejections_by_reason": dict(sorted(by_reason.items())),
            "observation_count": connection.execute("SELECT COUNT(*) FROM observations WHERE station_id=?",
                                                     (station_id,)).fetchone()[0]}


def quarantine_quality(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = store.many(connection, "SELECT reason_code,COUNT(*) AS unique_packets,SUM(attempts) AS attempts "
                      "FROM quarantine GROUP BY reason_code ORDER BY reason_code")
    return {"reasons": rows, "total_unique": sum(row["unique_packets"] for row in rows),
            "total_attempts": sum(row["attempts"] for row in rows)}
