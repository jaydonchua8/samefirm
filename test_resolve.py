import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import bench
import crustdata_api as api
import demo
import gen
import resolve
import sim_identify
from sim_identify import SimTransport, company

HERE = os.path.dirname(os.path.abspath(__file__))

# The name-lookup example from the Identify API reference, cut down to the
# fields this code reads: three companies, all with confidence_score 1.
DOCS_NAME_EXAMPLE = [{
    "matched_on": "Serve Robotics", "match_type": "name",
    "matches": [
        {"confidence_score": 1, "company_data": {"crustdata_company_id": 628895, "basic_info": {
            "crustdata_company_id": 628895, "name": "Serve Robotics", "primary_domain": "serverobotics.com",
            "employee_count_range": "51-200", "industries": ["Technology, Information and Internet"]}}},
        {"confidence_score": 1, "company_data": {"crustdata_company_id": 5825197, "basic_info": {
            "name": "Site Serve Robotics", "primary_domain": "siteserverobotics.co.uk", "employee_count_range": "2-10"}}},
        {"confidence_score": 1, "company_data": {"crustdata_company_id": 25592234, "basic_info": {
            "name": "iServe Robotics", "primary_domain": "iserve.ai", "employee_count_range": "2-10"}}},
    ],
}]


class Canned:
    """Transport that replays fixed responses and records what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def post(self, path, body):
        self.bodies.append(body)
        return self.responses.pop(0)


def acct(id_, name, website="", employees=None):
    return {"id": id_, "name": name, "website": website, "employees": employees}


def run(accounts, transport, **kw):
    r = resolve.Resolver(transport, sleep=lambda s: None, **kw)
    return r.resolve(accounts), r


class Requests(unittest.TestCase):
    def test_batches_of_25_and_dedup(self):
        values = ["c%d.example" % i for i in range(60)] + ["c0.example"]
        bodies = api.build_identify_requests("domains", values)
        self.assertEqual([len(b["domains"]) for b in bodies], [25, 25, 10])
        self.assertTrue(all(list(b) == ["domains"] for b in bodies))

    def test_exact_match_and_fields(self):
        body = api.build_identify_requests("names", ["A"], exact_match=True, fields=["basic_info"])[0]
        self.assertEqual(body, {"names": ["A"], "exact_match": True, "fields": ["basic_info"]})

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            api.build_identify_requests("emails", ["a@b.c"])
        with self.assertRaises(ValueError):
            api.build_identify_requests("names", ["ok", ""])
        with self.assertRaises(ValueError):
            api.build_identify_requests("crustdata_company_ids", ["12"])
        with self.assertRaises(ValueError):
            api.build_identify_requests("names", ["a"], fields=["headcount"])

    def test_http_transport_headers_and_error_body(self):
        seen = {}

        class Resp:
            status = 200
            headers = {"X-Credits-Used": "0.2"}

            def read(self):
                return json.dumps(DOCS_NAME_EXAMPLE).encode()

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data.decode())
            seen["method"] = req.get_method()
            return Resp()

        t = api.HttpTransport("k123", opener=opener)
        status, body, headers = t.post(api.IDENTIFY_PATH, {"names": ["Serve Robotics"]})
        self.assertEqual(seen["url"], "https://api.crustdata.com/company/identify")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["headers"]["authorization"], "Bearer k123")
        self.assertEqual(seen["headers"]["x-api-version"], "2025-11-01")
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        self.assertEqual(seen["body"], {"names": ["Serve Robotics"]})
        self.assertEqual((status, headers["x-credits-used"]), (200, "0.2"))
        self.assertEqual(len(body[0]["matches"]), 3)
        with self.assertRaises(ValueError):
            api.HttpTransport("")


class Parsing(unittest.TestCase):
    def test_docs_example(self):
        results, warnings = api.parse_identify_response(200, DOCS_NAME_EXAMPLE)
        self.assertEqual(warnings, 0)
        self.assertEqual(results[0].matched_on, "Serve Robotics")
        self.assertEqual([c.company_id for c in results[0].candidates], [628895, 5825197, 25592234])
        self.assertEqual(results[0].candidates[0].all_domains, ["serverobotics.com"])
        self.assertEqual({c.confidence for c in results[0].candidates}, {1.0})

    def test_empty_matches_and_404_are_both_no_match(self):
        body = [{"matched_on": "nope.example", "match_type": "domain", "matches": []}]
        self.assertEqual(api.parse_identify_response(200, body)[0][0].candidates, [])
        self.assertEqual(api.parse_identify_response(404, {"error": {"type": "not_found", "message": "No data found", "metadata": []}}), ([], 0))

    def test_error_body(self):
        with self.assertRaises(api.ApiError) as cm:
            api.parse_identify_response(400, {"error": {"type": "invalid_request", "message": "Exactly one identifier must be provided", "metadata": []}})
        self.assertEqual((cm.exception.status, cm.exception.type), (400, "invalid_request"))
        with self.assertRaises(api.ApiError) as cm:
            api.parse_identify_response(502, None)
        self.assertEqual(cm.exception.type, "unknown")
        with self.assertRaises(api.ApiError):
            api.parse_identify_response(200, {"not": "a list"})

    def test_malformed_entries_are_dropped_and_counted(self):
        body = [
            "junk",
            {"matched_on": "a", "match_type": "name", "matches": None},
            {"matched_on": "b", "match_type": "name", "matches": [
                {"confidence_score": "high", "company_data": {"crustdata_company_id": 7, "basic_info": {"name": None, "all_domains": None, "primary_domain": None}}},
                {"confidence_score": 1.0, "company_data": {"crustdata_company_id": "7"}},
                {"confidence_score": 1.0},
            ]},
        ]
        results, warnings = api.parse_identify_response(200, body)
        self.assertEqual([r.matched_on for r in results], ["a", "b"])
        self.assertEqual(warnings, 4)
        self.assertEqual(len(results[1].candidates), 1)
        c = results[1].candidates[0]
        self.assertEqual((c.company_id, c.name, c.all_domains, c.confidence), (7, "", [], 0.0))

    def test_employee_range(self):
        self.assertEqual(api.parse_employee_range("51-200"), (51, 200))
        self.assertEqual(api.parse_employee_range("10,001+"), (10001, None))
        self.assertEqual(api.parse_employee_range("1"), (1, 1))
        self.assertIsNone(api.parse_employee_range("unknown"))
        self.assertIsNone(api.parse_employee_range(""))
        self.assertIsNone(api.parse_employee_range("200-51"))
        self.assertEqual(resolve.employee_fit(120, "51-200"), 1)
        self.assertEqual(resolve.employee_fit(5, "51-200"), -1)
        self.assertEqual(resolve.employee_fit(20, "51-200"), 0)
        self.assertEqual(resolve.employee_fit(None, "51-200"), 0)
        self.assertEqual(resolve.employee_fit(50, "unknown"), 0)


class Simulator(unittest.TestCase):
    def setUp(self):
        self.t = SimTransport(demo.INDEX, seed=1)

    def test_validation_matches_docs(self):
        self.assertEqual(self.t.post(api.IDENTIFY_PATH, {"names": ["a"], "domains": ["b.example"]})[0], 400)
        self.assertEqual(self.t.post(api.IDENTIFY_PATH, {})[1]["error"]["type"], "invalid_request")
        self.assertEqual(self.t.post(api.IDENTIFY_PATH, {"names": ["n%d" % i for i in range(26)]})[0], 400)
        self.assertEqual(self.t.post(api.IDENTIFY_PATH, {"names": ["n%d" % i for i in range(25)]})[0], 200)

    def test_response_parses_and_credits(self):
        status, body, headers = self.t.post(api.IDENTIFY_PATH, {"domains": ["atlasrobotics.example", "nope.example"]})
        results, warnings = api.parse_identify_response(status, body)
        self.assertEqual((status, warnings, len(results)), (200, 0, 2))
        self.assertEqual(results[0].candidates[0].company_id, 1001)
        self.assertEqual(results[1].candidates, [])
        self.assertEqual(headers["x-credits-used"], "0.2")

    def test_exact_name_ignores_case_only(self):
        ask = lambda name, exact: api.parse_identify_response(*self.t.post(api.IDENTIFY_PATH, {"names": [name], "exact_match": exact})[:2])[0][0].candidates
        self.assertEqual(len(ask("northwind analytics", True)), 2)
        self.assertEqual(ask("Northwind Analytics Inc", True), [])
        self.assertEqual({c.company_id for c in ask("Serve Kitchens", False)}, {1006, 1007, 1008})

    def test_404_mode(self):
        t = SimTransport(demo.INDEX, no_match_as_404=True)
        status, body, _ = t.post(api.IDENTIFY_PATH, {"domains": ["nope.example"]})
        self.assertEqual((status, body["error"]["type"]), (404, "not_found"))


class Decisions(unittest.TestCase):
    def one(self, account, **kw):
        res, r = run([account], SimTransport(demo.INDEX, seed=1), **kw)
        return res[0]

    def test_domain_hit(self):
        r = self.one(acct("1", "Atlas Robotics, Inc.", "https://www.atlasrobotics.example/about"))
        self.assertEqual((r["status"], r["crustdata_company_id"], r["via"]), ("matched", 1001, "domain"))

    def test_secondary_domain_hit(self):
        r = self.one(acct("1", "ATLAS ROBOTICS", "atlasrobotics-eu.example"))
        self.assertEqual((r["status"], r["crustdata_company_id"]), ("matched", 1001))

    def test_stored_name_without_space(self):
        r = self.one(acct("1", "Bergen Technologies Ltd", "bergentech.example"))
        self.assertEqual((r["status"], r["crustdata_company_id"], r["score"]), ("matched", 1009, 1.0))

    def test_same_name_twice_headcount_decides(self):
        r = self.one(acct("1", "Northwind Analytics", "", 300))
        self.assertEqual((r["status"], r["crustdata_company_id"]), ("matched", 1002))
        r = self.one(acct("1", "Northwind Analytics", "", 5))
        self.assertEqual((r["status"], r["crustdata_company_id"]), ("matched", 1003))

    def test_same_name_twice_nothing_to_decide_goes_to_review(self):
        r = self.one(acct("1", "Northwind Analytics"))
        self.assertEqual((r["status"], r["crustdata_company_id"], r["reason"]), ("review", None, "tie between candidates"))
        self.assertEqual({c["crustdata_company_id"] for c in r["candidates"]}, {1002, 1003})

    def test_name_alone_is_not_enough(self):
        r = self.one(acct("1", "Halden Pharma"))
        self.assertEqual((r["status"], r["reason"]), ("review", "name only, no website or headcount to confirm"))
        self.assertEqual(self.one(acct("1", "Halden Pharma"), accept=0.75)["status"], "matched")

    def test_fuzzy_after_empty_exact(self):
        t = SimTransport(demo.INDEX, seed=1)
        res, _ = run([acct("1", "The Müller Robotik GmbH & Co. KG", "", 35)], t)
        self.assertEqual((res[0]["status"], res[0]["crustdata_company_id"], res[0]["via"]), ("matched", 1004, "name_fuzzy"))
        self.assertEqual(t.log, [
            {"names": ["The Müller Robotik GmbH & Co. KG"], "exact_match": True},
            {"names": ["Müller Robotik"], "exact_match": False},
        ])

    def test_wrong_website_goes_to_review(self):
        r = self.one(acct("1", "Halden Pharma", "atlasrobotics.example", 850))
        self.assertEqual((r["status"], r["reason"]), ("review", "website differs"))
        self.assertEqual({c["crustdata_company_id"] for c in r["candidates"]}, {1001, 1005})

    def test_trade_name_on_right_domain_goes_to_review_unless_trusted(self):
        r = self.one(acct("1", "ARX", "atlasrobotics.example"))
        self.assertEqual((r["status"], r["reason"]), ("review", "same domain, different name"))
        self.assertEqual(self.one(acct("1", "ARX", "atlasrobotics.example"), trust_domain_alone=True)["status"], "matched")

    def test_not_in_index(self):
        r = self.one(acct("1", "Corvid Foods", "corvidfoods.example", 20))
        self.assertEqual((r["status"], r["crustdata_company_id"], r["candidates"]), ("unmatched", None, []))

    def test_docs_tie_example_needs_headcount_or_domain(self):
        ok = (200, DOCS_NAME_EXAMPLE, {})
        res, _ = run([acct("1", "Serve Robotics", "", 120)], Canned([ok]))
        self.assertEqual((res[0]["status"], res[0]["crustdata_company_id"]), ("matched", 628895))
        empty = (200, [{"matched_on": "iserve.io", "match_type": "domain", "matches": []}], {})
        res, _ = run([acct("1", "Serve Robotics", "https://iserve.io", None)], Canned([empty, ok]))
        # The account's site is iserve.io. The exact-name candidate lists a
        # different site, and iServe Robotics owns the same label on another TLD,
        # so the website outweighs the exact name.
        self.assertEqual((res[0]["status"], res[0]["crustdata_company_id"], res[0]["reason"]), ("matched", 25592234, "same domain label"))
        res, _ = run([acct("1", "Serve Robotics")], Canned([ok]))
        # With no website or headcount, 'iServe' is one typo away from 'Serve'
        # and the two candidates are too close to pick between.
        self.assertEqual((res[0]["status"], res[0]["crustdata_company_id"], res[0]["reason"]), ("review", None, "tie between candidates"))

    def test_empty_rows(self):
        res, r = run([acct("1", "", ""), acct("2", "   ", "not a url")], SimTransport(demo.INDEX))
        self.assertEqual([x["status"] for x in res], ["unmatched", "unmatched"])
        self.assertEqual(res[0]["reason"], "no name or website")
        self.assertEqual(r.stats["requests"], 0)
        self.assertEqual(run([], SimTransport(demo.INDEX))[0], [])


class WholeFile(unittest.TestCase):
    def test_demo_list(self):
        res, r = run(demo.ACCOUNTS, SimTransport(demo.INDEX, seed=1))
        got = {x["account_id"]: (x["status"], x["crustdata_company_id"]) for x in res}
        self.assertEqual(got, {
            "1": ("matched", 1001), "2": ("matched", 1001), "3": ("matched", 1002), "4": ("review", None),
            "5": ("matched", 1004), "6": ("review", None), "7": ("matched", 1006), "8": ("matched", 1009),
            "9": ("unmatched", None), "10": ("review", None), "11": ("unmatched", None),
        })
        self.assertEqual(res[1]["duplicate_of"], "1")
        self.assertEqual(r.stats["duplicate_groups"], 1)
        self.assertEqual(r.stats["shared_hosts_ignored"], 1)
        self.assertEqual(r.stats["requests"], 3)
        self.assertEqual(r.stats["credits_estimated"], r.stats["credits_reported"])

    def test_link_page_host_is_never_sent(self):
        t = SimTransport(demo.INDEX, seed=1)
        run(demo.ACCOUNTS, t)
        sent = [d for b in t.log for d in b.get("domains", [])]
        self.assertNotIn("linkhub.example", sent)
        t = SimTransport(demo.INDEX, seed=1)
        run([acct("1", "Corvid Foods", "https://www.linkedin.com/company/corvid")], t)
        self.assertTrue(all("domains" not in b for b in t.log))

    def test_company_site_on_several_accounts_is_kept(self):
        rows = [acct("1", "Atlas Robotics", "atlasrobotics.example"), acct("2", "ARX", "atlasrobotics.example/about"), acct("3", "Atlas Robotics EU", "atlasrobotics.example/")]
        res, r = run(rows, SimTransport(demo.INDEX, seed=1))
        self.assertEqual(r.stats["shared_hosts_ignored"], 0)
        self.assertEqual(res[0]["status"], "matched")

    def test_more_than_25_identifiers(self):
        rows = [acct(str(i), "Nobody %d" % i, "nobody%d.example" % i) for i in range(60)]
        t = SimTransport(demo.INDEX, seed=1)
        res, r = run(rows, t)
        self.assertTrue(all(len(next(iter(b.values()))) <= 25 for b in t.log))
        self.assertEqual(r.stats["requests"], 9)
        self.assertEqual({x["status"] for x in res}, {"unmatched"})
        self.assertEqual(r.stats["credits_estimated"], 0.0)

    def test_404_for_no_match_is_handled(self):
        t = SimTransport(demo.INDEX, seed=1, no_match_as_404=True)
        res, r = run([acct("1", "Corvid Foods", "corvidfoods.example")], t)
        self.assertEqual(res[0]["status"], "unmatched")
        self.assertEqual(r.stats["requests"], 3)

    def test_rate_limit_then_success(self):
        waits = []
        t = SimTransport(demo.INDEX, seed=1, fail_plan={1: (429, "rate_limit_error"), 2: (503, "service_unavailable")})
        r = resolve.Resolver(t, sleep=waits.append)
        res = r.resolve([acct("1", "Atlas Robotics", "atlasrobotics.example")])
        self.assertEqual(res[0]["status"], "matched")
        self.assertEqual((r.stats["retries"], waits), (2, [1.0, 2.0]))

    def test_retry_after_header_is_used(self):
        ok = SimTransport(demo.INDEX).post(api.IDENTIFY_PATH, {"domains": ["atlasrobotics.example"]})
        limited = (429, {"error": {"type": "rate_limit_error", "message": "slow down", "metadata": []}}, {"retry-after": "7"})
        waits = []
        r = resolve.Resolver(Canned([limited, ok]), sleep=waits.append)
        self.assertEqual(r.resolve([acct("1", "Atlas Robotics", "atlasrobotics.example")])[0]["status"], "matched")
        self.assertEqual(waits, [7.0])

    def test_gives_up_after_max_retries(self):
        t = SimTransport(demo.INDEX, fail_plan={i: (500, "internal_error") for i in range(1, 10)})
        r = resolve.Resolver(t, sleep=lambda s: None, max_retries=2)
        with self.assertRaises(api.ApiError) as cm:
            r.resolve([acct("1", "Atlas Robotics", "atlasrobotics.example")])
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(t.requests, 3)

    def test_auth_error_is_not_retried(self):
        t = SimTransport(demo.INDEX, fail_plan={1: (401, "authentication_error")})
        with self.assertRaises(api.ApiError):
            run([acct("1", "Atlas Robotics", "atlasrobotics.example")], t)
        self.assertEqual(t.requests, 1)

    def test_credits_run_out_mid_file(self):
        t = SimTransport(demo.INDEX, seed=1, credit_limit=0.4)
        res, r = run(demo.ACCOUNTS, t)
        by = {x["account_id"]: x for x in res}
        self.assertTrue(r.stats["halted"])
        self.assertEqual(by["1"]["status"], "matched")
        self.assertEqual(by["3"]["status"], "not_attempted")
        self.assertEqual(by["3"]["reason"], "insufficient credits")
        self.assertEqual(r.stats["not_attempted"], 8)
        self.assertEqual(r.stats["matched"], 3)

    def test_missing_row_in_response_is_no_match(self):
        short = (200, [{"matched_on": "a.example", "match_type": "domain", "matches": []}], {})
        empty_names = (200, [], {})
        res, _ = run([acct("1", "Aaa", "a.example"), acct("2", "Bbb", "b.example")], Canned([short, empty_names, empty_names]))
        self.assertEqual([x["status"] for x in res], ["unmatched", "unmatched"])

    def test_deterministic(self):
        accounts, gold, tags, make = bench.make_world(21, {}, {}, 300, 200)
        a, _ = run(accounts, make())
        b, _ = run(accounts, make())
        self.assertEqual(a, b)


class Helpers(unittest.TestCase):
    def test_clean_query(self):
        self.assertEqual(resolve.clean_query("The Müller Robotik GmbH & Co. KG"), "Müller Robotik")
        self.assertEqual(resolve.clean_query("Atlas Robotics, Inc."), "Atlas Robotics")
        self.assertEqual(resolve.clean_query("Limited"), "Limited")
        self.assertEqual(resolve.clean_query("The Co"), "The")

    def test_url_path(self):
        self.assertEqual(resolve.url_path("https://linkhub.example/Corvid?x=1"), "corvid")
        self.assertEqual(resolve.url_path("atlas.example"), "")
        self.assertEqual(resolve.url_path("http://atlas.example/"), "")

    def test_plan(self):
        p = resolve.plan(demo.ACCOUNTS)
        self.assertEqual((p["accounts"], p["requests_domain_pass"]), (11, 1))
        self.assertLessEqual(p["max_requests"], 3)

    def test_csv_round_trip_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "accounts.csv")
            with open(src, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["id", "name", "website", "employees"])
                w.writeheader()
                for a in demo.ACCOUNTS:
                    w.writerow({k: ("" if v is None else v) for k, v in a.items()})
            rows = resolve.read_accounts(src)
            self.assertEqual(len(rows), 11)
            res, _ = run(rows, SimTransport(demo.INDEX, seed=1))
            self.assertEqual(res[0]["crustdata_company_id"], 1001)
            dst = os.path.join(tmp, "out.csv")
            resolve.write_resolutions(dst, rows, res)
            with open(dst, newline="", encoding="utf-8") as fh:
                back = list(csv.DictReader(fh))
            self.assertEqual(back[1]["duplicate_of"], "1")
            self.assertEqual(back[3]["status"], "review")
            env = {k: v for k, v in os.environ.items() if k != "CRUSTDATA_API_KEY"}
            dry = subprocess.run([sys.executable, os.path.join(HERE, "resolve.py"), src, "--dry-run"], capture_output=True, text=True, env=env)
            self.assertEqual(dry.returncode, 0)
            self.assertIn("max_credits", dry.stdout)
            live = subprocess.run([sys.executable, os.path.join(HERE, "resolve.py"), src], capture_output=True, text=True, env=env)
            self.assertEqual(live.returncode, 2)
            self.assertIn("CRUSTDATA_API_KEY", live.stderr)
            bad = os.path.join(tmp, "bad.csv")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("company,site\nAtlas,atlas.example\n")
            with self.assertRaises(ValueError):
                resolve.read_accounts(bad)


class Benchmark(unittest.TestCase):
    def test_generator_is_seeded_and_labels_truth(self):
        a = gen.generate_crm(5, 200, 100)
        self.assertEqual(a[2], gen.generate_crm(5, 200, 100)[2])
        entities, index_ids, accounts, truth, tags = a
        self.assertEqual(len(accounts), 100)
        self.assertTrue(any(t is None for t in truth))
        self.assertTrue(all(t is None or t in index_ids for t in truth))

    def test_old_generator_unchanged(self):
        # generate_crm must not disturb the record generator the linker benchmark uses
        records, truth, _ = gen.generate(0, 5)
        self.assertEqual(records[0]["name"], gen.generate(0, 5)[0][0]["name"])
        with open(os.path.join(HERE, "bench_results.json")) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["records"], 20924)

    def test_held_out_seed_beats_first_match_on_wrong_links(self):
        accounts, gold, tags, make = bench.make_world(77, {}, {}, 900, 600)
        base = bench.rates(bench.tally(accounts, gold, tags, bench.first_match(accounts, make())))
        mine = bench.rates(bench.run_resolver(accounts, gold, tags, make))
        self.assertGreater(mine["precision"], 0.985)
        self.assertGreater(mine["recall"], 0.55)
        self.assertLess(mine["wrong"] * 5, base["wrong"])
        self.assertGreater(mine["review_truth_shown"], 0.6 * mine["review"])


if __name__ == "__main__":
    unittest.main()
