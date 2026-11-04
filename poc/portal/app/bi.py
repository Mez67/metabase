"""Adapters for the two BI tools. Each one knows how to embed the dashboard for
a signed-in user so that the user sees only their own branch."""

import json
import os
import re
import time

import httpx
import jwt

CONFIG_DIR = os.environ.get("BI_CONFIG_DIR", "/config")
ALL_BRANCHES = "*"


def load_config(name: str) -> dict:
    """Read what the setup script recorded (dashboard ids, secrets)."""
    path = os.path.join(CONFIG_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise RuntimeError(f"{path} not found: run the setup script first")
    with open(path) as f:
        return json.load(f)


def checked_branch(branch: str) -> str:
    # The branch is inserted into a filter, so allow only simple names.
    if not re.fullmatch(r"[a-z0-9_-]+", branch):
        raise ValueError(f"bad branch value: {branch!r}")
    return branch


class MetabaseBackend:
    """Metabase open-source "static embedding": the portal signs a JWT that
    names the dashboard and fixes ("locks") the branch filter. The browser can
    not change a locked filter because it can not re-sign the token."""

    name = "metabase"

    def __init__(self):
        self.public_url = os.environ.get("MB_PUBLIC_URL", "http://localhost:3000").rstrip("/")

    def embed_for(self, user: dict) -> dict:
        cfg = load_config("metabase")
        branch = user["branch"]
        # A locked filter must be present in the token; an empty list means
        # "no filter", which is what managers (branch "*") get.
        value = [] if branch == ALL_BRANCHES else [checked_branch(branch)]
        payload = {
            "resource": {"dashboard": cfg["dashboard_id"]},
            "params": {"branch": value},
            "exp": int(time.time()) + 10 * 60,
        }
        token = jwt.encode(payload, cfg["embedding_secret"], algorithm="HS256")
        return {
            "kind": "iframe",
            "url": f"{self.public_url}/embed/dashboard/{token}#bordered=false&titled=true",
        }

    def guest_token(self, user: dict) -> str:
        raise NotImplementedError


class SupersetBackend:
    """Apache Superset "embedded dashboards": the portal asks Superset for a
    short-lived guest token that carries a row-level security clause; the
    browser loads the dashboard through Superset's embedded SDK."""

    name = "superset"

    def __init__(self):
        self.url = os.environ.get("SUPERSET_URL", "http://superset:8088").rstrip("/")
        self.public_url = os.environ.get("SUPERSET_PUBLIC_URL", "http://localhost:8088").rstrip("/")
        self.username = os.environ.get("SUPERSET_USER", "portal")
        self.password = os.environ.get("SUPERSET_PASSWORD", "portal")
        self.provider = os.environ.get("SUPERSET_PROVIDER", "db")

    def embed_for(self, user: dict) -> dict:
        cfg = load_config("superset")
        return {"kind": "superset", "uuid": cfg["embedded_uuid"], "domain": self.public_url}

    def guest_token(self, user: dict) -> str:
        cfg = load_config("superset")
        branch = user["branch"]
        rls = []
        if branch != ALL_BRANCHES:
            rls = [{"dataset": cfg["sales_dataset_id"], "clause": f"branch = '{checked_branch(branch)}'"}]
        with httpx.Client(base_url=self.url, timeout=30) as client:
            login = client.post("/api/v1/security/login", json={
                "username": self.username, "password": self.password,
                "provider": self.provider, "refresh": False,
            })
            login.raise_for_status()
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            csrf = client.get("/api/v1/security/csrf_token/", headers=headers)
            csrf.raise_for_status()
            headers["X-CSRFToken"] = csrf.json()["result"]
            headers["Referer"] = self.url
            resp = client.post("/api/v1/security/guest_token/", headers=headers, json={
                "user": {"username": user["username"], "first_name": user["name"], "last_name": ""},
                "resources": [{"type": "dashboard", "id": cfg["embedded_uuid"]}],
                "rls": rls,
            })
            resp.raise_for_status()
            return resp.json()["token"]


def make_backend(name: str):
    backends = {"metabase": MetabaseBackend, "superset": SupersetBackend}
    if name not in backends:
        raise ValueError(f"BI_BACKEND must be one of {sorted(backends)}")
    return backends[name]()
