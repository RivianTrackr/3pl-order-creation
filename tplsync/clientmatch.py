"""Fill in client details automatically.

* Syncore client groups: every contact at a company (store billing, employees,
  dropship) shares one client group, so clients are linked by group. Group names
  are matched to client names; one exact match is filled in, close names are
  saved as suggestions to pick from.
* 3PL Customer IDs: a client's own 3PL Central login only sees its own customer,
  so that customer is looked up once its Client ID and Secret are saved.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from .config import TplClient
from .db import Database, company_key, utcnow
from .tpl import TplCentralClient

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 6


def match_syncore_groups(db: Database, groups: List[dict]) -> Dict[str, int]:
    """Link clients without a Syncore client group to the group with the same name."""
    db.set_meta("syncore_groups", json.dumps(sorted(({"id": str(g["id"]), "name": g["name"]} for g in groups),
                                                    key=lambda g: g["name"].casefold())))
    db.set_meta("clients_matched_at", utcnow())
    by_key: Dict[str, List[dict]] = {}
    for g in groups:
        by_key.setdefault(company_key(g["name"]), []).append(g)
    taken = {r["syncore_group_id"] for r in db.list_clients() if r["syncore_group_id"]}
    counts = {"groups": len(groups), "matched": 0, "suggested": 0, "unmatched": 0}

    for row in db.list_clients():
        if row["syncore_group_id"]:
            continue
        keys = {k for k in (company_key(row["name"]), company_key(row["tpl_customer_name"])) if k}
        exact = {str(g["id"]): g for k in keys for g in by_key.get(k, []) if str(g["id"]) not in taken}
        if len(exact) == 1:
            group = next(iter(exact.values()))
            db.assign_syncore_group(row["id"], str(group["id"]), group["name"])
            taken.add(str(group["id"]))
            counts["matched"] += 1
            log.info("%s -> Syncore client group %s (%s)", row["name"], group["id"], group["name"])
            continue
        # Close names: one contains the other ("Park Quick" matches "ParkQuick Store", but two
        # differently-spelled names won't).
        close = exact or {str(g["id"]): g for g in groups if str(g["id"]) not in taken
                          and any(k and len(k) >= 4 and (k in company_key(g["name"]) or company_key(g["name"]) in k)
                                  for k in keys) and len(company_key(g["name"])) >= 4}
        if close:
            ranked = sorted(close.values(), key=lambda g: (len(g["name"]), g["name"]))[:MAX_SUGGESTIONS]
            db.set_suggestions(row["id"], [{"id": g["id"], "business_name": g["name"], "detail": "Syncore client group"}
                                           for g in ranked])
            counts["suggested"] += 1
            log.info("%s: possible Syncore client groups: %s", row["name"], ", ".join(g["name"] for g in ranked))
        else:
            db.set_suggestions(row["id"], [])
            counts["unmatched"] += 1
            log.info("%s: no Syncore client group with a similar name", row["name"])
    db.set_meta("clients_match_result", json.dumps(counts))
    return counts


def cached_groups(db: Database) -> List[dict]:
    return json.loads(db.get_meta("syncore_groups") or "[]")


def lookup_customer(base_url: str, client_id: str, client_secret: str, user_login: str,
                    facility_id: Optional[int] = None) -> List[Tuple[int, str]]:
    """The 3PL Central customer(s) a Client ID / Secret can see."""
    creds = TplClient(name="lookup", customer_id=0, client_id=client_id, client_secret=client_secret)
    return TplCentralClient(base_url, creds, user_login).list_customers(facility_id)
