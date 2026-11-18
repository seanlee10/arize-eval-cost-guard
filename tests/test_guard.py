import base64
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timezone

from cost_guard.policy import SpacePolicy, apply_action, evaluate_space, period_start
from webhook_handler import global_id_key, handle, verify_signature

SPACE = "U3BhY2U6MTpYWFhY"


def task(tid, type_="TEMPLATE_EVALUATION", rate=1.0, continuous=True, project="TW9kZWw6MTpYWA=="):
    return {"id": tid, "name": f"task-{tid}", "type": type_, "sampling_rate": rate,
            "is_continuous": continuous, "project_id": project}


class FakeClient:
    def __init__(self, tasks, tracing=None, runs=None):
        self.tasks = tasks
        self.tracing = tracing or {}
        self.runs = runs or {}
        self.calls = []

    def list_tasks(self, space_id):
        return iter(self.tasks)

    def list_runs_since(self, task_id, since):
        return iter(r for r in self.runs.get(task_id, []) if r["created_at"] >= since)

    def get_tracing_enabled(self, task_id):
        return self.tracing.get(task_id, True)

    def set_tracing_enabled(self, task_id, enabled):
        self.calls.append(("tracing", task_id, enabled))

    def update_task(self, task_id, fields):
        self.calls.append(("patch", task_id, fields))


NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)


class PolicyTests(unittest.TestCase):
    def test_period_start_respects_timezone(self):
        self.assertEqual(period_start(NOW, 0), "2026-11-01T00:00:00.000Z")
        self.assertEqual(period_start(NOW, 8), "2026-10-31T16:00:00.000Z")

    def test_skips_code_dataset_and_exempt_tasks(self):
        client = FakeClient([
            task("code", type_="CODE_EVALUATION"),
            task("dataset", project=None),
            task("exempt"),
        ], tracing={"code": False, "dataset": False, "exempt": False})
        policy = SpacePolicy(SPACE, exempt_task_ids=["exempt"])
        actions, summary = evaluate_space(client, policy, now=NOW)
        self.assertEqual(actions, [])
        self.assertEqual(summary["guarded_tasks"], 0)

    def test_caps_sampling_and_enables_traces(self):
        client = FakeClient([task("a", rate=1.0), task("b", rate=0.05)], tracing={"a": False})
        actions, _ = evaluate_space(client, SpacePolicy(SPACE, max_sampling_rate=0.1), now=NOW)
        self.assertEqual(sorted((a.task_id, a.kind) for a in actions),
                         [("a", "cap_sampling"), ("a", "enable_eval_traces")])

    def test_does_not_cap_paused_tasks(self):
        client = FakeClient([task("a", rate=1.0, continuous=False)])
        actions, _ = evaluate_space(client, SpacePolicy(SPACE), now=NOW)
        self.assertEqual(actions, [])

    def test_space_budget_pauses_every_continuous_task(self):
        runs = {"a": [{"created_at": "2026-11-10T00:00:00.000Z", "num_successes": 600}],
                "b": [{"created_at": "2026-11-12T00:00:00.000Z", "num_successes": 500},
                      {"created_at": "2026-10-30T00:00:00.000Z", "num_successes": 99999}]}
        client = FakeClient([task("a"), task("b", rate=0.1)], runs=runs)
        policy = SpacePolicy(SPACE, monthly_eval_budget=1000, cost_per_eval_usd=0.002)
        actions, summary = evaluate_space(client, policy, now=NOW)
        self.assertEqual(summary["evaluations_this_period"], 1100)
        self.assertEqual(summary["estimated_cost_usd"], 2.2)
        self.assertEqual(sorted((a.task_id, a.kind) for a in actions), [("a", "pause"), ("b", "pause")])

    def test_task_budget_pauses_only_the_heavy_task(self):
        runs = {"a": [{"created_at": "2026-11-10T00:00:00.000Z", "num_successes": 300}],
                "b": [{"created_at": "2026-11-10T00:00:00.000Z", "num_successes": 10}]}
        client = FakeClient([task("a", rate=0.1), task("b", rate=0.1)], runs=runs)
        actions, _ = evaluate_space(client, SpacePolicy(SPACE, task_monthly_eval_budget=200), now=NOW)
        self.assertEqual([(a.task_id, a.kind) for a in actions], [("a", "pause")])

    def test_apply_action_records_errors(self):
        class Failing(FakeClient):
            def update_task(self, task_id, fields):
                raise RuntimeError("403")
        actions, _ = evaluate_space(Failing([task("a")]), SpacePolicy(SPACE), now=NOW)
        done = apply_action(Failing([]), actions[0], 0.1)
        self.assertFalse(done.applied)
        self.assertIn("403", done.error)


def signed(secret, body, ts=None):
    ts = str(ts or int(time.time()))
    sig = "v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Arize-Webhook-Timestamp": ts, "X-Arize-Webhook-Signature": sig}


class WebhookTests(unittest.TestCase):
    secret = "whsec_test"

    def body(self, event="monitor.triggered", space=SPACE):
        return json.dumps({"event": event, "space_id": space, "monitor_name": "m",
                           "calculated_value": 250, "threshold": 100}).encode()

    def test_rejects_bad_or_stale_signature(self):
        body = self.body()
        headers = signed("wrong", body)
        self.assertEqual(handle(body, headers, FakeClient([]), [], self.secret)[0], 401)
        stale = signed(self.secret, body, ts=int(time.time()) - 3600)
        self.assertFalse(verify_signature(self.secret, stale["X-Arize-Webhook-Timestamp"], body,
                                          stale["X-Arize-Webhook-Signature"]))

    def test_ignores_cleared_and_unknown_spaces(self):
        client = FakeClient([task("a")])
        policies = [SpacePolicy(SPACE)]
        body = self.body(event="monitor.cleared")
        self.assertEqual(handle(body, signed(self.secret, body), client, policies, self.secret)[1],
                         {"ignored": "monitor.cleared"})
        body = self.body(space="U3BhY2U6OTk5OlpaWlo=")
        status, result = handle(body, signed(self.secret, body), client, policies, self.secret)
        self.assertEqual(result["ignored"], "space not in config")
        self.assertEqual(client.calls, [])

    def test_pauses_continuous_llm_tasks(self):
        client = FakeClient([task("a"), task("b", continuous=False), task("c", type_="CODE_EVALUATION")])
        body = self.body()
        status, result = handle(body, signed(self.secret, body), client, [SpacePolicy(SPACE)], self.secret)
        self.assertEqual(status, 200)
        self.assertEqual(client.calls, [("patch", "a", {"is_continuous": False})])

    def test_zero_sampling_mode(self):
        client = FakeClient([task("a")])
        body = self.body()
        handle(body, signed(self.secret, body), client, [SpacePolicy(SPACE)], self.secret, mode="zero_sampling")
        self.assertEqual(client.calls, [("patch", "a", {"sampling_rate": 0.0})])

    def test_space_ids_match_across_suffixes(self):
        other_suffix = base64.b64encode(b"Space:1:abcd").decode()
        self.assertEqual(global_id_key(SPACE), global_id_key(other_suffix))


if __name__ == "__main__":
    unittest.main()
