"""FAQ extraction from prompthealth.com pages.

Contract (data attributes on the Webflow FAQ component):
    [data-faq-item]      wraps one question/answer pair
    [data-faq-question]  the question text
    [data-faq-answer]    the answer rich text
    [data-faq-list]      the list wrapper (present on 26/31 pages; advisory only)

Anchored on [data-faq-item] so pages whose list wrapper predates the attribute
still work. All items on a page merge into that page's single FAQPage.
"""
import html
import re

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}

# Legacy markup, used only to tell "attributes were stripped" from "FAQ removed".
LEGACY_MARKERS = ('class="accordion-css__item"', "section_faq")

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(/?)>", re.S)


def _tag_start(doc, idx):
    """Walk back from a position inside a tag to the '<' that opens it."""
    i = doc.rfind("<", 0, idx)
    return i if i != -1 else None


def _element_span(doc, start):
    """Given the index of '<', return (inner_start, inner_end, end) for that element."""
    m = _TAG.match(doc, start)
    if not m:
        return None
    name = m.group(2).lower()
    if m.group(4) == "/" or name in VOID:
        return (m.end(), m.end(), m.end())
    depth = 1
    pos = m.end()
    inner_start = pos
    while depth:
        n = _TAG.search(doc, pos)
        if not n:
            return None
        nname = n.group(2).lower()
        if nname == name:
            if n.group(1) == "/":
                depth -= 1
                if depth == 0:
                    return (inner_start, n.start(), n.end())
            elif n.group(4) != "/" and nname not in VOID:
                depth += 1
        pos = n.end()
    return None


def _find_attr_elements(doc, attr):
    """Yield (inner_html, full_end) for every element carrying `attr`."""
    out = []
    for m in re.finditer(re.escape(attr) + r"[=\s>]", doc):
        start = _tag_start(doc, m.start())
        if start is None:
            continue
        # Make sure the attribute really belongs to this tag, not to text before it.
        t = _TAG.match(doc, start)
        if not t or attr not in t.group(3):
            continue
        span = _element_span(doc, start)
        if span:
            out.append((doc[span[0]:span[1]], span[2]))
    return out


def _abs_url(href, base):
    href = href.strip()
    if href.startswith(("http://", "https://", "mailto:", "tel:")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("#"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


def clean_text(fragment):
    """Strip every tag, unescape entities, collapse whitespace."""
    txt = re.sub(r"<[^>]+>", " ", fragment)
    txt = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", html.unescape(txt))
    return re.sub(r"\s+", " ", txt).strip()


_EMBED = re.compile(
    r'<div[^>]*(?:data-rt-embed-type|class="[^"]*w-embed)[^>]*>.*?</div>', re.S | re.I)


def clean_answer(fragment, base):
    """Answer text: keep only <a href>, everything else becomes plain text/newlines."""
    # Drop embeds outright. A schema embed sitting after a question heading
    # would otherwise be swept into that question's answer as raw JSON.
    fragment = _EMBED.sub(" ", fragment)
    out, pos = [], 0
    for m in _TAG.finditer(fragment):
        out.append(("text", fragment[pos:m.start()]))
        pos = m.end()
        closing, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if name == "a":
            if closing:
                out.append(("raw", "</a>"))
            else:
                h = re.search(r'href\s*=\s*"([^"]*)"', attrs) or \
                    re.search(r"href\s*=\s*'([^']*)'", attrs)
                out.append(("raw", '<a href="%s">' % html.escape(_abs_url(h.group(1), base), quote=True)
                            if h else ""))
        elif name == "br":
            out.append(("text", "\n"))
        elif name == "li" and not closing:
            out.append(("text", "\n• "))
        elif name in ("p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6") and closing:
            out.append(("text", "\n\n"))
    out.append(("text", fragment[pos:]))

    buf = []
    for kind, val in out:
        if kind == "raw":
            buf.append(val)
        else:
            # Collapse horizontal whitespace but preserve the newlines we inserted.
            buf.append(re.sub(r"[ \t\r\f\v]+", " ", html.unescape(val)))
    s = "".join(buf)
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)   # Webflow empty rich-text paragraphs
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


_NOINDEX = re.compile(
    r'<meta[^>]*name\s*=\s*["\']robots["\'][^>]*content\s*=\s*["\'][^"\']*noindex'
    r'|<meta[^>]*content\s*=\s*["\'][^"\']*noindex[^"\']*["\'][^>]*name\s*=\s*["\']robots',
    re.I)


def extract(doc, base="https://www.prompthealth.com"):
    """Return {'items': [(question, answer)], 'lists': int, 'legacy': bool,
    'noindex': bool}."""
    items = []
    for inner, _ in _find_attr_elements(doc, "data-faq-item"):
        qs = _find_attr_elements(inner, "data-faq-question")
        as_ = _find_attr_elements(inner, "data-faq-answer")
        q = clean_text(qs[0][0]) if qs else ""
        a = clean_answer(as_[0][0], base) if as_ else ""
        items.append((q, a))
    return {
        "items": items,
        "lists": len(_find_attr_elements(doc, "data-faq-list")),
        "legacy": any(mark in doc for mark in LEGACY_MARKERS),
        "noindex": bool(_NOINDEX.search(doc)),
    }


_LDJSON = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)


def existing_jsonld(doc):
    """Read the JSON-LD already served on the page.

    Returns (nodes, ok). ok=False means a block was present but unparseable --
    the caller must NOT write, or it would clobber schema it cannot see.

    The served HTML is the authoritative record of what Webflow currently
    renders, so this needs no API endpoint and can be verified by eye.
    """
    blocks = _LDJSON.findall(doc)
    if not blocks:
        return [], True
    nodes = []
    for b in blocks:
        try:
            parsed = __import__("json").loads(b.strip())
        except Exception:                                         # noqa: BLE001
            return [], False
        if isinstance(parsed, dict) and "@graph" in parsed:
            nodes += list(parsed["@graph"])
        elif isinstance(parsed, list):
            nodes += parsed
        elif isinstance(parsed, dict):
            nodes.append(parsed)
    return nodes, True


RICHTEXT_ATTR = "data-faq-richtext-list"
SCHEMA_ATTR = "data-richtext-schema"      # element that OUTPUTS schema, never a source of FAQs
RICHTEXT_HEADING_ATTR = "data-faq-richtext-heading"
DEFAULT_Q_TAG = "h3"


def has_richtext_faq(doc):
    """True when the page declares a rich-text FAQ container."""
    return RICHTEXT_ATTR in doc


def _richtext_containers(doc, container=None):
    """Yield (inner_html, open_tag) for every rich-text FAQ container.

    Every container, not just the first: a page can hold more than one, and
    matching only the first once silently dropped every real question when the
    blog template gained a hidden schema-output element.
    """
    container = container or RICHTEXT_ATTR
    for m in re.finditer(re.escape(container) + r"[=\s>]", doc):
        start = doc.rfind("<", 0, m.start())
        if start == -1:
            continue
        t = _TAG.match(doc, start)
        if not t or container not in t.group(3):
            continue
        # An element that OUTPUTS schema is never a source of FAQs, even if it
        # also carries the FAQ attribute.
        if SCHEMA_ATTR in t.group(3):
            continue
        span = _element_span(doc, start)
        if span:
            yield doc[span[0]:span[1]], t.group(3)


def _questions_from(inner, open_tag, base, q_tag):
    """Pull (question, answer) pairs out of one rich-text container."""
    if q_tag is None:
        m = re.search(RICHTEXT_HEADING_ATTR + r'\s*=\s*["\']\s*(h[1-6])\s*["\']',
                      open_tag, re.I)
        q_tag = m.group(1).lower() if m else DEFAULT_Q_TAG

    rank = int(q_tag[1])
    # A heading of the same or higher rank ends the current answer.
    stop = re.compile(r"<h([1-%d])\b" % rank, re.I)
    heads = list(re.finditer(r"<(%s)\b[^>]*>(.*?)</\1>" % q_tag, inner, re.S | re.I))

    items = []
    for i, m in enumerate(heads):
        q = clean_text(m.group(2))
        tail = inner[m.end():heads[i + 1].start()] if i + 1 < len(heads) else inner[m.end():]
        nxt = stop.search(tail)
        a = clean_answer(tail[:nxt.start()] if nxt else tail, base)
        if q and a:
            items.append((q, a))
    return items


# A paragraph led by bold text. House style uses this for emphasis ("What
# you'll notice"), so it is only read as a question when the container has no
# headings at all -- otherwise every emphasised lead-in becomes an FAQ.
_BOLD_LEAD = re.compile(r"<p[^>]*>\s*<strong>(.{3,140}?)</strong>", re.S | re.I)


def suspected_bold_questions(doc, base="https://www.prompthealth.com"):
    """FAQ containers that use bold where a heading belongs.

    Returns [] unless a container yields no questions AND contains bold-led
    paragraphs -- the signature of an FAQ written with bold instead of <h3>.
    Silence here would mean the block is simply never marked up and nobody
    finds out, so it is reported rather than guessed at.
    """
    found = []
    for inner, open_tag in _richtext_containers(doc):
        if _questions_from(inner, open_tag, base, None):
            continue
        leads = [clean_text(m.group(1)) for m in _BOLD_LEAD.finditer(inner)]
        leads = [t for t in leads if t]
        if leads:
            found.append((len(leads), leads[0]))
    return found
    """Pull (question, answer) pairs out of one rich-text container."""
    if q_tag is None:
        m = re.search(RICHTEXT_HEADING_ATTR + r'\s*=\s*["\']\s*(h[1-6])\s*["\']',
                      open_tag, re.I)
        q_tag = m.group(1).lower() if m else DEFAULT_Q_TAG

    rank = int(q_tag[1])
    stop = re.compile(r"<h([1-%d])\b" % rank, re.I)
    heads = list(re.finditer(r"<(%s)\b[^>]*>(.*?)</\1>" % q_tag, inner, re.S | re.I))

    items = []
    for i, m in enumerate(heads):
        q = clean_text(m.group(2))
        tail = inner[m.end():heads[i + 1].start()] if i + 1 < len(heads) else inner[m.end():]
        nxt = stop.search(tail)
        a = clean_answer(tail[:nxt.start()] if nxt else tail, base)
        if q and a:
            items.append((q, a))
    return items


def extract_richtext(doc, base="https://www.prompthealth.com",
                     container=RICHTEXT_ATTR, q_tag=None):
    """Extract Q&As from a single rich-text field, keyed on its headings.

    Some pages hold their whole FAQ in one Webflow rich-text field so that
    Finsweet's table of contents can index it. Per-item attributes cannot be
    added there, but the structure is already explicit: each `q_tag` heading is
    a question, and everything up to the next heading of the same or higher
    rank is its answer.

    Anchors on [data-faq-richtext-list], a dedicated attribute. Targeting
    Finsweet's fs-toc-element instead would be a workaround: every page on the
    site carries it, so blog and glossary subheadings would be published as
    FAQs. A purpose-built attribute means these pages can be auto-discovered.

    The question heading level defaults to <h3> and can be overridden per
    container with data-faq-richtext-heading="h2".
    """
    items, n = [], 0
    for inner, open_tag in _richtext_containers(doc, container):
        n += 1
        items += _questions_from(inner, open_tag, base, q_tag)

    return {"items": items, "lists": n, "legacy": False,
            "noindex": bool(_NOINDEX.search(doc))}


def extract_all(doc, base="https://www.prompthealth.com"):
    """Everything a page offers, from either markup style.

    A page may use the attributed accordion component, a rich-text container,
    or both. Take all of it; sync's repeated-heading dedupe covers any overlap.
    """
    res = extract(doc, base)
    res["richtext_container"] = has_richtext_faq(doc)
    res["bold_questions"] = suspected_bold_questions(doc, base)
    res["richtext"] = False
    if res["richtext_container"]:
        items = extract_richtext(doc, base)["items"]
        if items:
            res["items"] = res["items"] + items
            res["richtext"] = True
    return res
