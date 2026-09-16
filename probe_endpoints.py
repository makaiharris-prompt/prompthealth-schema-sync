#!/usr/bin/env python3
"""Find the real Webflow endpoints for per-page JSON-LD. Read-only.

Probes a page KNOWN to have schema (/products/kiosk), not an empty one --
an absent key on an empty page tells you nothing.
"""
import json, os, sys, urllib.error, urllib.request

SITE = "663e8ac23e061e4b80b016d0"
KIOSK = "6761f1fac2557a50ffc2cd46"   # has SoftwareApplication schema live
DEMO = "6750c2593988ae0d16946cbb"    # has none
TOKEN = os.environ.get("WEBFLOW_API_TOKEN") or sys.exit("WEBFLOW_API_TOKEN not set")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://api.webflow.com/" + path.lstrip("/"), data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "accept": "application/json",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", "replace")[:200]}
    except Exception as e:
        return 0, {"err": str(e)}


print("=== keys returned by GET /v2/pages/{id} for a page that HAS schema ===")
code, d = call("GET", f"v2/pages/{KIOSK}")
print("status", code)
print("keys:", sorted(d.keys()) if isinstance(d, dict) else d)
for k in d:
    if "json" in k.lower() or "schema" in k.lower() or "ld" in k.lower():
        print(f"  !! {k} = {str(d[k])[:200]}")

print("\n=== documented beta route ===")
code, d = call("GET", f"beta/pages/{KIOSK}/schema-markup")
print(f"{code} GET /beta/pages/{{kiosk}}/schema-markup")
if code == 200:
    print("   stored:", json.dumps(d)[:300])
code, d = call("GET", f"beta/pages/{DEMO}/schema-markup")
print(f"{code} GET /beta/pages/{{demo}}/schema-markup  (page has no schema)")
if code == 200:
    print("   stored:", json.dumps(d)[:200])

print("\n=== other candidate READ routes ===")
reads = [
    ("GET",  f"v2/pages/{KIOSK}/schema_markup"),
    ("GET",  f"v2/pages/{KIOSK}/schema-markup"),
    ("GET",  f"v2/pages/{KIOSK}/json_ld"),
    ("GET",  f"v2/sites/{SITE}/pages/{KIOSK}/schema_markup"),
    ("GET",  f"v2/sites/{SITE}/pages/schema_markup?pageIds={KIOSK}"),
    ("POST", f"v2/sites/{SITE}/pages/schema_markup"),
    ("POST", f"v2/sites/{SITE}/pages/query_schema_markup"),
    ("GET",  f"beta/pages/{KIOSK}"),
    ("GET",  f"v2/pages/{KIOSK}?localeId="),
]
for method, path in reads:
    body = {"pages": [{"id": KIOSK}]} if method == "POST" else None
    code, d = call(method, path, body)
    s = json.dumps(d)[:150] if code not in (0,) else str(d)
    hit = "  <== HIT" if code == 200 else ""
    print(f"{code:4} {method:5} /{path}{hit}")
    if code == 200:
        print("       ", s)
