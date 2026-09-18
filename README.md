# prompthealth-schema-sync

Keeps `FAQPage` JSON-LD on **prompthealth.com** in sync with the FAQs actually published,
without anyone maintaining schema by hand.

Reads the published HTML, builds one `FAQPage` per page, merges it into that page's existing
Webflow JSON-LD, and writes it back through the Webflow Data API. It never publishes on its own —
it stages the change and raises a GitHub issue for a person to publish.

**Running it day to day? See [RUNBOOK.md](RUNBOOK.md)** — how to trigger it from the GitHub UI,
what the output means, and what to do when something looks wrong. No terminal needed.

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

### Pages whose FAQ is one rich-text field

`/faq` holds its whole FAQ in a single Webflow rich-text field so Finsweet's table of contents
can index it, so per-item attributes cannot be added. Mark the wrapper instead:

| Attribute | Meaning |
|---|---|
| `[data-faq-richtext-list]` | this rich-text block holds the FAQ |
| `[data-faq-richtext-heading]` | optional, e.g. `"h2"` — which heading level is a question (default `h3`) |

Each question heading becomes a `Question`, and everything up to the next heading of the **same
or higher rank** becomes its answer — so `<h2>` category headings (Getting Started, Pricing, …)
end an answer rather than leaking into it.

Pages carrying the attribute are **discovered automatically**; there is no list to maintain.
That is exactly why it exists: keying off Finsweet's own `fs-toc-element` would have been a
workaround, since all 217 pages carry that container and 33 of them have question-shaped `<h3>`s
(blog posts, every glossary entry) that would have been published as FAQs.

A page may use the accordion component, a rich-text container, or both; all items merge into
that page's single `FAQPage`. A container that declares itself but yields no questions is
skipped and reported, not treated as breakage.

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

## Before publishing

```bash
WEBFLOW_API_TOKEN=... python3 verify.py
```

Read-only, and it runs automatically in CI after every write. For every FAQ page it compares the schema **staged** in Webflow against the schema
and FAQs currently **served** on the live page, and fails if:

- staged schema would drop a node the live page already has (something clobbered),
- staged schema contains a question not visible on the page (something invented),
- the live page serves JSON-LD that cannot be parsed.

It also reports whether the Designer has unpublished changes, since a full-site publish ships
those too -- not only the FAQ schema.

To revert a page's staged schema, edit it in Webflow. There is deliberately no tool for this:
one existed briefly, restored "whatever the live page serves", and wiped an unpublished Designer
edit because live had already stopped matching the pre-change state.

## Publishing is a human decision

Scheduled runs **stage only**. Single Page Publishing is unavailable on this site
(`pageId` returns `400 Invalid parameter`), so the only route is a full-site publish, which
would carry unrelated staged Designer work with it. Rather than make that call automatically,
the job opens a GitHub issue saying schema is waiting, and closes it once published.

Publish from Webflow, or run the workflow manually with `publish=true` to use the gate.
If Single Page Publishing is enabled later, the tool already prefers it and this becomes
fully automatic.

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
`CollectionPage`). The writer replaces **only** the `FAQPage` member and copies every other
node through untouched, emitting `@graph` when more than one node results. Merging is
idempotent, and removing the FAQ restores the original document exactly.

**Existing schema is read from the page's own served HTML, not from the API.** That is the
authoritative record of what Webflow renders today, it needs no endpoint, and you can check
it yourself with `curl`. This matters: the Data API has no route that returns the field
(`GET /v2/pages/{id}` omits it, and there is no bulk schema route), so an API-based read
silently returned "no existing schema" for every page -- which would have overwritten all of
the hand-written markup on the first run.

If a page serves JSON-LD that will not parse, that page is **skipped entirely** rather than
overwritten. Schema we cannot read is schema we must not replace.

## Validation

Every generated document is checked locally (parses, non-empty `mainEntity`, no empty or
duplicate questions, no too-short answers), then **changed** documents go through
`validator.schema.org`. Any error fails the run before a single write.

Only changed documents are sent remotely — unchanged ones were validated when first written,
and re-checking all of them every run just burns the validator's rate limit.

## CMS collections

A Collection template is one page serving many items, so page-level schema would stamp identical
FAQs on every post. Each item stores its own schema instead, in a multi-line **PlainText** field
that an **HTML Embed** element on the template outputs:

```
CMS_COLLECTIONS = {
    "<collection id>": {"name": "Blog posts",
                        "field": "faq-schema-embed",
                        "path": "/blog"},
}
```

Adding a collection is that entry plus three things in Webflow: the field, an HTML Embed on the
template containing `<script type="application/ld+json">` with the field bound inside it, and the
FAQ attributes on the template so the questions can be parsed.

**Why an HTML Embed and not the page-settings JSON-LD field.** Webflow HTML-escapes bindings in
the page-settings field — `'` becomes `&#39;`, `"` becomes `&quot;` — which turns valid JSON into
entities. An HTML Embed renders its binding raw, so the JSON survives. This is why the field holds
the bare document and the `<script>` tag lives in the Designer.

The value is capped at `MAX_CMS_TEXT_BYTES` (10,000). A PlainText field can truncate, and a
truncated document is malformed JSON on a live page — worse than no schema — so an over-long
document is reported and skipped rather than written.

CMS items publish **per item**, so this path ships without a full-site publish. That also means a
mistake goes live immediately, which is why `verify.py` checks that every stored schema actually
appears in the served HTML: a field the template does not output looks like success from the API.

## Scope

Auto-discovered every run from the Webflow page list, so a new FAQ page is covered with no
code change. Excluded: drafts, archived pages, `/wip/*`, and Collection templates
(`slug` starting `detail_`) — templates share their parent's `publishedPath`, which is how
`/customers` resolves to two different pages.

As of the first run: **31 pages, 187 Q&As.**
