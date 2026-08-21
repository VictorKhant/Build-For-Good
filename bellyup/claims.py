"""Who has agreed to collect what, tonight.

The board used to have two states: a business reported surplus, and a delivery
was confirmed. That is fine when one person drives the whole thing, but the
three roles need the step in between:

    reported  a business asks for a pickup. It cannot dispatch anything --
              a donor offers food, it does not assign anyone's van.
    accepted  an agency takes the job. Now it is on their run sheet, and
              nobody else is offered it.
    delivered it reaches people and enters the ledger.

Held in memory alongside the ledger, per evening.
"""

from __future__ import annotations

from datetime import datetime


class Claims:
    def __init__(self) -> None:
        self._by_supplier: dict[str, dict] = {}

    def accept(self, supplier_id: str, agency_id: str, when: datetime) -> dict:
        rec = {"supplier_id": supplier_id, "agency_id": agency_id,
               "accepted_at": when.strftime("%H:%M")}
        self._by_supplier[supplier_id] = rec
        return rec

    def release(self, supplier_id: str) -> bool:
        return self._by_supplier.pop(supplier_id, None) is not None

    def holder(self, supplier_id: str) -> str | None:
        rec = self._by_supplier.get(supplier_id)
        return rec["agency_id"] if rec else None

    def for_agency(self, agency_id: str) -> list[str]:
        return [s for s, r in self._by_supplier.items() if r["agency_id"] == agency_id]

    def is_claimed(self, supplier_id: str) -> bool:
        return supplier_id in self._by_supplier

    def all(self) -> dict[str, dict]:
        return dict(self._by_supplier)

    def reset(self) -> None:
        self._by_supplier.clear()


class Requests:
    """Pickup requests: who a business asked, and who has said no.

    A report of surplus and a request for collection are different facts. A
    kitchen with 129 lb has not asked anyone to drive out for it, and until it
    does the report is nobody else's business -- which is why the agency board
    shows requests, not reports.

    A request is ADDRESSED. It goes to the one collector the engine matched,
    and only that collector can act on it.

    What a decline means is the DONOR's call, set when they request:

      allow_fallback=True   a no from the matched collector releases the
                            request to everyone else, minus whoever declined.
                            A no from one agency is not a no from the city.
      allow_fallback=False  a no ends it. The request leaves every board.
                            Some kitchens will only hand food to the partner
                            they have an agreement with, and a platform that
                            quietly shopped their food around after that
                            partner said no would be overriding them.
    """

    def __init__(self) -> None:
        self._req: dict[str, dict] = {}

    # -- making one ---------------------------------------------------
    def open(self, supplier_id: str, target_id: str, when: datetime,
             allow_fallback: bool = True) -> dict:
        rec = {"supplier_id": supplier_id, "target": target_id,
               "declined_by": [], "open_to_all": False,
               "allow_fallback": bool(allow_fallback), "withdrawn": False,
               "requested_at": when.strftime("%H:%M")}
        self._req[supplier_id] = rec
        return rec

    def cancel(self, supplier_id: str) -> bool:
        return self._req.pop(supplier_id, None) is not None

    # -- reading it --------------------------------------------------
    def get(self, supplier_id: str) -> dict | None:
        return self._req.get(supplier_id)

    def is_open(self, supplier_id: str) -> bool:
        return supplier_id in self._req

    def visible_to(self, supplier_id: str, agency_id: str) -> bool:
        r = self._req.get(supplier_id)
        if r is None:
            return False                      # never requested
        if r.get("withdrawn"):
            return False                      # declined, donor allowed no fallback
        if agency_id in r["declined_by"]:
            return False                      # this one already said no
        if r["open_to_all"]:
            return True                       # released after a decline
        return r["target"] == agency_id        # addressed to this collector

    def target_of(self, supplier_id: str) -> str | None:
        r = self._req.get(supplier_id)
        return r["target"] if r else None

    # -- declining ---------------------------------------------------
    def decline(self, supplier_id: str, agency_id: str) -> dict | None:
        r = self._req.get(supplier_id)
        if r is None:
            return None
        if agency_id not in r["declined_by"]:
            r["declined_by"].append(agency_id)
        if r.get("allow_fallback", True):
            # a no releases it to the rest
            r["open_to_all"] = True
        else:
            # the donor asked one collector and only that collector
            r["withdrawn"] = True
        return r

    def is_withdrawn(self, supplier_id: str) -> bool:
        r = self._req.get(supplier_id)
        return bool(r and r.get("withdrawn"))

    def all(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._req.items()}

    def reset(self) -> None:
        self._req.clear()


CLAIMS = Claims()
REQUESTS = Requests()
