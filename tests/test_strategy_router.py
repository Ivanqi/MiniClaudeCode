"""StrategyRouter 规则测试。"""
import unittest

from src.StrategyRouter import StrategyRouter


class TestStrategyRouter(unittest.TestCase):
    def setUp(self):
        self.router = StrategyRouter()

    def test_short_explain_is_react(self):
        decision = self.router.recommend("解释这个函数")
        self.assertEqual(decision.strategy, "react")
        self.assertEqual(decision.replan_triggers, [])

    def test_fix_error_is_react_or_hybrid(self):
        decision = self.router.recommend("修复登录 500")
        self.assertIn(decision.strategy, ("react", "hybrid"))
        self.assertLess(decision.complexity, 0.75)

    def test_refactor_with_tests_needs_planning(self):
        decision = self.router.recommend("重构登录模块并补测试")
        self.assertIn(decision.strategy, ("hybrid", "plan"))
        self.assertTrue(decision.replan_triggers)

    def test_business_diagnosis(self):
        decision = self.router.recommend("门店营业额异常诊断")
        self.assertIn(decision.strategy, ("hybrid", "plan"))

    def test_security_scan_and_deploy(self):
        decision = self.router.recommend("代码安全扫描 + 部署")
        self.assertEqual(decision.strategy, "plan")
        self.assertIn("approval_required", decision.replan_triggers)

    def test_explicit_marker(self):
        decision = self.router.recommend("/plan 做一个登录页")
        self.assertEqual(decision.strategy, "plan")
        self.assertTrue(decision.signals["explicit_plan"])

    def test_teammates_and_prior_failure(self):
        decision = self.router.recommend("继续", active_teammates=True)
        self.assertEqual(decision.strategy, "hybrid")
        decision = self.router.recommend("继续", prior_failure=True)
        self.assertEqual(decision.strategy, "hybrid")

    def test_deterministic(self):
        first = self.router.recommend("重构登录模块并补测试")
        second = self.router.recommend("重构登录模块并补测试")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
