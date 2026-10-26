#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The recall gate: which notices are worth fetching, before a single byte moves.

This is the one piece of judgement that is not about a country. A CPV code means the same
thing wherever it is filed, and the terms a caller recalls on are their business rather than
a portal's, so the rule is written once, in a file that names no portal.

This file knows nothing about the caller's interest. The terms arrive from the caller as
policy text, so this file names no industry, no trade and no target, and a reader of this
repository learns the shape of the filter without learning what anyone points it at.
"""

import json
import os


# The one filter allowed before a document exists.
#
# A title is kept when it contains one of the caller's recall roots. Roots are matched as
# substrings rather than as whole words because the language this runs against inflects
# heavily; precision belongs to the later document-reading step, not to a title.
#
# One guard, and it fails toward fetching: no text at all means no evidence, so the notice is
# fetched.
#
# Exclusions win over recall, including over a matching title: `hard_exclude_prefixes` is
# tested before the recall terms and returns. A code exclusion that could not override a
# matching title could never drop anything, because a notice whose title does not match is
# dropped by the recall test anyway. It exists for the notice whose title matches and is
# still out of scope.
#
# That puts the whole weight on how narrowly a prefix is written. An exclusion is absolute
# for a notice carrying no other code, so write a prefix that names a purchase (four to six
# digits), never one that names a division, and reach for `override_prefixes` before
# widening one. A two-digit division such as 45 (construction works) would silently drop a
# large share of what is kept.
#
# An absent or unreadable policy means fetch everything, which is the only safe direction
# for a filter that failed to load: fetching too much costs time, and dropping silently
# costs a tender.
POLICY_ENV = "EIS_POLICY"


def load_policy(source=None):
    """The caller's recall policy, or None. None means no filter — fetch everything.

    `source` is the policy's JSON text, which the caller reads from the radar kit, or a path
    to a JSON file. With None, `EIS_POLICY` in the environment is read instead. No
    deployment's terms are committed here.
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
    exclude_prefixes = tuple(policy.get("hard_exclude_prefixes") or ())
    exclude_titles = tuple(t.casefold() for t in (policy.get("hard_exclude_title_terms") or ()))
    # A policy may be exclusions only.
    #
    # A recall list is a whitelist: a notice whose title carries none of its roots is
    # dropped before a byte moves. That suits a caller who buys a nameable thing. It is
    # wrong for a caller whose subject sits inside somebody else's purchase, where the
    # title names the whole and the part appears in the documents. For them the gate is the
    # buyer's own classification: drop what the codes place outside, keep the rest, and let
    # the documents decide.
    #
    # So recall terms are one optional half of a policy. `None` is returned for a policy
    # that says nothing at all — an absent, unreadable or empty policy means fetch
    # everything.
    if not (recall or exclude_prefixes or exclude_titles
            or policy.get("override_prefixes") or policy.get("recall_cpv_prefixes")):
        return None                       # a policy with no rules is no policy
    return (recall,
            exclude_prefixes,
            exclude_titles,
            # Codes that survive their own division. A purchase can carry a main code
            # inside an excluded division and nothing else — a buyer files it under the
            # service it is bought as rather than the thing it is. Without an override
            # such a notice is dropped before a byte moves.
            tuple(policy.get("override_prefixes") or ()),
            # Codes that recall on their own, because a title is not always the better
            # signal: a buyer may write a vague title and then classify the purchase
            # precisely. Absent, this changes nothing.
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
    # The first three fields are always present; the override and code-recall lists are
    # optional.
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

    # A code can recall, and it is asked before the title. The exclusions above still
    # bind — an excluded title term or an all-excluded code set has already returned — so
    # this only widens what is fetched.
    if recall_prefixes and any(c.startswith(recall_prefixes) for c in codes):
        return False

    # No recall list means no whitelist, not an empty one. A policy that carries only
    # exclusions has already had its say above. Asking an empty recall list whether it
    # matched would answer no for every notice and drop everything.
    if not recall_terms:
        return False

    if not title:
        return False                      # missing signal fails open
    return not any(term in title for term in recall_terms)
