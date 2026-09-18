#!/usr/bin/env python3
"""Check what a Webflow publish would actually make live. Read-only.

Compares, for every FAQ page:
  * the schema STAGED in Webflow (what publishing would ship)
  * against the schema currently SERVED on the live page
  * against the FAQs visible on the live page

Answers three questions before you publish:
  1. Does every page that has schema today still have it?   (nothing clobbered)
  2. Does the staged FAQ content match the visible page?     (no invented Q&As)
  3. What exactly changes?                                   (the diff)

CMS items are checked differently. They publish per item, so what is stored is
already live -- the question is whether it REACHES the page. A stored schema
missing from the served HTML means the template's HTML Embed is not bound to
the field, which looks like success from the API's side and ships nothing.

Exits non-zero if anything looks wrong. Writes nothing, publishes nothing.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import faqparse
from sync import BASE, BETA, CMS_COLLECTIONS, SITE_ID, Webflow, to_nodes

TOKEN = os.environ.get("WEBFLOW_API_TOKEN") or sys.exit("WEBFLOW_API_TOKEN not set")
wf = Webflow(TOKEN)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "verify/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def check_cms(problems):
    """Confirm each item's stored schema actually reaches its live page.

    Returns rows of (path, stored_qs, live_qs, visible_qs). Unlike static
    pages, there is no separate "staged" state to compare against: the field
    value IS what ships. So the failure this catches is a binding problem --
    correct JSON written to a field the template never outputs.
    """
    rows = []
    for cid, cfg in CMS_COLLECTIONS.items():
        field, prefix = cfg["field"], cfg["path"]
        try:
            items = wf.list_items(cid)
        except Exception as e:                                    # noqa: BLE001
            problems.append(f"{cfg['name']}: could not list items ({e})")
            continue

        for item in items:
            if item.get("isDraft") or item.get("isArchived"):
                continue
            fd = item.get("fieldData") or {}
            stored = (fd.get(field) or "").strip()
            if not stored:
                continue                        # no FAQs on this post; nothing to check
            path = f"{prefix}/{fd.get('slug')}"

            try:
                doc = json.loads(stored)
            except ValueError as e:
                problems.append(f"{path}: stored {field} is not valid JSON ({e})")
                continue
            stored_qs = [q.get("name") for q in doc.get("mainEntity", [])]

            try:
                html = fetch(BASE + path)
            except Exception as e:                                # noqa: BLE001
                problems.append(f"{path}: could not fetch live page ({e})")
                continue

            live_nodes, ok = faqparse.existing_jsonld(html)
            if not ok:
                problems.append(f"{path}: live page serves unparseable JSON-LD")
                continue
            live_faq = next((n for n in live_nodes
                             if n.get("@type") == "FAQPage"), None)
            if not live_faq:
                problems.append(
                    f"{path}: {field} holds {len(stored_qs)} question(s) but the "
                    "live page serves no FAQPage -- the template's HTML Embed is "
                    "probably not bound to this field, or is unpublished")
                continue
            live_qs = [q.get("name") for q in live_faq.get("mainEntity", [])]

            missing = [q for q in stored_qs if q not in live_qs]
            if missing:
                problems.append(f"{path}: {len(missing)} stored question(s) missing "
                                f"from the live page, e.g. {missing[0][:50]!r}")

            visible = faqparse.extract_all(html, BASE)
            visible_qs = [q for q, _ in visible["items"]]
            invented = [q for q in stored_qs if q not in visible_qs]
            if invented:
                problems.append(f"{path}: {len(invented)} question(s) in schema are "
                                f"not visible on the page, e.g. {invented[0][:50]!r}")

            rows.append((path, len(stored_qs), len(live_qs), len(visible_qs)))
    return rows


def main():
    code, site = wf._call("GET", f"sites/{SITE_ID}")
    if code != 200:
        sys.exit(f"auth failed ({code})")
    u, p = site.get("lastUpdated"), site.get("lastPublished")
    print("SITE")
    print(f"  lastUpdated   {u}")
    print(f"  lastPublished {p}")
    if u != p:
        print("  ! The Designer has changes that were never published. A full-site")
        print("    publish ships those too, not only the FAQ schema below.")
    print()

    pages = {}
    for pg in wf.pages():
        if pg.get("draft") or pg.get("archived"):
            continue
        if (pg.get("slug") or "").startswith("detail_"):
            continue
        path = pg.get("publishedPath") or ""
        if path and not path.startswith("/wip/"):
            pages.setdefault(path, pg)

    staged_all = wf.get_schema_bulk([p_["id"] for p_ in pages.values()])
    id_to_path = {p_["id"]: path for path, p_ in pages.items()}

    rows, problems = [], []
    for pid, staged in sorted(staged_all.items(), key=lambda kv: id_to_path.get(kv[0], "")):
        path = id_to_path.get(pid)
        if not path:
            continue
        staged_nodes = to_nodes(staged) if staged else []
        staged_faq = next((n for n in staged_nodes if n.get("@type") == "FAQPage"), None)
        if not staged_faq:
            continue                                   # not an FAQ page; nothing to check

        try:
            html = fetch(BASE + path)
        except Exception as e:                          # noqa: BLE001
            problems.append(f"{path}: could not fetch live page ({e})")
            continue

        live_nodes, ok = faqparse.existing_jsonld(html)
        if not ok:
            problems.append(f"{path}: live page serves unparseable JSON-LD")
            continue
        # extract_all, not extract: the same function sync.py uses, so a
        # rich-text page like /faq is not reported as having invented
        # every one of its questions.
        visible = faqparse.extract_all(html, BASE)

        live_types = {str(n.get("@type")) for n in live_nodes} - {"FAQPage"}
        staged_types = {str(n.get("@type")) for n in staged_nodes} - {"FAQPage"}
        lost = live_types - staged_types
        if lost:
            problems.append(f"{path}: staged schema DROPS existing node(s) {sorted(lost)}")

        staged_qs = [q.get("name") for q in staged_faq.get("mainEntity", [])]
        visible_qs = [q for q, _ in visible["items"]]
        invented = [q for q in staged_qs if q not in visible_qs]
        if invented:
            problems.append(f"{path}: staged schema has {len(invented)} question(s) "
                            f"not visible on the page, e.g. {invented[0][:50]!r}")

        had_faq = any(n.get("@type") == "FAQPage" for n in live_nodes)
        rows.append((path, len(staged_qs), len(visible_qs),
                     "+".join(sorted(staged_types)) or "-",
                     "update" if had_faq else "NEW"))

    print(f"{'PAGE':34} {'STAGED':>6} {'VISIBLE':>7}  {'KEPT ALONGSIDE':22} CHANGE")
    for path, sq, vq, types, change in rows:
        flag = " " if sq <= vq else "!"
        print(f"{flag}{path:33} {sq:6} {vq:7}  {types:22} {change}")

    print(f"\n{len(rows)} page(s) would gain or update FAQ schema.")
    print("STAGED vs VISIBLE differ where repeated headings were dropped "
          "(see the sync run's DUPE lines) - staged should never exceed visible.")

    cms_rows = check_cms(problems)
    if cms_rows:
        print(f"\n{'CMS ITEM':34} {'STORED':>6} {'LIVE':>7} {'VISIBLE':>8}")
        for path, sq, lq, vq in cms_rows:
            flag = " " if sq == lq and sq <= vq else "!"
            print(f"{flag}{path:33} {sq:6} {lq:7} {vq:8}")
        print(f"{len(cms_rows)} CMS item(s) serving FAQ schema. CMS items publish "
              "per item, so STORED and LIVE should always agree.")

    if problems:
        print("\nPROBLEMS:")
        for p_ in problems:
            print("  -", p_)
        sys.exit(1)
    print("\nNo problems found. Publishing would only add the FAQ schema above "
          "(plus any unpublished Designer work noted at the top).")


if __name__ == "__main__":
    main()
