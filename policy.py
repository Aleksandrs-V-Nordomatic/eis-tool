#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The recall gate: which notices are worth fetching, before a single byte moves.

WHY IT IS ITS OWN FILE. This is the one piece of judgement that is not about a country. A
CPV code means the same thing in Riga and in Vilnius, and the terms a caller recalls on are
their business rather than a portal's — so both country tools ran the identical rule, and
the Lithuanian one reached into `batch.py`, the Latvian shard driver, to borrow it. That
import was the only thing keeping the two countries in one repository.

WHAT THIS KNOWS ABOUT THE CALLER'S INTEREST: NOTHING — the same rule deliver_graph.py keeps
about its destination. The terms arrive in the environment, so this file names no industry,
no trade and no target, and a reader of this repository learns the shape of the filter
without learning what anyone points it at.
"""

import json
import os


# THE ONE FILTER ALLOWED BEFORE A DOCUMENT EXISTS.
#
# A title is kept when it contains one of the caller's recall roots. Roots are matched as
# substrings rather than as whole words because the language this runs against inflects
# heavily; precision belongs to the later document-reading step, not to a title.
#
# Two guards matter, and both fail toward fetching:
#   * no title means no evidence, so the notice is fetched;
#   * a classification code never vetoes a matching title, because the code is assigned by
#     the buyer and an imperfect one must not silently drop a notice whose title matches.
#
# Exclusions win over recall, and exclusion by code prefix covers notices whose title is
# absent or unhelpful.
#
# WHAT THIS KNOWS ABOUT THE CALLER'S INTEREST: NOTHING — the same rule deliver_graph.py
# keeps about its destination. The terms arrive in the environment, so this file names no
# industry, no trade and no target, and a reader of this repository learns the shape of the
# filter without learning what anyone points it at. An absent or unreadable policy means
# fetch everything, which is the only safe direction for a filter that failed to load:
# fetching too much costs time, and dropping silently costs a tender.
POLICY_ENV = "EIS_POLICY"


def load_policy(source=None):
    """The caller's recall policy, or None. None means no filter — fetch everything.

    `source` is JSON text, a path to a JSON file, or None to read `EIS_POLICY` from the
    environment. Tests pass a fixture through it; production passes nothing and the
    environment answers, so no deployment's terms are ever committed here.
    """
    raw = source if source is not None else os.environ.get(POLICY_ENV)
    if not raw or not raw.strip():
        return None
    text = raw
    if not raw.lstrip().startswith("{"):              # not JSON, so treat it as a path
        try:
            with open(raw, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
    try:
        policy = json.loads(text)
    except ValueError:
        return None                       # an unreadable policy must fail open, never drop all
    recall = tuple(t.casefold() for t in (policy.get("recall_title_terms") or ()))
    if not recall:
        return None                       # incomplete policy must fail open, never drop all
    return (recall,
            tuple(policy.get("hard_exclude_prefixes") or ()),
            tuple(t.casefold() for t in (policy.get("hard_exclude_title_terms") or ())),
            # CODES THAT SURVIVE THEIR OWN DIVISION. A control-system contract can carry a
            # main code inside an excluded division and nothing else — `72250000` software
            # services on a SCADA maintenance contract, `79711000` on an alarm-monitoring
            # one — and 62% of live procurements carry one code only. Without an override
            # such a notice is dropped before a byte moves, which is the one failure the
            # exclusions are least allowed to cause.
            tuple(policy.get("override_prefixes") or ()),
            # CODES THAT RECALL ON THEIR OWN, because a title is not always the better
            # signal. Recall was title-only, and a code could exclude or rescue from an
            # exclusion but never bring anything in — so a procurement whose title is vague
            # and whose code is exact was dropped before a byte moved. Measured on the
            # Lithuanian day of 24 Aug 2026: `Stebejimo sistema` under 32323500, which is
            # literally "video-surveillance system", and `LoRaWAN ... objektu parametru
            # kontrolei` under 32440000, telemetry. Both ours, both invisible, because a
            # buyer wrote a short title. Absent, this changes nothing.
            tuple(policy.get("recall_cpv_prefixes") or ()))

def cpv_codes(notice):
    """Every CPV code a notice carries, however the source spelled them."""
    codes = []
    raw = notice.get("cpv")
    if isinstance(raw, (list, tuple)):
        codes = [str(c.get("code", "")) if isinstance(c, dict) else str(c) for c in raw]
    elif raw:
        codes = [str(raw)]
    if notice.get("cpv_main"):
        codes.append(str(notice["cpv_main"]))
    return [c.strip() for c in codes if c and c.strip()]


def outside_scope(notice, policy):
    """Should this notice be excluded before any documents are fetched?"""
    if not policy:
        return False
    # Older policies carry three fields; the override list is the fourth and optional.
    recall_terms, exclude_prefixes, exclude_title_terms = policy[:3]
    override_prefixes = policy[3] if len(policy) > 3 else ()
    recall_prefixes = policy[4] if len(policy) > 4 else ()

    title = str(notice.get("title") or notice.get("name") or "").casefold()
    if title and any(term in title for term in exclude_title_terms):
        return True

    codes = cpv_codes(notice)
    # An override is read anyway, wherever its division sits. The gate asks what the buyer
    # classified this as; whether the work is ours is a later and different question.
    overridden = bool(override_prefixes) and any(c.startswith(override_prefixes)
                                                 for c in codes)
    if (codes and exclude_prefixes and not overridden
            and all(c.startswith(exclude_prefixes) for c in codes)):
        return True

    # A CODE CAN RECALL, AND IT IS ASKED BEFORE THE TITLE. The exclusions above still
    # bind — an excluded title term or an all-excluded code set has already returned — so
    # this widens what is fetched and can never drop anything the old gate kept.
    if recall_prefixes and any(c.startswith(recall_prefixes) for c in codes):
        return False

    if not title:
        return False                      # missing signal fails open
    return not any(term in title for term in recall_terms)
