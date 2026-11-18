import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ArizeError(RuntimeError):
    pass


class ArizeClient:
    def __init__(self, api_key, rest_url, graphql_url, timeout=30, max_retries=3):
        self.api_key = api_key
        self.rest_url = rest_url.rstrip("/")
        self.graphql_url = graphql_url
        self.timeout = timeout
        self.max_retries = max_retries

    def _send(self, req):
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                detail = e.read().decode("utf-8", "replace")[:500]
                raise ArizeError(f"{req.get_method()} {req.full_url} -> {e.code}: {detail}") from e

    def _rest(self, method, path, params=None, body=None):
        url = f"{self.rest_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def _graphql(self, query, variables=None):
        data = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.graphql_url, data=data, method="POST")
        req.add_header("x-api-key", self.api_key)
        req.add_header("Content-Type", "application/json")
        out = self._send(req)
        if out.get("errors"):
            raise ArizeError(f"GraphQL error: {out['errors']}")
        return out["data"]

    def list_tasks(self, space_id):
        cursor = None
        while True:
            page = self._rest("GET", "/tasks", {"space_id": space_id, "limit": 100, "cursor": cursor})
            yield from page.get("tasks", [])
            pagination = page.get("pagination") or {}
            if not pagination.get("has_more"):
                return
            cursor = pagination["next_cursor"]

    def list_runs_since(self, task_id, since_iso):
        """Runs come back newest first, so paging stops at the first run older than since_iso."""
        cursor = None
        while True:
            page = self._rest("GET", f"/tasks/{task_id}/runs", {"limit": 100, "cursor": cursor})
            for run in page.get("task_runs", []):
                if run["created_at"] < since_iso:
                    return
                yield run
            pagination = page.get("pagination") or {}
            if not pagination.get("has_more"):
                return
            cursor = pagination["next_cursor"]

    def update_task(self, task_id, fields):
        return self._rest("PATCH", f"/tasks/{task_id}", body=fields)

    def get_tracing_enabled(self, task_id):
        data = self._graphql(
            "query($id: ID!) { node(id: $id) { ... on OnlineTask { tracingEnabled } } }",
            {"id": task_id},
        )
        return bool((data.get("node") or {}).get("tracingEnabled"))

    def set_tracing_enabled(self, task_id, enabled):
        self._graphql(
            "mutation($input: PatchEvalTaskMutationInput!) {"
            " patchEvalTask(input: $input) { evalTask { id tracingEnabled } } }",
            {"input": {"onlineTaskId": task_id, "tracingEnabled": enabled}},
        )
