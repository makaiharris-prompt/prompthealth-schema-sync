"""Tests for the rules that decide whether it is safe to write."""
import json
import sys
import unittest

import faqparse
import sync

KNOWN = {f"p{i}" for i in range(31)}          # 31 pages had schema last run


def page(items=(), legacy=False, lists=1):
    return {"items": list(items), "legacy": legacy, "lists": lists}


QA = [("Is this a question?", "This is an answer long enough to pass the check.")]


class Assess(unittest.TestCase):
    def test_quiet_day_is_quiet(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        self.assertEqual(sync.assess(res, KNOWN), ([], [], [], None))

    def test_attributes_stripped_fails_on_first_page(self):
        """One page with legacy markup but no attributes is enough to stop everything."""
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=True)
        broken, lost, unmig, fatal = sync.assess(res, KNOWN)
        self.assertEqual(broken, ["/p0"])
        self.assertIn("data-faq-*", fatal)

    def test_single_genuine_removal_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=False)
        broken, lost, unmig, fatal = sync.assess(res, KNOWN)
        self.assertEqual((broken, lost, fatal), ([], ["/p0"], None))

    def test_three_removals_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(3):
            res[f"/p{i}"] = page([])
        self.assertIsNone(sync.assess(res, KNOWN)[3])

    def test_four_removals_aborts(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(4):
            res[f"/p{i}"] = page([])
        self.assertIn("lost their FAQ section", sync.assess(res, KNOWN)[3])

    def test_mass_loss_aborts(self):
        res = {f"/p{i}": page([]) for i in range(31)}
        self.assertIsNotNone(sync.assess(res, KNOWN)[3])

    def test_unknown_page_without_faq_is_not_a_loss(self):
        """A page that never had FAQs isn't a deletion just because it has none."""
        res = {"/brand-new": page([])}
        self.assertEqual(sync.assess(res, KNOWN), ([], [], [], None))

    def test_broken_beats_volume_check(self):
        """Stripped attributes are reported as breakage, not as mass deletion."""
        res = {f"/p{i}": page([], legacy=True) for i in range(31)}
        broken, lost, unmig, fatal = sync.assess(res, KNOWN)
        self.assertEqual(len(broken), 31)
        self.assertIn("legacy accordion markup", fatal)


class Unmigrated(unittest.TestCase):
    """A page on an older component that never had attributes must not block
    the pages that work -- otherwise one un-migrated page freezes all schema."""

    def test_never_seen_legacy_page_is_reported_not_fatal(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/systems4pt-migration"] = page([], legacy=True)
        broken, lost, unmig, fatal = sync.assess(res, KNOWN)
        self.assertIsNone(fatal)
        self.assertEqual(unmig, ["/systems4pt-migration"])
        self.assertEqual(broken, [])

    def test_previously_working_page_losing_attrs_is_still_fatal(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=True)          # /p0 is in KNOWN
        broken, lost, unmig, fatal = sync.assess(res, KNOWN)
        self.assertEqual(broken, ["/p0"])
        self.assertIsNotNone(fatal)

    def test_many_unmigrated_pages_never_block(self):
        res = {f"/legacy{i}": page([], legacy=True) for i in range(20)}
        res.update({f"/p{i}": page(QA) for i in range(31)})
        self.assertIsNone(sync.assess(res, KNOWN)[3])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
