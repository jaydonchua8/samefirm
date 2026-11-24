# samefirm

Match a CRM account list to `crustdata_company_id` values through Crustdata's Company Identify API, and decide for each account whether the match is safe to write back, needs a person to look at it, or is not there.

This is an independent prototype. It is not affiliated with, reviewed by or endorsed by Crustdata. I do not have an API key, so nothing here has been run against the live API. Every company, account and benchmark number below is synthetic.

## The problem

A customer who buys company data usually starts with a list of accounts from their own CRM: a name typed by a sales rep, sometimes a website, sometimes a rough employee count. Before any enrichment is useful, each row has to be tied to the provider's ID for that company. Getting a row wrong is worse than leaving it empty, because every later field (headcount, funding, hiring) is then written onto the wrong account and nobody notices.

## Why this matters to Crustdata specifically

These points come from Crustdata's public docs and job postings, which I read in November 2026.

- The Identify endpoint (`POST /company/identify`) takes domains, names, profile URLs or IDs and returns ranked candidates with a `confidence_score`. The docs say name lookups often return several candidates, that the first one is not always correct, and that the caller should compare `primary_domain` and `employee_count_range` to pick. Their own example, a lookup for the name "Serve Robotics", returns three different companies, all with a confidence score of 1. ([Identify guide](https://docs.crustdata.com/company-docs/identify/introduction.md), [API reference](https://docs.crustdata.com/api-reference/company-apis/identify-a-company-from-name-domain-id-or-profile-url.md))
- The same guide suggests trying `exact_match: true` first, retrying with `exact_match: false` when nothing comes back, and verifying the result before using it. It also says a request takes one identifier type and at most 25 values, that a no-match is a 200 with empty `matches` while the spec also defines a 404, and that an identifier costs 0.2 credits only when it matches.
- The Software Engineering Intern (Forward Deployed) posting lists connecting Crustdata to customer systems, implementing integrations, and turning prototypes into repeatable reference implementations. ([posting](https://www.ycombinator.com/companies/crustdata/jobs/xNllWNH-software-engineering-intern-forward-deployed-summer-2026))

So the docs describe a verification step and leave it to each customer to write. This repo is my attempt at that step as a reusable piece: the batching, the lookup order, the comparison, and a clear answer per account.

## What it does

`resolve.py` takes rows with `id`, `name`, `website` and `employees` (only `name` is required) and returns one of four states per row.

| state | meaning |
|---|---|
| `matched` | one candidate is clearly best, with the `crustdata_company_id` |
| `review` | a plausible candidate exists but is not safe to accept, with the top three candidates and a reason |
| `unmatched` | nothing returned is close |
| `not_attempted` | the run stopped because credits ran out before this row was looked up |

It also flags accounts that resolved to the same company as duplicates of each other.

`samefirm.py` is the earlier half of the project: it links company records from several sources with blocking, pair scoring and union-find clustering. The resolver reuses its name normalization, domain parsing and name similarity.

## Architecture

| file | role |
|---|---|
| `crustdata_api.py` | Builds Identify request bodies (one identifier type, batches of 25, duplicates sent once), parses responses and error bodies, and holds a small `urllib` transport |
| `resolve.py` | The resolver: lookup order, scoring, the accept / review / unmatched decision, retries, credit accounting, CSV in and out |
| `samefirm.py` | Name and domain normalization, Jaro-Winkler, IDF-weighted name similarity, and the multi-source record linker |
| `sim_identify.py` | Offline stand-in for the endpoint that answers in the documented response shape. Synthetic, not Crustdata's matcher |
| `gen.py` | Synthetic companies, multi-source records and CRM account lists with known answers |
| `bench.py` | Benchmarks for the linker and the resolver, and the threshold grid search |
| `demo.py` | A short offline run on eleven hand-written accounts |
| `test_resolve.py`, `test_samefirm.py` | Tests |

The resolver talks to anything with a `post(path, body)` method that returns `(status, body, headers)`. `HttpTransport` and `SimTransport` both do.

Lookup order per file:

1. `domains` for accounts with a usable website.
2. `names` with `exact_match: true` for accounts still open, sending the name as typed.
3. `names` with `exact_match: false`, only for names whose exact lookup came back empty, sending the name with the legal form removed (`The Müller Robotik GmbH & Co. KG` becomes `Müller Robotik`).

Every candidate an account has seen is scored on name similarity, whether its domains include the account's website, whether the domain label matches on another TLD, and whether the CRM employee count fits `employee_count_range`. An account is `matched` only when the best score is at least 0.85 and beats the runner-up by 0.05.

## Key decisions and tradeoffs

- A name match alone is not accepted. With no website and no employee count, an exact name match scores 0.80 and goes to review. On the dev split, accepting those (threshold 0.75) raised recall from 69.8% to 78.7% and wrong links from 5 to 17. When I read through them, nearly all were a different company with the same or almost the same name. I chose the stricter setting with the rule "highest recall with at most 0.5% wrong links", applied to the dev split only.
- A domain hit with an unrelated name goes to review. It is either a trade name on the right site or the wrong website in the CRM, and I could not find a field in the Identify response that tells them apart. Trusting the domain in that case cut review rows from 801 to 582 on the held-out split and raised wrong links from 13 to 92.
- Link pages are found from the file. A host that appears on three or more accounts with a different path and a different name each time is treated as shared and never sent. My first rule only counted names per host, and it threw away a real company domain that was listed on three accounts in the demo, so I added the path condition.
- The exact lookup is sent before the fuzzy one, as the docs suggest. Unmatched identifiers are free, so the extra request costs time against the rate limit and no credits.
- A 429, 500 or 503 is retried with backoff, or after `retry-after` when the header is present. A 402 stops the run and marks the remaining rows `not_attempted` so a partial file is still usable. A 400, 401 or 403 is raised at once.
- The simulator's search is deliberately not the resolver's matcher. It uses plain substring and word overlap with no accent folding, so the resolver is not graded against its own logic.

## Install and usage

Python 3.9 or newer. Nothing to install.

```
python demo.py                      # offline, a few seconds
python test_resolve.py              # 44 tests
python test_samefirm.py             # 22 tests
python bench.py resolve             # resolver benchmark, about a minute
python bench.py                     # linker benchmark, under a minute
python bench.py tune                # threshold grid on the dev split
python gen.py crm 0 50 > accounts.csv
python resolve.py accounts.csv --dry-run
python samefirm.py sample_records.json
```

`--dry-run` prints the request and credit ceiling for a file without sending anything. Without it, `resolve.py` reads `CRUSTDATA_API_KEY` from the environment, calls the live API and writes `resolved.csv`. I have not been able to run that path. The request it builds is checked in the tests against a stub (URL, method, the three headers, body).

```python
import crustdata_api, resolve

resolver = resolve.Resolver(crustdata_api.HttpTransport(api_key))
rows = resolver.resolve([{"id": "1", "name": "Atlas Robotics, Inc.", "website": "atlasrobotics.example", "employees": 150}])
```

## Example

`python demo.py`, first part. Nine made-up companies sit behind the simulated endpoint. Two share the name Northwind Analytics, and one is stored as `BergenTechnologies` with no space.

```
id  account                            status        id     via         score reason
1   Atlas Robotics, Inc.               matched       1001   domain      1.00  same domain
2   ATLAS ROBOTICS                     matched       1001   domain      1.00  same domain, duplicate of account 1
3   Northwind Analytics                matched       1002   name_exact  0.90  name and headcount
4   Northwind Analytics                review        -      name_exact  0.80  tie between candidates
5   The Müller Robotik GmbH & Co. KG   matched       1004   name_fuzzy  0.90  name and headcount
6   Halden Pharma                      review        -      name_exact  0.60  website differs
7   Serve Kitchens LLC                 matched       1006   name_fuzzy  0.90  name and headcount
8   Bergen Technologies Ltd            matched       1009   domain      1.00  same domain
9   Corvid Foods                       unmatched     -      -           0.00  no candidates
10  ARX                                review        -      domain      0.50  same domain, different name
11  Dunmore Media                      unmatched     -      -           0.00  no candidates

requests 3, credits 1.4, shared hosts ignored 1, duplicate groups 1
requests sent:
   {'domains': ['atlasrobotics.example', 'atlasrobotics-eu.example', 'bergentech.example']}
   {'names': ['Halden Pharma', 'ARX', 'Northwind Analytics', 'The Müller Robotik GmbH & Co. KG', 'Serve Kitchens LLC', 'Corvid Foods', 'Dunmore Media'], 'exact_match': True}
   {'names': ['ARX', 'Müller Robotik', 'Serve Kitchens', 'Corvid Foods', 'Dunmore Media'], 'exact_match': False}
```

Accounts 3 and 4 have the same name. Account 3 has an employee count of 300, which fits one Northwind (201-500) and not the other (2-10). Account 4 has nothing to separate them. Account 6 has Atlas Robotics' website on a Halden Pharma row. Accounts 7, 9 and 11 point at the same link page, so that host is skipped.

The second part of the demo gives the first request a 429 and caps the account at 0.4 credits. The resolver retries once, keeps the three matches it got from the domain lookup and marks the other eight rows `not_attempted`.

## Tests and verified results

All numbers are from runs on my machine in this repo. `bench_results.json` holds the raw output.

Tests: 66 pass (`python test_resolve.py` 44, `python test_samefirm.py` 22), and they also pass under `python -W error -m unittest`.

### Resolver, synthetic CRM lists

Each seed builds 3,000 companies, of which about 85% are in the simulated index, and 2,000 CRM accounts with noisy names, missing or wrong websites, link pages, rough or missing employee counts and 5% duplicate rows. About 20% of companies are look-alikes of another one (same head word and another sector, the same name in another country, or the same name plus one word). Thresholds were tuned on seed 0 only.

"First match" is the baseline: same lookup order, always take `matches[0]`.

Held out, same generator settings, seeds 1 to 3 (6,000 accounts, 5,071 with their company in the index):

| method | linked | wrong links | precision | recall | sent to review |
|---|---|---|---|---|---|
| first match | 4965 | 860 | 82.7% | 81.0% | 0 |
| resolver | 3624 | 13 | 99.6% | 71.2% | 801 (13.4%) |
| resolver, margin 0 | 3639 | 21 | 99.4% | 71.4% | 786 |
| resolver, trust domain alone | 3843 | 92 | 97.6% | 74.0% | 582 |

Held out, harder settings I did not tune on, seeds 4 and 5 (30% of companies missing from the index, 35% look-alikes, twice the wrong websites, 60% of employee counts missing, more shuffling in the simulated ranking; 4,000 accounts, 2,753 in the index):

| method | linked | wrong links | precision | recall | sent to review |
|---|---|---|---|---|---|
| first match | 2963 | 848 | 71.4% | 76.8% | 0 |
| resolver | 1752 | 7 | 99.6% | 63.4% | 805 (20.1%) |

Dev split, seed 0, for comparison: resolver 99.6% precision, 69.8% recall, 5 wrong links, 292 in review.

How to read these:

- Recall is capped by the simulated search, not by the resolver. On seeds 1 to 3 the true company came back in some response for 4,266 of 5,071 in-index accounts (84.1%), and the resolver linked 3,611 of them correctly.
- Of the 801 review rows on seeds 1 to 3, 654 show the true company in the top three candidates.
- The resolver used 905.0 credits on seeds 1 to 3 by the documented 0.2 per matched identifier, against 968.6 for the baseline.
- The baseline's error rate depends on how many look-alikes and wrong websites I put in the data and on how I made the simulator rank. It shows the direction, not the size, of the effect on real data.

### Linker, synthetic multi-source records

`python bench.py`, seeds 1 to 5, 20,924 records from three sources, 14,395 true pairs. Thresholds tuned on seed 0.

| method | precision | recall | F1 | wrongly merged pairs |
|---|---|---|---|---|
| exact name | 88.6% | 13.9% | 24.0% | 256 |
| normalized name | 82.3% | 72.9% | 77.3% | 2259 |
| normalized name or domain | 5.3% | 83.3% | 9.9% | 215173 |
| samefirm | 99.6% | 96.5% | 98.0% | 57 |
| samefirm, no source rule | 95.7% | 96.4% | 96.0% | 625 |

Blocking scored 258,593 candidate pairs out of 43,774,653 (99.4% skipped) and kept 96.5% of true pairs. These results are the same as before I added the resolver, which I checked by comparing `bench_results.json` before and after.

## Limitations and next steps

- Nothing has touched the live API. The response shape, limits, error bodies and credit rule come from the docs. How the real search ranks, how often it returns ties, and whether it folds accents are unknown to me, and those decide the real recall and the real size of the review queue.
- I wrote the data generator, the simulator and the resolver. The held-out seeds and the harder settings guard against tuning to one sample, not against my own blind spots about real CRM data.
- The weights in the score are set by hand and the two thresholds by a grid search. With a few hundred labelled review decisions from a real customer file I would fit them instead.
- Identify returns no location, so country and city are not used. Asking Enrich for `locations` on tied candidates would settle many of the "tie between candidates" rows at 2 credits per company, and I would add that as an optional step with a budget.
- Names are handled in Latin script only, and the name comparison treats `Serve` and `iServe` as one typo apart, which sends some rows to review that a person would accept.
- The client is sequential and does not pace itself to the per-minute rate limit. It reacts to 429s. A real integration needs a limiter and resumable progress for large files.
- The linker holds everything in memory and its `one_per_source` rule assumes a source does not list a company twice.
