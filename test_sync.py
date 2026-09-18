"""Tests for the rules that decide whether it is safe to write."""
import json
import sys
import unittest

import faqparse
import sync

KNOWN = {f"p{i}" for i in range(31)}          # 31 pages had schema last run


def page(items=(), legacy=False, lists=1, richtext_container=False):
    return {"items": list(items), "legacy": legacy, "lists": lists,
            "richtext_container": richtext_container}


QA = [("Is this a question?", "This is an answer long enough to pass the check.")]


class Assess(unittest.TestCase):
    def test_quiet_day_is_quiet(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        self.assertEqual(sync.assess(res, KNOWN), ([], [], [], [], None))

    def test_attributes_stripped_fails_on_first_page(self):
        """One page with legacy markup but no attributes is enough to stop everything."""
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=True)
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertEqual(broken, ["/p0"])
        self.assertIn("data-faq-*", fatal)

    def test_single_genuine_removal_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=False)
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertEqual((broken, lost, fatal), ([], ["/p0"], None))

    def test_three_removals_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(3):
            res[f"/p{i}"] = page([])
        self.assertIsNone(sync.assess(res, KNOWN)[4])

    def test_four_removals_aborts(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(4):
            res[f"/p{i}"] = page([])
        self.assertIn("lost their FAQ section", sync.assess(res, KNOWN)[4])

    def test_mass_loss_aborts(self):
        res = {f"/p{i}": page([]) for i in range(31)}
        self.assertIsNotNone(sync.assess(res, KNOWN)[4])

    def test_unknown_page_without_faq_is_not_a_loss(self):
        """A page that never had FAQs isn't a deletion just because it has none."""
        res = {"/brand-new": page([])}
        self.assertEqual(sync.assess(res, KNOWN), ([], [], [], [], None))

    def test_broken_beats_volume_check(self):
        """Stripped attributes are reported as breakage, not as mass deletion."""
        res = {f"/p{i}": page([], legacy=True) for i in range(31)}
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertEqual(len(broken), 31)
        self.assertIn("legacy accordion markup", fatal)


class Unmigrated(unittest.TestCase):
    """A page on an older component that never had attributes must not block
    the pages that work -- otherwise one un-migrated page freezes all schema."""

    def test_never_seen_legacy_page_is_reported_not_fatal(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/systems4pt-migration"] = page([], legacy=True)
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertIsNone(fatal)
        self.assertEqual(unmig, ["/systems4pt-migration"])
        self.assertEqual(broken, [])

    def test_previously_working_page_losing_attrs_is_still_fatal(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=True)          # /p0 is in KNOWN
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertEqual(broken, ["/p0"])
        self.assertIsNotNone(fatal)

    def test_many_unmigrated_pages_never_block(self):
        res = {f"/legacy{i}": page([], legacy=True) for i in range(20)}
        res.update({f"/p{i}": page(QA) for i in range(31)})
        self.assertIsNone(sync.assess(res, KNOWN)[4])


class Merge(unittest.TestCase):
    def setUp(self):
        self.existing = {"@context": "https://schema.org", "@type": "SoftwareApplication",
                         "name": "Prompt Kiosk", "featureList": ["a", "b"]}
        self.faq = {"@type": "FAQPage", "@id": "u#faq", "url": "u",
                    "mainEntity": [{"@type": "Question", "name": "Q",
                                    "acceptedAnswer": {"@type": "Answer", "text": "A" * 30}}]}

    def test_existing_preserved(self):
        m = sync.merge(dict(self.existing), self.faq)
        kept = [n for n in m["@graph"] if n["@type"] != "FAQPage"][0]
        self.assertEqual(kept, {k: v for k, v in self.existing.items() if k != "@context"})

    def test_idempotent(self):
        a = sync.merge(dict(self.existing), self.faq)
        b = sync.merge(json.loads(json.dumps(a)), self.faq)
        self.assertEqual(a, b)
        self.assertEqual(len(b["@graph"]), 2)

    def test_no_existing_gives_bare_node(self):
        m = sync.merge(None, self.faq)
        self.assertEqual(m["@type"], "FAQPage")
        self.assertNotIn("@graph", m)

    def test_removal_restores_original(self):
        merged = sync.merge(dict(self.existing), self.faq)
        back = sync.merge(merged, None)
        self.assertEqual(back, self.existing)

    def test_removal_of_only_node_clears_field(self):
        self.assertIsNone(sync.merge(sync.merge(None, self.faq), None))

    def test_handles_string_field(self):
        m = sync.merge(json.dumps(self.existing), self.faq)
        self.assertEqual(len(m["@graph"]), 2)

    def test_handles_script_wrapped_string(self):
        wrapped = ('<script type="application/ld+json">'
                   + json.dumps(self.existing) + "</script>")
        m = sync.merge(wrapped, self.faq)
        self.assertEqual(len(m["@graph"]), 2)

    def test_unparseable_existing_is_kept_not_destroyed(self):
        """Webflow CMS-binding tokens aren't valid JSON; never silently drop them."""
        weird = '{"@type":"Article","headline":"{{wf {&quot;path&quot;:&quot;t&quot;\\} }}"}'
        m = sync.merge(weird, self.faq)
        nodes = m["@graph"]
        self.assertEqual(len(nodes), 2)
        self.assertTrue(any("__opaque__" in n for n in nodes))


class Validate(unittest.TestCase):
    def ok(self):
        return {"@context": "https://schema.org", "@type": "FAQPage",
                "mainEntity": [{"@type": "Question", "name": "Q1",
                                "acceptedAnswer": {"@type": "Answer", "text": "A" * 30}}]}

    def test_valid(self):
        self.assertEqual(sync.validate_local("/x", self.ok()), [])

    def test_empty_question(self):
        d = self.ok(); d["mainEntity"][0]["name"] = ""
        self.assertIn("empty question", sync.validate_local("/x", d))

    def test_short_answer(self):
        d = self.ok(); d["mainEntity"][0]["acceptedAnswer"]["text"] = "no"
        self.assertTrue(any("too short" in e for e in sync.validate_local("/x", d)))

    def test_duplicates(self):
        d = self.ok(); d["mainEntity"].append(dict(d["mainEntity"][0]))
        self.assertTrue(any("duplicate" in e for e in sync.validate_local("/x", d)))

    def test_no_faq_node(self):
        self.assertEqual(sync.validate_local("/x", {"@type": "WebPage"}), ["no FAQPage node"])


class Parse(unittest.TestCase):
    DOC = '''<div data-faq-list="">
      <li data-faq-item=""><span data-faq-question="">Does it work?</span>
        <div data-faq-answer=""><p>Yes it does, <a href="/products/emr">see EMR</a>.</p>
        <ul><li>one</li><li>two</li></ul></div></li>
      <li data-faq-item=""><span data-faq-question="">And this &amp; that?</span>
        <div data-faq-answer=""><p>Line one.</p><p>Line two.</p></div></li>
    </div>'''

    def test_counts(self):
        r = faqparse.extract(self.DOC)
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(r["lists"], 1)

    def test_relative_links_absolutised(self):
        q, a = faqparse.extract(self.DOC)["items"][0]
        self.assertIn('<a href="https://www.prompthealth.com/products/emr">', a)

    def test_bullets_and_entities(self):
        items = faqparse.extract(self.DOC)["items"]
        self.assertIn("• one", items[0][1])
        self.assertEqual(items[1][0], "And this & that?")

    def test_paragraphs_become_blank_lines(self):
        self.assertIn("Line one.\n\nLine two.", faqparse.extract(self.DOC)["items"][1][1])

    def test_no_tags_survive_except_anchors(self):
        import re
        for _, a in faqparse.extract(self.DOC)["items"]:
            self.assertEqual(re.findall(r"<(?!/?a[ >])[a-zA-Z]", a), [])

    def test_legacy_detection(self):
        legacy = '<div class="section_faq"><li class="accordion-css__item">x</li></div>'
        self.assertTrue(faqparse.extract(legacy)["legacy"])
        self.assertEqual(faqparse.extract(legacy)["items"], [])

    def test_zero_width_stripped(self):
        doc = ('<li data-faq-item=""><span data-faq-question="">Q?</span>'
               '<div data-faq-answer=""><p>Answer.</p><p>‍</p></div></li>')
        self.assertEqual(faqparse.extract(doc)["items"][0][1], "Answer.")

    def test_nested_same_tag_does_not_truncate(self):
        doc = ('<li data-faq-item=""><span data-faq-question="">Q?</span>'
               '<div data-faq-answer=""><div><div>deep</div></div> tail</div></li>')
        self.assertIn("tail", faqparse.extract(doc)["items"][0][1])




class Shape(unittest.TestCase):
    """Validation samples one document per shape, so shapes must be distinguishing."""

    def test_bare_vs_graph(self):
        faq = {"@type": "FAQPage", "mainEntity": []}
        self.assertEqual(sync.shape_of(sync.merge(None, faq)), "bare:FAQPage")
        merged = sync.merge({"@type": "SoftwareApplication", "name": "x"}, faq)
        self.assertEqual(sync.shape_of(merged), "graph:FAQPage+SoftwareApplication")

    def test_shape_ignores_content(self):
        """Two pages with different questions must share a shape (that's the point)."""
        a = sync.merge(None, {"@type": "FAQPage",
                              "mainEntity": [{"@type": "Question", "name": "a"}]})
        b = sync.merge(None, {"@type": "FAQPage",
                              "mainEntity": [{"@type": "Question", "name": "b"},
                                             {"@type": "Question", "name": "c"}]})
        self.assertEqual(sync.shape_of(a), sync.shape_of(b))

    def test_different_companions_differ(self):
        faq = {"@type": "FAQPage", "mainEntity": []}
        s1 = sync.shape_of(sync.merge({"@type": "AboutPage"}, faq))
        s2 = sync.shape_of(sync.merge({"@type": "CollectionPage"}, faq))
        self.assertNotEqual(s1, s2)


class ExistingJsonLd(unittest.TestCase):
    """Existing schema is read from the served HTML, so this guard is load-bearing:
    a page whose JSON-LD we cannot parse must never be overwritten."""

    def wrap(self, payload):
        return ('<html><head><script type="application/ld+json">' + payload
                + "</script></head><body></body></html>")

    def test_no_schema(self):
        self.assertEqual(faqparse.existing_jsonld("<html></html>"), ([], True))

    def test_single_node(self):
        nodes, ok = faqparse.existing_jsonld(self.wrap('{"@type":"WebPage","name":"x"}'))
        self.assertTrue(ok)
        self.assertEqual(nodes, [{"@type": "WebPage", "name": "x"}])

    def test_graph_is_flattened(self):
        nodes, ok = faqparse.existing_jsonld(
            self.wrap('{"@graph":[{"@type":"A"},{"@type":"B"}]}'))
        self.assertTrue(ok)
        self.assertEqual([n["@type"] for n in nodes], ["A", "B"])

    def test_multiple_blocks_combine(self):
        doc = self.wrap('{"@type":"A"}') + self.wrap('{"@type":"B"}')
        nodes, ok = faqparse.existing_jsonld(doc)
        self.assertTrue(ok)
        self.assertEqual(len(nodes), 2)

    def test_unparseable_reports_not_ok(self):
        """Webflow CMS binding tokens are not valid JSON - must refuse, not ignore."""
        nodes, ok = faqparse.existing_jsonld(
            self.wrap('{"headline":"{{wf {&quot;path&quot;:&quot;t&quot;\\} }}"}'))
        self.assertFalse(ok)
        self.assertEqual(nodes, [])

    def test_unparseable_beats_valid_block(self):
        doc = self.wrap('{"@type":"A"}') + self.wrap("{not json}")
        self.assertFalse(faqparse.existing_jsonld(doc)[1])


class ApiLimits(unittest.TestCase):
    """The schema-markup endpoint documents 60KB / depth 32 / 5000 nodes.
    Catch violations locally rather than as an opaque API rejection."""

    def base(self, questions):
        return {"@context": "https://schema.org", "@type": "FAQPage",
                "mainEntity": [{"@type": "Question", "name": f"Q{i}",
                                "acceptedAnswer": {"@type": "Answer", "text": "A" * 30}}
                               for i in range(questions)]}

    def test_real_pages_are_well_inside_limits(self):
        import pathlib
        for f in pathlib.Path("schemas").glob("*.json"):
            doc = json.load(open(f))
            self.assertEqual(sync.validate_local(f.stem, doc), [], f.stem)

    def test_oversize_rejected(self):
        doc = self.base(2)
        doc["mainEntity"][0]["acceptedAnswer"]["text"] = "x" * (61 * 1024)
        self.assertTrue(any("byte API limit" in e
                            for e in sync.validate_local("/x", doc)))

    def test_node_counter_is_correct(self):
        """A Question contributes 2 nodes (itself + its Answer), plus 1 for the root.

        In practice the 60KB limit always binds before 5000 nodes -- 2500
        questions would be needed, and those cannot fit in 60KB -- so the node
        check is belt-and-braces. Test the counter, not an unreachable trip.
        """
        self.assertEqual(sync._shape_stats(self.base(1))[1], 3)
        self.assertEqual(sync._shape_stats(self.base(10))[1], 21)
        self.assertEqual(sync._shape_stats(self.base(2000))[1], 4001)

    def test_size_limit_binds_before_node_limit(self):
        errs = sync.validate_local("/x", self.base(2000))
        self.assertTrue(any("byte API limit" in e for e in errs), errs)

    def test_too_deep_rejected(self):
        doc = self.base(1)
        deep = cur = {}
        for _ in range(40):
            cur["x"] = {}
            cur = cur["x"]
        doc["mainEntity"][0]["deep"] = deep
        self.assertTrue(any("nesting depth" in e
                            for e in sync.validate_local("/x", doc)))


class NoIndex(unittest.TestCase):
    """noindex pages are deliberately out of search; schema cannot help them."""

    def test_detects_content_first(self):
        self.assertTrue(faqparse.extract(
            '<meta content="noindex" name="robots"/>')["noindex"])

    def test_detects_name_first(self):
        self.assertTrue(faqparse.extract(
            '<meta name="robots" content="noindex, nofollow">')["noindex"])

    def test_plain_page_is_not_noindex(self):
        self.assertFalse(faqparse.extract("<html><body>hi</body></html>")["noindex"])

    def test_index_directive_is_not_noindex(self):
        self.assertFalse(faqparse.extract(
            '<meta name="robots" content="index, follow">')["noindex"])


class Dedupe(unittest.TestCase):
    """A question repeated on one page must not fail the run for every page."""

    def test_repeated_heading_drops_every_copy(self):
        """'Ask <Competitor>' repeats as a label; keeping one ships nonsense."""
        from collections import Counter
        items = [("Ask WebPT", "How is your AI trained?"),
                 ("Ask WebPT", "How is support structured?"),
                 ("Real question?", "A genuine answer, long enough to pass.")]
        counts = Counter(q for q, _ in items)
        repeated = {q for q, c in counts.items() if c > 1}
        kept = [(q, a) for q, a in items if q not in repeated]
        self.assertEqual([q for q, _ in kept], ["Real question?"])
        node = sync.faq_node("https://x/y", kept)
        self.assertEqual(len(node["mainEntity"]), 1)
        self.assertEqual(sync.validate_local("/y", node), [])

    def test_repeated_question_kept_once(self):
        doc = ('<li data-faq-item=""><span data-faq-question="">Same?</span>'
               '<div data-faq-answer=""><p>First answer here, long enough.</p></div></li>'
               '<li data-faq-item=""><span data-faq-question="">Same?</span>'
               '<div data-faq-answer=""><p>Second answer here, long enough.</p></div></li>')
        items = faqparse.extract(doc)["items"]
        self.assertEqual(len(items), 2)          # parser reports what is on the page

        seen, deduped = set(), []
        for q, a in items:
            if q not in seen:
                seen.add(q)
                deduped.append((q, a))
        node = sync.faq_node("https://x/y", deduped)
        self.assertEqual(len(node["mainEntity"]), 1)
        self.assertEqual(sync.validate_local("/y", node), [])
        self.assertIn("First answer",
                      node["mainEntity"][0]["acceptedAnswer"]["text"])


class NestedFaq(unittest.TestCase):
    """/practice-type/universities embeds an FAQPage inside WebPage.mainEntity.
    merge() only replaces top-level nodes, so the page would declare two."""

    def test_detects_nested_faqpage(self):
        nodes = [{"@type": "WebPage", "name": "x",
                  "mainEntity": {"@type": "FAQPage", "mainEntity": []}}]
        self.assertEqual(sync.nested_faq_nodes(nodes), ["WebPage.mainEntity"])

    def test_detects_deeply_nested(self):
        nodes = [{"@type": "WebPage",
                  "about": {"thing": [{"@type": "FAQPage"}]}}]
        self.assertTrue(sync.nested_faq_nodes(nodes))

    def test_top_level_faqpage_is_not_nested(self):
        nodes = [{"@type": "FAQPage", "mainEntity": []},
                 {"@type": "SoftwareApplication", "name": "y"}]
        self.assertEqual(sync.nested_faq_nodes(nodes), [])

    def test_clean_existing_schema_passes(self):
        nodes = [{"@type": "SoftwareApplication", "name": "y",
                  "offers": {"@type": "Offer"}}]
        self.assertEqual(sync.nested_faq_nodes(nodes), [])


class RichText(unittest.TestCase):
    """/faq keeps its whole FAQ in one rich-text field for Finsweet's TOC, so
    per-item attributes cannot be added. [data-faq-richtext-list] marks it."""

    DOC = ('<div data-faq-richtext-list="" fs-toc-element="contents" class="w-richtext">'
           '<h2>Pricing</h2>'
           '<h3>How much is it?</h3><p>It depends on your plan and size.</p>'
           '<p>Second paragraph of the same answer.</p>'
           '<h3>Are there extra fees?</h3><p>No hidden fees for reminders.</p>'
           '<h2>Support</h2>'
           '<h3>Is support included?</h3><p>Yes, US-based and included.</p>'
           '</div>')

    def test_extracts_h3_questions_by_default(self):
        items = faqparse.extract_richtext(self.DOC)["items"]
        self.assertEqual([q for q, _ in items],
                         ["How much is it?", "Are there extra fees?",
                          "Is support included?"])

    def test_answer_stops_at_next_heading(self):
        items = faqparse.extract_richtext(self.DOC)["items"]
        self.assertIn("Second paragraph", items[0][1])
        self.assertNotIn("extra fees", items[0][1])

    def test_h2_does_not_leak_into_answer(self):
        """A category heading ends the answer; it is not part of it."""
        items = faqparse.extract_richtext(self.DOC)["items"]
        self.assertNotIn("Support", items[1][1])

    def test_heading_override(self):
        doc = ('<div data-faq-richtext-list="" data-faq-richtext-heading="h2">'
               '<h2>Q one?</h2><p>Answer one, long enough.</p>'
               '<h2>Q two?</h2><p>Answer two, long enough.</p></div>')
        self.assertEqual([q for q, _ in faqparse.extract_richtext(doc)["items"]],
                         ["Q one?", "Q two?"])

    def test_no_attribute_yields_nothing(self):
        """The blog/glossary false positive this attribute exists to prevent:
        a Finsweet TOC container with question-shaped <h3>s but no FAQ attribute."""
        blog = ('<div fs-toc-element="contents" class="w-richtext">'
                '<h3>Why does PT burnout happen?</h3><p>Several reasons.</p>'
                '<h3>How do you fix it?</h3><p>Several ways.</p></div>')
        self.assertFalse(faqparse.has_richtext_faq(blog))
        self.assertEqual(faqparse.extract_richtext(blog)["items"], [])

    def test_detects_container(self):
        self.assertTrue(faqparse.has_richtext_faq(self.DOC))

    def test_headings_without_answers_are_skipped(self):
        doc = ('<div data-faq-richtext-list=""><h3>Empty?</h3><h3>Real?</h3>'
               '<p>Yes, this one has an answer.</p></div>')
        self.assertEqual([q for q, _ in faqparse.extract_richtext(doc)["items"]],
                         ["Real?"])


class EmptyRichTextContainer(unittest.TestCase):
    """A mistagged container must not freeze schema for every other page."""

    def test_reported_not_fatal(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/mistagged"] = page([], richtext_container=True)
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertIsNone(fatal)
        self.assertEqual(empty_rt, ["/mistagged"])
        self.assertEqual(broken, [])

    def test_not_counted_as_removal(self):
        """Even for a page we synced before -- the container is still declared."""
        res = {"/p0": page([], richtext_container=True)}
        broken, lost, unmig, empty_rt, fatal = sync.assess(res, KNOWN)
        self.assertEqual(lost, [])
        self.assertEqual(empty_rt, ["/p0"])


class BothModes(unittest.TestCase):
    """A page may carry the accordion component and a rich-text container."""

    DOC = ('<li data-faq-item=""><span data-faq-question="">Accordion q?</span>'
           '<div data-faq-answer=""><p>Accordion answer, long enough.</p></div></li>'
           '<div data-faq-richtext-list="">'
           '<h3>Richtext q?</h3><p>Richtext answer, long enough.</p></div>')

    def test_contributes_both(self):
        r = faqparse.extract_all(self.DOC)
        self.assertEqual([q for q, _ in r["items"]], ["Accordion q?", "Richtext q?"])
        self.assertTrue(r["richtext"])

    def test_accordion_only_page_not_flagged_richtext(self):
        doc = ('<li data-faq-item=""><span data-faq-question="">Only q?</span>'
               '<div data-faq-answer=""><p>Only answer, long enough.</p></div></li>')
        r = faqparse.extract_all(doc)
        self.assertFalse(r["richtext"])
        self.assertFalse(r["richtext_container"])
        self.assertEqual(len(r["items"]), 1)

    def test_container_with_no_questions_is_flagged_but_empty(self):
        doc = '<div data-faq-richtext-list=""><p>Just prose, no headings.</p></div>'
        r = faqparse.extract_all(doc)
        self.assertTrue(r["richtext_container"])
        self.assertFalse(r["richtext"])
        self.assertEqual(r["items"], [])


class VerifyUsesSameExtractor(unittest.TestCase):
    """verify.py once used the accordion-only extractor, so every question on a
    rich-text page like /faq was reported as 'not visible on the page'."""

    def test_verify_calls_extract_all(self):
        src = open("verify.py").read()
        self.assertIn("faqparse.extract_all(html", src)
        self.assertNotIn("faqparse.extract(html", src)

    def test_extract_all_sees_richtext_questions(self):
        doc = ('<div data-faq-richtext-list="">'
               '<h3>Is it visible?</h3><p>Yes, and long enough to count.</p></div>')
        visible = [q for q, _ in faqparse.extract_all(doc)["items"]]
        staged = [q["name"] for q in
                  sync.faq_node("https://x/y", faqparse.extract_all(doc)["items"])["mainEntity"]]
        self.assertEqual(visible, staged)
        self.assertEqual([q for q in staged if q not in visible], [])


class RichTextContainers(unittest.TestCase):
    """A page can hold more than one rich-text container. Matching only the
    first silently dropped every real question when the blog template gained a
    hidden schema-output element."""

    def test_all_containers_contribute(self):
        doc = ('<div data-faq-richtext-list=""><h3>First?</h3><p>Answer one, long enough.</p></div>'
               '<div data-faq-richtext-list=""><h3>Second?</h3><p>Answer two, long enough.</p></div>')
        r = faqparse.extract_richtext(doc)
        self.assertEqual([q for q, _ in r["items"]], ["First?", "Second?"])
        self.assertEqual(r["lists"], 2)

    def test_schema_holder_is_never_a_source(self):
        """Even if it also carries the FAQ attribute."""
        doc = ('<div data-richtext-schema="" data-faq-richtext-list="">'
               '<h3>Should not appear?</h3><p>Schema holder body, long enough.</p></div>'
               '<div data-faq-richtext-list=""><h3>Real question?</h3>'
               '<p>Real answer, long enough.</p></div>')
        self.assertEqual([q for q, _ in faqparse.extract_richtext(doc)["items"]],
                         ["Real question?"])

    def test_container_with_only_a_script_yields_nothing(self):
        doc = ('<div data-faq-richtext-list=""><div data-rt-embed-type="true">'
               '<script type="application/ld+json">{"a":1}</script></div></div>')
        self.assertEqual(faqparse.extract_richtext(doc)["items"], [])

    def test_embed_after_a_heading_does_not_leak_into_the_answer(self):
        doc = ('<div data-faq-richtext-list=""><h3>Q?</h3><p>Real answer text here.</p>'
               '<div data-rt-embed-type="true"><script type="application/ld+json">'
               '{"@context":"https://schema.org","@type":"FAQPage"}</script></div></div>')
        answer = faqparse.extract_richtext(doc)["items"][0][1]
        self.assertNotIn("@context", answer)
        self.assertNotIn("mainEntity", answer)
        self.assertEqual(answer, "Real answer text here.")

    def test_w_embed_also_stripped(self):
        doc = ('<div data-faq-richtext-list=""><h3>Q?</h3><p>Kept text.</p>'
               '<div class="w-embed"><script>var x = 1;</script></div></div>')
        self.assertEqual(faqparse.extract_richtext(doc)["items"][0][1], "Kept text.")


class CmsEmbed(unittest.TestCase):
    """CMS items get schema through a rich-text embed, because page-settings
    bindings are HTML-escaped and would destroy the JSON."""

    def test_wrapper_matches_webflow_stored_shape(self):
        doc = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": []}
        value = sync.EMBED_OPEN + json.dumps(doc, ensure_ascii=False) + sync.EMBED_CLOSE
        self.assertTrue(value.startswith("<div data-rt-embed-type='true'>"))
        self.assertIn('<script type="application/ld+json">', value)
        self.assertTrue(value.endswith("</script></div>"))

    def test_quotes_are_json_escaped_not_html_escaped(self):
        """The failure mode that killed the page-settings route."""
        doc = {"@context": "https://schema.org", "@type": "FAQPage",
               "mainEntity": [{"@type": "Question", "name": 'Has "quotes" & an ampersand?',
                               "acceptedAnswer": {"@type": "Answer", "text": "A" * 30}}]}
        value = sync.EMBED_OPEN + json.dumps(doc, ensure_ascii=False) + sync.EMBED_CLOSE
        self.assertNotIn("&quot;", value)
        self.assertNotIn("&amp;", value)
        inner = value[len(sync.EMBED_OPEN):-len(sync.EMBED_CLOSE)]
        self.assertEqual(json.loads(inner)["mainEntity"][0]["name"],
                         'Has "quotes" & an ampersand?')

    def test_collections_are_configured_not_hardcoded(self):
        for cid, cfg in sync.CMS_COLLECTIONS.items():
            self.assertTrue({"name", "field", "path"} <= set(cfg))
            self.assertTrue(cfg["path"].startswith("/"))


class StagingGuard(unittest.TestCase):
    def test_apply_against_non_production_is_refused(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "sync.py", "--host",
             "https://prompt-health.webflow.io", "--apply"],
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSING", r.stdout + r.stderr)


class CmsPassIsReachable(unittest.TestCase):
    """sync_cms() was defined, unit-testable, and never called for an entire
    build cycle -- 79 tests passed while the feature was dead code. These are
    blunt source checks, but they catch exactly that: code that exists, works,
    and is never reached."""

    def test_main_calls_sync_cms(self):
        src = open("sync.py").read()
        main_src = src[src.index("def main():"):]
        self.assertIn("sync_cms(", main_src,
                      "sync_cms is never invoked from main()")

    def test_cms_changes_keep_the_run_alive(self):
        """A run with no static changes but pending CMS work must not
        short-circuit on the 'nothing to do' branch."""
        src = open("sync.py").read()
        self.assertIn("not changed and not cms_changed", src)


class ListItemsFailsLoudly(unittest.TestCase):
    """An empty list is indistinguishable from 'no items', so an auth or scope
    error must raise rather than silently reporting nothing to do."""

    class _Stub(sync.Webflow):
        def __init__(self, code, body):
            self._code, self._body = code, body

        def _call(self, method, path, body=None, base=None):
            return self._code, self._body

    def test_non_200_raises(self):
        wf = self._Stub(403, {"message": "forbidden"})
        with self.assertRaises(RuntimeError) as cm:
            wf.list_items("abc")
        self.assertIn("403", str(cm.exception))
        self.assertIn("CMS scope", str(cm.exception))

    def test_200_with_no_items_is_fine(self):
        wf = self._Stub(200, {"items": [], "pagination": {"total": 0}})
        self.assertEqual(wf.list_items("abc"), [])

    def test_pages_non_200_raises(self):
        wf = self._Stub(500, {"message": "boom"})
        with self.assertRaises(RuntimeError):
            wf.pages()


if __name__ == "__main__":
    unittest.main(verbosity=2)
