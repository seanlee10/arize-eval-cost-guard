from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

LLM_TASK_TYPES = {"TEMPLATE_EVALUATION", "DECISION_JUDGE_EVALUATION"}


@dataclass
class SpacePolicy:
    space_id: str
    name: str = ""
    max_sampling_rate: float = 0.1
    require_eval_traces: bool = True
    monthly_eval_budget: int | None = None
    task_monthly_eval_budget: int | None = None
    cost_per_eval_usd: float | None = None
    exempt_task_ids: list[str] = field(default_factory=list)


@dataclass
class Action:
    space_id: str
    task_id: str
    task_name: str
    kind: str
    detail: str
    applied: bool = False
    error: str | None = None


def period_start(now, tz_offset_hours=0):
    local = now.astimezone(timezone(timedelta(hours=tz_offset_hours)))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def is_guarded(task, policy):
    return (
        task.get("type") in LLM_TASK_TYPES
        and task.get("project_id") is not None
        and task["id"] not in policy.exempt_task_ids
    )


def evaluate_space(client, policy, now=None, tz_offset_hours=0):
    """Return the actions needed to bring one space within policy, without applying them."""
    now = now or datetime.now(timezone.utc)
    since = period_start(now, tz_offset_hours)
    tasks = [t for t in client.list_tasks(policy.space_id) if is_guarded(t, policy)]

    actions = []
    usage = {}
    for task in tasks:
        tid, tname = task["id"], task["name"]
        rate = task.get("sampling_rate")
        if task.get("is_continuous") and rate is not None and rate > policy.max_sampling_rate:
            actions.append(Action(policy.space_id, tid, tname, "cap_sampling",
                                  f"sampling_rate {rate} -> {policy.max_sampling_rate}"))
        if policy.require_eval_traces and not client.get_tracing_enabled(tid):
            actions.append(Action(policy.space_id, tid, tname, "enable_eval_traces",
                                  "eval traces off -> on"))
        if policy.monthly_eval_budget is not None or policy.task_monthly_eval_budget is not None:
            usage[tid] = sum(r.get("num_successes") or 0 for r in client.list_runs_since(tid, since))

    continuous = [t for t in tasks if t.get("is_continuous")]
    space_total = sum(usage.values())
    if policy.monthly_eval_budget is not None and space_total >= policy.monthly_eval_budget:
        for task in continuous:
            actions.append(Action(policy.space_id, task["id"], task["name"], "pause",
                                  f"space used {space_total} of {policy.monthly_eval_budget} evaluations since {since}"))
    elif policy.task_monthly_eval_budget is not None:
        for task in continuous:
            used = usage.get(task["id"], 0)
            if used >= policy.task_monthly_eval_budget:
                actions.append(Action(policy.space_id, task["id"], task["name"], "pause",
                                      f"task used {used} of {policy.task_monthly_eval_budget} evaluations since {since}"))

    paused = {a.task_id for a in actions if a.kind == "pause"}
    actions = [a for a in actions if not (a.kind == "cap_sampling" and a.task_id in paused)]

    summary = {
        "space_id": policy.space_id,
        "space_name": policy.name,
        "period_start": since,
        "guarded_tasks": len(tasks),
        "continuous_tasks": len(continuous),
        "evaluations_this_period": space_total if usage else None,
        "monthly_eval_budget": policy.monthly_eval_budget,
        "estimated_cost_usd": round(space_total * policy.cost_per_eval_usd, 2)
        if usage and policy.cost_per_eval_usd is not None else None,
        "per_task_evaluations": [{"task_id": t["id"], "task_name": t["name"], "evaluations": usage[t["id"]]} for t in tasks if t["id"] in usage],
    }
    return actions, summary


def apply_action(client, action, max_sampling_rate):
    try:
        if action.kind == "cap_sampling":
            client.update_task(action.task_id, {"sampling_rate": max_sampling_rate})
        elif action.kind == "enable_eval_traces":
            client.set_tracing_enabled(action.task_id, True)
        elif action.kind == "pause":
            client.update_task(action.task_id, {"is_continuous": False})
        action.applied = True
    except Exception as e:
        action.error = str(e)
    return action
