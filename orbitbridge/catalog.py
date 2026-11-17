"""站点准入和区域订阅规则。"""
from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal
from typing import Any

from . import store
from .normalize import REGIONS, iso_utc


class CatalogError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return iso_utc(dt.datetime.now(dt.timezone.utc))


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not REGIONS.fullmatch(value):
        raise CatalogError("identifier", f"{label} 标识无效")
    return value


def register_station(connection: sqlite3.Connection, station_id: str, label: str, *, when: str | None = None) -> dict[str, Any]:
    _identifier(station_id, "站点")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise CatalogError("label", "站点名称无效")
    with store.transaction(connection):
        if store.one(connection, "SELECT 1 FROM stations WHERE station_id=?", (station_id,)):
            raise CatalogError("station_exists", "站点已存在")
        timestamp = when or utc_now()
        connection.execute("INSERT INTO stations VALUES(?,?,1,?)", (station_id, label.strip(), timestamp))
        connection.execute("INSERT INTO station_cursors VALUES(?,0,NULL,?)", (station_id, timestamp))
        store.record_activity(connection, "station_registered", "station", station_id, timestamp, {"label": label.strip()})
    return station(connection, station_id)


def station(connection: sqlite3.Connection, station_id: str) -> dict[str, Any] | None:
    return store.one(connection, "SELECT s.*,c.committed_sequence,c.last_batch_id FROM stations s "
                     "JOIN station_cursors c USING(station_id) WHERE s.station_id=?", (station_id,))


def set_station_active(connection: sqlite3.Connection, station_id: str, active: bool) -> dict[str, Any]:
    if type(active) is not bool:
        raise CatalogError("active_type", "站点状态必须为布尔值")
    with store.transaction(connection):
        changed = connection.execute("UPDATE stations SET active=? WHERE station_id=?", (int(active), station_id)).rowcount
        if not changed:
            raise CatalogError("station_missing", "站点不存在")
        store.record_activity(connection, "station_state", "station", station_id, utc_now(), {"active": active})
    return station(connection, station_id)


def register_subscription(connection: sqlite3.Connection, subscription_id: str, region: str,
                          minimum_severity: int = 1, minimum_confidence: str = "0",
                          *, when: str | None = None) -> dict[str, Any]:
    _identifier(subscription_id, "订阅")
    _identifier(region, "区域")
    if type(minimum_severity) is not int or not 1 <= minimum_severity <= 5:
        raise CatalogError("severity", "最低等级须为 1 到 5")
    try:
        threshold = Decimal(str(minimum_confidence))
    except Exception as exc:
        raise CatalogError("confidence", "置信度阈值无效") from exc
    if not threshold.is_finite() or not Decimal(0) <= threshold <= Decimal(1):
        raise CatalogError("confidence", "置信度阈值超出范围")
    timestamp = when or utc_now()
    with store.transaction(connection):
        if store.one(connection, "SELECT 1 FROM subscriptions WHERE subscription_id=?", (subscription_id,)):
            raise CatalogError("subscription_exists", "订阅已存在")
        connection.execute("INSERT INTO subscriptions VALUES(?,?,?,?,1,?)",
                           (subscription_id, region, minimum_severity, str(threshold), timestamp))
        store.record_activity(connection, "subscription_registered", "subscription", subscription_id, timestamp,
                              {"region": region, "minimum_severity": minimum_severity,
                               "minimum_confidence": str(threshold)})
    return subscription(connection, subscription_id)


def subscription(connection: sqlite3.Connection, subscription_id: str) -> dict[str, Any] | None:
    return store.one(connection, "SELECT * FROM subscriptions WHERE subscription_id=?", (subscription_id,))


def set_subscription_active(connection: sqlite3.Connection, subscription_id: str, active: bool) -> dict[str, Any]:
    if type(active) is not bool:
        raise CatalogError("active_type", "订阅状态必须为布尔值")
    with store.transaction(connection):
        changed = connection.execute("UPDATE subscriptions SET active=? WHERE subscription_id=?",
                                     (int(active), subscription_id)).rowcount
        if not changed:
            raise CatalogError("subscription_missing", "订阅不存在")
        store.record_activity(connection, "subscription_state", "subscription", subscription_id, utc_now(),
                              {"active": active})
    return subscription(connection, subscription_id)


def matching_subscriptions(connection: sqlite3.Connection, region: str, severity: int,
                           confidence: str) -> list[dict[str, Any]]:
    candidates = store.many(connection, "SELECT * FROM subscriptions WHERE region=? AND active=1 "
                            "AND minimum_severity<=? ORDER BY subscription_id", (region, severity))
    return [item for item in candidates if Decimal(confidence) >= Decimal(item["minimum_confidence"])]


def list_stations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return store.many(connection, "SELECT s.*,c.committed_sequence FROM stations s "
                      "JOIN station_cursors c USING(station_id) ORDER BY s.station_id")


def list_subscriptions(connection: sqlite3.Connection, region: str | None = None) -> list[dict[str, Any]]:
    if region is not None:
        return store.many(connection, "SELECT * FROM subscriptions WHERE region=? ORDER BY subscription_id", (region,))
    return store.many(connection, "SELECT * FROM subscriptions ORDER BY subscription_id")


def register_group(connection: sqlite3.Connection, group_id: str, label: str, *, when: str | None = None) -> dict[str, Any]:
    _identifier(group_id, "班组")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise CatalogError("label", "班组名称无效")
    timestamp = when or utc_now()
    with store.transaction(connection):
        if store.one(connection, "SELECT 1 FROM duty_groups WHERE group_id=?", (group_id,)):
            raise CatalogError("group_exists", "班组已存在")
        connection.execute("INSERT INTO duty_groups VALUES(?,?,1,?)", (group_id, label.strip(), timestamp))
        store.record_activity(connection, "group_registered", "group", group_id, timestamp, {"label": label.strip()})
    return group(connection, group_id)


def group(connection: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    return store.one(connection, "SELECT * FROM duty_groups WHERE group_id=?", (group_id,))


def list_groups(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return store.many(connection, "SELECT * FROM duty_groups ORDER BY group_id")


def roster_add(connection: sqlite3.Connection, subscription_id: str, group_id: str,
               *, position: int | None = None, when: str | None = None) -> dict[str, Any]:
    if subscription(connection, subscription_id) is None:
        raise CatalogError("subscription_missing", "订阅不存在")
    if group(connection, group_id) is None:
        raise CatalogError("group_missing", "班组不存在")
    if position is not None and (type(position) is not int or position < 1):
        raise CatalogError("position", "接班顺序须为正整数")
    timestamp = when or utc_now()
    with store.transaction(connection):
        if store.one(connection, "SELECT 1 FROM subscription_roster WHERE subscription_id=? AND group_id=?",
                     (subscription_id, group_id)):
            raise CatalogError("roster_exists", "班组已在该订阅的接班序列中")
        if position is None:
            position = int(connection.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM subscription_roster WHERE subscription_id=?",
                (subscription_id,)).fetchone()[0])
        connection.execute("INSERT INTO subscription_roster VALUES(?,?,?,1,?)",
                           (subscription_id, group_id, position, timestamp))
        store.record_activity(connection, "roster_added", "subscription", subscription_id, timestamp,
                              {"group_id": group_id, "position": position})
    return roster_entry(connection, subscription_id, group_id)


def roster_entry(connection: sqlite3.Connection, subscription_id: str, group_id: str) -> dict[str, Any] | None:
    return store.one(connection, "SELECT r.*,g.label,g.active AS group_active FROM subscription_roster r "
                     "JOIN duty_groups g USING(group_id) WHERE r.subscription_id=? AND r.group_id=?",
                     (subscription_id, group_id))


def roster(connection: sqlite3.Connection, subscription_id: str) -> list[dict[str, Any]]:
    if subscription(connection, subscription_id) is None:
        raise CatalogError("subscription_missing", "订阅不存在")
    return store.many(connection, "SELECT r.*,g.label,g.active AS group_active FROM subscription_roster r "
                      "JOIN duty_groups g USING(group_id) WHERE r.subscription_id=? ORDER BY r.position,r.group_id",
                      (subscription_id,))


def eligible_groups(connection: sqlite3.Connection, subscription_id: str) -> list[dict[str, Any]]:
    return store.many(connection, "SELECT r.group_id,r.position FROM subscription_roster r "
                      "JOIN duty_groups g USING(group_id) "
                      "WHERE r.subscription_id=? AND r.active=1 AND g.active=1 ORDER BY r.position,r.group_id",
                      (subscription_id,))


def next_eligible_group(connection: sqlite3.Connection, subscription_id: str,
                        after_group_id: str | None) -> str | None:
    eligible = eligible_groups(connection, subscription_id)
    if not eligible:
        return None
    if after_group_id is None:
        return eligible[0]["group_id"]
    full = store.many(connection, "SELECT group_id,position FROM subscription_roster WHERE subscription_id=? "
                      "ORDER BY position,group_id", (subscription_id,))
    anchor = next((entry for entry in full if entry["group_id"] == after_group_id), None)
    if anchor is None:
        return eligible[0]["group_id"]
    key = (anchor["position"], anchor["group_id"])
    for entry in eligible:
        if (entry["position"], entry["group_id"]) > key:
            return entry["group_id"]
    return eligible[0]["group_id"]
