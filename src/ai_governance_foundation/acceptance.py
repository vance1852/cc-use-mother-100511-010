"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .redteam import RedTeamService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = RedTeamService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科研机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="复核专家", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        service.register_team(request_id="req-team-red", actor_id="operator-001", team_id="team-red",
                              organization_id="org-001", name="红队一组", specialty="攻击执行")
        service.register_team(request_id="req-team-review", actor_id="operator-001", team_id="team-review",
                              organization_id="org-001", name="复核组", specialty="发现复核")
        service.register_target(request_id="req-target", actor_id="operator-001", site_id="site-001",
                                target_id="target-001", name="待发布模型", model_version="model-v1",
                                description="开放前评估对象")
        service.register_environment(request_id="req-env", actor_id="operator-001", site_id="site-001",
                                     environment_id="env-001", name="隔离环境甲", kind="isolated_gpu")
        service.register_scenario(request_id="req-scenario", actor_id="operator-001", site_id="site-001",
                                  scenario_id="scn-001", name="提示注入探测", category="prompt_injection",
                                  description="验证系统提示防护")
        service.create_campaign(request_id="req-campaign", actor_id="operator-001", campaign_id="camp-001",
                                target_id="target-001", environment_id="env-001", owner_team_id="team-red",
                                name="第一轮红队测试",
                                steps=[{"scenario_id": "scn-001", "name": "注入探测执行",
                                        "owner_team_id": "team-red"}])
        explain = service.explain_campaign("camp-001")
        service.start_campaign(request_id="req-start", actor_id="operator-001", campaign_id="camp-001")
        service.attach_evidence(request_id="req-evidence", actor_id="operator-001", campaign_id="camp-001",
                                evidence_id="evd-001", kind="transcript", uri="file:///runs/camp-001/t1.log",
                                content_hash="a" * 64, note="攻击对话记录")
        service.complete_step(request_id="req-step", actor_id="operator-001", campaign_id="camp-001",
                              step_id="camp-001:step:1", note="完成注入探测")
        service.report_finding(request_id="req-finding", actor_id="operator-001", campaign_id="camp-001",
                               finding_id="fnd-001", title="系统提示可被绕过", severity="high",
                               description="多轮对话后可诱导越权回答", review_team_id="team-review",
                               step_id="camp-001:step:1")
        service.review_finding(request_id="req-review", actor_id="reviewer-001", finding_id="fnd-001",
                               outcome="confirmed", note="可复现，确认有效")
        blocked = service.release_readiness("target-001")
        service.complete_campaign(request_id="req-complete", actor_id="operator-001", campaign_id="camp-001")
        service.resolve_finding(request_id="req-resolve", actor_id="reviewer-001", finding_id="fnd-001",
                                evidence_id="evd-001", note="修复后复测通过")
        service.decide_release(request_id="req-decision", actor_id="reviewer-001", target_id="target-001",
                               decision="approved", rationale="严重发现已处置，复核通过")
        readiness = service.release_readiness("target-001")
        snapshot = service.get_snapshot("camp-001")
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "campaign_state_before_start": explain["state"],
                  "campaign_status": service.get_campaign("camp-001")["status"],
                  "snapshot_frozen": bool(snapshot["snapshot_hash"]),
                  "blocked_before_resolve": not blocked["approvable"],
                  "release_approvable": readiness["approvable"],
                  "release_decision": readiness["latest_decision"]["decision"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] and result["snapshot_frozen"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
