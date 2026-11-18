"""Scheduled cost guard for Arize evaluation tasks.

Run locally:   python3 guard.py --config config.json            (dry run, prints the plan)
               python3 guard.py --config config.json --apply    (makes the changes)
Run on Lambda: handler = guard.lambda_handler, scheduled by EventBridge.
"""
import argparse
import json
import os
from dataclasses import asdict

from cost_guard.config import load_config, make_client
from cost_guard.policy import apply_action, evaluate_space


def run(config_path=None, apply=False):
    raw, policies = load_config(config_path)
    client = make_client(raw)
    tz = raw.get("tz_offset_hours", 0)
    report = []
    for policy in policies:
        actions, summary = evaluate_space(client, policy, tz_offset_hours=tz)
        if apply:
            actions = [apply_action(client, a, policy.max_sampling_rate) for a in actions]
        report.append({**summary, "actions": [asdict(a) for a in actions]})
    return {"applied": apply, "spaces": report}


def lambda_handler(event, context):
    apply = os.environ.get("COST_GUARD_APPLY", "false").lower() == "true"
    result = run(apply=apply)
    print(json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--apply", action="store_true", help="make the changes instead of printing them")
    args = parser.parse_args()
    print(json.dumps(run(args.config, args.apply), indent=2))


if __name__ == "__main__":
    main()
