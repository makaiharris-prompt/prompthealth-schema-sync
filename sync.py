#!/usr/bin/env python3
"""Keep FAQPage JSON-LD on prompthealth.com in sync with the FAQs actually published.

Reads the published HTML at www.prompthealth.com, builds one FAQPage per page from
the [data-faq-*] contract, merges it into that page's existing Webflow JSON-LD via
@graph (never clobbering hand-written schema), and writes it back through the
Webflow Data API.

Safe by construction:
  * a normal day changes nothing and exits 0
  * it refuses to write empty schema when the scrape looks broken
  * it only publishes when the site had no unpublished work beforehand

Python 3 stdlib only.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import faqparse

SITE_ID = "663e8ac23e061e4b80b016d0"
BASE = "https://www.prompthealth.com"
API = "https://api.webflow.com/v2"
BETA = "https://api.webflow.com/beta"   # schema-markup lives here, not under v2
CUSTOM_DOMAINS = ["www.prompthealth.com", "prompthealth.com",
                  "www.promptemr.com", "promptemr.com"]
SNAP = Path(__file__).parent / "schemas"
UA = "prompthealth-schema-sync/1.0"

# Refuse-to-act thresholds (see README).
MAX_LOST_PAGES = 3
MAX_LOST_FRACTION = 0.20
MIN_ANSWER_CHARS = 20

# Documented limits on the schema-markup endpoint.
MAX_SCHEMA_BYTES = 60 * 1024
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_NODES = 5000


# ---------------------------------------------------------------- http

def _get(url, headers=None, timeout=45, retries=1):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:                                   # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2)
    raise last


class Webflow:
    """Minimal Data API v2 client. Endpoint shapes are probeable (see --probe)."""

    def __init__(self, token):
        self.token = token
        self.h = {"Authorization": f"Bearer {token}", "accept": "application/json"}

    def _call(self, method, path, body=None, base=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{base or API}/{path.lstrip('/')}", data=data, method=method,
            headers={**self.h, "User-Agent": UA, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:                                     # noqa: BLE001
                return e.code, {"raw": raw.decode("utf-8", "replace")[:500]}

    def site(self):
        return self._call("GET", f"sites/{SITE_ID}")[1]

    def pages(self):
        out, offset = [], 0
        while True:
            _, d = self._call("GET", f"sites/{SITE_ID}/pages?limit=100&offset={offset}")
            batch = d.get("pages", [])
            out += batch
            total = d.get("pagination", {}).get("total", len(out))
            offset += len(batch)
            if not batch or offset >= total:
                return out

    def get_schema(self, page_id):
        """Read a page's JSON-LD back from the API (used to verify a write landed)."""
        return self._call("GET", f"pages/{page_id}/schema-markup", base=BETA)

    def write_schema(self, updates):
        """updates: {page_id: doc-or-None}.

        PUT /beta/pages/{id}/schema-markup -- a beta route, which is why nothing
        under /v2 works. jsonLdSchema takes an object, a raw JSON string, or null
        to clear. There is no bulk route, so this is one call per changed page.
        """
        results = {}
        for pid, doc in updates.items():
            code, d = self._call("PUT", f"pages/{pid}/schema-markup",
                                 {"jsonLdSchema": doc}, base=BETA)
            results[pid] = (code, None if code in (200, 201, 202) else d)
        return results

    def publish(self):
        return self._call("POST", f"sites/{SITE_ID}/publish",
                          {"customDomains": CUSTOM_DOMAINS, "publishToWebflowSubdomain": True})


# ---------------------------------------------------------------- build

def faq_node(url, items):
    return {
        "@type": "FAQPage",
        "@id": f"{url}#faq",
        "url": url,
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in items
        ],
    }


def to_nodes(existing):
    """Normalise whatever is in the field into a flat list of nodes."""
    if not existing:
        return []
    if isinstance(existing, str):
        s = re.sub(r"</?script[^>]*>", "", existing).strip()
        try:
            existing = json.loads(s)
        except Exception:                                         # noqa: BLE001
            return [{"__opaque__": existing}]
    if isinstance(existing, list):
        return existing
    if isinstance(existing, dict):
        if "@graph" in existing:
            return list(existing["@graph"])
        return [existing]
    return []


def merge(existing, node):
    """Replace the FAQPage member, preserve every other node untouched.

    node=None removes FAQ schema (used when a page's FAQ section is deleted).
    """
    kept = [n for n in to_nodes(existing)
            if not (isinstance(n, dict) and n.get("@type") == "FAQPage")]
    for n in kept:
        n.pop("@context", None)
    nodes = kept + ([node] if node else [])
    if not nodes:
        return None
    if len(nodes) == 1:
        return {"@context": "https://schema.org", **nodes[0]}
    return {"@context": "https://schema.org", "@graph": nodes}


# ---------------------------------------------------------------- validate

def _shape_stats(o, d=1):
    """(max depth, node count) for the API's documented limits."""
    if isinstance(o, dict):
        subs = [_shape_stats(v, d + 1) for v in o.values()]
        return (max([x[0] for x in subs] or [d]), 1 + sum(x[1] for x in subs))
    if isinstance(o, list):
        subs = [_shape_stats(v, d + 1) for v in o]
        return (max([x[0] for x in subs] or [d]), sum(x[1] for x in subs))
    return (d, 0)


def validate_local(path, doc):
    errs = []
    raw = len(json.dumps(doc).encode())
    depth, nodes = _shape_stats(doc)
    if raw > MAX_SCHEMA_BYTES:
        errs.append(f"{raw} bytes exceeds the {MAX_SCHEMA_BYTES} byte API limit")
    if depth > MAX_SCHEMA_DEPTH:
        errs.append(f"nesting depth {depth} exceeds {MAX_SCHEMA_DEPTH}")
    if nodes > MAX_SCHEMA_NODES:
        errs.append(f"{nodes} nodes exceeds {MAX_SCHEMA_NODES}")
    faq = next((n for n in to_nodes(doc) if n.get("@type") == "FAQPage"), None)
    if not faq:
        return ["no FAQPage node"]
    qs = faq.get("mainEntity") or []
    if not qs:
        errs.append("mainEntity empty")
    seen = set()
    for q in qs:
        name = (q.get("name") or "").strip()
        text = (q.get("acceptedAnswer", {}).get("text") or "").strip()
        if not name:
            errs.append("empty question")
        if len(text) < MIN_ANSWER_CHARS:
            errs.append(f"answer too short for {name[:40]!r}")
        if name in seen:
            errs.append(f"duplicate question {name[:40]!r}")
        seen.add(name)
    return errs


_last_validate = [0.0]
VALIDATE_INTERVAL = 3.0      # the service 429s well before 31 rapid calls


def validate_remote(doc, retries=4):
    """Run through validator.schema.org. `html=` is the parameter that works."""
    page = ('<!doctype html><html><head><script type="application/ld+json">'
            + json.dumps(doc) + "</script></head><body></body></html>")
    body = urllib.parse.urlencode({"html": page}).encode()
    raw = None
    for attempt in range(retries + 1):
        gap = VALIDATE_INTERVAL - (time.time() - _last_validate[0])
        if gap > 0:
            time.sleep(gap)
        req = urllib.request.Request(
            "https://validator.schema.org/validate", data=body,
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
            _last_validate[0] = time.time()
            break
        except urllib.error.HTTPError as e:
            _last_validate[0] = time.time()
            if e.code in (429, 503) and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            raise
    d = json.loads(raw.split("\n", 1)[1] if raw.startswith(")]}'") else raw)
    # Errors nest at arbitrary depth (nodeProperties[].target.properties[].errors),
    # so collect recursively and dedupe.
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for e in o.get("errors") or []:
                if e.get("isSevere"):
                    found[(e.get("errorType"), tuple(e.get("args") or ()))] = e
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    errs = ["%s(%s)" % (k[0], ", ".join(k[1])) for k in found]
    return d.get("totalNumErrors", 0), d.get("totalNumWarnings", 0), errs


# ---------------------------------------------------------------- pipeline

def shape_of(doc):
    """Structural signature of a document: what a validator would actually test."""
    nodes = to_nodes(doc)
    types = "+".join(sorted(str(n.get("@type", "?")) for n in nodes))
    return ("graph:" if "@graph" in (doc or {}) else "bare:") + types


def slug_of(path):
    return (path.strip("/") or "home").replace("/", "-")


def discover(pages):
    """Static, writable, publicly-served pages only."""
    out = {}
    for p in pages:
        if p.get("draft") or p.get("archived"):
            continue
        slug = p.get("slug") or ""
        if slug.startswith("detail_"):          # Collection template: shares parent's path
            continue
        path = p.get("publishedPath") or ""
        if not path or path.startswith("/wip/"):
            continue
        out.setdefault(path, p)                 # first non-template wins
    return out


def fetch_page(path, host):
    try:
        code, body = _get(host + path, retries=1)
    except Exception as e:                                        # noqa: BLE001
        return path, None, f"fetch failed: {e}"
    if code != 200:
        return path, None, f"HTTP {code}"
    doc = body.decode("utf-8", "replace")
    res = faqparse.extract(doc, BASE)
    res["existing"], res["existing_ok"] = faqparse.existing_jsonld(doc)
    return path, res, None


def assess(results, known):
    """Classify pages that yielded no FAQ items. Returns (broken, lost, fatal_reason).

    `results` is {path: extract_result} for pages that fetched successfully.
    `known`   is the set of slugs we had schema for last run.

    broken     -> a page that HAD schema lost its attributes; never legitimate
    lost       -> FAQ section genuinely removed
    unmigrated -> FAQs on an older component, never had attributes; report only
    """
    broken, lost, unmigrated = [], [], []
    for path, r in sorted(results.items()):
        if r["items"]:
            continue
        if r["legacy"]:
            # Legacy markup and no attributes means one of two very different
            # things. If we produced schema for this page before, the component
            # lost its attributes -- stop everything. If we never have, it is
            # simply a page still on an older component: report it, but do not
            # let it block the pages that do work.
            (broken if slug_of(path) in known else unmigrated).append(path)
        elif slug_of(path) in known:
            lost.append(path)

    if broken:
        return broken, lost, unmigrated, (
            "FAQ attributes missing but legacy accordion markup still present on: "
            + ", ".join(broken)
            + ". The Webflow component likely lost its data-faq-* attributes.")

    if lost and (len(lost) > MAX_LOST_PAGES or
                 (known and len(lost) / len(known) > MAX_LOST_FRACTION)):
        return broken, lost, unmigrated, (
            f"{len(lost)} pages lost their FAQ section in one run "
            f"({', '.join(lost)}). Likely a publish or fetch anomaly.")

    return broken, lost, unmigrated, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to Webflow (default: dry run)")
    ap.add_argument("--publish", action="store_true", help="allow the gated publish after writing")
    ap.add_argument("--host", default=BASE, help="host to read FAQs from")
    ap.add_argument("--only", default="", help="comma-separated paths to restrict to")
    ap.add_argument("--probe", action="store_true", help="probe API endpoints and exit")
    ap.add_argument("--no-remote-validate", action="store_true",
                    help="skip validator.schema.org entirely")
    ap.add_argument("--validate-limit", type=int, default=6,
                    help="max distinct document shapes to send to schema.org per run")
    args = ap.parse_args()

    token = os.environ.get("WEBFLOW_API_TOKEN")
    wf = Webflow(token) if token else None
    summary, fail = [], False

    if args.probe:
        if not wf:
            sys.exit("WEBFLOW_API_TOKEN not set")

        code, site = wf._call("GET", f"sites/{SITE_ID}")
        print("GET  site      :", code)
        if code in (401, 403):
            sys.exit(f"\nAuth failed ({code}): {str(site)[:200]}\n"
                     "The token is missing, invalid, or lacks access to this site.\n"
                     "Create one at Webflow > Site settings > Apps & integrations > "
                     "API access, with scopes: sites:read, sites:write, "
                     "pages:read, pages:write.")
        if code != 200:
            sys.exit(f"Unexpected response {code}: {str(site)[:300]}")

        pgs = wf.pages()
        print("GET  pages     :", len(pgs), "pages")
        if not pgs:
            sys.exit("No pages returned - the token likely lacks the pages:read scope.")

        candidates = [p for p in pgs if p.get("publishedPath") == "/demo"
                      and not (p.get("slug") or "").startswith("detail_")]
        if not candidates:
            print("NOTE: /demo not found; probing the first discoverable page instead")
            candidates = [next(iter(discover(pgs).values()))]
        pid = candidates[0]["id"]
        print("             using page", pid, candidates[0].get("publishedPath"))

        code, d = wf._call("GET", f"pages/{pid}")
        print("GET  page      :", code, "| jsonLdSchema in response:",
              "jsonLdSchema" in d)

        code, d = wf._call("POST", f"sites/{SITE_ID}/pages/schema_markup/query",
                           {"pages": [{"id": pid}]})
        print("POST bulk read :", code, "|", str(d)[:200])

        print("\nRead path resolved:",
              "bulk" if code == 200 else "per-page GET fallback")
        return

    # 1 - discover -------------------------------------------------------
    if wf:
        code, site_probe = wf._call("GET", f"sites/{SITE_ID}")
        if code in (401, 403):
            sys.exit(f"FATAL: Webflow auth failed ({code}). Check WEBFLOW_API_TOKEN "
                     "and its scopes (sites:read, sites:write, pages:read, pages:write).")
        if code != 200:
            sys.exit(f"FATAL: Webflow returned {code} for the site: {str(site_probe)[:200]}")
        all_pages = wf.pages()
        if not all_pages:
            sys.exit("FATAL: Webflow returned zero pages. Refusing to continue - "
                     "an empty page list would look like every FAQ was deleted.")
        pages = discover(all_pages)
    else:
        print("! no WEBFLOW_API_TOKEN - discovery limited to known paths", file=sys.stderr)
        pages = {p.strip(): {"id": None} for p in
                 Path(__file__).with_name("faqurls.txt").read_text().split()
                 if p.strip()}
        pages = {k.replace(BASE, ""): v for k, v in pages.items()}

    paths = sorted(pages)
    if args.only:
        want = {p.strip() for p in args.only.split(",")}
        paths = [p for p in paths if p in want]

    # 2 - fetch + extract ------------------------------------------------
    results, fetch_errors = {}, {}
    with cf.ThreadPoolExecutor(6) as ex:
        for path, res, err in ex.map(lambda p: fetch_page(p, args.host), paths):
            if err:
                fetch_errors[path] = err
            elif res:
                results[path] = res

    known = {f.stem for f in SNAP.glob("*.json")}
    with_faq = {p: r for p, r in results.items() if r["items"]}

    # 3 - refuse-to-act --------------------------------------------------
    # A failed fetch is never a deletion: keep whatever schema that page has.
    for path, err in fetch_errors.items():
        summary.append(f"SKIP  {path}: {err} (existing schema left as-is)")

    broken, lost, unmigrated, fatal = assess(results, known)
    if fatal:
        print(f"FATAL: {fatal} Refusing to write.", file=sys.stderr)
        sys.exit(2)
    for path in unmigrated:
        summary.append(f"TODO  {path}: has FAQs on an older component with no "
                       "data-faq-* attributes - add them in Webflow to include "
                       "this page")

    # 4 - build ----------------------------------------------------------
    built = {}
    for path, r in sorted(with_faq.items()):
        built[path] = faq_node(BASE + path, r["items"])
        if r["lists"] == 0:
            summary.append(f"NOTE  {path}: no [data-faq-list] wrapper (items still found)")

    # 5 - merge ----------------------------------------------------------
    # Existing schema comes from the page's own served HTML, not the API: it is
    # the authoritative record of what Webflow renders today, it needs no
    # endpoint, and it can be checked by eye with curl. A page whose JSON-LD we
    # cannot parse is skipped entirely rather than overwritten.
    final, opaque = {}, []
    for path, node in built.items():
        r = results[path]
        if not r.get("existing_ok", True):
            opaque.append(path)
            continue
        final[path] = merge(
            {"@context": "https://schema.org", "@graph": r["existing"]}
            if r["existing"] else None, node)
    for path in lost:
        r = results.get(path, {})
        if not r.get("existing_ok", True):
            opaque.append(path)
            continue
        final[path] = merge(
            {"@context": "https://schema.org", "@graph": r["existing"]}
            if r.get("existing") else None, None)
    for path in opaque:
        summary.append(f"SKIP  {path}: page serves JSON-LD that will not parse; "
                       "refusing to overwrite schema it cannot read")

    # 6 - local validation (cheap, so everything gets it) ------------------
    for path, doc in sorted(final.items()):
        if doc is None:
            continue
        errs = validate_local(path, doc)
        if errs:
            print(f"FATAL: {path}: {'; '.join(errs)}", file=sys.stderr)
            fail = True
    if fail:
        sys.exit(2)

    # 7 - diff -------------------------------------------------------------
    SNAP.mkdir(exist_ok=True)
    changed = {}
    for path, doc in sorted(final.items()):
        f = SNAP / f"{slug_of(path)}.json"
        new = json.dumps(doc, indent=2, ensure_ascii=False) if doc else ""
        old = f.read_text() if f.exists() else ""
        if new.strip() != old.strip():
            changed[path] = (f, new, doc)

    # 8 - remote validation ------------------------------------------------
    # Only changed docs, and only one per distinct *shape*. Every page uses the
    # same generated template, so the documents differ by strings, not structure
    # -- validating all 31 tells you nothing that validating one of each shape
    # doesn't, and it trips the validator's rate limit.
    if changed and not args.no_remote_validate:
        by_shape = {}
        for path, (_, _, doc) in sorted(changed.items()):
            if doc is not None:
                by_shape.setdefault(shape_of(doc), path)
        picked = list(by_shape.items())[:args.validate_limit]
        skipped_shapes = len(by_shape) - len(picked)
        rate_limited = []
        for sig, path in picked:
            doc = changed[path][2]
            try:
                ne, nw, errs = validate_remote(doc)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    rate_limited.append(sig)
                    continue
                raise
            except Exception as e:                                # noqa: BLE001
                print(f"FATAL: {path}: schema.org validator unreachable ({e}). "
                      "Refusing to write unvalidated schema.", file=sys.stderr)
                fail = True
                continue
            if ne:
                print(f"FATAL: {path} [{sig}]: schema.org reported {ne} errors: "
                      f"{errs[:5]}", file=sys.stderr)
                fail = True
            else:
                summary.append(f"VALID {path} [{sig}]"
                               + (f" ({nw} warnings)" if nw else ""))
        if rate_limited:
            summary.append("WARN  schema.org rate-limited; unvalidated shapes: "
                           + ", ".join(rate_limited) + " (retried next run)")
        if skipped_shapes:
            summary.append(f"NOTE  {skipped_shapes} further shape(s) above "
                           f"--validate-limit={args.validate_limit}")
        if fail:
            sys.exit(2)

    n_q = sum(len(r["items"]) for r in with_faq.values())
    print(f"\n{len(with_faq)} FAQ pages, {n_q} Q&As, {len(changed)} changed\n")
    for path in sorted(final):
        mark = "CHANGED" if path in changed else "  ok   "
        mode = "-" if final[path] is None else ("graph" if "@graph" in final[path] else "bare")
        cnt = len(with_faq.get(path, {}).get("items", []))
        print(f"  {mark}  {path:34} q={cnt:<3} {mode}")
    for line in summary:
        print(" ", line)

    if not changed:
        print("\nNothing to do.")
        return

    if not args.apply:
        for _, (f, new, _) in changed.items():
            f.write_text(new) if new else (f.unlink() if f.exists() else None)
        print(f"\nDry run: wrote {len(changed)} snapshot(s) to schemas/. "
              "Re-run with --apply to write to Webflow.")
        return

    if not wf:
        sys.exit("--apply needs WEBFLOW_API_TOKEN")

    site_before = wf.site()
    clean = site_before.get("lastUpdated") == site_before.get("lastPublished")

    updates = {}
    for path, (_, new, doc) in changed.items():
        pid = pages.get(path, {}).get("id")
        if not pid:
            summary.append(f"WARN  {path}: no page id, skipped")
            continue
        updates[pid] = doc          # object or None; the API accepts both

    res = wf.write_schema(updates)
    bad = {p: v for p, v in res.items() if v[0] not in (200, 201, 202)}
    print(f"\nWrote {len(updates) - len(bad)}/{len(updates)} pages.")
    if bad:
        print("FAILED writes:", json.dumps(bad, indent=2)[:1500], file=sys.stderr)
        sys.exit(2)

    # Read back what the API actually stored. A write that reports 200 but stores
    # something else would otherwise go unnoticed until someone inspected the page.
    id_to_path = {pid: path for path, (_, _, _) in changed.items()
                  for pid in [pages.get(path, {}).get("id")] if pid}
    verify_fail = []
    for pid, sent in updates.items():
        path = id_to_path.get(pid, pid)
        code, back = wf.get_schema(pid)
        if code != 200:
            verify_fail.append(f"{path}: read-back returned {code}")
            continue
        stored = back.get("jsonLdSchema")
        if sent is None:
            if stored:
                verify_fail.append(f"{path}: expected cleared schema, found some")
            continue
        got = to_nodes(stored)
        want = to_nodes(sent)
        got_types = sorted(str(n.get("@type")) for n in got)
        want_types = sorted(str(n.get("@type")) for n in want)
        if got_types != want_types:
            verify_fail.append(f"{path}: stored {got_types}, sent {want_types}")
            continue
        gq = next((len(n.get("mainEntity", [])) for n in got
                   if n.get("@type") == "FAQPage"), 0)
        wq = next((len(n.get("mainEntity", [])) for n in want
                   if n.get("@type") == "FAQPage"), 0)
        if gq != wq:
            verify_fail.append(f"{path}: stored {gq} questions, sent {wq}")

    if verify_fail:
        print("\nFATAL: written schema did not read back as sent:", file=sys.stderr)
        for v in verify_fail:
            print("  ", v, file=sys.stderr)
        print("Not publishing. Investigate before re-running.", file=sys.stderr)
        sys.exit(2)
    print(f"Verified {len(updates)} page(s) read back as sent.")

    # Only record snapshots once the write is confirmed, so a failed run
    # retries the same pages next time instead of thinking it succeeded.
    for path, (f, new, _) in changed.items():
        f.write_text(new) if new else (f.unlink() if f.exists() else None)

    if not args.publish:
        print("Staged only (--publish not set). Schema goes live on your next publish.")
        return
    if not clean:
        print("\nREFUSING TO PUBLISH: the site had unpublished Designer work before this run "
              f"(lastUpdated={site_before.get('lastUpdated')} != "
              f"lastPublished={site_before.get('lastPublished')}).\n"
              "Schema is staged and will go live on your next publish.", file=sys.stderr)
        sys.exit(1)
    code, d = wf.publish()
    print("Published:", code)
    if code not in (200, 202):
        print(json.dumps(d)[:600], file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
