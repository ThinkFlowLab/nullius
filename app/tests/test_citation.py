"""A benchmark's BibTeX read into what its page shows: the catalogue's own
citations, every one, and the parts each is drawn with."""
from __future__ import annotations

import unittest

from nullius.catalogue import catalog, citation


class Citation(unittest.TestCase):
    def test_every_catalogue_citation_reads_into_its_papers(self):
        for b in catalog.load().benchmarks:
            with self.subTest(b.key):
                papers = citation.parse(b.citation)
                self.assertTrue(papers)
                for p in papers:
                    self.assertTrue(p["title"] and p["authors"] and p["year"] and p["venue"] and p["url"], p["key"])
                    self.assertTrue(p["bibtex"].startswith("@") and p["bibtex"].endswith("}"))
                    self.assertIn(p["bibtex"], b.citation.replace("\r\n", "\n"))           # the entry as written is what is copied

    def test_the_parts_a_seal_needs(self):
        tb = citation.parse(catalog.load().benchmark("terminal-bench").citation)[0]
        self.assertEqual((tb["venue_short"], tb["year"], tb["kind"]), ("ICLR", "2026", "conference"))
        self.assertTrue(tb["authors_short"].endswith("and 81 others"))
        tau = citation.parse(catalog.load().benchmark("tau3-bench").citation)
        self.assertEqual(len(tau), 3)                                                          # its own papers, not the README's task fixes
        self.assertEqual(tau[0]["title"][:7], "τ²-Benc")                                      # BibTeX's $\tau^2$, as text
        self.assertEqual((tau[0]["kind"], tau[0]["venue_short"], tau[0]["url"]), ("preprint", "arXiv", "https://arxiv.org/abs/2506.07982"))
        w = citation.parse("@inproceedings{k, title={{A}: B {\\textemdash} C}, author={Doe, Jane}, booktitle={ICLR 2026 Workshop on X}, year={2026}}")[0]
        self.assertEqual(w["kind"], "workshop"); self.assertIn("—", w["title"])               # {\textemdash}

    def test_an_entry_in_last_first_form_and_an_empty_text(self):
        p = citation.parse('@article{k, title={A {\\"o} b}, author={Doe, Jane and Roe, Rick}, year={2020}, journal={J}}')[0]
        self.assertEqual((p["title"], p["authors"], p["authors_short"]), ("A ö b", ["Jane Doe", "Rick Roe"], "Jane Doe and Rick Roe"))
        self.assertEqual(citation.parse(""), [])

    def test_a_link_is_http_or_none(self):
        bad = citation.parse('@misc{k, title={T}, author={A B}, year={2020}, url={javascript:alert(1)}}')[0]
        self.assertEqual(bad["url"], "")
        self.assertEqual(citation.parse('@misc{k, title={T}, url={javascript:x}, eprint={2401.01234}}')[0]["url"], "https://arxiv.org/abs/2401.01234")
        self.assertEqual(citation.parse('@misc{k, title={T}, eprint={x" onclick="y}}')[0]["url"], "")


if __name__ == "__main__":
    unittest.main()
