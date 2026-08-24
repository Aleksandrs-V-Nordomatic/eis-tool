"""One Lithuanian day, delivered: `day.json`, `changes.json`, and a home per procurement.

This is `batch.run` and `collect_day` for Lithuania, and it is one file because the two
things that made them large on the Latvian side are absent here. There is no register to
resolve against, so discovery is a window; and there is no address lottery, so there are no
shards to reconcile — a shard exists to let four runners draw four addresses at a portal
that refuses a third of them, and EPPS refuses none.

WHAT A DAY IS. The same two files a reader already knows: `changes.json` says what moved,
`day.json` says what the day contains and is written LAST, so a reader that lists folders
instead of reading it reads the wrong day. Neither holds tender bytes; both point at
`tenders/<pid>/`, which is permanent and shared across days.

WHAT MOVED IS ASKED TWICE, ON PURPOSE. `changes.fingerprint` compares the bytes of every
document, which is the floor and is what makes *the extractor rendered it differently* a
different sentence from *the buyer replaced it*. On top of that, Lithuania states the answer
outright: every catalogue row carries the number of the amendment that placed it. So a
record can say WHICH document moved and under which amendment, instead of only that
something did — which is the whole difference between an update a person can act on and one
that says a file changed.

    python3 lt_day.py 2026-08-20 --out work/LT --limit 5
"""
import argparse
import json
import os
import time

import lt_fetch
import lt_page
import lt_targets

try:
    import changes as changes_mod
except Exception:
    changes_mod = None

try:
    import batch as batch_mod
except Exception:
    batch_mod = None


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def run(date, out_root, limit=None, keep=None, run_id=None, policy=None):
    """Fetch the window's procurements into homes and write the day's two files."""
    run_id = run_id or time.strftime("%Y%m%dT%H%M%S+0300", time.localtime())
    stamp = date.replace("-", "/") if "-" not in date else date
    y, m, d = (date.split("-") if "-" in date else reversed(date.split("/")))
    window = "%s/%s/%s" % (d, m, y)

    targets = lt_targets.day(window, window)
    if keep:
        targets = [t for t in targets if t["kind"] in keep]
    if limit:
        targets = targets[:limit]

    # THE GATE FIRES BEFORE A BYTE MOVES. A card costs about 30 KB and an archive costs
    # megabytes, so the day reads every card, decides, and only then asks for the tenders
    # that survived. It is `batch.outside_scope` unchanged — CPV is European and the policy
    # is a file, so the gate never needed a country of its own.
    rules = batch_mod.load_policy(policy) if batch_mod is not None else None

    moves, delivered, failed, gated = [], [], [], []
    for target in targets:
        pid = target["pid"]
        home = os.path.join(out_root, "tenders", pid)
        previous = _read(os.path.join(home, "state.json"))

        notice = None
        if rules is not None:
            try:
                notice = lt_page.notice_only(pid, target["kind"])
            except Exception as exc:
                failed.append({"pid": pid, "kind": target["kind"],
                               "reason": "card unreadable: %s" % str(exc)[:160]})
                continue
            if notice is None:
                failed.append({"pid": pid, "kind": target["kind"],
                               "reason": "not published, or EPPS answered with a login form"})
                continue
            if batch_mod.outside_scope(notice, rules):
                # Named, never merely dropped. A tender nobody fetched and nobody mentioned
                # reads exactly like a tender that does not exist.
                gated.append({"pid": pid, "kind": target["kind"], "title": target["title"],
                              "buyer": target["buyer"],
                              "cpv": batch_mod.cpv_codes(notice)})
                continue

        try:
            done = lt_fetch.fetch(pid, out_root, target["kind"], notice=notice)
        except Exception as exc:                      # one tender must not lose the day
            failed.append({"pid": pid, "kind": target["kind"], "reason": str(exc)[:200]})
            continue

        record = None
        if changes_mod is not None and done.get("state") is not None:
            record = changes_mod.diff(previous, done["state"], date=date, run_id=run_id)
        status = (record or {}).get("status") or ("new" if previous is None else "changed")
        # Lithuania says it outright, so the record carries it beside the byte comparison.
        amendments = sorted({x["amendment"] for x in done["index"]["documents"]
                             if x.get("amendment")}, key=str)
        moves.append({
            "pid": pid, "kind": target["kind"], "status": status,
            "title": target["title"], "buyer": target["buyer"],
            "published": target["published"], "deadline": done.get("deadline"),
            "cpv_main": done.get("cpv_main"),
            "amendments": amendments,
            "documents": done["documents"],
            "uncatalogued": done["uncatalogued"],
            "moved": (record or {}).get("moves") or [],
        })
        delivered.append({
            "pid": pid, "kind": target["kind"], "status": status,
            "home": "tenders/%s" % pid,
            "documents": done["documents"], "bytes": done["bytes"],
            "title": target["title"],
        })

    by_status = {}
    for row in moves:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    changes = {
        "schema": "day-changes/1", "date": date, "country": "LT", "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": not failed,
        "counts": dict(by_status, tenders=len(moves), gated=len(gated)),
        "gated": gated,
        "tenders": moves,
    }
    _write(os.path.join(out_root, date, "changes.json"), changes)

    day = {
        "schema": "day/1", "date": date, "country": "LT", "source": "EPPS",
        "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "changes_path": "%s/changes.json" % date,
        "tenders_path": "tenders",
        # A day is complete when every target the window named reached a home. There are no
        # shards to be missing, so this is the only way it can be short.
        "complete": not failed,
        "coverage": {"targets": len(targets), "delivered": len(delivered),
                     "gated": len(gated), "failed": len(failed)},
        "counts": dict(by_status, tenders=len(delivered), gated=len(gated),
                       documents=sum(t["documents"] for t in delivered),
                       bytes=sum(t["bytes"] for t in delivered)),
        "lost": failed,
        "tenders": delivered,
    }
    _write(os.path.join(out_root, date, "day.json"), day)      # last, as the contract says
    return day, changes


def main(argv=None):
    ap = argparse.ArgumentParser(description="One Lithuanian day into the delivery shape.")
    ap.add_argument("date", help="yyyy-mm-dd")
    ap.add_argument("--out", default="work/LT")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", choices=("tender", "consultation"), default=None)
    ap.add_argument("--policy", default=None,
                    help="recall policy: JSON, a path to one, or EIS_POLICY from "
                         "the environment. Absent means fetch everything.")
    args = ap.parse_args(argv)
    day, changes = run(args.date, args.out, args.limit,
                       keep=(args.only,) if args.only else None, policy=args.policy)
    print("%s: %d/%d delivered, %d gated, %d document(s), %.1f MB — %s"
          % (day["date"], day["coverage"]["delivered"], day["coverage"]["targets"],
             day["coverage"]["gated"],
             day["counts"]["documents"], day["counts"]["bytes"] / 1048576.0,
             "complete" if day["complete"] else "SHORT"))
    for row in changes["tenders"]:
        flag = (" · поправки %s" % ",".join(row["amendments"])) if row["amendments"] else ""
        print("  %-6s %-12s %s%s" % (row["status"], row["kind"], (row["title"] or "")[:52],
                                     flag))
    return day


if __name__ == "__main__":
    main()
