"""Benchmark samefirm against simple baselines on synthetic records with ground truth."""
import json
import sys
import time
from collections import defaultdict

import gen
import samefirm


def true_pairs(truth):
    by = defaultdict(list)
    for i, e in enumerate(truth):
        by[e].append(i)
    pairs = set()
    for members in by.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((min(members[i], members[j]), max(members[i], members[j])))
    return pairs, by


def cluster_pairs(clusters):
    pairs = set()
    for members in clusters:
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return pairs


def by_key(records, keyfn):
    groups = defaultdict(list)
    for i, r in enumerate(records):
        k = keyfn(r)
        groups[k if k else ("solo", i)].append(i)
    return list(groups.values())


def union_keys(records, keyfns):
    parent = list(range(len(records)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for fn in keyfns:
        first = {}
        for i, r in enumerate(records):
            k = fn(r)
            if not k:
                continue
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i
    groups = defaultdict(list)
    for i in range(len(records)):
        groups[find(i)].append(i)
    return list(groups.values())


def prf(pred, gold):
    tp = len(pred & gold)
    p = tp / len(pred) if pred else 1.0
    r = tp / len(gold) if gold else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def exact_entities(clusters, by_entity):
    got = {tuple(sorted(c)) for c in clusters}
    return sum(1 for m in by_entity.values() if tuple(sorted(m)) in got) / len(by_entity)


def run(seeds, n_entities):
    methods = ["exact name", "normalized name", "normalized name or domain", "samefirm", "samefirm, no source rule"]
    agg = {m: {"tp": 0, "pred": 0, "gold": 0, "exact": 0.0} for m in methods}
    info = {"records": 0, "all_pairs": 0, "candidate_pairs": 0, "gold_in_candidates": 0, "gold": 0, "seconds": 0.0}
    miss_by_tag = defaultdict(lambda: [0, 0])
    for seed in seeds:
        records, truth, tags = gen.generate(seed, n_entities)
        gold, by_entity = true_pairs(truth)
        n = len(records)
        norm = lambda r: " ".join(samefirm.name_tokens(r["name"]))
        dom = lambda r: samefirm.domain_parts(r["domain"])[0]
        preds = {
            "exact name": by_key(records, lambda r: r["name"]),
            "normalized name": by_key(records, norm),
            "normalized name or domain": union_keys(records, [norm, dom]),
        }
        t0 = time.perf_counter()
        linker = samefirm.Linker(records)
        clusters, _ = linker.link()
        info["seconds"] += time.perf_counter() - t0
        preds["samefirm"] = clusters
        cands = linker.candidates()
        info["records"] += n
        info["all_pairs"] += n * (n - 1) // 2
        info["candidate_pairs"] += len(cands)
        info["gold_in_candidates"] += len(gold & cands)
        info["gold"] += len(gold)
        preds["samefirm, no source rule"] = samefirm.Linker(records, one_per_source=False).link()[0]
        for m in methods:
            pp = cluster_pairs(preds[m])
            agg[m]["tp"] += len(pp & gold)
            agg[m]["pred"] += len(pp)
            agg[m]["gold"] += len(gold)
            agg[m]["exact"] += exact_entities(preds[m], by_entity)
        found = cluster_pairs(clusters)
        for (i, j) in gold:
            for tag in set(tags[i]) | set(tags[j]) or {"clean"}:
                miss_by_tag[tag][1] += 1
                if (i, j) not in found:
                    miss_by_tag[tag][0] += 1
    rows = []
    for m in methods:
        a = agg[m]
        p = a["tp"] / a["pred"] if a["pred"] else 1.0
        r = a["tp"] / a["gold"]
        f = 2 * p * r / (p + r) if p + r else 0.0
        rows.append({"method": m, "precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4),
                     "false_merge_pairs": a["pred"] - a["tp"], "entities_exact": round(a["exact"] / len(seeds), 4)})
    out = {
        "seeds": list(seeds), "entities_per_seed": n_entities, "records": info["records"], "true_pairs": info["gold"],
        "methods": rows,
        "blocking": {
            "all_pairs": info["all_pairs"], "candidate_pairs": info["candidate_pairs"],
            "reduction": round(1 - info["candidate_pairs"] / info["all_pairs"], 5),
            "true_pairs_kept": round(info["gold_in_candidates"] / info["gold"], 4),
        },
        "seconds_per_seed": round(info["seconds"] / len(seeds), 2),
        "records_per_second": round(info["records"] / info["seconds"]),
        "missed_by_noise": {t: {"missed": v[0], "pairs": v[1], "recall": round(1 - v[0] / v[1], 4)} for t, v in sorted(miss_by_tag.items())},
    }
    return out


def show(out):
    print("seeds %s, %d entities each, %d records, %d true pairs" % (out["seeds"], out["entities_per_seed"], out["records"], out["true_pairs"]))
    print()
    print("| method | precision | recall | F1 | wrongly merged pairs | companies recovered exactly |")
    print("|---|---|---|---|---|---|")
    for r in out["methods"]:
        print("| %s | %.1f%% | %.1f%% | %.1f%% | %d | %.1f%% |" % (r["method"], 100 * r["precision"], 100 * r["recall"], 100 * r["f1"], r["false_merge_pairs"], 100 * r["entities_exact"]))
    b = out["blocking"]
    print()
    print("blocking: %d candidate pairs out of %d (%.3f%% skipped), %.1f%% of true pairs kept" % (b["candidate_pairs"], b["all_pairs"], 100 * b["reduction"], 100 * b["true_pairs_kept"]))
    print("time: %.2f s per seed, %d records per second" % (out["seconds_per_seed"], out["records_per_second"]))
    print()
    print("| noise on either record | true pairs | found by samefirm |")
    print("|---|---|---|")
    for t, v in out["missed_by_noise"].items():
        print("| %s | %d | %.1f%% |" % (t, v["pairs"], 100 * v["recall"]))


# ---------------------------------------------------------------------------
# Resolver benchmark: CRM accounts -> crustdata_company_id through the simulated
# Identify endpoint. Everything here is synthetic, see sim_identify.py.

DEV = {"name": "dev (tuned on this)", "seeds": [0], "gen": {}, "sim": {}}
HELD_OUT = {"name": "held out, same settings", "seeds": [1, 2, 3], "gen": {}, "sim": {}}
SHIFTED = {
    "name": "held out, harder settings", "seeds": [4, 5],
    "gen": {"in_index": 0.7, "lookalike_rate": 0.35, "wrong_site": 0.06, "emp_missing": 0.6},
    "sim": {"rank_noise": 0.7, "squash_rate": 0.1, "alt_domain_rate": 0.3},
}


def make_world(seed, gen_kw, sim_kw, n_companies=3000, n_accounts=2000):
    import sim_identify
    entities, index_ids, accounts, truth, tags = gen.generate_crm(seed, n_companies, n_accounts, **gen_kw)
    sim_kw = dict(sim_kw)
    rank_noise = sim_kw.pop("rank_noise", 0.3)
    companies = sim_identify.build_companies(entities, index_ids, seed, **sim_kw)
    gold = [None if t is None else sim_identify.company_id_for(t) for t in truth]
    make = lambda **kw: sim_identify.SimTransport(companies, seed=seed, rank_noise=rank_noise, **kw)
    return accounts, gold, tags, make


def first_match(accounts, transport):
    """Baseline: domain, then exact name, then fuzzy name, and always take matches[0]."""
    import crustdata_api as api

    def ask(kind, values, exact):
        got = {}
        for body in api.build_identify_requests(kind, values, exact_match=exact):
            status, payload, _ = transport.post(api.IDENTIFY_PATH, body)
            for r in api.parse_identify_response(status, payload)[0]:
                got[r.matched_on] = r.candidates
        return got

    picked = {}
    todo = [a for a in accounts if a["website"]]
    hosts = {a["id"]: samefirm.domain_parts(a["website"])[0] for a in todo}
    got = ask("domains", [h for h in hosts.values() if h], None)
    for a in todo:
        if got.get(hosts[a["id"]]):
            picked[a["id"]] = got[hosts[a["id"]]][0].company_id
    for exact in (True, False):
        todo = [a for a in accounts if a["id"] not in picked and a["name"]]
        got = ask("names", [a["name"] for a in todo], exact)
        for a in todo:
            if got.get(a["name"]):
                picked[a["id"]] = got[a["name"]][0].company_id
    return picked


def tally(accounts, gold, tags, decided, reviews=None, pools=None):
    """decided: {account id: company id}. reviews: {account id: [candidate ids shown]}."""
    reviews = reviews or {}
    t = {"accounts": len(accounts), "in_index": 0, "matched": 0, "correct": 0, "wrong": 0,
         "wrong_not_in_index": 0, "review": 0, "review_truth_shown": 0, "truth_returned": 0}
    wrong_tags = defaultdict(int)
    for a, g, tg in zip(accounts, gold, tags):
        if g is not None:
            t["in_index"] += 1
            if pools is not None and g in pools.get(a["id"], ()):
                t["truth_returned"] += 1
        if a["id"] in decided:
            t["matched"] += 1
            if decided[a["id"]] == g:
                t["correct"] += 1
            else:
                t["wrong"] += 1
                if g is None:
                    t["wrong_not_in_index"] += 1
                for x in tg:
                    wrong_tags[x] += 1
        elif a["id"] in reviews:
            t["review"] += 1
            if g is not None and g in reviews[a["id"]]:
                t["review_truth_shown"] += 1
    t["wrong_by_tag"] = dict(sorted(wrong_tags.items()))
    return t


def add(total, part):
    for k, v in part.items():
        if isinstance(v, dict):
            d = total.setdefault(k, {})
            for kk, vv in v.items():
                d[kk] = d.get(kk, 0) + vv
        else:
            total[k] = total.get(k, 0) + v
    return total


def rates(t):
    t = dict(t)
    t["precision"] = round(t["correct"] / t["matched"], 4) if t["matched"] else 1.0
    t["recall"] = round(t["correct"] / t["in_index"], 4) if t["in_index"] else 0.0
    t["review_rate"] = round(t["review"] / t["accounts"], 4)
    return t


def run_resolver(accounts, gold, tags, make, **kw):
    import resolve
    transport = make()
    r = resolve.Resolver(transport, sleep=lambda s: None, **kw)
    res = r.resolve(accounts)
    decided = {x["account_id"]: x["crustdata_company_id"] for x in res if x["status"] == "matched"}
    reviews = {x["account_id"]: [c["crustdata_company_id"] for c in x["candidates"]] for x in res if x["status"] == "review"}
    t = tally(accounts, gold, tags, decided, reviews, r.pools)
    t["requests"] = r.stats["requests"]
    t["credits"] = r.stats["credits_estimated"]
    t["duplicate_groups"] = r.stats["duplicate_groups"]
    return t


def resolve_split(split):
    variants = {
        "first match (baseline)": None,
        "resolver": {},
        "resolver, margin 0": {"margin": 0.0},
        "resolver, trust domain alone": {"trust_domain_alone": True},
    }
    totals = {name: {} for name in variants}
    for seed in split["seeds"]:
        accounts, gold, tags, make = make_world(seed, split["gen"], split["sim"])
        for name, kw in variants.items():
            if kw is None:
                transport = make()
                t = tally(accounts, gold, tags, first_match(accounts, transport))
                t["requests"] = transport.requests
                t["credits"] = round(transport.credits, 1)
            else:
                t = run_resolver(accounts, gold, tags, make, **kw)
            add(totals[name], t)
    return {"split": split["name"], "seeds": split["seeds"], "settings": {"gen": split["gen"], "sim": split["sim"]},
            "methods": {name: rates(t) for name, t in totals.items()}}


def show_resolve(block):
    print("%s, seeds %s" % (block["split"], block["seeds"]))
    any_m = block["methods"]["resolver"]
    print("%d accounts, %d of them have their company in the index" % (any_m["accounts"], any_m["in_index"]))
    print()
    print("| method | linked | wrong links | precision | recall | sent to review | credits |")
    print("|---|---|---|---|---|---|---|")
    for name, m in block["methods"].items():
        print("| %s | %d | %d | %.1f%% | %.1f%% | %d (%.1f%%) | %.1f |" % (
            name, m["matched"], m["wrong"], 100 * m["precision"], 100 * m["recall"], m["review"], 100 * m["review_rate"], m["credits"]))
    m = block["methods"]["resolver"]
    print()
    print("resolver: true company was returned by the API for %d of %d in-index accounts (%.1f%%), the ceiling on recall" % (
        m["truth_returned"], m["in_index"], 100 * m["truth_returned"] / m["in_index"]))
    print("resolver: %d of %d review rows show the true company in the top 3" % (m["review_truth_shown"], m["review"]))
    print("resolver: %d wrong links were accounts whose company is not in the index" % m["wrong_not_in_index"])
    print("resolver: wrong links by noise tag %s" % m["wrong_by_tag"])
    print()


def tune():
    """Grid search on the dev split only. Prints the grid, picks nothing automatically."""
    accounts, gold, tags, make = make_world(0, {}, {})
    print("dev split, seed 0. Rule used: highest recall with at most 0.5% wrong links.")
    print("| accept | margin | linked | wrong | precision | recall | review |")
    print("|---|---|---|---|---|---|---|")
    best = None
    for accept in (0.65, 0.7, 0.75, 0.8, 0.85, 0.9):
        for margin in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3):
            m = rates(run_resolver(accounts, gold, tags, make, accept=accept, margin=margin))
            print("| %.2f | %.2f | %d | %d | %.2f%% | %.2f%% | %d |" % (accept, margin, m["matched"], m["wrong"], 100 * m["precision"], 100 * m["recall"], m["review"]))
            if m["wrong"] <= 0.005 * m["matched"] and (best is None or m["recall"] > best[0]):
                best = (m["recall"], accept, margin)
    print("pick: accept %.2f, margin %.2f" % (best[1], best[2]) if best else "no setting met the rule")


def load_results():
    try:
        with open("bench_results.json") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "tune":
        tune()
        sys.exit(0)
    if mode == "resolve":
        blocks = [resolve_split(s) for s in (DEV, HELD_OUT, SHIFTED)]
        for b in blocks:
            show_resolve(b)
        saved = load_results()
        saved["resolve"] = blocks
        with open("bench_results.json", "w") as fh:
            json.dump(saved, fh, indent=1)
        sys.exit(0)
    seeds = [1, 2, 3, 4, 5]
    n = 2000
    if mode == "dev":
        seeds = [0]
    out = run(seeds, n)
    show(out)
    if seeds != [0]:
        saved = load_results()
        if "resolve" in saved:
            out["resolve"] = saved["resolve"]
        with open("bench_results.json", "w") as fh:
            json.dump(out, fh, indent=1)
