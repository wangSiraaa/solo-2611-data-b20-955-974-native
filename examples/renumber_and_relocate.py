"""End-to-end narrative: renumbering history, frozen parcels and cabinet moves.

Run with::

    python3 examples/renumber_and_relocate.py

The scenario mirrors the counter stories in the task:

* three letters delivered to street S at doors 1/2/3 and a 1->2, 2->3, 3->1
  renumbering, relocated in a four-cell cabinet with one empty cell;
* the identity-renumber variant (zero moves, new-number claim refused);
* the sealed-empty-cell variant (no legal first carry);
* a two-era swap and an alias correction made *after* delivery, proving the
  frozen street id is never re-guessed;
* live pickup slips under a per-step seal and under a whole-ring seal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from address_delivery import (  # noqa: E402
    AddressService,
    CounterSession,
    POLICY_CYCLE,
    POLICY_STEP,
    ReturnCabinet,
)
from address_delivery.renumbering import Door  # noqa: E402

CELLS = ["1", "2", "3", "T"]
CHANNELS = [(a, b) for a in CELLS for b in CELLS if a != b]


def build_service() -> AddressService:
    svc = AddressService(":memory:")
    svc.catalog.load_initial(
        {
            "revision": "r1",
            "cities": [{"city_id": "C1", "name": "临川市"}],
            "districts": [
                {"district_id": "D1", "city_id": "C1", "name": "东区",
                 "aliases": ["东", "同区"]},
                {"district_id": "D2", "city_id": "C1", "name": "西区",
                 "aliases": ["西"]},
            ],
            "streets": [
                # Same street text in two districts: distinct stable entities.
                {"street_id": "S", "district_id": "D1", "name": "同街",
                 "aliases": ["旧招牌"], "active": True},
                {"street_id": "S2", "district_id": "D2", "name": "同街",
                 "aliases": [], "active": True},
            ],
        },
        {"revision": "r1", "coverages": [
            {"coverage_id": "K1", "street_id": "S", "station_id": "ST",
             "lo": 1, "hi": 99, "parity": "any"},
        ]},
    )
    return svc


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    svc = build_service()

    # -- 1. Real parcels through submit_new / replay_submitted --------------
    submitted = {}
    for pid, door in (("甲", 1), ("乙", 2), ("丙", 3)):
        result = svc.submit_new(pid, "C1", "东", "旧招牌", door)
        assert result.submitted_new, result.message
        submitted[pid] = result.job
    replay = svc.replay_submitted("甲")
    show("replay_submitted freezes the original fact",
         {"outcome": replay.outcome, "street_id": replay.job["street_id"],
          "house_no": replay.job["house_no"]})

    # -- 2. Era 0 door book and the three-cycle publication -----------------
    svc.register_initial_era([Door("S", 1), Door("S", 2), Door("S", 3)], label="旧号")
    svc.publish_renumbering(
        [
            # The narrative rotation 1->2, 2->3, 3->1 is expressed as three
            # singleton strips (a single arithmetic sequence cannot wrap).
            {"strip_id": "R1", "old_street_id": "S", "old_lo": 1, "old_hi": 1,
             "parity": "any", "new_street_id": "S", "new_first": 2, "new_step": 1},
            {"strip_id": "R2", "old_street_id": "S", "old_lo": 2, "old_hi": 2,
             "parity": "any", "new_street_id": "S", "new_first": 3, "new_step": 1},
            {"strip_id": "R3", "old_street_id": "S", "old_lo": 3, "old_hi": 3,
             "parity": "any", "new_street_id": "S", "new_first": 1, "new_step": 1},
        ],
        label="新号",
    )
    traces = {pid: svc.trace_parcel(pid).as_dict() for pid in submitted}
    show("era-by-era traces with the strips used", traces)

    for pid in submitted:
        svc.issue_final_door(pid)

    # -- 3. Alias correction after delivery never re-guesses the street -----
    svc.apply_revision({"revision": "r2", "operations": [
        {"op": "remove_street_alias", "street_id": "S", "alias": "旧招牌"},
        {"op": "add_street_alias", "street_id": "S", "alias": "新招牌"},
    ]})
    frozen = svc.replay_submitted("甲")
    claim_on_s = svc.verify_pickup_claim("甲", "S", 2)
    claim_guessing_s2 = svc.verify_pickup_claim("甲", "S2", 2)
    show("post-delivery alias correction keeps the frozen stable id",
         {"replayed_street": frozen.job["street_id"],
          "claim_S_new2": claim_on_s,
          "claim_S2_guessed_by_same_name": claim_guessing_s2})

    # -- 4. Cabinet puzzle: 4 moves, identity 0 moves, sealed cell unsolvable
    targets = {pid: svc.issued_door(pid).house_no for pid in submitted}
    target_cells = {pid: str(no) for pid, no in targets.items()}
    cabinet = ReturnCabinet(CELLS, CHANNELS,
                            {"甲": "1", "乙": "2", "丙": "3"}, target_cells)
    plan = cabinet.plan_min_moves()
    show("minimum relocation plan", plan.as_dict())

    identity = ReturnCabinet(CELLS, CHANNELS,
                             {"甲": "1", "乙": "2", "丙": "3"},
                             {"甲": "1", "乙": "2", "丙": "3"}).plan_min_moves()
    show("identity renumber needs no carries", identity.as_dict())

    sealed = ReturnCabinet(
        ["1", "2", "3"],
        [(a, b) for a in ("1", "2", "3") for b in ("1", "2", "3") if a != b],
        {"甲": "1", "乙": "2", "丙": "3"}, target_cells,
    ).plan_min_moves()
    show("sealed empty cell: reviewable no-solution reason", sealed.as_dict())

    # -- 5. Counter with cap 1: 甲 moved to T, claimed at new 2, replan -----
    step_session = CounterSession(
        ReturnCabinet(CELLS, CHANNELS,
                      {"甲": "1", "乙": "2", "丙": "3"}, target_cells),
        svc.renumbering, sealing_policy=POLICY_STEP, max_pending=1,
    )
    step_session.begin_move("甲", "T")
    step_session.complete_move()
    valid = step_session.request_pickup("甲", "S", 2)
    invalid = step_session.request_pickup("甲", "S", 1)
    step_session.run_moves()
    show("per-step seal: immediate serve from T plus two replanned carries",
         {"valid_claim": valid, "old_number_claim": invalid,
          "total_moves": step_session.completed_moves,
          "pickup_ledger": step_session.pickup_summary()})

    # -- 6. Whole-ring seal: the same claim waits four carries --------------
    ring_session = CounterSession(
        ReturnCabinet(CELLS, CHANNELS,
                      {"甲": "1", "乙": "2", "丙": "3"}, target_cells),
        svc.renumbering, sealing_policy=POLICY_CYCLE, max_pending=3,
    )
    ring_session.begin_move("丙", "T")           # opens the ring seal
    queued = ring_session.request_pickup("甲", "S", 2)
    ring_session.complete_move()
    ring_session.run_moves()
    show("cycle seal: the slip is queued and the waiting cost is booked",
         {"queued": queued,
          "total_moves": ring_session.completed_moves,
          "pickup_ledger": ring_session.pickup_summary()})

    # -- 7. Two-era swap in a separate ledger -------------------------------
    swap = build_service()
    swap.register_initial_era([Door("S", 5), Door("S", 7)])
    swap.publish_renumbering([
        {"strip_id": "SW1", "old_street_id": "S", "old_lo": 5, "old_hi": 5,
         "parity": "any", "new_street_id": "S", "new_first": 7, "new_step": 1},
        {"strip_id": "SW2", "old_street_id": "S", "old_lo": 7, "old_hi": 7,
         "parity": "any", "new_street_id": "S", "new_first": 5, "new_step": 1},
    ])
    show("two-era swap door books", swap.renumbering_snapshot()["doors"])


if __name__ == "__main__":
    main()
