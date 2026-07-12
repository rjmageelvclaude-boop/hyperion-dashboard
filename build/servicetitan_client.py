#!/usr/bin/env python3
"""
Thin client for the live ServiceTitan API - the data source for every hosted
dashboard, and handy for ad-hoc questions against real-time data.

Credentials live in secrets/servicetitan.json (git-ignored, never commit).
There can be more than one OAuth "app" (e.g. a Sierra-only connection and a
network-wide "Enterprise Hub" connection) - secrets/servicetitan.json maps
each tenant code to the app that's authorized for it via `tenant_app`.

Transport notes (this module is shared by every board refresh):
  - HTTPS connections are kept alive and pooled per thread, and responses are
    gzip-compressed - both matter because a full refresh makes thousands of
    paginated GETs.
  - Thread-safe: boards fan out page fetches and companies across threads.
    A process-wide semaphore (ST_MAX_CONCURRENCY, default 8) caps in-flight
    requests so parallel boards don't stampede the API.
  - 429/5xx responses are retried in-place with backoff, honoring Retry-After.

Usage as a library:
    from servicetitan_client import st_get, TENANTS

    jobs = st_get("SIE", "/jpm/v2/tenant/{tenant}/jobs", params={"pageSize": 5})

Usage from the CLI (quick smoke test):
    py build/servicetitan_client.py SIE /crm/v2/tenant/{tenant}/customers?pageSize=1
"""
import gzip
import http.client
import json
import os
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CREDS_PATH = os.path.join(ROOT, "secrets", "servicetitan.json")
TOKEN_CACHE_PATH = os.path.join(ROOT, "secrets", ".token_cache.json")

AUTH_URL = "https://auth.servicetitan.io/connect/token"
API_BASE = "https://api.servicetitan.io"
# Wall-clock cap per request; the API occasionally stalls, so never hang forever.
REQUEST_TIMEOUT = int(os.environ.get("ST_TIMEOUT", "60"))
# Max in-flight requests per process; the refresh workflow runs several board
# processes at once, so keep each one's share of the API polite.
MAX_CONCURRENCY = max(1, int(os.environ.get("ST_MAX_CONCURRENCY", "8")))
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4

_creds = None
_sem = threading.BoundedSemaphore(MAX_CONCURRENCY)
_token_lock = threading.Lock()   # serializes auth calls + token cache access
_tokens = None                   # in-memory mirror of the disk token cache
_local = threading.local()       # per-thread keep-alive connections


def _load_creds():
    global _creds
    if _creds is None:
        if not os.path.exists(CREDS_PATH):
            raise RuntimeError(
                f"Missing {CREDS_PATH} - ServiceTitan credentials are not set up locally."
            )
        with open(CREDS_PATH, encoding="utf-8") as f:
            _creds = json.load(f)
    return _creds


TENANTS = {k: v["id"] for k, v in _load_creds().get("tenants", {}).items()} if os.path.exists(CREDS_PATH) else {}


def _app_for_tenant_code(tenant_code):
    """Which entry in creds['apps'] is authorized for this partner code."""
    creds = _load_creds()
    apps = creds.get("apps", {})
    tenant_app = creds.get("tenant_app", {})
    app_name = tenant_app.get(tenant_code.upper())
    if app_name and app_name in apps:
        return app_name, apps[app_name]
    # Single legacy-style creds file with no apps/tenant_app mapping.
    if "client_id" in creds:
        return "default", creds
    raise RuntimeError(f"No ServiceTitan app configured for tenant '{tenant_code}'.")


# ---------------------------------------------------------------- transport
def _connection(host):
    """Keep-alive HTTPS connection, one per (thread, host)."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(host)
    if conn is None:
        conn = conns[host] = http.client.HTTPSConnection(host, timeout=REQUEST_TIMEOUT)
    return conn


def _drop_connection(host):
    conn = getattr(_local, "conns", {}).pop(host, None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def _http(method, url, headers, body=None):
    """One HTTP round trip over a pooled connection.
    Returns (status, lowercase-header-dict, body-bytes). A kept-alive socket
    can go stale between requests - reconnect once before giving up."""
    parts = urllib.parse.urlsplit(url)
    target = parts.path + ("?" + parts.query if parts.query else "")
    headers = dict(headers, **{"Accept-Encoding": "gzip", "Connection": "keep-alive"})
    with _sem:
        for attempt in (1, 2):
            conn = _connection(parts.netloc)
            try:
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                break
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError):
                _drop_connection(parts.netloc)
                if attempt == 2:
                    raise
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    if resp_headers.get("content-encoding", "").lower() == "gzip":
        data = gzip.decompress(data)
    return resp.status, resp_headers, data


# ---------------------------------------------------------------- auth
def _load_tokens():
    global _tokens
    if _tokens is None:
        try:
            with open(TOKEN_CACHE_PATH, encoding="utf-8") as f:
                _tokens = json.load(f)
        except (OSError, json.JSONDecodeError):
            _tokens = {}
    return _tokens


def _save_tokens():
    # Atomic + unique tmp name: parallel board processes share this file.
    tmp = f"{TOKEN_CACHE_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_tokens, f)
        os.replace(tmp, TOKEN_CACHE_PATH)
    except OSError:
        pass  # cache is best-effort; worst case we re-auth next call


def get_access_token(app_name, app_creds, tenant_id=None, force_refresh=False):
    """Client-credentials OAuth2 token for one app, cached in memory + on disk.
    Multi-tenant clients (e.g. an Enterprise Hub connection) require the
    target tenant id in the token request itself and get a token scoped to
    that tenant, so the cache key includes tenant_id when given."""
    cache_key = f"{app_name}:{tenant_id}" if tenant_id else app_name
    with _token_lock:
        tokens = _load_tokens()
        if not force_refresh:
            entry = tokens.get(cache_key)
            if entry and entry.get("expires_at", 0) > time.time() + 60:
                return entry["access_token"]

        form = {
            "grant_type": "client_credentials",
            "client_id": app_creds["client_id"],
            "client_secret": app_creds["client_secret"],
        }
        if tenant_id:
            form["tenant"] = str(tenant_id)
        body = urllib.parse.urlencode(form).encode("utf-8")
        status, _, data = _http("POST", AUTH_URL, {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Hyperion-Dashboard/1.0",
            "Accept": "application/json",
        }, body=body)
        if status != 200:
            text = data.decode("utf-8", "ignore")
            if status == 400 and "tenant_required" in text and not tenant_id:
                raise RuntimeError(
                    f"App '{app_name}' is a multi-tenant client but no tenant_id was supplied for this token request."
                )
            raise RuntimeError(f"ServiceTitan auth failed for app '{app_name}' ({status}): {text}")

        payload = json.loads(data)
        tokens[cache_key] = {
            "access_token": payload["access_token"],
            "expires_at": time.time() + payload.get("expires_in", 900),
        }
        _save_tokens()
        return tokens[cache_key]["access_token"]


def resolve_tenant(tenant):
    """Accepts a partner code (SIE, BRO, ...) or a raw numeric tenant id."""
    if isinstance(tenant, int) or (isinstance(tenant, str) and tenant.isdigit()):
        return int(tenant)
    key = tenant.upper()
    if key not in TENANTS:
        raise ValueError(f"Unknown tenant '{tenant}'. Known: {', '.join(TENANTS)}")
    return TENANTS[key]


def _code_for_tenant_id(tenant_id):
    for code, tid in TENANTS.items():
        if tid == tenant_id:
            return code
    return None


def st_get(tenant, path, params=None):
    """GET against the ServiceTitan API for a given tenant (code or numeric id).
    `path` may contain a literal '{tenant}' placeholder."""
    return _st_request("GET", tenant, path, params=params)


def st_post(tenant, path, json_body=None, params=None):
    return _st_request("POST", tenant, path, params=params, json_body=json_body)


def _st_request(method, tenant, path, params=None, json_body=None):
    tenant_id = resolve_tenant(tenant)
    tenant_code = tenant.upper() if isinstance(tenant, str) and not tenant.isdigit() else _code_for_tenant_id(tenant_id)
    if not tenant_code:
        raise ValueError(f"Cannot determine partner code for tenant id {tenant_id}; pass a code like SIE instead.")

    app_name, app_creds = _app_for_tenant_code(tenant_code)

    formatted_path = path.format(tenant=tenant_id)
    if not formatted_path.startswith("/"):
        formatted_path = "/" + formatted_path

    url = API_BASE + formatted_path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")

    refreshed = False
    for attempt in range(MAX_RETRIES + 1):
        headers = {
            "Authorization": f"Bearer {get_access_token(app_name, app_creds, tenant_id=tenant_id)}",
            "ST-App-Key": app_creds["app_key"],
            "User-Agent": "Hyperion-Dashboard/1.0",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        status, resp_headers, data = _http(method, url, headers, body=body)
        if status < 300:
            return json.loads(data) if data else None
        if status == 401 and not refreshed:
            refreshed = True
            get_access_token(app_name, app_creds, tenant_id=tenant_id, force_refresh=True)
            continue
        if status in RETRY_STATUSES and attempt < MAX_RETRIES:
            try:
                delay = min(float(resp_headers.get("retry-after", "")), 30.0)
            except ValueError:
                delay = 1.5 * (attempt + 1)
            time.sleep(delay)
            continue
        raise RuntimeError(
            f"ServiceTitan API error ({status}) for {url} [app={app_name}]: "
            f"{data.decode('utf-8', 'ignore')[:500]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: py build/servicetitan_client.py <TENANT_CODE|tenant_id> <path> [query]")
        print(f"Known tenants: {', '.join(TENANTS)}")
        sys.exit(1)
    tenant_arg = sys.argv[1]
    path_arg = sys.argv[2]
    result = st_get(tenant_arg, path_arg)
    print(json.dumps(result, indent=2)[:4000])
