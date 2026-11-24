"""Synthetic company records from three sources, with known ground truth.

Each true company appears in 1 to 3 sources. Each appearance is written the way
a different data vendor might write it. There are also look-alike companies
that must not be merged.
"""
import math
import random

SOURCES = ("registry", "jobs", "social")

ONSETS = ["b", "br", "d", "f", "g", "gr", "h", "k", "kl", "l", "m", "n", "p", "pr", "r", "s", "st", "t", "tr", "v", "w", "z", "ch", "sch"]
VOWELS = ["a", "e", "i", "o", "u", "ä", "ö", "ü", "é", "è", "au", "ei", "ou"]
CODAS = ["", "n", "r", "l", "s", "x", "nd", "rt", "ll", "ck", "m", "ß"]
SECTORS = [
    "Robotics", "Logistics", "Analytics", "Systems", "Technologies", "Energy", "Health",
    "Software", "Capital", "Biotech", "Mobility", "Security", "Networks", "Foods",
    "Pharma", "Solutions", "Consulting", "Media", "Aerospace", "Materials", "Labs",
    "International", "Group", "Services", "Manufacturing", "Maschinenbau", "Conseil",
    "Ingénierie", "Versicherung", "Bâtiment",
]
EXTRAS = ["Global", "Digital", "Nord", "Europe", "Partners", "Holdings", "Works", "Direct"]
COUNTRIES = {
    "US": (["San Francisco", "New York", "Austin", "Chicago", "Boston", "Seattle"], ["Inc", "Inc.", "Incorporated", "Corp", "LLC", ""], "com"),
    "DE": (["München", "Köln", "Berlin", "Hamburg", "Nürnberg", "Stuttgart"], ["GmbH", "AG", "GmbH & Co. KG", "UG", ""], "de"),
    "FR": (["Paris", "Lyon", "Marseille", "Toulouse", "Nantes", "Lille"], ["SAS", "S.A.S.", "SA", "SARL", ""], "fr"),
    "GB": (["London", "Manchester", "Leeds", "Bristol", "Edinburgh", "Cambridge"], ["Ltd", "Limited", "Ltd.", "PLC", ""], "co.uk"),
}
CITY_EXONYM = {"München": "Munich", "Köln": "Cologne", "Nürnberg": "Nuremberg"}
ABBREV_OUT = {"International": "Intl", "Technologies": "Tech", "Systems": "Sys", "Services": "Svcs", "Group": "Grp", "Manufacturing": "Mfg", "Labs": "Lab"}
SHARED_HOSTS = ["linkhub.example", "storefronts.example", "profiles.example"]
TRANSLIT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "â": "a"}


def make_head(rng):
    parts = []
    for _ in range(rng.choice((2, 2, 3))):
        parts.append(rng.choice(ONSETS) + rng.choice(VOWELS))
    return ("".join(parts) + rng.choice(CODAS)).capitalize()


def slug(text):
    out = []
    for ch in text.lower():
        ch = TRANSLIT.get(ch, ch)
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def typo(rng, word):
    if len(word) < 5:
        return word
    i = rng.randrange(1, len(word) - 1)
    kind = rng.choice(("drop", "swap", "dup"))
    if kind == "drop":
        return word[:i] + word[i + 1:]
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    return word[:i] + word[i] + word[i:]


def make_entities(rng, n, lookalike_rate=0.2):
    entities = []
    seen = set()
    while len(entities) < n:
        country = rng.choice(list(COUNTRIES))
        if entities and rng.random() < lookalike_rate:
            base = rng.choice(entities)
            head = base["head"]
            mode = rng.choice(("sector", "country", "extra"))
            if mode == "sector":
                words = [rng.choice(SECTORS)]
                country = base["country"]
            elif mode == "country":
                words = list(base["words"])
                others = [c for c in COUNTRIES if c != base["country"]]
                country = rng.choice(others)
            else:
                words = list(base["words"]) + [rng.choice(EXTRAS)]
                country = base["country"]
            lookalike = mode
        else:
            head = make_head(rng)
            words = [rng.choice(SECTORS)]
            if rng.random() < 0.15:
                words.append(rng.choice(EXTRAS))
            lookalike = ""
        key = (head, tuple(words), country)
        if key in seen:
            continue
        seen.add(key)
        cities, suffixes, tld = COUNTRIES[country]
        label = slug(head + "".join(words))
        if lookalike == "country":
            label = label + "-" + country.lower()
        entities.append({
            "id": len(entities), "head": head, "words": words, "country": country,
            "city": rng.choice(cities), "suffix": rng.choice(suffixes),
            "domain": label + "." + (tld if rng.random() < 0.6 else rng.choice(("com", "io", "ai"))),
            "alias": (head[:2] + "".join(w[0] for w in words)).upper() + rng.choice(("X", "IO", "Q")),
            "lookalike": lookalike,
        })
    return entities


def render(rng, ent, source):
    """One vendor's view of the company. Returns (record, list of noise tags)."""
    tags = []
    head, words = ent["head"], list(ent["words"])
    if rng.random() < 0.04:
        name = ent["alias"]
        tags.append("alias")
    else:
        if rng.random() < 0.3 and any(c in TRANSLIT for c in head):
            style = rng.choice(("digraph", "strip"))
            head = "".join((TRANSLIT.get(c, c) if style == "digraph" else TRANSLIT.get(c, c)[0]) for c in head)
            tags.append("translit")
        if rng.random() < 0.1:
            new = typo(rng, head)
            if new != head:
                head = new
                tags.append("typo")
        if rng.random() < 0.25:
            new = [ABBREV_OUT.get(w, w) for w in words]
            if new != words:
                words = new
                tags.append("abbrev")
        name = " ".join([head] + words)
        r = rng.random()
        if r < 0.45:
            if ent["suffix"]:
                name += " " + ent["suffix"]
        elif r < 0.6:
            other = rng.choice(COUNTRIES[ent["country"]][1])
            if other:
                name += (", " if rng.random() < 0.5 else " ") + other
            tags.append("suffix")
        else:
            tags.append("suffix")
        if rng.random() < 0.08:
            name = "The " + name
        if rng.random() < 0.15:
            name = name.upper() if rng.random() < 0.5 else name.lower()
            tags.append("case")
    r = rng.random()
    if r < 0.33:
        domain = ""
        tags.append("no_domain")
    elif r < 0.37:
        domain = "https://" + rng.choice(SHARED_HOSTS) + "/" + slug(ent["head"]) + str(rng.randrange(100))
        tags.append("shared_host")
    elif r < 0.45:
        label = ent["domain"].split(".", 1)[0]
        domain = label + "." + rng.choice(("com", "io", "net", "eu"))
        if domain != ent["domain"]:
            tags.append("other_tld")
    else:
        domain = ent["domain"]
        if rng.random() < 0.5:
            domain = rng.choice(("https://www.", "http://", "www.")) + domain + rng.choice(("", "/", "/about"))
    city = ent["city"]
    if rng.random() < 0.3:
        city = ""
        tags.append("no_city")
    elif city in CITY_EXONYM and rng.random() < 0.5:
        city = CITY_EXONYM[city]
    country = ent["country"] if rng.random() < 0.9 else ""
    rec = {"source": source, "name": name, "domain": domain, "city": city, "country": country}
    return rec, tags


def generate(seed, n_entities=2000):
    """Return (records, truth, tags). truth[i] is the entity id of records[i]."""
    rng = random.Random(seed)
    entities = make_entities(rng, n_entities)
    rows = []
    for ent in entities:
        k = rng.choices((1, 2, 3), weights=(25, 40, 35))[0]
        for source in rng.sample(SOURCES, k):
            rec, tags = render(rng, ent, source)
            rows.append((rec, ent["id"], tags))
    rng.shuffle(rows)
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]



def generate_crm(seed, n_companies=3000, n_accounts=2000, in_index=0.85, lookalike_rate=0.2,
                 wrong_site=0.03, dup_rate=0.05, emp_missing=0.4):
    """Synthetic CRM account list plus the company universe it was drawn from.

    Returns (entities, index_ids, accounts, truth, tags).
      entities   every company that exists, each with a `headcount`
      index_ids  ids of the companies a data provider knows about
      accounts   CRM rows: {"id", "name", "website", "employees"}
      truth      truth[i] is the entity id behind accounts[i], or None when that
                 company is not in the index and the right answer is "no match"
      tags       noise applied to each row, for the breakdown in bench.py

    A CRM row is one sales rep's view: the name as they typed it, a website that
    may be missing, a link page or (wrong_site) another company's site, and a
    rough employee count that may be missing.
    """
    rng = random.Random(seed)
    entities = make_entities(rng, n_companies, lookalike_rate)
    extra = random.Random(seed * 7919 + 13)
    for ent in entities:
        ent["headcount"] = max(1, int(math.exp(extra.gauss(3.6, 1.7))))
    index_ids = {e["id"] for e in entities if extra.random() < in_index}
    indexed = [e for e in entities if e["id"] in index_ids]
    n_dup = int(n_accounts * dup_rate)
    chosen = extra.sample(entities, min(len(entities), n_accounts - n_dup))
    chosen += [extra.choice(chosen) for _ in range(n_dup)]
    extra.shuffle(chosen)
    accounts, truth, tags = [], [], []
    for i, ent in enumerate(chosen):
        rec, t = render(rng, ent, "crm")
        if rec["domain"] and extra.random() < wrong_site:
            other = extra.choice(indexed)
            if other["id"] != ent["id"]:
                rec["domain"] = other["domain"]
                t = [x for x in t if x not in ("shared_host", "other_tld")] + ["wrong_site"]
        employees = None
        if extra.random() >= emp_missing:
            employees = max(1, int(round(ent["headcount"] * extra.uniform(0.6, 1.6))))
        else:
            t.append("no_employees")
        accounts.append({"id": "A%05d" % i, "name": rec["name"], "website": rec["domain"], "employees": employees})
        truth.append(ent["id"] if ent["id"] in index_ids else None)
        tags.append(t + ([] if ent["id"] in index_ids else ["not_in_index"]))
    return entities, index_ids, accounts, truth, tags


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "crm":
        # python gen.py crm [seed] [n_accounts] > accounts.csv
        import csv
        seed = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        accounts = generate_crm(seed, max(60, n * 2), n)[2]
        out = csv.DictWriter(sys.stdout, fieldnames=["id", "name", "website", "employees"], lineterminator="\n")
        out.writeheader()
        for a in accounts:
            out.writerow({k: ("" if v is None else v) for k, v in a.items()})
        sys.exit(0)
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    records, truth, _ = generate(seed, n)
    json.dump(records, sys.stdout, ensure_ascii=False, indent=1)
