import json
import os
import unittest

import bench
import gen
import samefirm
from samefirm import Linker, domain_parts, fold, jaro_winkler, link_records, name_tokens

HERE = os.path.dirname(os.path.abspath(__file__))


def rec(source, name, domain="", city="", country=""):
    return {"source": source, "name": name, "domain": domain, "city": city, "country": country}


def same(records, i, j, **kw):
    clusters, _, _ = link_records(records, **kw)
    return any(i in c and j in c for c in clusters)


class Normalising(unittest.TestCase):
    def test_fold_accents_and_digraphs(self):
        self.assertEqual(fold("Müller"), fold("Mueller"))
        self.assertEqual(fold("Müller"), fold("Muller"))
        self.assertEqual(fold("Straße"), "strasse")
        self.assertEqual(fold("Société"), "societe")

    def test_legal_suffixes_dropped(self):
        self.assertEqual(name_tokens("Atlas Robotics, Inc."), ["atlas", "robotics"])
        self.assertEqual(name_tokens("Atlas Robotics S.A.S."), ["atlas", "robotics"])
        self.assertEqual(name_tokens("The Atlas Robotics GmbH & Co. KG"), ["atlas", "robotics"])

    def test_abbreviations(self):
        self.assertEqual(name_tokens("Bergen Intl Tech"), name_tokens("Bergen International Technologies"))

    def test_domain_parts(self):
        self.assertEqual(domain_parts("https://www.Atlas.de/about?x=1"), ("atlas.de", "atlas"))
        self.assertEqual(domain_parts("atlas.co.uk"), ("atlas.co.uk", "atlas"))
        self.assertEqual(domain_parts("http://jobs.atlas.com:8080/"), ("jobs.atlas.com", "atlas"))
        self.assertEqual(domain_parts(""), ("", ""))

    def test_jaro_winkler(self):
        self.assertEqual(jaro_winkler("atlas", "atlas"), 1.0)
        self.assertAlmostEqual(jaro_winkler("martha", "marhta"), 0.9611, places=3)
        self.assertAlmostEqual(jaro_winkler("dixon", "dicksonx"), 0.8133, places=3)
        self.assertEqual(jaro_winkler("abc", "xyz"), 0.0)
        self.assertEqual(jaro_winkler("", "abc"), 0.0)


class Pairs(unittest.TestCase):
    def test_same_domain_different_spelling(self):
        r = [rec("a", "Müller Robotik GmbH", "mueller-robotik.de"), rec("b", "MUELLER ROBOTIK", "https://www.mueller-robotik.de/jobs")]
        self.assertTrue(same(r, 0, 1))

    def test_alias_joined_by_domain(self):
        r = [rec("a", "IBM", "ibm.com", "", "US"), rec("b", "International Business Machines Corp", "http://www.ibm.com", "", "US")]
        self.assertTrue(same(r, 0, 1))

    def test_typo_without_domain(self):
        r = [rec("a", "Halvorsen Biotech Ltd", "", "Leeds", "GB"), rec("b", "Halvorsn Biotech", "", "Leeds", "GB")]
        self.assertTrue(same(r, 0, 1))

    def test_same_head_different_sector_stays_apart(self):
        r = [rec("a", "Atlas Robotics Inc", "", "Austin", "US"), rec("b", "Atlas Logistics Inc", "", "Austin", "US")]
        self.assertFalse(same(r, 0, 1))

    def test_same_name_different_country_stays_apart(self):
        r = [rec("a", "Atlas Robotics Inc", "", "Austin", "US"), rec("b", "Atlas Robotics GmbH", "", "Berlin", "DE")]
        self.assertFalse(same(r, 0, 1))

    def test_different_domains_stay_apart(self):
        r = [rec("a", "Atlas Robotics", "atlasrobotics.com", "", "US"), rec("b", "Atlas Robotics", "atlas-robotics-group.com", "", "US")]
        self.assertFalse(same(r, 0, 1))

    def test_same_label_other_tld(self):
        r = [rec("a", "Atlas Robotics", "atlasrobotics.de", "", "DE"), rec("b", "ARX", "atlasrobotics.com", "", "DE")]
        self.assertTrue(same(r, 0, 1))

    def test_name_equals_domain_label(self):
        r = [rec("a", "ARX", "atlasrobotics.com", "", "US"), rec("b", "Atlas Robotics Inc", "", "", "US")]
        self.assertTrue(same(r, 0, 1))

    def test_exonym_city(self):
        r = [rec("a", "Kestrel Energy", "", "München", "DE"), rec("b", "Kestrel Energy AG", "", "Munich", "DE")]
        self.assertTrue(same(r, 0, 1))


class SharedHostsAndClusters(unittest.TestCase):
    def test_shared_host_is_not_identity(self):
        r = [
            rec("a", "Corvid Foods", "https://linkhub.example/corvid"),
            rec("b", "Halden Pharma", "https://linkhub.example/halden"),
            rec("c", "Dunmore Media", "https://linkhub.example/dunmore"),
            rec("a", "Bergen Capital", "https://linkhub.example/bergen"),
        ]
        linker = Linker(r)
        self.assertIn("linkhub.example", linker.shared_hosts)
        clusters, _ = linker.link()
        self.assertEqual(len(clusters), 4)

    def test_one_per_source(self):
        r = [rec("a", "Atlas Robotics Inc", "", "Austin", "US"), rec("a", "Atlas Robotics", "", "Austin", "US")]
        self.assertFalse(same(r, 0, 1))
        self.assertTrue(same(r, 0, 1, one_per_source=False))

    def test_no_chaining_across_domains(self):
        # the middle record has no domain and looks like both; the two ends must not be glued together
        r = [
            rec("a", "Atlas Robotics", "atlasrobotics.com", "Austin", "US"),
            rec("b", "Atlas Robotics", "", "Austin", "US"),
            rec("c", "Atlas Robotics", "atlas-robotics-group.com", "Austin", "US"),
        ]
        clusters, _, _ = link_records(r)
        self.assertFalse(any(0 in c and 2 in c for c in clusters))
        self.assertEqual(len(clusters), 2)

    def test_empty_and_single(self):
        self.assertEqual(link_records([])[0], [])
        self.assertEqual(link_records([rec("a", "Solo")])[0], [[0]])

    def test_sample_file(self):
        with open(os.path.join(HERE, "sample_records.json"), encoding="utf-8") as fh:
            records = json.load(fh)
        clusters, _, _ = link_records(records)
        multi = sorted(c for c in clusters if len(c) > 1)
        self.assertEqual(multi, [[0, 1, 2], [4, 5], [6, 7], [8, 9]])

    def test_deterministic(self):
        records, _, _ = gen.generate(11, 150)
        self.assertEqual(link_records(records)[0], link_records(records)[0])


class EndToEnd(unittest.TestCase):
    def test_beats_baseline_on_unseen_seed(self):
        records, truth, _ = gen.generate(42, 400)
        gold, _ = bench.true_pairs(truth)
        clusters, _, stats = link_records(records)
        p, r, f = bench.prf(bench.cluster_pairs(clusters), gold)
        norm = bench.by_key(records, lambda x: " ".join(name_tokens(x["name"])))
        bp, br, bf = bench.prf(bench.cluster_pairs(norm), gold)
        self.assertGreater(p, 0.97)
        self.assertGreater(r, 0.9)
        self.assertGreater(f, bf)
        n = len(records)
        self.assertLess(stats["candidate_pairs"], 0.05 * n * (n - 1) / 2)

    def test_generator_is_seeded(self):
        self.assertEqual(gen.generate(3, 50), gen.generate(3, 50))


if __name__ == "__main__":
    unittest.main()
