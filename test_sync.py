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
        self.assertEqual(sync.assess(res, KNOWN), ([], [], None))

    def test_attributes_stripped_fails_on_first_page(self):
        """One page with legacy markup but no attributes is enough to stop everything."""
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=True)
        broken, lost, fatal = sync.assess(res, KNOWN)
        self.assertEqual(broken, ["/p0"])
        self.assertIn("data-faq-*", fatal)

    def test_single_genuine_removal_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        res["/p0"] = page([], legacy=False)
        broken, lost, fatal = sync.assess(res, KNOWN)
        self.assertEqual((broken, lost, fatal), ([], ["/p0"], None))

    def test_three_removals_allowed(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(3):
            res[f"/p{i}"] = page([])
        self.assertIsNone(sync.assess(res, KNOWN)[2])

    def test_four_removals_aborts(self):
        res = {f"/p{i}": page(QA) for i in range(31)}
        for i in range(4):
            res[f"/p{i}"] = page([])
        self.assertIn("lost their FAQ section", sync.assess(res, KNOWN)[2])

    def test_mass_loss_aborts(self):
        res = {f"/p{i}": page([]) for i in range(31)}
        self.assertIsNotNone(sync.assess(res, KNOWN)[2])

    def test_unknown_page_without_faq_is_not_a_loss(self):
        """A page that never had FAQs isn't a deletion just because it has none."""
        res = {"/brand-new": page([])}
        self.assertEqual(sync.assess(res, KNOWN), ([], [], None))

    def test_broken_beats_volume_check(self):
        """Stripped attributes are reported as breakage, not as mass deletion."""
        res = {f"/p{i}": page([], legacy=True) for i in range(31)}
        broken, lost, fatal = sync.assess(res, KNOWN)
        self.assertEqual(len(broken), 31)
        self.assertIn("legacy accordion markup", fatal)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
