"""Enapps ERP connector — Project Order Lines (POL) push only.

Trimmed from The Hub's own app/erp_connector.py (github: 4. Pricing Sheet Project) to just
the piece this tool needs: pushing a picking list's items onto an existing Enapps project as
Project Order Lines, the same import chain Hub's Second Fix Tracker and its own picking-list
Push-to-ERP modal both use. Deliberately shares the SAME env var names and chain/template
defaults as Hub so the two deployments can be configured identically (same ENAPPS_ACCESS_TOKEN)
without any behaviour drift between them.

AUTH — two modes:
  * Static access token (current): set ENAPPS_ACCESS_TOKEN to the Bearer JWT issued by the Enapps
    team. Used directly on every call, no handshake.
  * Login handshake (fallback): POST /api/v1/auth {login,password} with Authorization: Bearer
    <ENAPPS_SECRET_KEY>, returns an access token. Used only if ENAPPS_ACCESS_TOKEN is unset.

Config (env): ENAPPS_URL, ENAPPS_ACCESS_TOKEN (or ENAPPS_SECRET_KEY/ENAPPS_USER/ENAPPS_PASSWORD),
  ENAPPS_VERIFY_SSL ('1' to verify TLS), ENAPPS_POL_CHAIN_ID (default '79'),
  ENAPPS_POL_TEMPLATE_ID (default '122').

project_id: Enapps' own full project name (e.g. "WSO086524 Gross Springs 15913"), not just the
bare WSO — there's no dedicated WSO field on their side, so this is a required, hand-entered
value; this module does no lookup/normalisation of its own.

sale_cost is DELIBERATELY always 0 — Enapps already holds the authoritative cost against each
product's own master record, so a picking-list push must never allocate this tool's own (or
anyone's) cost figure onto a line here. The column is still SENT because Enapps' do_import
rejects a missing/null sale_cost outright; 0 is the only safe, always-accepted value.
"""
import os, ssl, time, json, threading, urllib.request, urllib.parse

_LOCK = threading.Lock()
_TOKEN = {"value": None, "exp": 0}

# do_import (the configurator/POL append) scales with line count and can genuinely take longer
# than a short timeout to come back even on a push that ultimately succeeds — see Hub's own
# erp_connector.py for the incident this default is copied from. 120s is comfortably under
# Railway's own ~300s edge-proxy ceiling for a request.
IMPORT_TIMEOUT = 120


def is_configured():
    url = os.environ.get("ENAPPS_URL")
    static = os.environ.get("ENAPPS_ACCESS_TOKEN")
    handshake = all(os.environ.get(k) for k in ("ENAPPS_SECRET_KEY", "ENAPPS_USER", "ENAPPS_PASSWORD"))
    return bool(url and (static or handshake))


def _ctx():
    if os.environ.get("ENAPPS_VERIFY_SSL") == "1":
        return ssl.create_default_context()
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    return c


def _url(path, params=None):
    base = os.environ["ENAPPS_URL"].rstrip("/")
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return base + path + q


def _send(path, method="GET", token=None, body=None, params=None, timeout=30):
    headers = {"Accept": "application/json;charset=UTF-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_url(path, params), data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(detail)
            detail = j.get("error", {}).get("message") or j.get("title") or detail
        except Exception:
            pass
        raise RuntimeError(f"Enapps {e.code}: {str(detail)[:200]}")


def _token():
    static = os.environ.get("ENAPPS_ACCESS_TOKEN")
    if static:
        return static
    with _LOCK:
        if _TOKEN["value"] and _TOKEN["exp"] - 30 > time.time():
            return _TOKEN["value"]
        key = os.environ.get("ENAPPS_SECRET_KEY")
        user = os.environ.get("ENAPPS_USER")
        pw = os.environ.get("ENAPPS_PASSWORD")
        if not (key and user and pw):
            raise RuntimeError("Enapps not configured — set ENAPPS_ACCESS_TOKEN (or SECRET_KEY/USER/PASSWORD)")
        res = _send("/api/v1/auth", "POST", token=key, body={"login": user, "password": pw})
        tok = res.get("access_token") or res.get("token") or \
            next((v for v in res.values() if isinstance(v, str) and v.count(".") == 2), None)
        if not tok:
            raise RuntimeError(f"Auth ok but no access token in response: {list(res)[:6]}")
        _TOKEN["value"] = tok; _TOKEN["exp"] = time.time() + 3000
        return tok


def custom_view(table_name):
    """GET /api/v1/custom_view?table_name=<view> — a read-only Postgres view connected to the API
    user (Enapps admin: Settings > Users > Postgres views). Used here only to resolve a WSO to its
    ea_project's full name — see find_ea_project_by_wso."""
    return _send("/api/v1/custom_view", "GET", token=_token(), params={"table_name": table_name})


def fetch_ea_projects():
    """[{id, name}, ...] for every ea_project record — there's no REST endpoint for ea_project at
    all (every /api/v1/project* guess 404s on this instance), so this rides the same connected
    Postgres view Hub already reads for the same purpose. `name` is the project's full name (e.g.
    "WSO086524 Gross Springs 15913") — there's no dedicated WSO field, so callers match it by
    substring (see find_ea_project_by_wso)."""
    view = os.environ.get("ENAPPS_PRODUCT_COST_VIEW", "readonly_view_tompostgres")
    res = custom_view(view)
    if res.get("errors"):
        raise RuntimeError("Enapps custom_view '%s' returned errors: %s" % (view, res["errors"]))
    rows = res.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected custom_view response shape: %r" % (res,))
    return [{"id": r.get("code"), "name": r.get("description")} for r in rows if r.get("row_type") == "ea_project"]


def find_ea_project_by_wso(wso):
    """The ea_project whose name contains this WSO reference (e.g. "WSO086616" inside
    "WSO086616 Parsons Wells 80518"), or None. First match wins — WSO references are unique in
    practice."""
    wso = str(wso or "").strip().lower()
    if not wso:
        return None
    for p in fetch_ea_projects():
        if wso in str(p.get("name") or "").lower():
            return p
    return None


def build_pol_payload(lines, project_id, chain_id=None, template_id=None):
    """Assemble the do_import (amend) body from picking-list lines
    ([{product_id, description, sale_price, qty}, ...]). Pure builder (no network)."""
    chain_id = str(chain_id or os.environ.get("ENAPPS_POL_CHAIN_ID", "79"))
    template_id = str(template_id or os.environ.get("ENAPPS_POL_TEMPLATE_ID", "122"))
    tkey = "template_%s" % template_id
    entries = []
    for ln in (lines or []):
        entries.append({tkey: {
            "project_id": project_id,
            "product_id": ln.get("product_id", ""),
            "name": ln.get("description") or "",
            "sale_cost": 0,               # never a WMS-derived figure — see module docstring
            "sale_price": ln.get("sale_price", 0),
            "product_uom_qty": int(ln.get("qty", 1) or 1),
        }})
    return {"import_data": {chain_id: entries}}


def push_pol_import(lines, project_id, dry_run=True):
    """Build and (unless dry_run) POST the Project Order Lines import. Returns the assembled
    payload either way; on a live push also returns the raw API response, so a failure can be
    diagnosed directly against what Enapps actually rejected."""
    payload = build_pol_payload(lines, project_id)
    if dry_run:
        return {"dry_run": True, "payload": payload}
    res = _send("/api/v1/import_chain/do_import", "POST", token=_token(), body=payload, timeout=IMPORT_TIMEOUT)
    return {"dry_run": False, "result": res, "payload": payload}
