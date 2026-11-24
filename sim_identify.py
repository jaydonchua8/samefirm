"""Offline stand-in for POST /company/identify, used only for tests, the demo and the benchmark.

SYNTHETIC. This is not Crustdata's matcher and says nothing about how their
search ranks results. It exists because I have no API key, and the resolver
needs something that answers in the documented response shape.

What is taken from the public docs (see crustdata_api.py for the links):
  - request validation: exactly one identifier array, at most 25 values, 400 otherwise
  - response shape: [{"matched_on", "match_type", "matches": [{"confidence_score",
    "company_data": {"crustdata_company_id", "basic_info": {...}}}]}]
  - a no-match is a 200 with empty `matches` (optionally a 404, which the spec also defines)
  - exact_match on a name compares the stored name ignoring case only
  - a name lookup can return several companies with the same confidence_score
    (the docs' own "Serve Robotics" example returns three, all at 1)
  - a stored name can differ from the common spelling, for example no space
  - 0.2 credits per identifier that returns at least one match

What I made up, and vary in the benchmark instead of trusting:
  - fuzzy name retrieval: the query appears inside the stored name (confidence
    1.0), or word-level Jaccard >= 0.5 (confidence 0.5)
  - ordering inside a confidence tier: closest name length first, except that
    with probability rank_noise the tier is shuffled
  - at most 5 matches per identifier
"""
import random

import crustdata_api as api

RANGES = ((1, 1, "1"), (2, 10, "2-10"), (11, 50, "11-50"), (51, 200, "51-200"), (201, 500, "201-500"),
          (501, 1000, "501-1000"), (1001, 5000, "1001-5000"), (5001, 10000, "5001-10000"))
MAX_MATCHES = 5


def employee_range(headcount):
    for lo, hi, label in RANGES:
        if lo <= headcount <= hi:
            return label
    return "10001+"


def company(company_id, name, primary_domain, headcount, extra_domains=()):
    """A company_data object in the documented shape."""
    return {
        "crustdata_company_id": company_id,
        "basic_info": {
            "crustdata_company_id": company_id,
            "name": name,
            "primary_domain": primary_domain,
            "all_domains": [primary_domain] + list(extra_domains),
            "website": "https://" + primary_domain + "/",
            "employee_count_range": employee_range(headcount),
        },
    }


def company_id_for(entity_id):
    return 100000 + 7 * entity_id


def build_companies(entities, index_ids, seed, squash_rate=0.05, alt_domain_rate=0.5):
    """Turn gen.py entities into indexed companies. Returns a list of company_data."""
    rng = random.Random(seed * 104729 + 1)
    out = []
    for ent in entities:
        if ent["id"] not in index_ids:
            continue
        name = " ".join([ent["head"]] + list(ent["words"]))
        if rng.random() < squash_rate:
            name = name.replace(" ", "")
        elif ent["suffix"] and rng.random() < 0.5:
            name += " " + ent["suffix"]
        label = ent["domain"].split(".", 1)[0]
        extras = []
        if rng.random() < alt_domain_rate:
            alt = label + "." + rng.choice(("com", "io", "net", "eu"))
            if alt != ent["domain"]:
                extras.append(alt)
        out.append(company(company_id_for(ent["id"]), name, ent["domain"], ent["headcount"], extras))
    return out


def _host(value):
    h = value.strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0]
    return h[4:] if h.startswith("www.") else h


def _words(text):
    return ["".join(c for c in w if c.isalnum()) for w in text.lower().split()]


class SimTransport:
    """Same `post(path, body)` interface as crustdata_api.HttpTransport."""

    def __init__(self, companies, seed=0, rank_noise=0.3, no_match_as_404=False, fail_plan=None, credit_limit=None):
        self.companies = companies
        self.rank_noise = rank_noise
        self.no_match_as_404 = no_match_as_404
        # fail_plan: {request number (from 1): (status, error type)} to inject failures
        self.fail_plan = dict(fail_plan or {})
        self.credit_limit = credit_limit
        self.seed = seed
        self.requests = 0
        self.credits = 0.0
        self.log = []
        self._by_domain = {}
        self._by_id = {}
        self._lower = []
        for c in companies:
            info = c["basic_info"]
            self._by_id[c["crustdata_company_id"]] = c
            for d in info["all_domains"]:
                self._by_domain.setdefault(d.lower(), []).append(c)
            self._lower.append((info["name"].lower(), set(_words(info["name"])) - {""}, c))

    # ---- lookups ------------------------------------------------------
    def _domain(self, value, exact):
        host = _host(value)
        hits = self._by_domain.get(host, [])
        if exact:
            hits = [c for c in hits if c["basic_info"]["primary_domain"].lower() == host]
        return [(1.0, c) for c in hits]

    def _name(self, value, exact):
        q = value.strip().lower()
        if not q:
            return []
        if exact:
            return [(1.0, c) for low, _, c in self._lower if low == q]
        qwords = set(_words(q)) - {""}
        tiers = {1.0: [], 0.5: []}
        for low, words, c in self._lower:
            if q in low:
                tiers[1.0].append((abs(len(low) - len(q)), c))
            elif qwords and words:
                jac = len(qwords & words) / len(qwords | words)
                if jac >= 0.5:
                    tiers[0.5].append((-jac, c))
        out = []
        rng = random.Random("%s|%s" % (self.seed, q))
        for conf in (1.0, 0.5):
            tier = sorted(tiers[conf], key=lambda t: (t[0], t[1]["crustdata_company_id"]))
            if rng.random() < self.rank_noise:
                rng.shuffle(tier)
            out.extend((conf, c) for _, c in tier)
        return out[:MAX_MATCHES]

    # ---- transport ----------------------------------------------------
    @staticmethod
    def _error(status, type_, message):
        return status, {"error": {"type": type_, "message": message, "metadata": []}}, {}

    def post(self, path, body):
        self.requests += 1
        self.log.append(body)
        if self.requests in self.fail_plan:
            status, type_ = self.fail_plan[self.requests]
            return self._error(status, type_, "injected failure")
        if path != api.IDENTIFY_PATH:
            return self._error(404, "not_found", "unknown path")
        if not isinstance(body, dict):
            return self._error(400, "invalid_request", "body must be an object")
        kinds = [k for k in api.IDENTIFIER_KINDS if body.get(k) is not None]
        if len(kinds) != 1:
            return self._error(400, "invalid_request", "Exactly one identifier must be provided: crustdata_company_ids, names, domains, or professional_network_profile_urls")
        kind = kinds[0]
        values = body[kind]
        if not isinstance(values, list) or not values or len(values) > api.MAX_IDENTIFIERS:
            return self._error(400, "invalid_request", "between 1 and 25 identifiers are allowed")
        if self.credit_limit is not None and self.credits >= self.credit_limit:
            return self._error(402, "insufficient_credits", "credit limit reached")
        exact = body.get("exact_match")
        out = []
        billed = set()
        for v in values:
            if kind == "domains":
                hits = self._domain(str(v), exact is True)
            elif kind == "names":
                hits = self._name(str(v), exact is True)
            elif kind == "crustdata_company_ids":
                hits = [(1.0, self._by_id[v])] if v in self._by_id else []
            else:
                hits = []
            if hits:
                billed.add(v)
            out.append({
                "matched_on": v,
                "match_type": {"domains": "domain", "names": "name", "crustdata_company_ids": "crustdata_company_id"}.get(kind, "professional_network_profile_url"),
                "matches": [{"confidence_score": conf, "company_data": c} for conf, c in hits],
            })
        used = api.CREDITS_PER_MATCHED_IDENTIFIER * len(billed)
        self.credits += used
        if not billed and self.no_match_as_404:
            return self._error(404, "not_found", "No data found")
        return 200, out, {"x-credits-used": "%g" % used}
