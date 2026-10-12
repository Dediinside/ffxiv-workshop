import unittest

from leveplanner.planner import Plan, Progress
from leveplanner.results import money, order_indices


def plan(cost, reward=30, accepts=2, allowance=2, turns=3, name="Sample", method="Покупка", quality="HQ", problems=None):
    return Plan({"name": name, "level": 20}, method, quality, 1,
                Progress(25, accepts, turns, 1000, 25, 0), allowance, reward,
                cost=cost, problems=problems or [])


class ResultTests(unittest.TestCase):
    def test_balance_and_signs(self):
        self.assertEqual(plan(2992, reward=7714).balance, 4722)
        self.assertEqual(money(plan(2992, reward=7714).balance, True), "+4 722")
        self.assertEqual(money(-2992, True), "-2 992")
        self.assertEqual(money(plan(100, reward=30).balance, True), "-70")
        self.assertEqual(money(0, True), "0")
        self.assertEqual(money(None, True), "—")

    def test_numeric_cost_and_unknown_last_both_directions(self):
        plans = [plan(100), plan(None), plan(20), plan(9)]
        self.assertEqual(order_indices(plans, "cost", False), [3, 2, 0, 1])
        self.assertEqual(order_indices(plans, "cost", True), [0, 2, 3, 1])

    def test_profit_and_fewer_leves_are_different_priorities(self):
        plans = [plan(20, reward=90, accepts=10), plan(10, reward=15, accepts=1)]
        self.assertEqual(order_indices(plans, "balance", True), [0, 1])
        self.assertEqual(order_indices(plans, "accepts", False), [1, 0])

    def test_fewer_allowances_not_same_as_fewer_accepts(self):
        plans = [plan(20, accepts=1, allowance=10), plan(20, accepts=2, allowance=2)]
        self.assertEqual(order_indices(plans, "accepts", False), [0, 1])
        self.assertEqual(order_indices(plans, "allow", False), [1, 0])

    def test_available_plans_first(self):
        plans = [plan(1, problems=["Бюджет"]), plan(100)]
        self.assertEqual(order_indices(plans, "cost", False), [1, 0])
        self.assertEqual(order_indices(plans, only_feasible=True), [1])

    def test_filters_compose_without_mutating_plans(self):
        plans = [plan(20, name="Iron Spear"), plan(10, name="Iron Bow", method="Крафт"),
                 plan(20, name="Iron Spear", quality="NQ")]
        self.assertEqual(order_indices(plans, method="Покупка", quality="HQ", search="  IRON  "), [0])
        self.assertEqual(order_indices(plans, search="not found"), [])
        self.assertEqual(len(plans), 3)
