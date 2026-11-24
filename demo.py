"""Under a minute, offline: resolve a small hand-written CRM list against a simulated Identify endpoint.

Every company here is made up. The endpoint is sim_identify.SimTransport, a
stand-in that answers in the response shape from Crustdata's public docs. It is
not their matcher.
"""
import bench
import resolve
import sim_identify
from sim_identify import company

INDEX = [
    company(1001, "Atlas Robotics", "atlasrobotics.example", 140, ["atlasrobotics-eu.example"]),
    company(1002, "Northwind Analytics", "northwind-analytics.example", 320),
    company(1003, "Northwind Analytics", "northwind-fr.example", 6),
    company(1004, "Müller Robotik", "mueller-robotik.example", 40),
    company(1005, "Halden Pharma", "haldenpharma.example", 900),
    company(1006, "Serve Kitchens", "servekitchens.example", 120),
    company(1007, "Site Serve Kitchens", "siteservekitchens.example", 5),
    company(1008, "iServe Kitchens", "iserve-kitchens.example", 8),
    company(1009, "BergenTechnologies", "bergentech.example", 60),
]

ACCOUNTS = [
    {"id": "1", "name": "Atlas Robotics, Inc.", "website": "https://www.atlasrobotics.example/about", "employees": 150},
    {"id": "2", "name": "ATLAS ROBOTICS", "website": "atlasrobotics-eu.example", "employees": None},
    {"id": "3", "name": "Northwind Analytics", "website": "", "employees": 300},
    {"id": "4", "name": "Northwind Analytics", "website": "", "employees": None},
    {"id": "5", "name": "The Müller Robotik GmbH & Co. KG", "website": "", "employees": 35},
    {"id": "6", "name": "Halden Pharma", "website": "atlasrobotics.example", "employees": 850},
    {"id": "7", "name": "Serve Kitchens LLC", "website": "https://linkhub.example/serve", "employees": 100},
    {"id": "8", "name": "Bergen Technologies Ltd", "website": "www.bergentech.example", "employees": 55},
    {"id": "9", "name": "Corvid Foods", "website": "https://linkhub.example/corvid", "employees": 20},
    {"id": "10", "name": "ARX", "website": "atlasrobotics.example", "employees": None},
    {"id": "11", "name": "Dunmore Media", "website": "https://linkhub.example/dunmore", "employees": None},
]


def show(rows, results):
    print("%-3s %-34s %-13s %-6s %-11s %-5s %s" % ("id", "account", "status", "id", "via", "score", "reason"))
    for row, r in zip(rows, results):
        note = r["reason"] + (", duplicate of account %s" % r["duplicate_of"] if r["duplicate_of"] else "")
        print("%-3s %-34s %-13s %-6s %-11s %-5.2f %s" % (row["id"], row["name"][:34], r["status"], r["crustdata_company_id"] or "-", r.get("via", "") or "-", r["score"], note))


def main():
    print("1. Eleven accounts, nine indexed companies (all made up)\n")
    transport = sim_identify.SimTransport(INDEX, seed=1)
    resolver = resolve.Resolver(transport, sleep=lambda s: None)
    results = resolver.resolve(ACCOUNTS)
    show(ACCOUNTS, results)
    st = resolver.stats
    print("\nrequests %d, credits %.1f, shared hosts ignored %d, duplicate groups %d" % (st["requests"], st["credits_estimated"], st["shared_hosts_ignored"], st["duplicate_groups"]))
    print("requests sent:")
    for body in transport.log:
        print("  ", body)

    print("\n2. Same list, first request gets a 429 and the account has 0.4 credits\n")
    transport = sim_identify.SimTransport(INDEX, seed=1, fail_plan={1: (429, "rate_limit_error")}, credit_limit=0.4)
    waits = []
    resolver = resolve.Resolver(transport, sleep=waits.append)
    results = resolver.resolve(ACCOUNTS)
    show(ACCOUNTS, results)
    st = resolver.stats
    print("\nretries %d (waited %s s), halted %s, not attempted %d" % (st["retries"], waits, st["halted"], st["not_attempted"]))

    print("\n3. Held-out synthetic benchmark, seed 9, 1,200 companies and 800 accounts\n")
    accounts, gold, tags, make = bench.make_world(9, {}, {}, 1200, 800)
    transport = make()
    base = bench.rates(bench.tally(accounts, gold, tags, bench.first_match(accounts, transport)))
    mine = bench.rates(bench.run_resolver(accounts, gold, tags, make))
    for name, m in (("first match", base), ("resolver", mine)):
        print("%-12s linked %4d  wrong %3d  precision %5.1f%%  recall %5.1f%%  review %3d" % (name, m["matched"], m["wrong"], 100 * m["precision"], 100 * m["recall"], m["review"]))


if __name__ == "__main__":
    main()
