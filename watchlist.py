"""The watch list: tenders whose cards on a monday.com board are still live.

A card is watched when a linked deal is in a group that is not closed; with no linked deal,
when its decision is one of the watched values. The board, the columns and both lists come
from the caller's settings (`watch_list.json` in the radar kit).
"""

import json
import urllib.request

API = "https://api.monday.com/v2"

QUERY = """query ($board: [ID!], $cursor: String, $columns: [String!]) {
  boards(ids: $board) {
    items_page(limit: 200, cursor: $cursor) {
      cursor
      items {
        id
        name
        column_values(ids: $columns) {
          id
          text
          ... on BoardRelationValue { linked_items { id group { title } } }
        }
      }
    }
  }
}"""


def _post(token, query, variables, opener=None):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": token, "Content-Type": "application/json", "User-Agent": "eis-tool/1"})
    with (opener or urllib.request.urlopen)(req, timeout=120) as resp:
        answer = json.loads(resp.read())
    if answer.get("errors"):
        raise RuntimeError("monday.com: %s" % answer["errors"][0].get("message"))
    return answer["data"]


def cards(token, settings, opener=None):
    """Every card of the board with its key, decision and linked deal groups."""
    columns = [settings["key_column"], settings["decision_column"], settings["deal_column"]]
    out, cursor = [], None
    while True:
        data = _post(token, QUERY, {"board": [str(settings["board"])], "cursor": cursor,
                                    "columns": columns}, opener)
        page = data["boards"][0]["items_page"]
        for item in page["items"]:
            values = {v["id"]: v for v in item["column_values"]}
            deal = values.get(settings["deal_column"]) or {}
            out.append({"id": item["id"], "name": item["name"],
                        "key": (values.get(settings["key_column"]) or {}).get("text") or "",
                        "decision": (values.get(settings["decision_column"]) or {}).get("text") or "",
                        "deal_groups": [((d.get("group") or {}).get("title") or "")
                                        for d in deal.get("linked_items") or []]})
        cursor = page.get("cursor")
        if not cursor:
            return out


def watched(card, settings):
    """The card-on-watch rule."""
    closed = set(settings["closed_deal_groups"])
    if card["deal_groups"]:
        return any(group not in closed for group in card["deal_groups"])
    return card["decision"] in set(settings["watched_decisions"])


def keys(token, settings, opener=None):
    """(watched keys like 'EIS:123' or 'IUB:<uuid>', names of watched cards without a key)."""
    found, keyless = [], []
    for card in cards(token, settings, opener):
        if not watched(card, settings):
            continue
        if card["key"].strip():
            found.append(card["key"].strip())
        else:
            keyless.append(card["name"])
    return sorted(set(found)), keyless
