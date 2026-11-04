"""Configure open-source Metabase for the proof of concept: first-run setup,
connect the sales database, build a dashboard with a locked "branch" filter,
turn on static embedding and LDAP sign-in, and record what the portal needs in
/config/metabase.json. Safe to run more than once."""

import json
import os
import secrets
import sys
import time

import httpx

URL = os.environ.get("MB_URL", "http://metabase:3000")
ADMIN_EMAIL = os.environ.get("MB_ADMIN_EMAIL", "admin@corp.local")
ADMIN_PASSWORD = os.environ.get("MB_ADMIN_PASSWORD", "Adm1n-Passw0rd!")
CONFIG_PATH = os.environ.get("BI_CONFIG_DIR", "/config") + "/metabase.json"
DATA_DB = {
    "host": "sample-db", "port": 5432, "dbname": "sales",
    "user": "bi_reader", "password": "bi_reader", "ssl": False,
}
LDAP = {
    "ldap-enabled": True,
    "ldap-host": os.environ.get("LDAP_HOST", "ldap"),
    "ldap-port": int(os.environ.get("LDAP_PORT", "389")),
    "ldap-security": "none",
    "ldap-bind-dn": os.environ.get("LDAP_BIND_DN", "cn=readonly,dc=corp,dc=local"),
    "ldap-password": os.environ.get("LDAP_BIND_PASSWORD", "readonly"),
    "ldap-user-base": os.environ.get("LDAP_USER_BASE", "ou=users,dc=corp,dc=local"),
    # Active Directory: (&(objectClass=user)(|(sAMAccountName={login})(mail={login})))
    "ldap-user-filter": os.environ.get(
        "LDAP_USER_FILTER", "(&(objectClass=inetOrgPerson)(|(uid={login})(mail={login})))"),
    "ldap-attribute-email": "mail",
    "ldap-attribute-firstname": "givenName",
    "ldap-attribute-lastname": "sn",
    "ldap-group-sync": True,
    "ldap-group-base": os.environ.get("LDAP_GROUP_BASE", "ou=groups,dc=corp,dc=local"),
}
ANALYST_GROUP_DN = os.environ.get("LDAP_ANALYST_GROUP", "cn=bi-analysts,ou=groups,dc=corp,dc=local")


class Metabase:
    def __init__(self):
        self.http = httpx.Client(base_url=URL, timeout=120)

    def call(self, method, path, **kwargs):
        resp = self.http.request(method, f"/api{path}", **kwargs)
        if resp.status_code >= 400:
            sys.exit(f"{method} {path} failed: {resp.status_code} {resp.text[:500]}")
        return resp.json() if resp.content else {}

    def wait(self):
        for _ in range(180):
            try:
                if self.http.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(5)
        sys.exit("Metabase did not become healthy")

    def sign_in(self):
        token = self.call("GET", "/session/properties").get("setup-token")
        if token and not self.call("GET", "/session/properties").get("has-user-setup"):
            self.call("POST", "/setup", json={
                "token": token,
                "user": {"first_name": "Admin", "last_name": "User", "email": ADMIN_EMAIL,
                         "password": ADMIN_PASSWORD, "site_name": "Reports"},
                "prefs": {"site_name": "Reports", "allow_tracking": False},
            })
        session = self.call("POST", "/session", json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        self.http.headers["X-Metabase-Session"] = session["id"]

    def setting(self, key, value):
        self.call("PUT", f"/setting/{key}", json={"value": value})


def wait_for_fields(mb, db_id):
    """Sync runs in the background after the database is added."""
    for _ in range(60):
        meta = mb.call("GET", f"/database/{db_id}/metadata")
        tables = {t["name"]: t for t in meta.get("tables", [])}
        if {"sales", "products"} <= tables.keys() and tables["sales"]["fields"]:
            return tables
        time.sleep(3)
    sys.exit("database sync did not finish")


def main():
    mb = Metabase()
    mb.wait()
    mb.sign_in()

    dbs = mb.call("GET", "/database")
    dbs = dbs.get("data", dbs)
    db_id = next((d["id"] for d in dbs if d["name"] == "Sales"), None)
    if not db_id:
        db_id = mb.call("POST", "/database", json={"engine": "postgres", "name": "Sales", "details": DATA_DB})["id"]
    tables = wait_for_fields(mb, db_id)
    sales, products = tables["sales"], tables["products"]
    f = {fld["name"]: fld["id"] for fld in sales["fields"]}
    category = next(fld["id"] for fld in products["fields"] if fld["name"] == "category")

    # Static embedding: available in the open-source edition.
    secret = secrets.token_hex(32)
    mb.setting("enable-embedding-static", True)
    mb.setting("embedding-secret-key", secret)

    dashboards = mb.call("GET", "/dashboard")
    dash = next((d for d in dashboards if d["name"] == "Sales" and not d.get("archived")), None)
    if not dash:
        dash = create_dashboard(mb, db_id, sales["id"], f, category)

    # Analysts in the directory group become Metabase administrators.
    groups = mb.call("GET", "/permissions/group")
    admin_group = next(g["id"] for g in groups if g["name"] == "Administrators")
    mb.call("PUT", "/ldap/settings", json={**LDAP, "ldap-group-mappings": {ANALYST_GROUP_DN: [admin_group]}})

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump({"dashboard_id": dash["id"], "embedding_secret": secret}, fh)
    print(f"Metabase ready: dashboard {dash['id']}")


def create_dashboard(mb, db_id, sales_id, f, category_id):
    amount = ["field", f["amount"], None]
    total = [["sum", amount]]

    def card(name, display, query, size):
        c = mb.call("POST", "/card", json={
            "name": name, "display": display, "visualization_settings": {},
            "dataset_query": {"database": db_id, "type": "query",
                              "query": {"source-table": sales_id, "aggregation": total, **query}},
        })
        return c["id"], size

    cards = [
        card("Total sales", "scalar", {}, (6, 4)),
        card("Monthly sales", "line",
             {"breakout": [["field", f["sale_date"], {"temporal-unit": "month"}]]}, (12, 8)),
        card("Sales by category", "bar",
             {"breakout": [["field", category_id, {"source-field": f["product_id"]}]]}, (12, 8)),
        card("Sales by branch", "pie", {"breakout": [["field", f["branch"], None]]}, (6, 8)),
    ]

    param = {"id": "branch01", "name": "Branch", "slug": "branch", "type": "string/="}
    dash = mb.call("POST", "/dashboard", json={"name": "Sales", "parameters": [param]})

    dashcards, col, row = [], 0, 0
    for i, (card_id, (w, h)) in enumerate(cards):
        if col + w > 24:
            col, row = 0, row + 8
        dashcards.append({
            "id": -(i + 1), "card_id": card_id, "row": row, "col": col, "size_x": w, "size_y": h,
            "parameter_mappings": [{"parameter_id": "branch01", "card_id": card_id,
                                    "target": ["dimension", ["field", f["branch"], None]]}],
        })
        col += w
    return mb.call("PUT", f"/dashboard/{dash['id']}", json={
        "dashcards": dashcards,
        "enable_embedding": True,
        # "locked": the value must come from the signed token, never the browser.
        "embedding_params": {"branch": "locked"},
    })


if __name__ == "__main__":
    main()
