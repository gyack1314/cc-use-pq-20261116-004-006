from __future__ import annotations

from orbitbridge import audit, catalog, handoff, ingest, quality, store
from helpers import DatabaseCase, STAMP, observation, packet, register_duty

T1 = "2026-10-28T12:04:00Z"


class ClaimTests(DatabaseCase):
    def _create(self, sequence: int = 1, records: list | None = None):
        result = ingest.ingest_bytes(self.db, packet(sequence, records), when=STAMP)
        item = handoff.list_handoffs(self.db, when=STAMP)[-1]
        return result, item

    def _claim(self, handoff_id, group="shift-a", operator="operator-a", when=T1, **kwargs):
        return handoff.claim_handoff(self.db, handoff_id, group, operator, when=when, **kwargs)

    def test_claim_assigns_current_holder_with_deadline(self):
        register_duty(self.db)
        _, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=600)
        self.assertEqual(claim["status"], "active")
        self.assertEqual(claim["sequence"], 1)
        self.assertEqual(claim["deadline"], "2026-10-28T12:14:00Z")
        row = handoff.list_handoffs(self.db, when="2026-10-28T12:05:00Z")[0]
        self.assertEqual(row["active_claim_id"], claim["claim_id"])
        self.assertEqual(row["active_group_id"], "shift-a")
        self.assertEqual(row["active_claim_deadline"], "2026-10-28T12:14:00Z")

    def test_claim_requires_known_group(self):
        _, item = self._create()
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(item["handoff_id"], group="ghost")
        self.assertEqual(error.exception.code, "group_missing")

    def test_claim_requires_roster_eligibility(self):
        _, item = self._create()
        catalog.register_group(self.db, "outsider", "序列外班组", when=STAMP)
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(item["handoff_id"], group="outsider")
        self.assertEqual(error.exception.code, "group_ineligible")
        register_duty(self.db)
        handoff.apply_group_state(self.db, "shift-a", False, when=T1)
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(item["handoff_id"])
        self.assertEqual(error.exception.code, "group_ineligible")

    def test_second_claim_blocked_while_active(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        self._claim(item["handoff_id"])
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(item["handoff_id"], group="shift-b")
        self.assertEqual(error.exception.code, "claim_active")

    def test_claim_rejected_on_acknowledged_handoff(self):
        register_duty(self.db)
        _, item = self._create()
        claim = self._claim(item["handoff_id"])
        handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                            claim_id=claim["claim_id"], when="2026-10-28T12:05:00Z")
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(item["handoff_id"], when="2026-10-28T12:06:00Z")
        self.assertEqual(error.exception.code, "handoff_acknowledged")

    def test_ttl_must_be_bounded_positive_int(self):
        register_duty(self.db)
        _, item = self._create()
        for bad in (0, -5, "3600", 8 * 24 * 3600):
            with self.assertRaises(handoff.HandoffError) as error:
                self._claim(item["handoff_id"], ttl_seconds=bad)
            self.assertEqual(error.exception.code, "ttl")

    def test_expiry_reassigns_to_next_group_in_roster_order(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=60)
        result = handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["reassigned"], 1)
        self.assertEqual(result["results"][0]["group_id"], "shift-b")
        claims = handoff.list_claims(self.db, handoff_id=item["handoff_id"], when="2026-10-28T12:06:00Z")
        self.assertEqual([row["status"] for row in claims], ["expired", "active"])
        self.assertEqual(claims[0]["claim_id"], claim["claim_id"])
        self.assertEqual(claims[1]["group_id"], "shift-b")
        self.assertEqual(claims[1]["sequence"], 2)
        self.assertEqual(claims[1]["deadline"], "2026-10-28T16:06:00Z")

    def test_reassignment_cycles_through_roster_order(self):
        register_duty(self.db, groups=("shift-a", "shift-b", "shift-c"))
        _, item = self._create()
        self._claim(item["handoff_id"], ttl_seconds=60)
        handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        handoff.sweep(self.db, when="2026-10-28T16:07:00Z")
        handoff.sweep(self.db, when="2026-10-28T20:08:00Z")
        claims = handoff.list_claims(self.db, handoff_id=item["handoff_id"], when="2026-10-28T20:09:00Z")
        self.assertEqual([row["group_id"] for row in claims],
                         ["shift-a", "shift-b", "shift-c", "shift-a"])
        self.assertEqual([row["status"] for row in claims], ["expired", "expired", "expired", "active"])

    def test_ack_at_exact_deadline_still_valid(self):
        register_duty(self.db)
        _, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=60)
        response = handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                                       claim_id=claim["claim_id"], when="2026-10-28T12:05:00Z")
        self.assertEqual(response["status"], "acknowledged")

    def test_ack_racing_timeout_loses_and_old_list_cannot_override(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=60)
        late = handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                                   claim_id=claim["claim_id"], when="2026-10-28T12:05:01Z")
        self.assertEqual(late["status"], "disputed")
        self.assertEqual(late["reason"], "expired")
        current = handoff.list_handoffs(self.db, when="2026-10-28T12:05:01Z")[0]
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["active_group_id"], "shift-b")
        self.assertNotEqual(current["active_claim_id"], claim["claim_id"])
        takeover = handoff.acknowledge(self.db, item["handoff_id"], "operator-b",
                                       claim_id=current["active_claim_id"], when="2026-10-28T12:06:00Z")
        self.assertEqual(takeover["status"], "acknowledged")
        self.assertEqual(store.get_handoff(self.db, item["handoff_id"])["operator"], "operator-b")
        disputes = handoff.list_disputes(self.db, handoff_id=item["handoff_id"])
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["claim_id"], claim["claim_id"])
        self.assertEqual(disputes[0]["group_id"], "shift-a")

    def test_late_ack_on_superseded_claim_is_disputed(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        claim = self._claim(item["handoff_id"])
        handoff.apply_group_state(self.db, "shift-a", False, when="2026-10-28T12:10:00Z")
        late = handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                                   claim_id=claim["claim_id"], when="2026-10-28T12:11:00Z")
        self.assertEqual(late["status"], "disputed")
        self.assertEqual(late["reason"], "superseded")

    def test_ack_with_unknown_or_foreign_claim_is_rejected(self):
        register_duty(self.db)
        self._create()
        self._create(2, [observation("sim-2")])
        first, second = handoff.list_handoffs(self.db, when=STAMP)
        claim = self._claim(first["handoff_id"])
        with self.assertRaises(handoff.HandoffError) as error:
            handoff.acknowledge(self.db, first["handoff_id"], "operator-a", claim_id=999)
        self.assertEqual(error.exception.code, "claim_missing")
        with self.assertRaises(handoff.HandoffError) as error:
            handoff.acknowledge(self.db, second["handoff_id"], "operator-a", claim_id=claim["claim_id"])
        self.assertEqual(error.exception.code, "claim_mismatch")

    def test_group_deactivation_applies_one_policy_to_claimed_backlog_and_new(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        self._create()
        self._create(2, [observation("sim-2")])
        first, second = handoff.list_handoffs(self.db, when=STAMP)
        held = self._claim(first["handoff_id"], ttl_seconds=3600)
        result = handoff.apply_group_state(self.db, "shift-a", False, when="2026-10-28T12:10:00Z")
        self.assertEqual(result["group"]["active"], 0)
        self.assertEqual(len(result["reassigned"]), 1)
        self.assertEqual(result["reassigned"][0]["group_id"], "shift-b")
        moved = handoff.list_handoffs(self.db, when="2026-10-28T12:10:00Z")
        self.assertEqual(moved[0]["active_group_id"], "shift-b")
        self.assertIsNone(moved[1]["active_claim_id"])
        old = store.get_claim(self.db, held["claim_id"])
        self.assertEqual(old["status"], "superseded")
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(second["handoff_id"], when="2026-10-28T12:11:00Z")
        self.assertEqual(error.exception.code, "group_ineligible")
        self._claim(second["handoff_id"], group="shift-b", when="2026-10-28T12:11:00Z")
        self._create(3, [observation("sim-3")])
        third = handoff.list_handoffs(self.db, when="2026-10-28T12:12:00Z")[-1]
        with self.assertRaises(handoff.HandoffError) as error:
            self._claim(third["handoff_id"], when="2026-10-28T12:12:00Z")
        self.assertEqual(error.exception.code, "group_ineligible")
        claim = self._claim(third["handoff_id"], group="shift-b", when="2026-10-28T12:12:00Z")
        self.assertEqual(claim["status"], "active")

    def test_roster_eligibility_removal_is_scoped_to_subscription(self):
        catalog.register_subscription(self.db, "backup", "north", 1, "0.1", when=STAMP)
        register_duty(self.db, "north-watch", ("shift-a", "shift-b"))
        register_duty(self.db, "backup", ("shift-a",))
        self._create()
        main = handoff.list_handoffs(self.db, subscription_id="north-watch", when=STAMP)[0]
        spare = handoff.list_handoffs(self.db, subscription_id="backup", when=STAMP)[0]
        self._claim(main["handoff_id"], ttl_seconds=3600)
        self._claim(spare["handoff_id"], ttl_seconds=3600)
        result = handoff.apply_roster_state(self.db, "north-watch", "shift-a", False,
                                            when="2026-10-28T12:10:00Z")
        self.assertEqual(len(result["reassigned"]), 1)
        self.assertEqual(result["reassigned"][0]["group_id"], "shift-b")
        moved = handoff.list_handoffs(self.db, subscription_id="north-watch", when="2026-10-28T12:10:00Z")[0]
        self.assertEqual(moved["active_group_id"], "shift-b")
        untouched = handoff.list_handoffs(self.db, subscription_id="backup", when="2026-10-28T12:10:00Z")[0]
        self.assertEqual(untouched["active_group_id"], "shift-a")

    def test_multiple_subscriptions_keep_independent_obligations(self):
        catalog.register_subscription(self.db, "backup", "north", 1, "0.1", when=STAMP)
        register_duty(self.db, "north-watch", ("shift-a",))
        register_duty(self.db, "backup", ("shift-b",))
        self._create()
        main = handoff.list_handoffs(self.db, subscription_id="north-watch", when=STAMP)[0]
        spare = handoff.list_handoffs(self.db, subscription_id="backup", when=STAMP)[0]
        claim = self._claim(main["handoff_id"])
        handoff.acknowledge(self.db, main["handoff_id"], "operator-a",
                            claim_id=claim["claim_id"], when="2026-10-28T12:05:00Z")
        spare_after = handoff.list_handoffs(self.db, subscription_id="backup",
                                            when="2026-10-28T12:05:00Z")[0]
        self.assertEqual(spare_after["status"], "pending")
        self.assertIsNone(spare_after["active_claim_id"])
        summary = handoff.subscription_summary(self.db, "backup", when="2026-10-28T12:05:00Z")
        self.assertEqual((summary["pending"], summary["acknowledged"]), (1, 0))
        claim_b = self._claim(spare["handoff_id"], group="shift-b", when="2026-10-28T12:06:00Z")
        self.assertEqual(claim_b["group_id"], "shift-b")

    def test_unassigned_when_no_eligible_group_and_recovery_after_reactivation(self):
        register_duty(self.db)
        _, item = self._create()
        self._claim(item["handoff_id"], ttl_seconds=3600)
        result = handoff.apply_group_state(self.db, "shift-a", False, when="2026-10-28T12:10:00Z")
        self.assertEqual(result["reassigned"][0]["outcome"], "unassigned")
        row = handoff.list_handoffs(self.db, when="2026-10-28T12:10:00Z")[0]
        self.assertIsNone(row["active_claim_id"])
        events = audit.list_activity(self.db, entity_type="handoff", entity_id=str(item["handoff_id"]))
        self.assertEqual(events[-1]["event_type"], "handoff_unassigned")
        handoff.apply_group_state(self.db, "shift-a", True, when="2026-10-28T12:20:00Z")
        claim = self._claim(item["handoff_id"], when="2026-10-28T12:21:00Z")
        self.assertEqual(claim["sequence"], 2)
        self.assertEqual(claim["status"], "active")

    def test_claim_deadlines_and_roster_survive_restart(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        self._claim(item["handoff_id"], ttl_seconds=60)
        self.db.close()
        self.db = store.open_database(self.db_path)
        roster = catalog.roster(self.db, "north-watch")
        self.assertEqual([row["group_id"] for row in roster], ["shift-a", "shift-b"])
        result = handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        self.assertEqual(result["expired"], 1)
        row = handoff.list_handoffs(self.db, when="2026-10-28T12:06:00Z")[0]
        self.assertEqual(row["active_group_id"], "shift-b")

    def test_trace_lists_shift_history_current_holder_invalidated_and_disputes(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        result, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=60)
        handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                            claim_id=claim["claim_id"], when="2026-10-28T12:07:00Z")
        trace = audit.handoff_trace(self.db, item["handoff_id"], when="2026-10-28T12:08:00Z")
        self.assertEqual(trace["handoff"]["batch_id"], result.batch_id)
        self.assertEqual([row["group_id"] for row in trace["claims"]], ["shift-a", "shift-b"])
        self.assertEqual(trace["current_claim"]["group_id"], "shift-b")
        self.assertEqual(len(trace["invalidated_claims"]), 1)
        self.assertEqual(trace["invalidated_claims"][0]["status"], "expired")
        self.assertEqual(len(trace["disputes"]), 1)
        self.assertEqual(trace["disputes"][0]["claim_id"], claim["claim_id"])
        kinds = [event["event_type"] for event in trace["activity"]]
        self.assertEqual(kinds.count("claim_created"), 2)
        self.assertIn("claim_expired", kinds)
        self.assertIn("ack_disputed", kinds)

    def test_list_claims_filters_by_group_and_status(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        self._claim(item["handoff_id"], ttl_seconds=60)
        handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        expired = handoff.list_claims(self.db, status="expired", when="2026-10-28T12:06:00Z")
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["group_id"], "shift-a")
        held = handoff.list_claims(self.db, group_id="shift-b", status="active", when="2026-10-28T12:06:00Z")
        self.assertEqual(len(held), 1)
        with self.assertRaises(handoff.HandoffError) as error:
            handoff.list_claims(self.db, status="unknown")
        self.assertEqual(error.exception.code, "status")

    def test_summary_reports_claimed_and_unclaimed(self):
        register_duty(self.db)
        self._create()
        self._create(2, [observation("sim-2")])
        first, _ = handoff.list_handoffs(self.db, when=STAMP)
        self._claim(first["handoff_id"])
        summary = handoff.subscription_summary(self.db, "north-watch", when="2026-10-28T12:05:00Z")
        self.assertEqual((summary["pending"], summary["claimed"], summary["unclaimed"]), (2, 1, 1))

    def test_quality_and_lineage_hold_after_full_lifecycle(self):
        register_duty(self.db, groups=("shift-a", "shift-b"))
        _, item = self._create()
        claim = self._claim(item["handoff_id"], ttl_seconds=60)
        handoff.sweep(self.db, when="2026-10-28T12:06:00Z")
        handoff.acknowledge(self.db, item["handoff_id"], "operator-a",
                            claim_id=claim["claim_id"], when="2026-10-28T12:07:00Z")
        current = handoff.list_handoffs(self.db, when="2026-10-28T12:07:00Z")[0]
        handoff.acknowledge(self.db, item["handoff_id"], "operator-b",
                            claim_id=current["active_claim_id"], when="2026-10-28T12:08:00Z")
        self.assertTrue(quality.check_all(self.db)["ok"])
        self.assertEqual(audit.verify_lineage(self.db), [])
