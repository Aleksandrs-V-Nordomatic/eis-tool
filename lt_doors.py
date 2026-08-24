"""The standing doors: dynamic purchasing systems and qualification systems.

Neither is a tender and neither is a stream. A DPS (VPĮ 79 str.) and a KVS — the utilities'
equivalent under PĮ — are pools a buyer opens for a category and keeps open for years.
Applications are accepted for the whole life of the system and the qualification bar cannot
be changed part-way, so there is no deadline to miss and nothing to hurry. What there is, is
a door: qualify once and every actual purchase inside that category arrives as an invitation
afterwards, none of which is ever advertised.

WHY THEY NEED THEIR OWN COMMAND. A nightly window would show a DPS on the one day it was
created and never again, which is exactly backwards — the day it was created is the least
useful day to hear about it, and every day after is equally good. So these are enumerated as
a stock, on demand, and what a reader wants from them is a list to work through once rather
than a card each morning.

AND THEY HAVE NO CODES, which shapes what can be asked of them. A door is linked from the
results table to its own view, and that view answers 500 — anonymously and inside the search
session alike. So there is no card, no BVPŽ, and the gate runs on the title alone; the
absence is recorded as `cpv_available: false` rather than as an empty list, because a reader
must not take a missing code for a code that failed to match. The documents are reachable:
`listContractDocuments` and the archive answer for a door id exactly as for a tender, and
that is where the qualification requirements actually are.

    python3 lt_doors.py --out work/LT --policy lt_policy.example.json
"""
import argparse
import json
import os
import time

import lt_page
import lt_targets

try:
    import batch as batch_mod
except Exception:
    batch_mod = None

KINDS = {"dps": (lt_targets.DPS, "Dinaminė pirkimo sistema"),
         "kvs": (lt_targets.QS, "Kvalifikacijos reikalavimų sistema")}

# A system the buyer withdrew still sits in the list, saying so in its own title. Applying
# to one is wasted paperwork, so they are dropped by name — the only place in this tool
# where a title decides anything, and it is the portal's word rather than a judgement.
RETIRED = ("NEGALIOJANTIS", "NUTRAUKTAS", "PANAIKINTAS", "ATŠAUKTAS")


def enumerate_doors(which, session=None):
    procedure, label = KINDS[which]
    rows = lt_targets.targets_from(None, None, procedure, which, session=session)
    return [r for r in rows
            if not any(word in (r["title"] or "").upper() for word in RETIRED)], label


def harvest(out_root, policy=None, kinds=("dps", "kvs"), limit=None):
    rules = batch_mod.load_policy(policy) if batch_mod is not None else None
    session = lt_targets.Session()
    base = os.path.join(out_root, "doors")
    os.makedirs(base, exist_ok=True)

    kept, counts, failed = [], {}, []
    for which in kinds:
        rows, label = enumerate_doors(which, session)
        counts[which] = {"open": len(rows)}
        if limit:
            rows = rows[:limit]
        matched = 0
        for row in rows:
            # NO CARD, SO NO CODES. The door's own view answers 500 (see `lt_page.DOOR`),
            # so the gate runs on the title alone here. `outside_scope` already handles a
            # notice with no codes the right way — the code veto needs codes to apply, and
            # the recall test does not — so this is the same gate, given less to work with.
            candidate = {"title": row["title"], "cpv": []}
            if rules is not None and batch_mod.outside_scope(candidate, rules):
                continue
            matched += 1
            kept.append({
                "pid": row["pid"], "door": which, "door_label": label,
                "title": row["title"],
                "buyer": row["buyer"],
                "published": row.get("published"),
                "status": row.get("status"),
                # Said out loud rather than left null-shaped: a reader must not take an
                # absent code for a code that did not match.
                "cpv_main": None,
                "cpv": [],
                "cpv_available": False,
                # A door has no submission deadline by design — applications stay open for
                # the life of the system. Whatever the table prints here is the system's
                # own end date, not a date to hurry for.
                "deadline": row.get("deadline"),
                "documents": lt_page.DOCS % row["pid"],
                "archive": lt_page.ARCHIVE % row["pid"],
                "link": lt_page.DOOR % row["pid"],
            })
        counts[which]["ours"] = matched

    path = os.path.join(base, "doors.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    index = {
        "schema": "doors/1", "country": "LT", "source": "EPPS",
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": counts, "kept": len(kept), "failed": failed,
        "doors_path": "doors/doors.jsonl",
        "_what": "Systems to apply into, not tenders to bid on. Applications stay open for "
                 "the life of each system and the qualification bar cannot change, so this "
                 "is a list to work through once.",
    }
    with open(os.path.join(base, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return index, kept


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lithuanian DPS and KVS worth applying into.")
    ap.add_argument("--out", default="work/LT")
    ap.add_argument("--policy", default=None)
    ap.add_argument("--only", choices=sorted(KINDS), default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    kinds = (args.only,) if args.only else tuple(sorted(KINDS))
    index, kept = harvest(args.out, args.policy, kinds, args.limit)
    for which in kinds:
        c = index["counts"].get(which, {})
        print("%s: %d open, %d ours" % (which.upper(), c.get("open", 0), c.get("ours", 0)))
    for row in kept[:14]:
        print("  %-4s %-9s %-30s %s" % (row["door"].upper(), row["cpv_main"] or "—",
                                        (row["buyer"] or "")[:30], (row["title"] or "")[:52]))
    return index


if __name__ == "__main__":
    main()
