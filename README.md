# prompthealth-schema-sync

Keeps `FAQPage` JSON-LD on **prompthealth.com** in sync with the FAQs actually published,
without anyone maintaining schema by hand.

Reads the published HTML, builds one `FAQPage` per page, merges it into that page's existing
Webflow JSON-LD, writes it back through the Webflow Data API, and publishes — but only when
that's safe.

## Why this exists

Google retired FAQ rich results in May 2026, so the audience for this markup is AI answer
engines and Bing. Most AI crawlers **don't execute JavaScript**, so the schema has to be in
the HTML Webflow serves — not injected client-side. Webflow's native per-page JSON-LD field
renders server-side, so that's where this writes.

## The contract it depends on

Data attributes on the Webflow FAQ component:

| Attribute | Meaning |
|---|---|
| `[data-faq-item]` | wraps one question/answer pair — **this is what the script anchors on** |
| `[data-faq-question]` | the question text |
| `[data-faq-answer]` | the answer rich text |
| `[data-faq-list]` | the list wrapper — advisory only, reported if missing |

**If you change the FAQ component, keep these attributes.** The script detects their removal
and hard-fails rather than silently wiping schema off every page.

All items on a page merge into that page's single `FAQPage`. That's the canonical shape and
the one that keeps each answer anchored to its page URL for retrieval.

## Usage

```bash
python3 sync.py                      # dry run: generate, validate, write snapshots only
python3 sync.py --apply              # write to Webflow, stage only
python3 sync.py --apply --publish    # write, then publish if the gate allows
python3 sync.py --only /demo         # restrict to specific pages
python3 sync.py --probe              # check which Webflow API endpoints respond
```

Needs `WEBFLOW_API_TOKEN` (scopes: `sites:read`, `sites:write`, `pages:read`, `pages:write`).
Without it, discovery falls back to the paths in `faqurls.txt` and no writes are possible.

`schemas/*.json` is the committed snapshot of every page's generated schema — the audit log.
Diff it to see exactly what changed and when.

## Safety

A normal day changes nothing and exits 0. These rules only engage on the two cases that matter:
the scrape breaking, and schema being deleted.

| Observation | Read as | Action |
|---|---|---|
| No `[data-faq-item]` but legacy accordion markup still present | Attributes stripped from the component | **Hard fail on the first page.** Never legitimate |
| `[data-faq-item]` present, question or answer empty | Partial attribute application | Fail that page, write nothing for it |
| Fetch failed (non-200, timeout, TLS) | Transient | Skip the page, keep its existing schema. **Never** read as a deletion |
| No FAQ attributes *and* no legacy markup, page fetched 200 | FAQ genuinely removed | Drop the `FAQPage` node, keep other nodes |

Plus a volume check on that last row: if more than **3 pages**, or **>20%** of known FAQ pages,
lose their FAQ section in one run, the run aborts untouched. Five FAQ sections vanishing the
same day is far more likely a publish anomaly than five deliberate deletions.

### The publish gate

Writing schema only changes Webflow's *staged* state. Before making any change the script
compares the site's `lastUpdated` to `lastPublished`:

- **equal** — the site was clean, so the only pending change is ours → publish.
- **different** — someone has Designer work in flight → **stage the schema, publish nothing,
  fail the run.** The schema goes live on the team's next publish.

This is deliberate: a blind full-site publish would push whatever is staged in the Designer
to production. An SEO automation should never be able to ship someone's half-finished page.

### Existing schema is never clobbered

Several pages carry hand-written JSON-LD (`SoftwareApplication`, `AboutPage`, `WebPage`,
`CollectionPage`). The writer reads the current field, replaces **only** the `FAQPage` member,
and copies every other node through untouched, emitting `@graph` when more than one node
results. Merging is idempotent, and removing the FAQ restores the original document exactly.

## Validation

Every generated document is checked locally (parses, non-empty `mainEntity`, no empty or
duplicate questions, no too-short answers), then **changed** documents go through
`validator.schema.org`. Any error fails the run before a single write.

Only changed documents are sent remotely — unchanged ones were validated when first written,
and re-checking all of them every run just burns the validator's rate limit.

## Scope

Auto-discovered every run from the Webflow page list, so a new FAQ page is covered with no
code change. Excluded: drafts, archived pages, `/wip/*`, and Collection templates
(`slug` starting `detail_`) — templates share their parent's `publishedPath`, which is how
`/customers` resolves to two different pages.

As of the first run: **31 pages, 187 Q&As.**
