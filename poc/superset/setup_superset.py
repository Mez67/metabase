"""Configure Superset for the proof of concept: connect the sales database,
build a dashboard, enable embedding, create the guest role, and record the ids
the portal needs in /config/superset.json. Safe to run more than once."""

import json
import os
import sys
import time

import requests

URL = os.environ.get("SUPERSET_URL", "http://superset:8088")
USER = os.environ.get("SUPERSET_USER", "portal")
PASSWORD = os.environ.get("SUPERSET_PASSWORD", "portal")
PROVIDER = os.environ.get("SUPERSET_PROVIDER", "db")
DATA_DB_URI = os.environ.get(
    "DATA_DB_URI", "postgresql+psycopg2://bi_reader:bi_reader@sample-db:5432/sales"
)
CONFIG_PATH = os.environ.get("BI_CONFIG_DIR", "/config") + "/superset.json"
GUEST_ROLE = "EmbedViewer"

DATASET_SQL = """SELECT s.sale_date, s.branch, s.quantity, s.amount,
       p.name AS product, p.category
FROM sales s JOIN products p ON p.id = s.product_id"""

TOTAL = {"expressionType": "SIMPLE", "column": {"column_name": "amount"},
         "aggregate": "SUM", "label": "Total sales"}


def wait_for_superset(session):
    for _ in range(120):
        try:
            if session.get(f"{URL}/health", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    sys.exit("Superset did not become healthy")


def api(session, method, path, **kwargs):
    resp = session.request(method, f"{URL}/api/v1{path}", timeout=60, **kwargs)
    if not resp.ok:
        sys.exit(f"{method} {path} failed: {resp.status_code} {resp.text[:500]}")
    return resp.json() if resp.content else {}


def find(session, resource, column, value):
    q = json.dumps({"filters": [{"col": column, "opr": "eq", "value": value}]})
    result = api(session, "GET", f"/{resource}/", params={"q": q})["result"]
    return result[0]["id"] if result else None


def main():
    s = requests.Session()
    wait_for_superset(s)
    token = api(s, "POST", "/security/login", json={
        "username": USER, "password": PASSWORD, "provider": PROVIDER, "refresh": False,
    })["access_token"]
    s.headers["Authorization"] = f"Bearer {token}"
    s.headers["X-CSRFToken"] = api(s, "GET", "/security/csrf_token/")["result"]
    s.headers["Referer"] = URL

    db_id = find(s, "database", "database_name", "Sales") or api(s, "POST", "/database/", json={
        "database_name": "Sales", "sqlalchemy_uri": DATA_DB_URI, "expose_in_sqllab": True,
    })["id"]

    ds_id = find(s, "dataset", "table_name", "sales_detail")
    if not ds_id:
        ds_id = api(s, "POST", "/dataset/", json={
            "database": db_id, "schema": "public", "table_name": "sales_detail", "sql": DATASET_SQL,
        })["id"]
        api(s, "PUT", f"/dataset/{ds_id}/refresh")

    dash_id = find(s, "dashboard", "dashboard_title", "Sales")
    if not dash_id:
        dash_id = api(s, "POST", "/dashboard/", json={
            "dashboard_title": "Sales", "slug": "sales", "published": True,
        })["id"]
        datasource = f"{ds_id}__table"
        charts = [
            ("Total sales", "big_number_total", {"metric": TOTAL}),
            ("Monthly sales", "echarts_timeseries_line",
             {"x_axis": "sale_date", "time_grain_sqla": "P1M", "metrics": [TOTAL], "groupby": []}),
            ("Sales by category", "echarts_timeseries_bar",
             {"x_axis": "category", "metrics": [TOTAL], "groupby": []}),
            ("Sales by branch", "pie", {"groupby": ["branch"], "metric": TOTAL}),
        ]
        chart_ids = []
        for name, viz, extra in charts:
            params = {"viz_type": viz, "datasource": datasource, "adhoc_filters": [],
                      "row_limit": 10000, **extra}
            chart_ids.append(api(s, "POST", "/chart/", json={
                "slice_name": name, "viz_type": viz, "datasource_id": ds_id,
                "datasource_type": "table", "params": json.dumps(params), "dashboards": [dash_id],
            })["id"])
        api(s, "PUT", f"/dashboard/{dash_id}", json={"position_json": json.dumps(layout(chart_ids))})

    embedded = api(s, "POST", f"/dashboard/{dash_id}/embedded", json={"allowed_domains": []})
    uuid = embedded["result"]["uuid"]

    grant_guest_role(ds_id)

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump({"embedded_uuid": uuid, "sales_dataset_id": ds_id, "dashboard_id": dash_id}, f)
    print(f"Superset ready: dashboard {dash_id}, embedded uuid {uuid}")


def layout(chart_ids):
    """Two charts per row."""
    pos = {"DASHBOARD_VERSION_KEY": "v2",
           "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
           "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]}}
    for i in range(0, len(chart_ids), 2):
        row = f"ROW-{i}"
        pos["GRID_ID"]["children"].append(row)
        pos[row] = {"type": "ROW", "id": row, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
                    "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for cid in chart_ids[i:i + 2]:
            key = f"CHART-{cid}"
            pos[row]["children"].append(key)
            pos[key] = {"type": "CHART", "id": key, "children": [],
                        "parents": ["ROOT_ID", "GRID_ID", row],
                        "meta": {"chartId": cid, "width": 6, "height": 50}}
    return pos


def grant_guest_role(dataset_id):
    """Guest role = Gamma's permissions + read access to the one dataset."""
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        from superset import db, security_manager as sm
        from superset.connectors.sqla.models import SqlaTable

        role = sm.find_role(GUEST_ROLE) or sm.add_role(GUEST_ROLE)
        for pv in sm.find_role("Gamma").permissions:
            sm.add_permission_role(role, pv)
        dataset = db.session.get(SqlaTable, dataset_id)
        pv = sm.add_permission_view_menu("datasource_access", dataset.perm)
        sm.add_permission_role(role, pv)
        db.session.commit()


if __name__ == "__main__":
    main()
