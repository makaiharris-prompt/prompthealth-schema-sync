#!/usr/bin/env python3
"""Revert a page's STAGED schema to whatever the live page currently serves.

Useful when a run staged something that should not ship. Because nothing has
been published, the live page is still the pre-change state, so it is the
authoritative thing to restore.

    WEBFLOW_API_TOKEN=... python3 restore_page.py /practice-type/universities
"""
import json
import os
import sys
import urllib.request

import faqparse
from sync import BASE, SITE_ID, Webflow, to_nodes

if len(sys.argv) < 2:
    sys.exit(__doc__)
paths = sys.argv[1:]
TOKEN = os.environ.get("WEBFLOW_API_TOKEN") or sys.exit("WEBFLOW_API_TOKEN not set")
wf = Webflow(TOKEN)

by_path = {}
for pg in wf.pages():
    p = pg.get("publishedPath") or ""
    if p and not (pg.get("slug") or "").startswith("detail_"):
        by_path.setdefault(p, pg)

for path in paths:
    pg = by_path.get(path)
    if not pg:
        print(f"{path}: no such page");  continue
    html = urllib.request.urlopen(
        urllib.request.Request(BASE + path, headers={"User-Agent": "restore/1.0"}),
        timeout=45).read().decode("utf-8", "replace")
    nodes, ok = faqparse.existing_jsonld(html)
    if not ok:
        print(f"{path}: live JSON-LD will not parse; refusing to guess");  continue

    if not nodes:
        doc = None
    elif len(nodes) == 1:
        doc = {"@context": "https://schema.org", **nodes[0]}
    else:
        doc = {"@context": "https://schema.org", "@graph": nodes}

    code, resp = wf._call("PUT", f"pages/{pg['id']}/schema-markup",
                          {"jsonLdSchema": doc},
                          base="https://api.webflow.com/beta")
    if code not in (200, 201, 202):
        print(f"{path}: FAILED {code} {json.dumps(resp)[:200]}");  continue

    back = wf.get_schema_bulk([pg["id"]]).get(pg["id"])
    got = sorted(str(n.get("@type")) for n in to_nodes(back)) if back else []
    want = sorted(str(n.get("@type")) for n in nodes)
    print(f"{path}: restored -> {got}  {'OK' if got == want else 'MISMATCH, expected ' + str(want)}")
