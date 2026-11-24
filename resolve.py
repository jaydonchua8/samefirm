"""Map a CRM account list to crustdata_company_id values through the Company Identify API.

The Identify docs say a name lookup often returns several candidates, that the
first one is not always right, and that the caller should compare
primary_domain and employee_count_range before trusting it. This module is that
caller. For every account it ends in one of four states:

  matched        one candidate is clearly best, safe to write back to the CRM
  review         there is a plausible candidate but not a safe one, a person decides
  unmatched      nothing returned is close enough
  not_attempted  the run stopped (credits ran out) before this account was looked up

Lookup order, cheapest and most reliable first:
  1. domains                      for accounts with a usable website
  2. names, exact_match=true      for accounts still open
  3. names, exact_match=false     only for names whose exact lookup came back empty
Steps 2 and 3 follow the order the docs suggest. Requests are batched 25 at a
time and identical identifiers are sent once.

Usage:
  python resolve.py accounts.csv --dry-run        plan only, nothing is sent
  CRUSTDATA_API_KEY=... python resolve.py accounts.csv --out resolved.csv
The second form calls the live API. I have not been able to run it, see README.
"""
import csv
import math
import os
import sys
import time
from collections import defaultdict

import crustdata_api as api
import samefirm

# Chosen by `python bench.py tune` on the dev split only (seed 0, default settings):
# highest recall with at most 0.5% wrong links. At 0.85 a match on name alone is
# not accepted without a website or headcount that agrees, it goes to review.
ACCEPT = 0.85
MARGIN = 0.05
REVIEW = 0.45
LOW_NAME = 0.3

# Hosts that are profile or link pages, never a company's own site.
PROFILE_HOSTS = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "linktr.ee",
    "crunchbase.com", "youtube.com", "github.com", "medium.com", "gmail.com", "google.com",
}


class CreditsExhausted(Exception):
    pass


class Account:
    __slots__ = ("id", "name", "tokens", "host", "label", "path", "use_host", "employees")

    def __init__(self, row):
        self.id = str(row.get("id", ""))
        self.name = (row.get("name") or "").strip()
        self.tokens = samefirm.name_tokens(self.name)
        self.host, self.label = samefirm.domain_parts(row.get("website") or "")
        self.use_host = bool(self.host) and "." in self.host
        self.path = url_path(row.get("website") or "")
        emp = row.get("employees")
        try:
            self.employees = int(emp) if emp not in (None, "") else None
        except (TypeError, ValueError):
            self.employees = None
        if self.employees is not None and self.employees <= 0:
            self.employees = None


def url_path(url):
    """First path segment of a URL, lowercased. 'https://linkhub.example/corvid?x=1' -> 'corvid'."""
    rest = url.strip().split("://", 1)[-1]
    if "/" not in rest:
        return ""
    return rest.split("/", 1)[1].split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].lower()


def clean_query(name):
    """Drop leading 'The' and trailing legal-form words, keep case and accents.

    'The Atlas Robotik GmbH & Co. KG' -> 'Atlas Robotik'. Used for the fuzzy
    lookup only. The exact lookup sends the name as typed.
    """
    words = name.split()

    def legal(w):
        k = samefirm.fold("".join(c for c in w if c.isalnum()))
        return k == "" or k in samefirm.LEGAL

    while len(words) > 1 and legal(words[-1]):
        words.pop()
    if len(words) > 1 and words[0].lower() == "the":
        words.pop(0)
    return " ".join(words).rstrip(",")


def employee_fit(employees, range_text):
    """+1 when the CRM count is within 2x of the range, -1 when it is over 4x away, else 0."""
    rng = api.parse_employee_range(range_text)
    if employees is None or rng is None:
        return 0
    lo, hi = rng
    if employees >= lo / 2 and (hi is None or employees <= hi * 2):
        return 1
    if employees < lo / 4 or (hi is not None and employees > hi * 4):
        return -1
    return 0


class Resolver:
    def __init__(self, transport, accept=ACCEPT, margin=MARGIN, review=REVIEW, trust_domain_alone=False,
                 max_retries=3, sleep=time.sleep, extra_shared_hosts=()):
        self.transport = transport
        self.accept = accept
        self.margin = margin
        self.review = review
        self.trust_domain_alone = trust_domain_alone
        self.max_retries = max_retries
        self.sleep = sleep
        self.extra_shared = {h.lower() for h in extra_shared_hosts}
        self.stats = {}

    # ---- scoring ------------------------------------------------------
    def score(self, acct, cand):
        """Return (score, reason) for one account and one candidate."""
        ctokens = samefirm.name_tokens(cand.name)
        ns = samefirm.soft_token_sim(acct.tokens, ctokens, self.idf, self.default_idf)
        if acct.tokens and "".join(acct.tokens) == "".join(ctokens):
            ns = 1.0  # 'Atlas Robotics' stored as 'AtlasRobotics'
        fit = employee_fit(acct.employees, cand.employee_count_range)
        cand_hosts = {samefirm.domain_parts(d)[0] for d in cand.all_domains}
        cand_labels = {samefirm.fold(samefirm.domain_parts(d)[1]) for d in cand.all_domains}
        squashed = "".join(acct.tokens)
        name_is_label = bool(squashed) and len(squashed) >= 6 and squashed in cand_labels
        if acct.use_host and acct.host in cand_hosts:
            if ns >= LOW_NAME or name_is_label:
                return (0.9 + 0.1 * ns, "same domain")
            # The site is theirs but the name shares nothing. Either a trade name
            # or the CRM has the wrong website. Headcount cannot settle it.
            if self.trust_domain_alone and fit >= 0:
                return (0.9, "same domain, different name")
            return (0.5, "same domain, different name")
        if acct.use_host and samefirm.fold(acct.label) in cand_labels:
            return (0.55 + 0.35 * ns + 0.1 * fit, "same domain label")
        if acct.use_host and cand_hosts:
            return (0.6 * ns - 0.1 + (0.1 if fit > 0 else 0.0), "website differs")
        s = 0.8 * ns + (0.1 if fit > 0 else (-0.2 if fit < 0 else 0.0))
        if name_is_label:
            s = max(s, 0.8)
        return (s, "name" + (" and headcount" if fit > 0 else ""))

    def decide(self, acct, cands):
        """Return a resolution dict for one account given every candidate seen so far."""
        seen = {}
        for c in cands:
            if c.company_id not in seen:
                s, why = self.score(acct, c)
                seen[c.company_id] = (s, why, c)
        ranked = sorted(seen.values(), key=lambda t: (-t[0], t[2].company_id))
        shown = [{"crustdata_company_id": c.company_id, "name": c.name, "primary_domain": c.primary_domain,
                  "employee_count_range": c.employee_count_range, "score": round(s, 3), "why": why}
                 for s, why, c in ranked[:3]]
        out = {"account_id": acct.id, "status": "unmatched", "crustdata_company_id": None, "score": 0.0,
               "reason": "no candidates", "candidates": shown}
        if not ranked:
            return out
        best, why, cand = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else -1.0
        out["score"] = round(best, 3)
        if best >= self.accept and best - second >= self.margin:
            out.update(status="matched", crustdata_company_id=cand.company_id, reason=why)
        elif best >= self.review:
            if why in ("same domain, different name", "website differs"):
                reason = why
            elif best - second < self.margin:
                reason = "tie between candidates"
            elif why == "name":
                reason = "name only, no website or headcount to confirm"
            else:
                reason = "weak match"
            out.update(status="review", reason=reason)
        else:
            out["reason"] = "no close candidate"
        return out

    # ---- transport ----------------------------------------------------
    def _post(self, body):
        for attempt in range(self.max_retries + 1):
            status, payload, headers = self.transport.post(api.IDENTIFY_PATH, body)
            self.stats["requests"] += 1
            try:
                results, warnings = api.parse_identify_response(status, payload)
            except api.ApiError as err:
                if err.status == 402 or err.type == "insufficient_credits":
                    raise CreditsExhausted(str(err))
                if (err.status in (429, 500, 503) or err.type in api.RETRYABLE) and attempt < self.max_retries:
                    self.stats["retries"] += 1
                    wait = headers.get("retry-after") if isinstance(headers, dict) else None
                    try:
                        wait = float(wait)
                    except (TypeError, ValueError):
                        wait = float(2 ** attempt)
                    self.sleep(wait)
                    continue
                raise
            self.stats["warnings"] += warnings
            try:
                self.stats["credits_reported"] += float((headers or {}).get("x-credits-used", 0))
            except (TypeError, ValueError):
                pass
            return results
        raise api.ApiError(0, "retries_exhausted", "gave up")  # not reachable, kept for clarity

    def _lookup(self, kind, values, exact):
        """Return {identifier: [Candidate]} for identifiers that were answered."""
        answered = {}
        if self.halted or not values:
            return answered
        for body in api.build_identify_requests(kind, values, exact_match=exact):
            try:
                results = self._post(body)
            except CreditsExhausted:
                self.halted = True
                break
            sent = body[kind]
            got = {r.matched_on: r.candidates for r in results}
            for v in sent:
                # A 404, or a row missing from the array, is treated as no match.
                answered[v] = got.get(v, [])
                if answered[v]:
                    self.stats["identifiers_matched"] += 1
        return answered

    # ---- main ---------------------------------------------------------
    def resolve(self, rows):
        """rows: dicts with id, name, website, employees. Returns a list of resolution dicts."""
        self.stats = {"accounts": len(rows), "requests": 0, "retries": 0, "warnings": 0,
                      "identifiers_matched": 0, "credits_reported": 0.0}
        self.halted = False
        accts = [Account(r) for r in rows]
        n = max(1, len(accts))
        df = defaultdict(int)
        for a in accts:
            for t in set(a.tokens):
                df[t] += 1
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.default_idf = math.log(1 + n)

        # A link page or marketplace gives every company its own path under one
        # host. Three or more accounts on one host, each with a different path
        # and a different name, marks the host as shared. A real company site
        # listed on several accounts (divisions, a mistyped website) has no
        # per-account path, so it is kept.
        by_host = defaultdict(set)
        for a in accts:
            if a.use_host and a.path:
                by_host[a.host].add((a.path, " ".join(a.tokens)))
        shared = set(self.extra_shared)
        for h, seen in by_host.items():
            if len({p for p, _ in seen}) >= 3 and len({nm for _, nm in seen}) >= 3:
                shared.add(h)
        for a in accts:
            if a.use_host and (a.host in shared or a.host in PROFILE_HOSTS or any(a.host.endswith("." + p) for p in PROFILE_HOSTS)):
                a.use_host = False
        self.stats["shared_hosts_ignored"] = len(shared)

        pool = {a.id: [] for a in accts}
        final = {}
        via = {}

        def settle(todo, key, answered, label):
            still = []
            for a in todo:
                k = key(a)
                if k not in answered:
                    still.append(a)
                    continue
                pool[a.id].extend(answered[k])
                res = self.decide(a, pool[a.id])
                if res["status"] == "matched":
                    res["via"] = label
                    final[a.id] = res
                else:
                    if answered[k]:
                        via[a.id] = label
                    still.append(a)
            return still

        # 1. domains
        with_host = [a for a in accts if a.use_host]
        ans_dom = self._lookup("domains", [a.host for a in with_host], None)
        open_ = settle(with_host, lambda a: a.host, ans_dom, "domain") + [a for a in accts if not a.use_host]
        # 2. exact names
        named = [a for a in open_ if a.name]
        ans_exact = self._lookup("names", [a.name for a in named], True)
        open_ = settle(named, lambda a: a.name, ans_exact, "name_exact") + [a for a in open_ if not a.name]
        # 3. fuzzy names, only where the exact lookup was answered and empty
        fuzzy = [a for a in open_ if a.name and ans_exact.get(a.name) == [] and clean_query(a.name)]
        ans_fuzzy = self._lookup("names", [clean_query(a.name) for a in fuzzy], False)
        settle(fuzzy, lambda a: clean_query(a.name), ans_fuzzy, "name_fuzzy")

        # ids of every candidate returned for each account, for evaluation
        self.pools = {a.id: {c.company_id for c in pool[a.id]} for a in accts}
        out = []
        for a in accts:
            if a.id in final:
                out.append(final[a.id])
                continue
            res = self.decide(a, pool[a.id])
            res["via"] = via.get(a.id, "")
            if not a.name and not a.use_host:
                res["reason"] = "no name or website"
            elif self.halted:
                asked_dom = (not a.use_host) or a.host in ans_dom
                asked_exact = (not a.name) or a.name in ans_exact
                asked_fuzzy = a.name not in ans_exact or ans_exact[a.name] != [] or not clean_query(a.name) or clean_query(a.name) in ans_fuzzy
                if not (asked_dom and asked_exact and asked_fuzzy):
                    res.update(status="not_attempted", reason="insufficient credits")
            out.append(res)

        groups = defaultdict(list)
        for r in out:
            if r["status"] == "matched":
                groups[r["crustdata_company_id"]].append(r["account_id"])
        dups = {cid: ids for cid, ids in groups.items() if len(ids) > 1}
        for r in out:
            r["duplicate_of"] = ""
            if r["status"] == "matched" and r["crustdata_company_id"] in dups:
                first = dups[r["crustdata_company_id"]][0]
                if first != r["account_id"]:
                    r["duplicate_of"] = first
        counts = defaultdict(int)
        for r in out:
            counts[r["status"]] += 1
        self.stats.update(
            matched=counts["matched"], review=counts["review"], unmatched=counts["unmatched"],
            not_attempted=counts["not_attempted"], duplicate_groups=len(dups), halted=self.halted,
            credits_estimated=round(self.stats["identifiers_matched"] * api.CREDITS_PER_MATCHED_IDENTIFIER, 1),
        )
        self.stats["credits_reported"] = round(self.stats["credits_reported"], 1)
        return out


def plan(rows):
    """Upper bound on requests and credits for a file, without sending anything."""
    accts = [Account(r) for r in rows]
    hosts = {a.host for a in accts if a.use_host and a.host not in PROFILE_HOSTS}
    names = {a.name for a in accts if a.name}
    batches = lambda k: (k + api.MAX_IDENTIFIERS - 1) // api.MAX_IDENTIFIERS
    worst_ids = len(hosts) + len(names)
    return {
        "accounts": len(accts), "distinct_hosts": len(hosts), "distinct_names": len(names),
        "requests_domain_pass": batches(len(hosts)),
        "max_requests": batches(len(hosts)) + 2 * batches(len(names)),
        "max_credits": round(worst_ids * api.CREDITS_PER_MATCHED_IDENTIFIER, 1),
    }


def read_accounts(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "name" not in reader.fieldnames:
            raise ValueError("%s needs a header row with at least a 'name' column (optional: id, website, employees)" % path)
        rows = []
        for i, row in enumerate(reader):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            row.setdefault("id", "")
            if not row["id"]:
                row["id"] = "row%d" % (i + 2)
            rows.append(row)
    return rows


def write_resolutions(path, rows, resolutions):
    fields = ["id", "name", "website", "status", "crustdata_company_id", "score", "via", "reason", "duplicate_of", "top_candidates"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row, res in zip(rows, resolutions):
            w.writerow({
                "id": row.get("id", ""), "name": row.get("name", ""), "website": row.get("website", ""),
                "status": res["status"], "crustdata_company_id": res["crustdata_company_id"] or "",
                "score": res["score"], "via": res.get("via", ""), "reason": res["reason"], "duplicate_of": res["duplicate_of"],
                "top_candidates": "; ".join("%s %s (%s) %.2f" % (c["crustdata_company_id"], c["name"], c["primary_domain"], c["score"]) for c in res["candidates"]),
            })


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    out_path = None
    if "--out" in flags:
        i = argv.index("--out")
        if i + 1 >= len(argv):
            print("--out needs a path", file=sys.stderr)
            return 2
        out_path = argv[i + 1]
        args = [a for a in args if a != out_path]
    if len(args) != 1:
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    try:
        rows = read_accounts(args[0])
    except (OSError, ValueError) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    if "--dry-run" in flags:
        for k, v in plan(rows).items():
            print("%-22s %s" % (k, v))
        return 0
    key = os.environ.get("CRUSTDATA_API_KEY", "")
    if not key:
        print("error: set CRUSTDATA_API_KEY, or pass --dry-run to see the plan without calling the API", file=sys.stderr)
        return 2
    resolver = Resolver(api.HttpTransport(key))
    try:
        resolutions = resolver.resolve(rows)
    except api.ApiError as err:
        print("error: API returned %s" % err, file=sys.stderr)
        return 1
    write_resolutions(out_path or "resolved.csv", rows, resolutions)
    for k, v in resolver.stats.items():
        print("%-22s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
