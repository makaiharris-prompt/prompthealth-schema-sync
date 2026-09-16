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


def clean_answer(fragment, base):
    """Answer text: keep only <a href>, everything else becomes plain text/newlines."""
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


def extract(doc, base="https://www.prompthealth.com"):
    """Return {'items': [(question, answer)], 'lists': int, 'legacy': bool}."""
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
    }
