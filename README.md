# Arize evaluation cost guard

Sample code that keeps LLM-as-a-judge spend on Arize evaluation tasks inside limits you set per space. Two parts:

| Part | Runs | Does |
| --- | --- | --- |
| `guard.py` | On a schedule (EventBridge + Lambda, or cron) | Caps task sampling, forces eval traces on, pauses tasks once a space or task goes over its monthly evaluation budget |
| `webhook_handler.py` | When an Arize monitor triggers (Lambda Function URL) | Pauses every continuous LLM evaluation task in the monitor's space, or sets their sampling to 0 |

Python 3.10+, standard library only. Code evaluations and dataset (experiment) tasks are never touched, since they don't call an LLM on a schedule.

## What the guard enforces

For every project-based `TEMPLATE_EVALUATION` and `DECISION_JUDGE_EVALUATION` task in each configured space:

1. **Sampling cap.** A continuous task with `sampling_rate` above `max_sampling_rate` is lowered to the cap.
2. **Eval traces on.** A task with eval traces off has them turned on, so every judge call lands in the space's **Eval Traces** project with model, tokens and `llm.cost.total`. The cost dashboards and the monitor below depend on this.
3. **Budget.** Evaluations are counted from task runs since the start of the month (`num_successes`). If the space total reaches `monthly_eval_budget`, every continuous task in the space is paused. If `task_monthly_eval_budget` is set instead, only tasks over that limit are paused. `cost_per_eval_usd` turns the count into an estimate in the report.

Pausing sets `is_continuous` to false. Nothing is deleted, and a paused task is resumed from the task's page in the UI (or with the same API call set back to true). The guard never re-enables a task by itself.

## Configure

Copy `examples/config.example.json` to `config.json`:

| Field | Meaning |
| --- | --- |
| `arize_host` | Base URL of your Arize deployment. REST is `<host>/v2` and GraphQL is `<host>/graphql` unless `rest_url` or `graphql_url` override them |
| `tz_offset_hours` | Time zone the monthly budget resets in (8 for Singapore) |
| `defaults` | Values applied to every space unless the space sets its own |
| `spaces[].space_id` | Space ID as returned by `GET /v2/spaces` |
| `max_sampling_rate` | Highest sampling rate allowed on a continuous task (default 0.1) |
| `require_eval_traces` | Turn eval traces on where they are off (default true) |
| `monthly_eval_budget` | Evaluations per month for the whole space |
| `task_monthly_eval_budget` | Evaluations per month for any single task |
| `cost_per_eval_usd` | Optional, for the cost estimate in the report |
| `exempt_task_ids` | Tasks the guard leaves alone |

The API key needs permission to read and update evaluation tasks in every configured space. Use a service key scoped to those spaces.

## Run locally

```bash
export ARIZE_API_KEY=<service key>
python3 guard.py --config config.json            # dry run: prints usage and planned changes
python3 guard.py --config config.json --apply    # makes the changes
```

Start with dry runs for a week to calibrate budgets against real usage before switching on `--apply`.

## Deploy on AWS

`deploy/template.yaml` is an AWS SAM template with both functions. Put `config.json` in the repository root before building, and store the API key and the webhook signing secret in Secrets Manager.

```bash
sam build -t deploy/template.yaml
sam deploy --guided   # ApplyChanges=false for the first week
```

Both functions run in the subnets you pass, which must reach the Arize host. The webhook function gets a Function URL. If your Arize deployment can't call that URL from where it runs, put an internal load balancer or a private API Gateway in front of the function instead. Every request is checked against the webhook's HMAC signature, and requests older than five minutes are refused.

## Monitor and webhook setup

1. **Create the webhook.** Settings → Webhooks → New webhook, with the function URL and auth type **HMAC SHA256**. Copy the signing secret shown once at creation into Secrets Manager. The same can be done with `POST /v2/webhooks` (`auth_type: HMAC_SHA256`).
2. **Create the monitor** in the space's **Eval Traces** project: a data quality monitor on span count, filtered to `openinference.span.kind = LLM`. Each span there is one judge call. Pick a window and threshold that represent a runaway, for example more than 5,000 judge calls in one hour for a space that normally runs 500.
3. **Attach the webhook** in the monitor's notification settings and subscribe it to the **triggered** event.

When the monitor triggers, the function pauses the space's continuous LLM evaluation tasks (or zeroes their sampling with `WebhookAction=zero_sampling`). Cleared and no-data events are logged and ignored. The space must be in `config.json`, so a monitor in another space can't stop tasks it shouldn't.

The monitor only sees judge calls from tasks with eval traces on, so run the scheduled guard with `require_eval_traces` too.

### Testing the circuit breaker

HMAC-signed webhooks don't support Arize's test delivery, so test with a real monitor:

1. Exempt every task in the test space except one you can pause safely.
2. Create a monitor on the Eval Traces project with a threshold of 0 and the webhook attached. A new monitor evaluates when it's created, so with any judge calls in the window it triggers straight away.
3. Check the webhook's delivery attempts (`GET /v2/webhooks/<WEBHOOK_ID>/delivery-attempts`) for a 200, and the function's logs for the task it paused.
4. Resume the task and delete the test monitor.

A Function URL with auth type NONE needs both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on the function's resource policy. Scope the second one with the `lambda:InvokedViaFunctionUrl` condition so the function can't be invoked any other way.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```
