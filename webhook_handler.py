"""Monitor webhook receiver: stops LLM evaluation tasks in a space when an Arize monitor triggers.

Deploy as a Lambda behind a Function URL (or API Gateway) and register its URL as an
Arize webhook with HMAC_SHA256 signing. Subscribe the webhook to the monitor's
"triggered" event.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict

from cost_guard.config import load_config, make_client
from cost_guard.policy import Action, apply_action, is_guarded

MAX_SKEW_SECONDS = 300


def global_id_key(value):
    """Compare global IDs by type and numeric id only; the trailing suffix can differ between APIs."""
    try:
        padded = value + "=" * (-len(value) % 4)
        parts = base64.b64decode(padded).decode().split(":")
        return tuple(parts[:2])
    except Exception:
        return (value,)


def verify_signature(secret, timestamp, raw_body, signature_header, now=None):
    if not (secret and timestamp and signature_header):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > MAX_SKEW_SECONDS:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1] if signature_header.startswith("v1=") else ""
    return hmac.compare_digest(expected, provided)


def stop_space(client, policy, mode):
    actions = []
    for task in client.list_tasks(policy.space_id):
        if not (is_guarded(task, policy) and task.get("is_continuous")):
            continue
        if mode == "zero_sampling":
            action = Action(policy.space_id, task["id"], task["name"], "cap_sampling", "sampling_rate -> 0")
            actions.append(apply_action(client, action, 0.0))
        else:
            action = Action(policy.space_id, task["id"], task["name"], "pause", "is_continuous -> false")
            actions.append(apply_action(client, action, policy.max_sampling_rate))
    return actions


def handle(raw_body, headers, client=None, policies=None, secret=None, mode=None):
    headers = {k.lower(): v for k, v in headers.items()}
    secret = secret if secret is not None else os.environ.get("ARIZE_WEBHOOK_SECRET", "")
    if not verify_signature(secret, headers.get("x-arize-webhook-timestamp"), raw_body,
                            headers.get("x-arize-webhook-signature", "")):
        return 401, {"error": "invalid signature"}

    payload = json.loads(raw_body)
    if payload.get("event") != "monitor.triggered":
        return 200, {"ignored": payload.get("event")}

    if policies is None:
        raw, policies = load_config()
        client = client or make_client(raw)
    target = global_id_key(payload.get("space_id") or "")
    policy = next((p for p in policies if global_id_key(p.space_id) == target), None)
    if policy is None:
        return 200, {"ignored": "space not in config", "space_id": payload.get("space_id")}

    mode = mode or os.environ.get("WEBHOOK_ACTION", "pause")
    actions = stop_space(client, policy, mode)
    result = {
        "monitor": payload.get("monitor_name"),
        "space_id": policy.space_id,
        "value": payload.get("calculated_value"),
        "threshold": payload.get("threshold"),
        "actions": [asdict(a) for a in actions],
    }
    print(json.dumps(result))
    return 200, result


def lambda_handler(event, context):
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    status, result = handle(raw, event.get("headers") or {})
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(result)}
