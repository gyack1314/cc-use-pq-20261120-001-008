from copy import deepcopy
from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import (decimal_text, identifier, integer, number, overlaps,
                         query_spec, require, shape)
from .exposure import Exposure


def portfolio_spec(value):
    shape(value, ("query", "budget_cents", "proposals"))
    query = query_spec(value["query"])
    budget = integer(value["budget_cents"], "budget_cents", 0, 100_000_000_000)
    require(isinstance(value["proposals"], list) and len(value["proposals"]) <= 16,
            "proposals", "一次组合比较最多支持16个候选行动")
    proposals = []
    for record in value["proposals"]:
        shape(record, ("action", "cost_cents"))
        proposals.append({"action": identifier(record["action"], "action"),
                          "cost_cents": integer(record["cost_cents"], "cost_cents", 1, 100_000_000_000)})
    require(len({p["action"] for p in proposals}) == len(proposals), "proposals", "候选行动不能重复")
    return {"query": query, "budget_cents": budget, "proposals": sorted(proposals, key=lambda p: p["action"])}


class Portfolio:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def plan(self, value):
        spec = portfolio_spec(value)
        baseline = Exposure(self.snapshot).analyze(spec["query"])
        require(baseline["status"] == "qualified", "incomplete", "资料不完整，不能对行动组合排序")
        candidates = []
        excluded = []
        active = [a for a in self.snapshot["action"].values() if a["status"] == "active"]
        for proposal in spec["proposals"]:
            action = self.snapshot["action"].get(proposal["action"])
            require(action is not None, "action", "候选行动不存在")
            require(action["status"] == "planned", "action", "只能将计划中的行动用于假设比较")
            require(action["metric"] == spec["query"]["metric"] and action["zone"] in spec["query"]["zones"],
                    "scope", "候选行动超出分析范围")
            reason = None
            if not overlaps(action, spec["query"]):
                reason = "outside_period"
            elif any(a["facility"] == action["facility"] and overlaps(a, action) for a in active):
                reason = "facility_committed"
            elif proposal["cost_cents"] > spec["budget_cents"]:
                reason = "over_budget"
            if reason:
                excluded.append({"action": action["id"], "reason": reason})
            else:
                candidates.append({**proposal, "record": action})
        conflicts = self.conflicts(candidates)
        combinations = 0
        baseline_burden = Decimal(baseline["total"]["known_weighted_burden"])
        best = {"actions": [], "cost_cents": 0, "avoided_burden": Decimal(0), "additional_protected": 0}
        cache = {}

        def evaluate(selected, cost):
            nonlocal combinations, best
            key = tuple(selected)
            if key in cache:
                return cache[key]
            snapshot = deepcopy(self.snapshot)
            names = []
            for index in selected:
                action_id = candidates[index]["action"]
                snapshot["action"][action_id]["status"] = "active"
                names.append(action_id)
            Adaptation(snapshot).validate()
            analysis = Exposure(snapshot).analyze(spec["query"])
            combinations += 1
            avoided = baseline_burden - Decimal(analysis["total"]["known_weighted_burden"])
            additional = (analysis["total"]["known_protected_person_days"]
                          - baseline["total"]["known_protected_person_days"])
            candidate = {"actions": names, "cost_cents": cost, "avoided_burden": avoided,
                         "additional_protected": additional}
            ranking = (-avoided, -additional, cost, names)
            incumbent = (-best["avoided_burden"], -best["additional_protected"], best["cost_cents"], best["actions"])
            if ranking < incumbent:
                best = candidate
            cache[key] = candidate
            return candidate

        def search(index, selected, cost):
            if index == len(candidates):
                evaluate(selected, cost)
                return
            search(index + 1, selected, cost)
            proposal = candidates[index]
            if cost + proposal["cost_cents"] <= spec["budget_cents"] and not any(
                    (min(index, other), max(index, other)) in conflicts for other in selected):
                search(index + 1, selected + [index], cost + proposal["cost_cents"])

        search(0, [], 0)
        standalone = []
        for index, candidate in enumerate(candidates):
            outcome = evaluate([index], candidate["cost_cents"])
            standalone.append({**outcome, "avoided_burden": decimal_text(outcome["avoided_burden"])})
        chosen = deepcopy(self.snapshot)
        for name in best["actions"]:
            chosen["action"][name]["status"] = "active"
        result = Exposure(chosen).analyze(spec["query"])
        return {"request": spec, "status": "hypothetical", "selected": best["actions"],
                "cost_cents": best["cost_cents"], "remaining_cents": spec["budget_cents"] - best["cost_cents"],
                "avoided_weighted_burden": decimal_text(best["avoided_burden"]),
                "additional_protected_person_days": best["additional_protected"],
                "baseline": baseline["total"], "projected": result["total"], "standalone": standalone,
                "excluded": excluded, "feasible_combinations": combinations,
                "conflicts": [[candidates[a]["action"], candidates[b]["action"]] for a, b in sorted(conflicts)]}

    @staticmethod
    def conflicts(candidates):
        result = set()
        for index, candidate in enumerate(candidates):
            action = candidate["record"]
            for other in range(index):
                previous = candidates[other]["record"]
                if action["facility"] == previous["facility"] and overlaps(action, previous):
                    result.add((other, index))
        return result
