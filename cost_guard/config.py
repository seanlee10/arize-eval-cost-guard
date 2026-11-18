import json
import os

from .arize import ArizeClient
from .policy import SpacePolicy


def load_config(path=None):
    path = path or os.environ.get("COST_GUARD_CONFIG", "config.json")
    with open(path) as f:
        raw = json.load(f)
    defaults = raw.get("defaults", {})
    policies = [SpacePolicy(**{**defaults, **space}) for space in raw["spaces"]]
    return raw, policies


def make_client(raw):
    api_key = os.environ.get("ARIZE_API_KEY")
    if not api_key:
        raise SystemExit("ARIZE_API_KEY is not set")
    host = raw["arize_host"].rstrip("/")
    return ArizeClient(
        api_key=api_key,
        rest_url=raw.get("rest_url", f"{host}/v2"),
        graphql_url=raw.get("graphql_url", f"{host}/graphql"),
    )
