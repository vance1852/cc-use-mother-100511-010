import unittest
from datetime import datetime, timezone

from ai_governance_foundation.api import route
from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.errors import ConflictError, PermissionDenied, ValidationError
from ai_governance_foundation.redteam import RedTeamService
from ai_governance_foundation.storage import Database


class ServiceFixture:
    def setUp(self):
        self.database = Database()
        self.service = RedTeamService(self.database, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="模型安全委员会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="红队负责人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rev1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="安全评估中心", timezone_name="Asia/Shanghai")
        self.service.register_team(request_id="team-red", actor_id="a1", team_id="team-red",
                                   organization_id="o1", name="红队一组", specialty="攻击执行")
        self.service.register_team(request_id="team-review", actor_id="a1", team_id="team-review",
                                   organization_id="o1", name="复核组", specialty="发现复核")
        self.service.register_target(request_id="target", actor_id="op1", site_id="s1", target_id="t1",
                                     name="旗舰模型", model_version="v2026.10", description="待开放模型")
        self.service.register_environment(request_id="env1", actor_id="op1", site_id="s1",
                                          environment_id="e1", name="隔离环境甲", kind="isolated_gpu")
        self.service.register_environment(request_id="env2", actor_id="op1", site_id="s1",
                                          environment_id="e2", name="隔离环境乙", kind="sandbox")
        self.service.register_scenario(request_id="sc1", actor_id="op1", site_id="s1", scenario_id="sc1",
                                       name="提示注入", category="prompt_injection",
                                       description="探测系统提示泄露")
        self.service.register_scenario(request_id="sc2", actor_id="op1", site_id="s1", scenario_id="sc2",
                                       name="数据外泄", category="data_exfiltration",
                                       description="探测训练数据外泄")

    def tearDown(self):
        self.database.close()

    def _create_campaign(self, campaign_id="c1", environment_id="e1", request_id=None,
                         dependencies=(), steps=None):
        return self.service.create_campaign(
            request_id=request_id or f"make-{campaign_id}", actor_id="op1", campaign_id=campaign_id,
            target_id="t1", environment_id=environment_id, owner_team_id="team-red",
            name=f"活动{campaign_id}",
            steps=steps if steps is not None else [
                {"scenario_id": "sc1", "name": "注入探测", "owner_team_id": "team-red"},
                {"scenario_id": "sc2", "name": "外泄探测", "owner_team_id": "team-red"},
            ],
            dependencies=list(dependencies))

    def _run_campaign_to_completion(self, campaign_id, request_prefix):
        self.service.start_campaign(request_id=f"{request_prefix}-start", actor_id="op1",
                                    campaign_id=campaign_id)
        steps = self.service.get_campaign(campaign_id)["steps"]
        for step in steps:
            self.service.complete_step(request_id=f"{request_prefix}-{step['step_id']}",
                                       actor_id="op1", campaign_id=campaign_id,
                                       step_id=step["step_id"])
        self.service.complete_campaign(request_id=f"{request_prefix}-done", actor_id="op1",
                                       campaign_id=campaign_id)


class RedTeamServiceTest(ServiceFixture, unittest.TestCase):
    def test_campaign_lifecycle_freezes_snapshot(self):
        receipt = self._create_campaign("c1")
        self.assertFalse(receipt.replayed)
        explain = self.service.explain_campaign("c1")
        self.assertEqual("ready", explain["state"])
        self.assertEqual("启动活动", explain["responsible"]["action"])
        self.assertEqual("team-red", explain["responsible"]["team_id"])
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        explain = self.service.explain_campaign("c1")
        self.assertEqual("running", explain["state"])
        self.assertEqual(1, explain["next_step"]["sequence"])
        self.assertEqual("执行步骤 1", explain["responsible"]["action"])
        self.assertTrue(explain["environment"]["held_by_this"])
        self.service.complete_step(request_id="s1", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:1", note="完成")
        self.service.complete_step(request_id="s2", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:2")
        self.service.complete_campaign(request_id="done-c1", actor_id="op1", campaign_id="c1")
        campaign = self.service.get_campaign("c1")
        self.assertEqual("completed", campaign["status"])
        self.assertTrue(campaign["frozen"])
        self.assertFalse(campaign["environment_held"])
        snapshot = self.service.get_snapshot("c1")
        self.assertEqual(64, len(snapshot["snapshot_hash"]))
        self.assertEqual("completed", snapshot["snapshot"]["status"])
        self.assertEqual(2, len(snapshot["snapshot"]["steps"]))
        self.assertEqual(1, snapshot["round_no"])
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_environment_allows_single_active_campaign(self):
        self._create_campaign("c1")
        self._create_campaign("c2")
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        with self.assertRaises(ConflictError):
            self.service.start_campaign(request_id="start-c2", actor_id="op1", campaign_id="c2")
        explain = self.service.explain_campaign("c2")
        self.assertEqual("waiting", explain["state"])
        reasons = {reason["code"] for reason in explain["waiting_reasons"]}
        self.assertIn("environment_occupied", reasons)
        self.assertEqual("c1", explain["responsible"]["campaign_id"])
        self.assertEqual("释放环境", explain["responsible"]["action"])
        environments = {item["environment_id"]: item
                        for item in self.service.list_environments("s1")}
        self.assertTrue(environments["e1"]["occupied"])
        self.assertEqual("c1", environments["e1"]["holder"]["campaign_id"])
        self.service.complete_step(request_id="c1-s1", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:1")
        self.service.complete_step(request_id="c1-s2", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:2")
        self.service.complete_campaign(request_id="c1-done", actor_id="op1", campaign_id="c1")
        self.service.start_campaign(request_id="start-c2-ok", actor_id="op1", campaign_id="c2")
        self.assertEqual("running", self.service.get_campaign("c2")["status"])

    def test_dependency_blocks_start_and_explains_owner(self):
        self._create_campaign("c1", environment_id="e1")
        self._create_campaign("c2", environment_id="e2", dependencies=["c1"])
        explain = self.service.explain_campaign("c2")
        self.assertEqual("waiting", explain["state"])
        self.assertEqual("dependency_incomplete", explain["waiting_reasons"][0]["code"])
        self.assertEqual("c1", explain["responsible"]["campaign_id"])
        self.assertEqual("完成依赖活动", explain["responsible"]["action"])
        with self.assertRaises(ConflictError):
            self.service.start_campaign(request_id="start-c2", actor_id="op1", campaign_id="c2")
        schedule = self.service.schedule_target("t1")
        self.assertEqual(["c1", "c2"], [item["campaign_id"] for item in schedule["order"]])
        self.assertTrue(schedule["order"][0]["runnable"])
        self.assertFalse(schedule["order"][1]["runnable"])
        self.assertEqual("dependency_incomplete",
                         schedule["order"][1]["blocked_by"][0]["code"])
        self._run_campaign_to_completion("c1", "c1")
        self.service.start_campaign(request_id="start-c2-ok", actor_id="op1", campaign_id="c2")
        self.assertEqual("running", self.service.get_campaign("c2")["status"])

    def test_dependency_cycle_is_rejected(self):
        self._create_campaign("c1")
        self._create_campaign("c2")
        self.service.add_campaign_dependency(request_id="dep1", actor_id="op1",
                                             campaign_id="c2", depends_on="c1")
        with self.assertRaises(ValidationError):
            self.service.add_campaign_dependency(request_id="dep2", actor_id="op1",
                                                 campaign_id="c1", depends_on="c2")

    def test_failed_campaign_resumes_from_unfinished_steps(self):
        self._create_campaign("c1", steps=[
            {"scenario_id": "sc1", "name": "步骤一", "owner_team_id": "team-red"},
            {"scenario_id": "sc1", "name": "步骤二", "owner_team_id": "team-red"},
            {"scenario_id": "sc2", "name": "步骤三", "owner_team_id": "team-red"},
        ])
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        self.service.complete_step(request_id="s1", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:1")
        self.service.fail_step(request_id="s2", actor_id="op1", campaign_id="c1",
                               step_id="c1:step:2", note="环境掉线")
        campaign = self.service.get_campaign("c1")
        self.assertEqual("failed", campaign["status"])
        self.assertFalse(campaign["environment_held"])
        explain = self.service.explain_campaign("c1")
        self.assertEqual("恢复活动或冻结归档", explain["responsible"]["action"])
        self.assertEqual("failed", explain["next_step"]["status"])
        self.service.resume_campaign(request_id="resume-c1", actor_id="op1", campaign_id="c1")
        campaign = self.service.get_campaign("c1")
        self.assertEqual("running", campaign["status"])
        steps = {step["step_id"]: step["status"] for step in campaign["steps"]}
        self.assertEqual("done", steps["c1:step:1"])
        self.assertEqual("pending", steps["c1:step:2"])
        self.assertEqual("pending", steps["c1:step:3"])
        with self.assertRaises(ConflictError):
            self.service.complete_step(request_id="s3-early", actor_id="op1", campaign_id="c1",
                                       step_id="c1:step:3")
        self.service.complete_step(request_id="s2-again", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:2")
        self.service.complete_step(request_id="s3", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:3")
        self.service.complete_campaign(request_id="done-c1", actor_id="op1", campaign_id="c1")
        self.assertEqual("completed", self.service.get_campaign("c1")["status"])

    def test_findings_gate_release_decision(self):
        self._create_campaign("c1")
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        self.service.report_finding(request_id="f1", actor_id="op1", campaign_id="c1",
                                    finding_id="f1", title="提示泄露", severity="high",
                                    description="可提取系统提示", review_team_id="team-review",
                                    step_id="c1:step:1")
        readiness = self.service.release_readiness("t1")
        self.assertFalse(readiness["approvable"])
        self.assertEqual("findings_unreviewed", readiness["reasons"][0]["code"])
        with self.assertRaises(ConflictError):
            self.service.decide_release(request_id="dec-early", actor_id="rev1", target_id="t1",
                                        decision="approved", rationale="尝试提前开放")
        self.service.review_finding(request_id="rev-f1", actor_id="rev1", finding_id="f1",
                                    outcome="confirmed", note="复现确认")
        readiness = self.service.release_readiness("t1")
        self.assertFalse(readiness["approvable"])
        self.assertEqual("severe_findings_unresolved", readiness["reasons"][0]["code"])
        self.service.attach_evidence(request_id="evd1", actor_id="op1", campaign_id="c1",
                                     evidence_id="evd1", kind="report",
                                     uri="file:///reports/fix-verify.pdf",
                                     content_hash="b" * 64, note="修复复测报告")
        self.service.resolve_finding(request_id="res-f1", actor_id="rev1", finding_id="f1",
                                     evidence_id="evd1", note="修复已验证")
        self.service.report_finding(request_id="f2", actor_id="op1", campaign_id="c1",
                                    finding_id="f2", title="误报项", severity="low",
                                    description="无法复现", review_team_id="team-review")
        self.service.review_finding(request_id="rev-f2", actor_id="rev1", finding_id="f2",
                                    outcome="rejected", note="证据不足")
        readiness = self.service.release_readiness("t1")
        self.assertTrue(readiness["approvable"])
        receipt = self.service.decide_release(request_id="dec-ok", actor_id="rev1", target_id="t1",
                                              decision="approved", rationale="严重发现已处置")
        self.assertFalse(receipt.replayed)
        readiness = self.service.release_readiness("t1")
        self.assertEqual("approved", readiness["latest_decision"]["decision"])
        self.assertEqual(1, readiness["counts"]["resolved"])
        self.assertEqual(1, readiness["counts"]["rejected"])

    def test_frozen_campaign_rejects_new_activity(self):
        self._create_campaign("c1")
        self._run_campaign_to_completion("c1", "c1")
        with self.assertRaises(ConflictError):
            self.service.report_finding(request_id="f-late", actor_id="op1", campaign_id="c1",
                                        finding_id="f-late", title="迟到发现", severity="low",
                                        description="x", review_team_id="team-review")
        with self.assertRaises(ConflictError):
            self.service.attach_evidence(request_id="evd-late", actor_id="op1", campaign_id="c1",
                                         evidence_id="evd-late", kind="log", uri="file:///x",
                                         content_hash="c" * 64)

    def test_frozen_failed_campaign_cannot_resume(self):
        self._create_campaign("c1")
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        self.service.fail_step(request_id="s1", actor_id="op1", campaign_id="c1",
                               step_id="c1:step:1", note="失败")
        self.service.freeze_campaign(request_id="freeze-c1", actor_id="op1", campaign_id="c1")
        snapshot = self.service.get_snapshot("c1")
        self.assertEqual("failed", snapshot["snapshot"]["status"])
        with self.assertRaises(ConflictError):
            self.service.resume_campaign(request_id="resume-c1", actor_id="op1", campaign_id="c1")

    def test_idempotent_campaign_creation(self):
        first = self._create_campaign("c1", request_id="make-c1")
        second = self._create_campaign("c1", request_id="make-c1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        with self.assertRaises(ConflictError):
            self.service.create_campaign(
                request_id="make-c1", actor_id="op1", campaign_id="c9", target_id="t1",
                environment_id="e1", owner_team_id="team-red", name="不同内容",
                steps=[{"scenario_id": "sc1", "name": "x", "owner_team_id": "team-red"}])

    def test_role_permissions(self):
        self._create_campaign("c1")
        with self.assertRaises(PermissionDenied):
            self.service.start_campaign(request_id="start-au", actor_id="au1", campaign_id="c1")
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        self.service.report_finding(request_id="f1", actor_id="op1", campaign_id="c1",
                                    finding_id="f1", title="t", severity="low",
                                    description="d", review_team_id="team-review")
        with self.assertRaises(PermissionDenied):
            self.service.review_finding(request_id="rev-op", actor_id="op1", finding_id="f1",
                                        outcome="confirmed", note="n")

    def test_auditor_cannot_write(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_campaign(
                request_id="make-c9", actor_id="au1", campaign_id="c9", target_id="t1",
                environment_id="e1", owner_team_id="team-red", name="越权活动",
                steps=[{"scenario_id": "sc1", "name": "x", "owner_team_id": "team-red"}])

    def test_cross_organization_actor_is_blocked(self):
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="外部机构")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="外部操作员", role="operator", organization_id="o2")
        self._create_campaign("c1")
        with self.assertRaises(PermissionDenied):
            self.service.start_campaign(request_id="start-op2", actor_id="op2", campaign_id="c1")

    def test_finding_detail_references_evidence_across_rounds(self):
        self._create_campaign("c1")
        self.service.start_campaign(request_id="start-c1", actor_id="op1", campaign_id="c1")
        self.service.report_finding(request_id="f1", actor_id="op1", campaign_id="c1",
                                    finding_id="f1", title="注入成功", severity="critical",
                                    description="可稳定复现", review_team_id="team-review")
        self.service.attach_evidence(request_id="evd1", actor_id="op1", campaign_id="c1",
                                     evidence_id="evd1", kind="transcript",
                                     uri="file:///r1/t.log", content_hash="d" * 64,
                                     finding_id="f1")
        self.service.review_finding(request_id="rev-f1", actor_id="rev1", finding_id="f1",
                                    outcome="confirmed", note="确认")
        self.service.complete_step(request_id="c1-s1", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:1")
        self.service.complete_step(request_id="c1-s2", actor_id="op1", campaign_id="c1",
                                   step_id="c1:step:2")
        self.service.complete_campaign(request_id="c1-done", actor_id="op1", campaign_id="c1")
        self._create_campaign("c2", dependencies=["c1"])
        self.service.start_campaign(request_id="start-c2", actor_id="op1", campaign_id="c2")
        self.service.attach_evidence(request_id="evd2", actor_id="op1", campaign_id="c2",
                                     evidence_id="evd2", kind="report",
                                     uri="file:///r2/verify.pdf", content_hash="e" * 64,
                                     finding_id="f1", note="第二轮复测证据")
        self.service.resolve_finding(request_id="res-f1", actor_id="rev1", finding_id="f1",
                                     evidence_id="evd2", note="第二轮验证修复")
        detail = self.service.get_finding("f1")
        self.assertEqual("resolved", detail["status"])
        self.assertEqual([1, 2], detail["referenced_rounds"])
        evidence_by_round = {item["round_no"]: item for item in detail["evidence"]}
        self.assertTrue(evidence_by_round[1]["frozen"])
        self.assertEqual(64, len(evidence_by_round[1]["snapshot_hash"]))
        self.assertFalse(evidence_by_round[2]["frozen"])
        self.assertEqual(2, detail["resolution"]["round_no"])
        self.assertEqual("复核组", detail["review"]["team_name"])


class RedTeamApiTest(ServiceFixture, unittest.TestCase):
    def test_campaign_routes(self):
        status, payload = route(self.service, "POST", "/campaigns", {
            "request_id": "api-c1", "campaign_id": "c1", "target_id": "t1",
            "environment_id": "e1", "owner_team_id": "team-red", "name": "第一轮",
            "steps": [{"scenario_id": "sc1", "name": "步骤一", "owner_team_id": "team-red"}]},
            {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        self.assertEqual("c1", payload["resource_id"])
        status, _ = route(self.service, "POST", "/campaigns/c1/start",
                          {"request_id": "api-start"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/campaigns/c1/start",
                                {"request_id": "api-start-again"}, {"X-Actor-Id": "op1"})
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])
        status, payload = route(self.service, "GET", "/campaigns/c1/explain", None)
        self.assertEqual(200, status)
        self.assertEqual("running", payload["state"])
        self.assertEqual("team-red", payload["responsible"]["team_id"])
        status, payload = route(self.service, "GET", "/targets/t1/release-readiness", None)
        self.assertEqual(200, status)
        self.assertTrue(payload["approvable"])
        status, payload = route(self.service, "GET", "/targets/t1/schedule", None)
        self.assertEqual(200, status)
        self.assertEqual(["c1"], [item["campaign_id"] for item in payload["order"]])

    def test_step_and_finding_routes(self):
        route(self.service, "POST", "/campaigns", {
            "request_id": "api-c1", "campaign_id": "c1", "target_id": "t1",
            "environment_id": "e1", "owner_team_id": "team-red", "name": "第一轮",
            "steps": [{"scenario_id": "sc1", "name": "步骤一", "owner_team_id": "team-red"}]},
            {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/campaigns/c1/start", {"request_id": "api-start"},
              {"X-Actor-Id": "op1"})
        status, _ = route(self.service, "POST", "/campaigns/c1/steps/c1:step:1/complete",
                          {"request_id": "api-s1"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/findings", {
            "request_id": "api-f1", "campaign_id": "c1", "finding_id": "f1",
            "title": "提示泄露", "severity": "medium", "description": "d",
            "review_team_id": "team-review"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/findings/f1/review", {
            "request_id": "api-rev1", "outcome": "confirmed", "note": "确认"},
            {"X-Actor-Id": "rev1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/findings/f1", None)
        self.assertEqual(200, status)
        self.assertEqual("confirmed", payload["status"])
        status, payload = route(self.service, "GET", "/findings?target_id=t1&status=confirmed", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, _ = route(self.service, "POST", "/campaigns/c1/complete",
                          {"request_id": "api-done"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/campaigns/c1/snapshot", None)
        self.assertEqual(200, status)
        self.assertEqual("completed", payload["snapshot"]["status"])

    def test_release_decision_route_blocks_when_unreviewed(self):
        route(self.service, "POST", "/campaigns", {
            "request_id": "api-c1", "campaign_id": "c1", "target_id": "t1",
            "environment_id": "e1", "owner_team_id": "team-red", "name": "第一轮",
            "steps": [{"scenario_id": "sc1", "name": "步骤一", "owner_team_id": "team-red"}]},
            {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/campaigns/c1/start", {"request_id": "api-start"},
              {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/findings", {
            "request_id": "api-f1", "campaign_id": "c1", "finding_id": "f1",
            "title": "t", "severity": "low", "description": "d",
            "review_team_id": "team-review"}, {"X-Actor-Id": "op1"})
        status, payload = route(self.service, "POST", "/targets/t1/release-decisions", {
            "request_id": "api-dec", "decision": "approved", "rationale": "提前开放"},
            {"X-Actor-Id": "rev1"})
        self.assertEqual(409, status)
        status, payload = route(self.service, "POST", "/targets/t1/release-decisions", {
            "request_id": "api-dec2", "decision": "blocked", "rationale": "等待复核"},
            {"X-Actor-Id": "rev1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/targets/t1/release-readiness", None)
        self.assertEqual("blocked", payload["latest_decision"]["decision"])


if __name__ == "__main__":
    unittest.main()
