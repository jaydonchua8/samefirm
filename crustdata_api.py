"""Request building and response parsing for Crustdata's Company Identify API.

Written from the public docs (API version 2025-11-01):
  https://docs.crustdata.com/company-docs/identify/introduction.md
  https://docs.crustdata.com/api-reference/company-apis/identify-a-company-from-name-domain-id-or-profile-url.md

What the docs state and this module enforces or handles:
  - POST /company/identify, exactly one identifier array per request
  - at most 25 values per array, 26 or more is a 400
  - a no-match is a 200 with an empty `matches` list, and the spec also defines a 404
  - errors come back as {"error": {"type", "message", "metadata"}}
  - 0.2 credits per identifier that returns at least one match

HttpTransport has only been exercised against a stub opener in the tests. I do
not have an API key, so it has never been run against the live API.
"""
import json
import urllib.error
import urllib.request

API_VERSION = "2025-11-01"
BASE_URL = "https://api.crustdata.com"
IDENTIFY_PATH = "/company/identify"
MAX_IDENTIFIERS = 25
IDENTIFIER_KINDS = ("domains", "names", "crustdata_company_ids", "professional_network_profile_urls")
IDENTIFY_FIELDS = ("crustdata_company_id", "basic_info", "social_profiles")
CREDITS_PER_MATCHED_IDENTIFIER = 0.2
RETRYABLE = ("rate_limit_error", "service_unavailable", "internal_error")


class ApiError(Exception):
    """A non-success response. `type` is the documented error.type when the body had one."""

    def __init__(self, status, type_, message):
        Exception.__init__(self, "%s %s: %s" % (status, type_, message))
        self.status = status
        self.type = type_
        self.message = message


class Candidate:
    """One entry of `matches`, flattened to the fields the resolver reads."""

    __slots__ = ("company_id", "name", "primary_domain", "all_domains", "employee_count_range", "confidence", "rank")

    def __init__(self, company_id, name, primary_domain, all_domains, employee_count_range, confidence, rank):
        self.company_id = company_id
        self.name = name
        self.primary_domain = primary_domain
        self.all_domains = all_domains
        self.employee_count_range = employee_count_range
        self.confidence = confidence
        self.rank = rank


class IdentifyResult:
    __slots__ = ("matched_on", "match_type", "candidates")

    def __init__(self, matched_on, match_type, candidates):
        self.matched_on = matched_on
        self.match_type = match_type
        self.candidates = candidates


def build_identify_requests(kind, values, exact_match=None, fields=None):
    """Return a list of request bodies, each with one identifier array of at most 25 values.

    Duplicate values are sent once. Order of first appearance is kept so the
    requests are reproducible.
    """
    if kind not in IDENTIFIER_KINDS:
        raise ValueError("unknown identifier kind %r, expected one of %s" % (kind, ", ".join(IDENTIFIER_KINDS)))
    if exact_match not in (None, True, False):
        raise ValueError("exact_match must be True, False or None")
    if fields is not None:
        bad = [f for f in fields if f not in IDENTIFY_FIELDS]
        if bad:
            raise ValueError("identify does not accept fields %s" % bad)
    seen = set()
    unique = []
    for v in values:
        if kind == "crustdata_company_ids":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError("crustdata_company_ids must be integers, got %r" % (v,))
        else:
            if not isinstance(v, str) or not v.strip():
                raise ValueError("%s must be non-empty strings, got %r" % (kind, v))
        if v not in seen:
            seen.add(v)
            unique.append(v)
    bodies = []
    for i in range(0, len(unique), MAX_IDENTIFIERS):
        body = {kind: unique[i:i + MAX_IDENTIFIERS]}
        if exact_match is not None:
            body["exact_match"] = exact_match
        if fields is not None:
            body["fields"] = list(fields)
        bodies.append(body)
    return bodies


def _candidate(match, rank):
    """Return a Candidate, or None when the entry cannot be used."""
    if not isinstance(match, dict):
        return None
    data = match.get("company_data")
    if not isinstance(data, dict):
        return None
    info = data.get("basic_info")
    if not isinstance(info, dict):
        info = {}
    cid = data.get("crustdata_company_id", info.get("crustdata_company_id"))
    if isinstance(cid, bool) or not isinstance(cid, int):
        return None
    conf = match.get("confidence_score")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        conf = 0.0
    domains = info.get("all_domains")
    domains = [d for d in domains if isinstance(d, str)] if isinstance(domains, list) else []
    primary = info.get("primary_domain") if isinstance(info.get("primary_domain"), str) else ""
    if primary and primary not in domains:
        domains.insert(0, primary)
    name = info.get("name") if isinstance(info.get("name"), str) else ""
    ecr = info.get("employee_count_range") if isinstance(info.get("employee_count_range"), str) else ""
    return Candidate(cid, name, primary, domains, ecr, float(conf), rank)


def parse_identify_response(status, body):
    """Return (results, warnings) for a 200 or 404. Raise ApiError for anything else.

    results is a list of IdentifyResult. A 404 means "no data found" and gives
    an empty list, which callers treat the same as empty `matches`. Entries or
    matches that do not have the documented shape are dropped and counted in
    warnings rather than failing the whole batch.
    """
    if status == 404:
        return [], 0
    if status != 200:
        etype, message = "unknown", ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            etype = str(body["error"].get("type", "unknown"))
            message = str(body["error"].get("message", ""))
        raise ApiError(status, etype, message)
    if not isinstance(body, list):
        raise ApiError(status, "malformed_response", "expected a top-level array")
    results = []
    warnings = 0
    for entry in body:
        if not isinstance(entry, dict) or "matched_on" not in entry:
            warnings += 1
            continue
        matches = entry.get("matches")
        if not isinstance(matches, list):
            matches = []
            warnings += 1
        cands = []
        for rank, m in enumerate(matches):
            c = _candidate(m, rank)
            if c is None:
                warnings += 1
            else:
                cands.append(c)
        results.append(IdentifyResult(entry["matched_on"], str(entry.get("match_type", "")), cands))
    return results, warnings


def parse_employee_range(text):
    """'51-200' -> (51, 200), '10001+' -> (10001, None), '1' -> (1, 1), anything else -> None."""
    if not text:
        return None
    t = text.replace(",", "").replace(" ", "")
    try:
        if t.endswith("+"):
            return (int(t[:-1]), None)
        if "-" in t:
            lo, hi = t.split("-", 1)
            lo, hi = int(lo), int(hi)
            return (lo, hi) if lo <= hi else None
        return (int(t), int(t))
    except ValueError:
        return None


class HttpTransport:
    """POST JSON to the API. Returns (status, parsed body, headers dict with lowercase keys)."""

    def __init__(self, api_key, base_url=BASE_URL, opener=None, timeout=30):
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def headers(self):
        return {
            "authorization": "Bearer " + self._key,
            "x-api-version": API_VERSION,
            "content-type": "application/json",
        }

    def post(self, path, body):
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode("utf-8"), headers=self.headers(), method="POST")
        try:
            resp = self._open(req, timeout=self.timeout)
            status, raw, hdrs = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            status, raw, hdrs = err.code, err.read(), err.headers
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = None
        return status, parsed, {k.lower(): v for k, v in (hdrs.items() if hdrs else [])}
