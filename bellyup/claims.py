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


CLAIMS = Claims()
