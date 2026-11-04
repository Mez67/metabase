"""Internal portal: users sign in with Active Directory (LDAP), see a dashboard
embedded from the BI tool filtered to their own branch, and can ask the AI for
a report. Every action is written to an audit log.

BI_BACKEND chooses the BI tool: "metabase" or "superset".
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from ldap3 import Connection, Server
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from starlette.middleware.sessions import SessionMiddleware

import bi

LDAP_URL = os.environ.get("LDAP_URL", "ldap://ldap:389")
# A read-only account used to look users up. In AD e.g. "svc-reports@corp.local".
LDAP_BIND_DN = os.environ.get("LDAP_BIND_DN", "cn=readonly,dc=corp,dc=local")
LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "readonly")
# For Active Directory e.g. "DC=corp,DC=local".
LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "dc=corp,dc=local")
# For Active Directory: "(sAMAccountName={username})".
LDAP_USER_FILTER = os.environ.get("LDAP_USER_FILTER", "(uid={username})")
# The directory attribute that holds the user's branch. In AD this is often "department".
LDAP_BRANCH_ATTR = os.environ.get("LDAP_BRANCH_ATTR", "departmentNumber")
AI_SERVICE_URL = os.environ.get("AI_SERVICE_URL", "http://ai-service:8000")
AI_SERVICE_TOKEN = os.environ.get("AI_SERVICE_TOKEN", "change-me")
AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit.db")
ALL_BRANCHES = "*"

app = FastAPI(title="Reports portal")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("PORTAL_SESSION_SECRET", "change-me"),
    max_age=8 * 3600,
    same_site="lax",
)
HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
backend = bi.make_backend(os.environ.get("BI_BACKEND", "metabase"))


# --- audit log -------------------------------------------------------------

def audit_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(AUDIT_DB), exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit (
             id INTEGER PRIMARY KEY, at TEXT, username TEXT, branch TEXT,
             action TEXT, detail TEXT, ip TEXT)"""
    )
    return conn


def audit(request: Request, action: str, detail: dict | None = None, username: str | None = None):
    user = request.session.get("user") or {}
    with audit_conn() as conn:
        conn.execute(
            "INSERT INTO audit (at, username, branch, action, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                username or user.get("username"),
                user.get("branch"),
                action,
                json.dumps(detail or {}, ensure_ascii=False),
                request.client.host if request.client else None,
            ),
        )


# --- LDAP / Active Directory -----------------------------------------------

def ldap_login(username: str, password: str) -> dict | None:
    """Find the user with the read-only service account, then bind as the user
    to check the password (the usual pattern for Active Directory too)."""
    if not password or not re.fullmatch(r"[a-z0-9._-]{1,64}", username):
        return None
    server = Server(LDAP_URL, connect_timeout=5)
    try:
        with Connection(server, user=LDAP_BIND_DN, password=LDAP_BIND_PASSWORD, auto_bind=True) as svc:
            svc.search(
                LDAP_BASE_DN,
                LDAP_USER_FILTER.format(username=escape_filter_chars(username)),
                attributes=["cn", "mail", LDAP_BRANCH_ATTR],
            )
            if len(svc.entries) != 1:
                return None
            entry = svc.entries[0]
        # Raises if the password is wrong.
        Connection(server, user=entry.entry_dn, password=password, auto_bind=True).unbind()
    except LDAPException:
        return None
    branch = entry[LDAP_BRANCH_ATTR].value if LDAP_BRANCH_ATTR in entry else None
    if not branch:
        return None  # no branch assigned: no access
    return {
        "username": username,
        "name": str(entry.cn.value),
        "email": str(entry.mail.value) if "mail" in entry else "",
        "branch": str(branch).lower(),
    }


def current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


# --- pages -----------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "backend": backend.name})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    user = ldap_login(username, password)
    if not user:
        audit(request, "login_failed", username=username)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "نام کاربری یا رمز عبور اشتباه است", "backend": backend.name},
            status_code=401,
        )
    request.session["user"] = user
    audit(request, "login")
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    if request.session.get("user"):
        audit(request, "logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        embed = backend.embed_for(user)
        error = None
    except Exception as e:  # BI tool not set up yet, or unreachable
        embed, error = None, str(e)
    audit(request, "view_dashboard", {"backend": backend.name})
    return templates.TemplateResponse(
        request, "home.html",
        {"user": user, "embed": embed, "error": error, "backend": backend.name,
         "is_manager": user["branch"] == ALL_BRANCHES},
    )


@app.get("/api/guest-token")
def guest_token(request: Request):
    """Superset's embedded SDK asks for a fresh guest token through this."""
    user = current_user(request)
    return {"token": backend.guest_token(user)}


@app.post("/api/ask")
async def ask(request: Request):
    user = current_user(request)
    body = await request.json()
    question = str(body.get("question", "")).strip()
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(
            f"{AI_SERVICE_URL}/ask",
            headers={"X-Service-Token": AI_SERVICE_TOKEN},
            json={"question": question, "branch": user["branch"], "user": user["username"]},
        )
    result = resp.json()
    audit(request, "ai_ask", {
        "question": question,
        "ok": resp.status_code == 200,
        "sql": result.get("sql"),
        "rows": len(result.get("rows", [])),
        "seconds": round(time.monotonic() - started, 1),
    })
    return JSONResponse(result, status_code=resp.status_code)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    user = current_user(request)
    if user["branch"] != ALL_BRANCHES:
        raise HTTPException(status_code=403, detail="managers only")
    with audit_conn() as conn:
        rows = conn.execute(
            "SELECT at, username, branch, action, detail, ip FROM audit ORDER BY id DESC LIMIT 500"
        ).fetchall()
    return templates.TemplateResponse(request, "audit.html", {"user": user, "rows": rows})


@app.get("/health")
def health():
    return {"ok": True, "backend": backend.name}
