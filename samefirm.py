"""samefirm: decide which company records from different sources are the same company.

Standard library only. See README.md.
"""
import json
import math
import sys
import unicodedata
from collections import defaultdict

LEGAL = {
    "inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited", "gmbh",
    "ag", "ug", "kg", "sa", "sas", "sarl", "se", "co", "company", "plc", "bv",
    "mbh", "cie", "the", "and", "et", "und",
}
ABBREV = {
    "intl": "international", "tech": "technologies", "technology": "technologies",
    "sys": "systems", "svcs": "services", "grp": "group", "labs": "laboratories",
    "lab": "laboratories", "mfg": "manufacturing", "solns": "solutions",
}
CITY_ALIASES = {
    "munchen": "munich", "koln": "cologne", "nurnberg": "nuremberg",
    "wien": "vienna", "geneve": "geneva", "zurich": "zurich",
}

# Tuned on gen.py seed 0 only. bench.py reports on other seeds.
ACCEPT = 0.62
MAX_BLOCK = 60


def fold(text):
    """Lowercase, strip accents, map German digraphs so Mueller == Müller == Muller."""
    text = text.lower().replace("ß", "ss")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    for a, b in (("ae", "a"), ("oe", "o"), ("ue", "u")):
        text = text.replace(a, b)
    return text


def name_tokens(name):
    text = fold(name).replace("&", " ").replace(".", "")
    text = "".join(c if c.isalnum() else " " for c in text)
    out = []
    for tok in text.split():
        tok = ABBREV.get(tok, tok)
        if tok in LEGAL:
            continue
        out.append(tok)
    return out


def domain_parts(url):
    """Return (host, label). label is the part before the public suffix, roughly."""
    if not url:
        return ("", "")
    host = url.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    bits = host.split(".")
    if len(bits) >= 3 and bits[-2] in ("co", "com") and len(bits[-1]) == 2:
        label = bits[-3]
    elif len(bits) >= 2:
        label = bits[-2]
    else:
        label = host
    return (host, label)


def city_key(city):
    key = fold(city or "").strip()
    return CITY_ALIASES.get(key, key)


def jaro_winkler(a, b):
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    if window < 0:
        window = 0
    ma, mb = [False] * la, [False] * lb
    matches = 0
    for i in range(la):
        lo, hi = max(0, i - window), min(lb, i + window + 1)
        for j in range(lo, hi):
            if not mb[j] and a[i] == b[j]:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    trans, k = 0, 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            if a[i] != b[k]:
                trans += 1
            k += 1
    jaro = (matches / la + matches / lb + (matches - trans / 2) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def soft_token_sim(ta_list, tb_list, idf, default_idf):
    """IDF-weighted soft token overlap between two token lists, in 0..1.

    Each token of the first list is paired with its best unused partner in the
    second. A pair counts when it is equal or its Jaro-Winkler similarity is at
    least 0.9, so one typo does not lose the word. Unpaired tokens on either
    side add their weight to the denominator only.
    """
    if not ta_list or not tb_list:
        return 0.0
    used = set()
    shared = 0.0
    total = 0.0
    for ta in ta_list:
        wa = idf.get(ta, default_idf)
        best, best_j = 0.0, -1
        for j, tb in enumerate(tb_list):
            if j in used:
                continue
            s = 1.0 if ta == tb else jaro_winkler(ta, tb)
            if s > best:
                best, best_j = s, j
        if best >= 0.9:
            used.add(best_j)
            wb = idf.get(tb_list[best_j], default_idf)
            w = min(wa, wb)
            shared += w * best
            total += w
        else:
            total += wa
    for j, tb in enumerate(tb_list):
        if j not in used:
            total += idf.get(tb, default_idf)
    return shared / total if total else 0.0


class Prepared:
    __slots__ = ("idx", "rec", "tokens", "host", "label", "city", "country", "source")

    def __init__(self, idx, rec):
        self.idx = idx
        self.rec = rec
        self.tokens = name_tokens(rec.get("name", ""))
        self.host, self.label = domain_parts(rec.get("domain", ""))
        self.city = city_key(rec.get("city", ""))
        self.country = (rec.get("country") or "").upper()
        self.source = rec.get("source", "")


class Linker:
    def __init__(self, records, accept=ACCEPT, one_per_source=True, max_block=MAX_BLOCK):
        self.items = [Prepared(i, r) for i, r in enumerate(records)]
        self.accept = accept
        self.one_per_source = one_per_source
        self.max_block = max_block
        n = max(1, len(self.items))
        df = defaultdict(int)
        hosts = defaultdict(int)
        for it in self.items:
            for tok in set(it.tokens):
                df[tok] += 1
            if it.host:
                hosts[it.host] += 1
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.default_idf = math.log(1 + n)
        # A host used by more records than there are sources is a shared host
        # (a link page, a marketplace profile), not evidence of identity.
        n_sources = max(1, len({it.source for it in self.items}))
        self.shared_hosts = {h for h, c in hosts.items() if c > n_sources}
        self.stats = {}

    # ---- pair scoring -------------------------------------------------
    def name_sim(self, a, b):
        """IDF-weighted soft token overlap. A typo'd token still matches at JW >= 0.9."""
        return soft_token_sim(a.tokens, b.tokens, self.idf, self.default_idf)

    def score(self, a, b):
        """Return (score in 0..1, reason string)."""
        ns = self.name_sim(a, b)
        a_dom = a.host and a.host not in self.shared_hosts
        b_dom = b.host and b.host not in self.shared_hosts
        country_conflict = bool(a.country and b.country and a.country != b.country)
        city_agree = bool(a.city and b.city and a.city == b.city)
        city_conflict = bool(a.city and b.city and a.city != b.city)

        if a_dom and b_dom:
            if a.host == b.host:
                return (0.97 if ns >= 0.3 else 0.9, "same domain")
            if a.label == b.label:
                # atlas.de vs atlas.com: same label, different suffix
                # A long label is unlikely to be shared by accident; a short one is not.
                base = 0.66 if len(a.label) >= 8 else 0.45
                s = base + 0.3 * ns - (0.15 if country_conflict else 0.0)
                return (s, "same domain label")
            # Two different real domains is strong evidence against.
            s = 0.5 * ns + (0.1 if city_agree else 0.0) - 0.15
            return (s, "different domains")

        # At most one usable domain: fall back to name and location.
        s = 0.78 * ns
        if city_agree:
            s += 0.2
        elif city_conflict:
            s -= 0.25
        elif not country_conflict and a.country and b.country:
            s += 0.06
        if country_conflict:
            s -= 0.35
        # a record's name squashed into the other's domain label, e.g. "Atlas Robotics" ~ atlasrobotics.io
        for x, y in ((a, b), (b, a)):
            if y.label and y.host not in self.shared_hosts and "".join(x.tokens) == fold(y.label):
                if len(y.label) >= 8 and not country_conflict and not city_conflict:
                    return (max(s, 0.8), "name equals domain label")
                s += 0.1
                break
        return (s, "name and location")

    # ---- blocking -----------------------------------------------------
    def block_keys(self, it):
        keys = set()
        if it.label and it.host not in self.shared_hosts:
            keys.add("d:" + fold(it.label))
        joined = "".join(it.tokens)
        if joined:
            keys.add("d:" + joined)
        for tok in it.tokens:
            if len(tok) >= 4:
                keys.add("p:" + tok[:4])
                keys.add("s:" + tok[-4:])
            else:
                keys.add("t:" + tok)
            # whole token plus the first letter of each other token, so a typo
            # in one word can still meet its partner through the other word
            for other in it.tokens:
                if other is not tok:
                    keys.add("c:" + tok + "|" + other[0])
        return keys

    def candidates(self):
        blocks = defaultdict(list)
        for it in self.items:
            for key in self.block_keys(it):
                blocks[key].append(it.idx)
        pairs = set()
        skipped = 0
        for key, members in blocks.items():
            if len(members) > self.max_block and not key.startswith("d:"):
                skipped += 1
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    pairs.add((members[i], members[j]))
        self.stats["blocks"] = len(blocks)
        self.stats["blocks_skipped"] = skipped
        self.stats["candidate_pairs"] = len(pairs)
        return pairs

    # ---- clustering ---------------------------------------------------
    def link(self):
        """Return (clusters, accepted_pairs). clusters is a list of lists of record indexes."""
        scored = []
        for i, j in self.candidates():
            a, b = self.items[i], self.items[j]
            if self.one_per_source and a.source == b.source:
                continue
            s, why = self.score(a, b)
            if s >= self.accept:
                scored.append((s, i, j, why))
        scored.sort(key=lambda t: (-t[0], t[1], t[2]))
        parent = list(range(len(self.items)))
        sources = [{it.source} for it in self.items]
        labels = [({fold(it.label)} if it.host and it.host not in self.shared_hosts else set()) for it in self.items]

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        accepted = []
        blocked = 0
        for s, i, j, why in scored:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            # Best pairs merge first. A later, weaker pair may not glue together
            # two clusters that already disagree.
            if self.one_per_source and sources[ri] & sources[rj]:
                blocked += 1
                continue
            if labels[ri] and labels[rj] and not (labels[ri] & labels[rj]):
                blocked += 1
                continue
            parent[rj] = ri
            sources[ri] |= sources[rj]
            labels[ri] |= labels[rj]
            accepted.append({"a": i, "b": j, "score": round(s, 3), "why": why})
        groups = defaultdict(list)
        for i in range(len(self.items)):
            groups[find(i)].append(i)
        self.stats["accepted_pairs"] = len(accepted)
        self.stats["merges_blocked"] = blocked
        return (sorted(groups.values()), accepted)


def link_records(records, **kw):
    linker = Linker(records, **kw)
    clusters, pairs = linker.link()
    return clusters, pairs, linker.stats


def main(argv):
    if len(argv) != 2:
        print("usage: python samefirm.py records.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        records = json.load(fh)
    clusters, pairs, stats = link_records(records)
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        print("same company:")
        for i in cluster:
            r = records[i]
            print("  [%s] %s | %s | %s %s" % (r.get("source", ""), r.get("name", ""), r.get("domain", "") or "-", r.get("city", "") or "-", r.get("country", "") or "-"))
    singles = sum(1 for c in clusters if len(c) == 1)
    print("%d records, %d clusters, %d unmatched, %d candidate pairs scored" % (len(records), len(clusters), singles, stats["candidate_pairs"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
