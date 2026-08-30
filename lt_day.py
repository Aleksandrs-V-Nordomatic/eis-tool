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
import datetime
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


def resolve(pid):
    """Which of the three views answers for an id nobody told us the kind of.

    A card on the board carries `EPPS:<id>` and nothing else, because that is all the key
    needs to be. The kind — competition, market consultation, door — decides which view
    serves it, so it is asked of the portal rather than stored: a kind kept on the card
    would be one more thing that can be wrong, and the three requests cost one page each.
    """
    for kind in ("tender", "consultation", "door"):
        try:
            notice = lt_page.notice_only(pid, kind)
        except Exception:
            notice = None
        if notice is not None:
            return kind, notice
    return None, None


def run(date, out_root, limit=None, keep=None, run_id=None, policy=None, watch=None):
    """Fetch the window's procurements into homes and write the day's two files.

    `watch` is the ids somebody is still deciding about — the cards whose `Lēmums` has not
    been settled. They ride in the SAME pass as the window, deduplicated against it, for
    the reason the Latvian dispatch carries its watch list too: two passes are two draws at
    one portal for one date, and two answers about what a day contained.

    THE GATE DOES NOT APPLY TO THEM. The gate decides what is worth fetching for the first
    time; a watched procurement was already judged worth a card by a person, and dropping
    it here would silently stop answering the question the card is open for.
    """
    run_id = run_id or time.strftime("%Y%m%dT%H%M%S+0300", time.localtime())
    stamp = date.replace("-", "/") if "-" not in date else date
    y, m, d = (date.split("-") if "-" in date else reversed(date.split("/")))
    window = "%s/%s/%s" % (d, m, y)

    targets = lt_targets.day(window, window)
    # AN EMPTY WINDOW ON A WORKING DAY IS A BROKEN CRAWL, AND NOTHING ELSE WOULD SAY SO.
    # Lithuania publishes on the order of a hundred resources a working day. What produces
    # zero is our own discovery breaking — the results table gaining a column, or the
    # displaytag page parameter changing when the portal is redeployed — and every one of
    # those failures returns an empty list rather than an error. Left unremarked it becomes
    # a green run, a complete day, an empty morning, and nothing to tell it from a holiday.
    #
    # BUT ONLY ON A WORKING DAY. Measured 30 Aug 2026 against the live portal: Friday
    # 28 August returned 106 rows and Saturday 29 August returned 0. The country does not
    # publish at the weekend, so a flag that fired on an empty Saturday would cry wolf twice
    # every week until nobody read it — which is the failure this flag exists to prevent.
    published_today = datetime.date(*(int(p) for p in date.split("-"))).weekday() < 5
    discovery_failed = published_today and not targets
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

    # The watch list, added to the window and deduplicated against it. `limit` has already
    # been applied above and deliberately does not reach here: a trial run asks for fewer
    # of the day's procurements, never for fewer of the ones somebody is waiting on.
    known = {str(t["pid"]) for t in targets}
    for pid in (watch or []):
        pid = str(pid)
        if pid in known:
            continue
        kind, notice = resolve(pid)
        if kind is None:
            # A watched card whose resource no view will serve is a hole in the watch, and
            # the report has to be able to name it.
            failed.append({"pid": pid, "kind": None, "watched": True,
                           "reason": "no view served it — withdrawn, or EPPS answered "
                                     "with a login form"})
            continue
        targets.append({"pid": pid, "kind": kind, "watched": True, "notice": notice,
                        "title": notice.get("title"), "buyer": notice.get("buyer"),
                        "published": notice.get("published")})
        known.add(pid)

    for target in targets:
        pid = target["pid"]
        home = os.path.join(out_root, "tenders", pid)
        previous = _read(os.path.join(home, "state.json"))

        # Already in hand for a watched procurement, whose view had to be found by asking.
        notice = target.get("notice")
        if rules is not None and not target.get("watched"):
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
            # Which population this came from. A reader answers two different questions of
            # the two — "is this worth a card" and "has what I am waiting on moved" — and a
            # day that did not say which was which would make it guess from the date.
            "watched": bool(target.get("watched")),
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
            "watched": bool(target.get("watched")),
            "home": "tenders/%s" % pid,
            "documents": done["documents"], "bytes": done["bytes"],
            "title": target["title"],
        })

    by_status = {}
    for row in moves:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    watched_count = sum(1 for row in moves if row["watched"])

    # WHOSE FAILURE MAKES A DAY SHORT. The day is the window; a watched card is a standing
    # question somebody asked of it. A watched resource no view will serve is a hole in the
    # watch and must be reported as one — but it is not the window arriving short, and
    # letting it say so would mark every night incomplete until a person edited the board,
    # which teaches a reader to ignore the flag exactly when it starts meaning something.
    lost_window = [f for f in failed if not f.get("watched")]
    lost_watch = [f for f in failed if f.get("watched")]
    complete = not lost_window and not discovery_failed

    changes = {
        "schema": "day-changes/1", "date": date, "country": "LT", "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": complete,
        "discovery_failed": discovery_failed,
        "counts": dict(by_status, tenders=len(moves), gated=len(gated),
                           watched=watched_count),
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
        # A day is complete when the window was discovered at all and every target it named
        # reached a home. There are no shards to be missing, so those are the only two ways
        # it can be short.
        "complete": complete,
        # Said outright rather than inferred from a zero, because a reader looking at
        # `targets: 0` cannot tell a broken crawl from a day nobody published on.
        "discovery_failed": discovery_failed,
        "coverage": {"targets": len(targets), "delivered": len(delivered),
                     "gated": len(gated), "failed": len(lost_window),
                     "watch_holes": len(lost_watch)},
        "counts": dict(by_status, tenders=len(delivered), gated=len(gated),
                       watched=watched_count,
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
