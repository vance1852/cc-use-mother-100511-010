"""编排红队测试活动：目标、环境、场景、发现、复核与开放决定。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService


SEVERITIES = frozenset({"low", "medium", "high", "critical"})
BLOCKING_SEVERITIES = frozenset({"high", "critical"})
EVIDENCE_KINDS = frozenset({"log", "transcript", "screenshot", "artifact", "report"})
REVIEW_OUTCOMES = frozenset({"confirmed", "rejected"})
RELEASE_DECISIONS = frozenset({"approved", "conditional", "blocked"})


class RedTeamService(DomainService):
    """在基础服务的权限、幂等、事务与审计能力之上编排红队测试活动。"""

    # ---------- 内部助手 ----------

    def _check_org(self, actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的资源")

    def _optional_text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _target(self, connection, target_id: str):
        row = connection.execute("SELECT * FROM targets WHERE target_id=?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError("测试目标不存在")
        return row

    def _campaign(self, connection, campaign_id: str):
        row = connection.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError("测试活动不存在")
        return row

    def _finding(self, connection, finding_id: str):
        row = connection.execute("SELECT * FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None:
            raise NotFoundError("发现项不存在")
        return row

    def _team(self, connection, team_id: str):
        row = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFoundError("团队不存在")
        return row

    def _site_organization(self, connection, site_id: str) -> str:
        row = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row["organization_id"]

    def _target_organization(self, connection, target_id: str) -> str:
        row = connection.execute(
            "SELECT s.organization_id AS org FROM targets t JOIN sites s ON t.site_id=s.site_id "
            "WHERE t.target_id=?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError("测试目标不存在")
        return row["org"]

    def _campaign_organization(self, connection, campaign_id: str) -> str:
        row = connection.execute(
            "SELECT s.organization_id AS org FROM campaigns c "
            "JOIN targets t ON c.target_id=t.target_id JOIN sites s ON t.site_id=s.site_id "
            "WHERE c.campaign_id=?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError("测试活动不存在")
        return row["org"]

    def _snapshot_row(self, connection, campaign_id: str):
        return connection.execute(
            "SELECT * FROM campaign_snapshots WHERE campaign_id=?", (campaign_id,)).fetchone()

    def _active_allocation(self, connection, environment_id: str):
        return connection.execute(
            "SELECT * FROM environment_allocations WHERE environment_id=? AND status='active'",
            (environment_id,)).fetchone()

    def _acquire_environment(self, connection, *, actor_id: str, campaign, now: str) -> None:
        holder = self._active_allocation(connection, campaign["environment_id"])
        if holder is not None and holder["campaign_id"] != campaign["campaign_id"]:
            raise ConflictError(f"环境正被活动 {holder['campaign_id']} 占用")
        if holder is None:
            connection.execute(
                "INSERT INTO environment_allocations(allocation_id,environment_id,campaign_id,status,acquired_at) "
                "VALUES(?,?,?,'active',?)",
                (uuid.uuid4().hex, campaign["environment_id"], campaign["campaign_id"], now))
            append_event(connection, actor_id=actor_id, action="environment.acquired",
                         resource_type="environment", resource_id=campaign["environment_id"],
                         detail={"campaign_id": campaign["campaign_id"]}, occurred_at=now)

    def _release_environment(self, connection, *, actor_id: str, campaign_id: str, now: str) -> None:
        row = connection.execute(
            "SELECT * FROM environment_allocations WHERE campaign_id=? AND status='active'",
            (campaign_id,)).fetchone()
        if row is None:
            return
        connection.execute(
            "UPDATE environment_allocations SET status='released', released_at=? WHERE allocation_id=?",
            (now, row["allocation_id"]))
        append_event(connection, actor_id=actor_id, action="environment.released",
                     resource_type="environment", resource_id=row["environment_id"],
                     detail={"campaign_id": campaign_id}, occurred_at=now)

    def _dependency_rows(self, connection, campaign_id: str):
        return connection.execute(
            "SELECT d.depends_on, c.name, c.round_no, c.status, c.owner_team_id "
            "FROM campaign_dependencies d JOIN campaigns c ON c.campaign_id=d.depends_on "
            "WHERE d.campaign_id=? ORDER BY c.round_no, c.campaign_id", (campaign_id,)).fetchall()

    def _incomplete_dependencies(self, connection, campaign_id: str):
        return [row for row in self._dependency_rows(connection, campaign_id)
                if row["status"] != "completed"]

    def _assert_acyclic(self, connection, campaign_id: str, depends_on: str) -> None:
        stack = [depends_on]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == campaign_id:
                raise ValidationError("依赖关系会形成循环")
            if current in seen:
                continue
            seen.add(current)
            rows = connection.execute(
                "SELECT depends_on FROM campaign_dependencies WHERE campaign_id=?", (current,)).fetchall()
            stack.extend(row["depends_on"] for row in rows)

    def _freeze_snapshot(self, connection, *, actor_id: str, campaign, now: str) -> tuple[str, str]:
        """把一轮活动的步骤、发现与证据固化为不可变快照。"""

        campaign_id = campaign["campaign_id"]
        steps = connection.execute(
            "SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY sequence", (campaign_id,)).fetchall()
        findings = connection.execute(
            "SELECT * FROM findings WHERE campaign_id=? ORDER BY created_at, finding_id", (campaign_id,)).fetchall()
        evidence = connection.execute(
            "SELECT * FROM evidence WHERE campaign_id=? ORDER BY created_at, evidence_id", (campaign_id,)).fetchall()
        dependencies = [row["depends_on"] for row in connection.execute(
            "SELECT depends_on FROM campaign_dependencies WHERE campaign_id=? ORDER BY depends_on",
            (campaign_id,)).fetchall()]
        payload = {
            "campaign_id": campaign_id,
            "name": campaign["name"],
            "round_no": campaign["round_no"],
            "target_id": campaign["target_id"],
            "environment_id": campaign["environment_id"],
            "owner_team_id": campaign["owner_team_id"],
            "status": campaign["status"],
            "started_at": campaign["started_at"],
            "completed_at": campaign["completed_at"],
            "dependencies": dependencies,
            "steps": [{"step_id": row["step_id"], "sequence": row["sequence"],
                       "scenario_id": row["scenario_id"], "owner_team_id": row["owner_team_id"],
                       "name": row["name"], "status": row["status"],
                       "result_note": row["result_note"], "finished_at": row["finished_at"]}
                      for row in steps],
            "findings": [{"finding_id": row["finding_id"], "step_id": row["step_id"],
                          "scenario_id": row["scenario_id"], "title": row["title"],
                          "severity": row["severity"], "status": row["status"],
                          "review_team_id": row["review_team_id"], "reviewed_by": row["reviewed_by"],
                          "reviewed_at": row["reviewed_at"], "review_note": row["review_note"],
                          "created_by": row["created_by"], "created_at": row["created_at"]}
                         for row in findings],
            "evidence": [{"evidence_id": row["evidence_id"], "finding_id": row["finding_id"],
                          "kind": row["kind"], "uri": row["uri"], "content_hash": row["content_hash"],
                          "note": row["note"], "created_by": row["created_by"],
                          "created_at": row["created_at"]}
                         for row in evidence],
        }
        snapshot_hash = digest(payload)
        snapshot_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO campaign_snapshots(snapshot_id,campaign_id,target_id,round_no,snapshot_json,"
            "snapshot_hash,frozen_by,frozen_at) VALUES(?,?,?,?,?,?,?,?)",
            (snapshot_id, campaign_id, campaign["target_id"], campaign["round_no"],
             canonical_json(payload), snapshot_hash, actor_id, now))
        append_event(connection, actor_id=actor_id, action="campaign.frozen",
                     resource_type="campaign", resource_id=campaign_id,
                     detail={"snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash,
                             "round_no": campaign["round_no"], "status": campaign["status"]},
                     occurred_at=now)
        return snapshot_id, snapshot_hash

    def _readiness(self, connection, target_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT finding_id, title, severity, status FROM findings WHERE target_id=? "
            "ORDER BY created_at, finding_id", (target_id,)).fetchall()
        counts = {"open": 0, "confirmed": 0, "rejected": 0, "resolved": 0}
        for row in rows:
            counts[row["status"]] += 1
        open_findings = [dict(row) for row in rows if row["status"] == "open"]
        blocking = [dict(row) for row in rows
                    if row["status"] == "confirmed" and row["severity"] in BLOCKING_SEVERITIES]
        reasons = []
        if open_findings:
            reasons.append({"code": "findings_unreviewed", "count": len(open_findings),
                            "finding_ids": [row["finding_id"] for row in open_findings]})
        if blocking:
            reasons.append({"code": "severe_findings_unresolved", "count": len(blocking),
                            "finding_ids": [row["finding_id"] for row in blocking]})
        latest = connection.execute(
            "SELECT * FROM release_decisions WHERE target_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (target_id,)).fetchone()
        return {"approvable": not reasons, "reasons": reasons, "counts": counts,
                "open_findings": open_findings, "blocking_findings": blocking,
                "latest_decision": dict(latest) if latest else None}

    def _describe_team(self, connection, team_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT team_id, name FROM teams WHERE team_id=?",
                                 (team_id,)).fetchone()
        if row is None:
            return {"team_id": team_id, "team_name": None}
        return {"team_id": row["team_id"], "team_name": row["name"]}

    # ---------- 登记 ----------

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      organization_id: str, name: str, specialty: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "team_id": team_id, "organization_id": organization_id,
                   "name": name, "specialty": specialty}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._check_org(actor, organization_id)
            team_id = self._identifier(team_id, "team_id")
            name = self._text(name, "name")
            specialty = self._text(specialty, "specialty", 80)
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO teams(team_id,organization_id,name,specialty,created_at) VALUES(?,?,?,?,?)",
                        (team_id, organization_id, name, specialty, self._now()))
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "specialty": specialty}, occurred_at=self._now())
                return "team", team_id, {"team_id": team_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_team", payload=payload, create=create)

    def register_target(self, *, request_id: str, actor_id: str, site_id: str, target_id: str,
                        name: str, model_version: str, description: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "target_id": target_id,
                   "name": name, "model_version": model_version, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            organization_id = self._site_organization(connection, site_id)
            self._check_org(actor, organization_id)
            target_id = self._identifier(target_id, "target_id")
            name = self._text(name, "name")
            model_version = self._text(model_version, "model_version", 80)
            description = self._optional_text(description, "description", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO targets(target_id,site_id,name,model_version,description,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (target_id, site_id, name, model_version, description, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("测试目标编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="target.registered",
                             resource_type="target", resource_id=target_id,
                             detail={"site_id": site_id, "name": name, "model_version": model_version},
                             occurred_at=self._now())
                return "target", target_id, {"target_id": target_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_target", payload=payload, create=create)

    def register_environment(self, *, request_id: str, actor_id: str, site_id: str,
                             environment_id: str, name: str, kind: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "environment_id": environment_id,
                   "name": name, "kind": kind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._check_org(actor, self._site_organization(connection, site_id))
            environment_id = self._identifier(environment_id, "environment_id")
            name = self._text(name, "name")
            kind = self._text(kind, "kind", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO environments(environment_id,site_id,name,kind,created_at) VALUES(?,?,?,?,?)",
                        (environment_id, site_id, name, kind, self._now()))
                except Exception as exc:
                    raise ConflictError("环境编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="environment.registered",
                             resource_type="environment", resource_id=environment_id,
                             detail={"site_id": site_id, "name": name, "kind": kind},
                             occurred_at=self._now())
                return "environment", environment_id, {"environment_id": environment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_environment", payload=payload, create=create)

    def register_scenario(self, *, request_id: str, actor_id: str, site_id: str, scenario_id: str,
                          name: str, category: str, description: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "scenario_id": scenario_id,
                   "name": name, "category": category, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._check_org(actor, self._site_organization(connection, site_id))
            scenario_id = self._identifier(scenario_id, "scenario_id")
            name = self._text(name, "name")
            category = self._text(category, "category", 80)
            description = self._optional_text(description, "description", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO scenarios(scenario_id,site_id,name,category,description,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (scenario_id, site_id, name, category, description, self._now()))
                except Exception as exc:
                    raise ConflictError("攻击场景编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="scenario.registered",
                             resource_type="scenario", resource_id=scenario_id,
                             detail={"site_id": site_id, "name": name, "category": category},
                             occurred_at=self._now())
                return "scenario", scenario_id, {"scenario_id": scenario_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_scenario", payload=payload, create=create)

    # ---------- 活动编排 ----------

    def create_campaign(self, *, request_id: str, actor_id: str, campaign_id: str, target_id: str,
                        environment_id: str, owner_team_id: str, name: str,
                        steps: list[dict[str, Any]], dependencies: list[str] | None = None) -> WriteReceipt:
        if not isinstance(steps, list) or not steps:
            raise ValidationError("steps 必须是非空列表")
        dependencies = list(dependencies or [])
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "target_id": target_id,
                   "environment_id": environment_id, "owner_team_id": owner_team_id, "name": name,
                   "steps": steps, "dependencies": dependencies}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign_id = self._identifier(campaign_id, "campaign_id")
            name = self._text(name, "name")
            self._target(connection, target_id)
            organization_id = self._target_organization(connection, target_id)
            self._check_org(actor, organization_id)
            environment = connection.execute("SELECT * FROM environments WHERE environment_id=?",
                                             (environment_id,)).fetchone()
            if environment is None:
                raise NotFoundError("隔离环境不存在")
            if self._site_organization(connection, environment["site_id"]) != organization_id:
                raise ValidationError("环境与测试目标不属于同一组织")
            owner = self._team(connection, owner_team_id)
            if owner["organization_id"] != organization_id:
                raise ValidationError("负责团队与测试目标不属于同一组织")
            normalized_steps = []
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    raise ValidationError("steps 元素必须是对象")
                scenario_id = str(step.get("scenario_id", "")).strip()
                scenario = connection.execute("SELECT * FROM scenarios WHERE scenario_id=?",
                                              (scenario_id,)).fetchone()
                if scenario is None:
                    raise NotFoundError("攻击场景不存在")
                if self._site_organization(connection, scenario["site_id"]) != organization_id:
                    raise ValidationError("攻击场景与测试目标不属于同一组织")
                step_team = self._team(connection, str(step.get("owner_team_id", "")).strip())
                if step_team["organization_id"] != organization_id:
                    raise ValidationError("步骤负责团队与测试目标不属于同一组织")
                normalized_steps.append({
                    "sequence": index, "scenario_id": scenario_id,
                    "owner_team_id": step_team["team_id"],
                    "name": self._text(step.get("name", ""), "steps.name"),
                })
            normalized_dependencies = []
            for depends_on in dependencies:
                depends_on = self._identifier(depends_on, "depends_on")
                if depends_on == campaign_id:
                    raise ValidationError("活动不能依赖自身")
                if depends_on in normalized_dependencies:
                    raise ValidationError("依赖列表存在重复")
                self._campaign(connection, depends_on)
                normalized_dependencies.append(depends_on)

            def create() -> tuple[str, str, dict[str, Any]]:
                round_no = connection.execute(
                    "SELECT COALESCE(MAX(round_no),0)+1 AS next FROM campaigns WHERE target_id=?",
                    (target_id,)).fetchone()["next"]
                try:
                    connection.execute(
                        "INSERT INTO campaigns(campaign_id,target_id,environment_id,owner_team_id,name,"
                        "round_no,status,created_by,created_at) VALUES(?,?,?,?,?,?,'planned',?,?)",
                        (campaign_id, target_id, environment_id, owner_team_id, name, round_no,
                         actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("活动编号已经存在") from exc
                for step in normalized_steps:
                    connection.execute(
                        "INSERT INTO campaign_steps(step_id,campaign_id,sequence,scenario_id,owner_team_id,"
                        "name,status) VALUES(?,?,?,?,?,?,'pending')",
                        (f"{campaign_id}:step:{step['sequence']}", campaign_id, step["sequence"],
                         step["scenario_id"], step["owner_team_id"], step["name"]))
                for depends_on in normalized_dependencies:
                    connection.execute(
                        "INSERT INTO campaign_dependencies(campaign_id,depends_on,created_at) VALUES(?,?,?)",
                        (campaign_id, depends_on, self._now()))
                append_event(connection, actor_id=actor_id, action="campaign.created",
                             resource_type="campaign", resource_id=campaign_id,
                             detail={"target_id": target_id, "round_no": round_no,
                                     "environment_id": environment_id, "owner_team_id": owner_team_id,
                                     "steps": len(normalized_steps),
                                     "dependencies": normalized_dependencies},
                             occurred_at=self._now())
                return "campaign", campaign_id, {"campaign_id": campaign_id, "round_no": round_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_campaign", payload=payload, create=create)

    def add_campaign_dependency(self, *, request_id: str, actor_id: str, campaign_id: str,
                                depends_on: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "depends_on": depends_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))
            depends_on = self._identifier(depends_on, "depends_on")
            if depends_on == campaign_id:
                raise ValidationError("活动不能依赖自身")
            self._campaign(connection, depends_on)

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "planned":
                    raise ConflictError("只有计划中的活动可以调整依赖")
                self._assert_acyclic(connection, campaign_id, depends_on)
                try:
                    connection.execute(
                        "INSERT INTO campaign_dependencies(campaign_id,depends_on,created_at) VALUES(?,?,?)",
                        (campaign_id, depends_on, self._now()))
                except Exception as exc:
                    raise ConflictError("依赖关系已经存在") from exc
                append_event(connection, actor_id=actor_id, action="campaign.dependency_added",
                             resource_type="campaign", resource_id=campaign_id,
                             detail={"depends_on": depends_on}, occurred_at=self._now())
                return "campaign", campaign_id, {"campaign_id": campaign_id, "depends_on": depends_on}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_campaign_dependency", payload=payload, create=create)

    def start_campaign(self, *, request_id: str, actor_id: str, campaign_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "planned":
                    raise ConflictError("只有计划中的活动可以启动")
                incomplete = self._incomplete_dependencies(connection, campaign_id)
                if incomplete:
                    blockers = ", ".join(row["depends_on"] for row in incomplete)
                    raise ConflictError(f"依赖活动尚未完成: {blockers}")
                self._acquire_environment(connection, actor_id=actor_id, campaign=campaign,
                                          now=self._now())
                connection.execute(
                    "UPDATE campaigns SET status='running', started_at=? WHERE campaign_id=?",
                    (self._now(), campaign_id))
                append_event(connection, actor_id=actor_id, action="campaign.started",
                             resource_type="campaign", resource_id=campaign_id,
                             detail={"round_no": campaign["round_no"],
                                     "environment_id": campaign["environment_id"]},
                             occurred_at=self._now())
                return "campaign", campaign_id, {"campaign_id": campaign_id, "status": "running"}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_campaign", payload=payload, create=create)

    def _step_for_update(self, connection, campaign_id: str, step_id: str):
        step = connection.execute(
            "SELECT * FROM campaign_steps WHERE step_id=? AND campaign_id=?",
            (step_id, campaign_id)).fetchone()
        if step is None:
            raise NotFoundError("步骤不存在")
        if step["status"] != "pending":
            raise ConflictError("步骤已被处理")
        earliest = connection.execute(
            "SELECT step_id FROM campaign_steps WHERE campaign_id=? AND status='pending' "
            "ORDER BY sequence LIMIT 1", (campaign_id,)).fetchone()
        if earliest is None or earliest["step_id"] != step_id:
            raise ConflictError("必须按顺序执行步骤")
        return step

    def complete_step(self, *, request_id: str, actor_id: str, campaign_id: str, step_id: str,
                      note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "step_id": step_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))
            note = self._optional_text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "running":
                    raise ConflictError("活动不在执行中")
                step = self._step_for_update(connection, campaign_id, step_id)
                connection.execute(
                    "UPDATE campaign_steps SET status='done', result_note=?, finished_at=? WHERE step_id=?",
                    (note, self._now(), step_id))
                append_event(connection, actor_id=actor_id, action="campaign.step_completed",
                             resource_type="campaign_step", resource_id=step_id,
                             detail={"campaign_id": campaign_id, "sequence": step["sequence"],
                                     "note": note}, occurred_at=self._now())
                return "campaign_step", step_id, {"step_id": step_id, "status": "done"}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_step", payload=payload, create=create)

    def fail_step(self, *, request_id: str, actor_id: str, campaign_id: str, step_id: str,
                  note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "step_id": step_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))
            note = self._optional_text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "running":
                    raise ConflictError("活动不在执行中")
                step = self._step_for_update(connection, campaign_id, step_id)
                connection.execute(
                    "UPDATE campaign_steps SET status='failed', result_note=?, finished_at=? WHERE step_id=?",
                    (note, self._now(), step_id))
                connection.execute("UPDATE campaigns SET status='failed' WHERE campaign_id=?",
                                   (campaign_id,))
                self._release_environment(connection, actor_id=actor_id, campaign_id=campaign_id,
                                          now=self._now())
                append_event(connection, actor_id=actor_id, action="campaign.step_failed",
                             resource_type="campaign_step", resource_id=step_id,
                             detail={"campaign_id": campaign_id, "sequence": step["sequence"],
                                     "note": note, "campaign_status": "failed"},
                             occurred_at=self._now())
                return "campaign_step", step_id, {"step_id": step_id, "status": "failed",
                                                  "campaign_status": "failed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="fail_step", payload=payload, create=create)

    def resume_campaign(self, *, request_id: str, actor_id: str, campaign_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "failed":
                    raise ConflictError("只有失败的活动可以恢复")
                if self._snapshot_row(connection, campaign_id) is not None:
                    raise ConflictError("活动已冻结，不能恢复")
                self._acquire_environment(connection, actor_id=actor_id, campaign=campaign,
                                          now=self._now())
                connection.execute(
                    "UPDATE campaign_steps SET status='pending', result_note='', finished_at=NULL "
                    "WHERE campaign_id=? AND status='failed'", (campaign_id,))
                connection.execute("UPDATE campaigns SET status='running' WHERE campaign_id=?",
                                   (campaign_id,))
                next_step = connection.execute(
                    "SELECT sequence FROM campaign_steps WHERE campaign_id=? AND status='pending' "
                    "ORDER BY sequence LIMIT 1", (campaign_id,)).fetchone()
                append_event(connection, actor_id=actor_id, action="campaign.resumed",
                             resource_type="campaign", resource_id=campaign_id,
                             detail={"resume_from_sequence": next_step["sequence"] if next_step else None},
                             occurred_at=self._now())
                return "campaign", campaign_id, {"campaign_id": campaign_id, "status": "running"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resume_campaign", payload=payload, create=create)

    def complete_campaign(self, *, request_id: str, actor_id: str, campaign_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "running":
                    raise ConflictError("只有执行中的活动可以完成")
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM campaign_steps WHERE campaign_id=? AND status!='done'",
                    (campaign_id,)).fetchone()["count"]
                if remaining:
                    raise ConflictError("仍有未完成的步骤")
                now = self._now()
                connection.execute(
                    "UPDATE campaigns SET status='completed', completed_at=? WHERE campaign_id=?",
                    (now, campaign_id))
                self._release_environment(connection, actor_id=actor_id, campaign_id=campaign_id, now=now)
                append_event(connection, actor_id=actor_id, action="campaign.completed",
                             resource_type="campaign", resource_id=campaign_id,
                             detail={"round_no": campaign["round_no"]}, occurred_at=now)
                frozen = self._campaign(connection, campaign_id)
                snapshot_id, snapshot_hash = self._freeze_snapshot(
                    connection, actor_id=actor_id, campaign=frozen, now=now)
                return "campaign", campaign_id, {"campaign_id": campaign_id, "status": "completed",
                                                 "snapshot_id": snapshot_id,
                                                 "snapshot_hash": snapshot_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_campaign", payload=payload, create=create)

    def freeze_campaign(self, *, request_id: str, actor_id: str, campaign_id: str) -> WriteReceipt:
        """把失败的活动归档为不可变的历史轮次，之后只能以新轮次继续。"""

        payload = {"actor_id": actor_id, "campaign_id": campaign_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] != "failed":
                    raise ConflictError("只有失败的活动需要手动冻结")
                if self._snapshot_row(connection, campaign_id) is not None:
                    raise ConflictError("活动已冻结")
                snapshot_id, snapshot_hash = self._freeze_snapshot(
                    connection, actor_id=actor_id, campaign=campaign, now=self._now())
                return "campaign", campaign_id, {"campaign_id": campaign_id,
                                                 "snapshot_id": snapshot_id,
                                                 "snapshot_hash": snapshot_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_campaign", payload=payload, create=create)

    # ---------- 发现项、复核与证据 ----------

    def report_finding(self, *, request_id: str, actor_id: str, campaign_id: str, finding_id: str,
                       title: str, severity: str, description: str, review_team_id: str,
                       step_id: str | None = None, scenario_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "finding_id": finding_id,
                   "title": title, "severity": severity, "description": description,
                   "review_team_id": review_team_id, "step_id": step_id, "scenario_id": scenario_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            organization_id = self._campaign_organization(connection, campaign_id)
            self._check_org(actor, organization_id)
            finding_id = self._identifier(finding_id, "finding_id")
            title = self._text(title, "title")
            description = self._text(description, "description", 2000)
            if severity not in SEVERITIES:
                raise ValidationError("severity 不在允许范围内")
            review_team = self._team(connection, review_team_id)
            if review_team["organization_id"] != organization_id:
                raise ValidationError("复核团队与测试目标不属于同一组织")

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] not in ("running", "failed"):
                    raise ConflictError("活动未在执行中，不能登记发现项")
                if self._snapshot_row(connection, campaign_id) is not None:
                    raise ConflictError("活动已冻结，不能登记发现项")
                step_row = None
                if step_id:
                    step_row = connection.execute(
                        "SELECT * FROM campaign_steps WHERE step_id=? AND campaign_id=?",
                        (step_id, campaign_id)).fetchone()
                    if step_row is None:
                        raise NotFoundError("步骤不存在")
                final_scenario = scenario_id or (step_row["scenario_id"] if step_row else None)
                if final_scenario and connection.execute(
                        "SELECT 1 FROM scenarios WHERE scenario_id=?",
                        (final_scenario,)).fetchone() is None:
                    raise NotFoundError("攻击场景不存在")
                try:
                    connection.execute(
                        "INSERT INTO findings(finding_id,target_id,campaign_id,step_id,scenario_id,title,"
                        "severity,description,status,review_team_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'open',?,?,?)",
                        (finding_id, campaign["target_id"], campaign_id, step_id, final_scenario,
                         title, severity, description, review_team_id, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("发现项编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="finding.reported",
                             resource_type="finding", resource_id=finding_id,
                             detail={"campaign_id": campaign_id, "target_id": campaign["target_id"],
                                     "severity": severity, "review_team_id": review_team_id},
                             occurred_at=self._now())
                return "finding", finding_id, {"finding_id": finding_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_finding", payload=payload, create=create)

    def review_finding(self, *, request_id: str, actor_id: str, finding_id: str,
                       outcome: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "finding_id": finding_id, "outcome": outcome, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            finding = self._finding(connection, finding_id)
            self._check_org(actor, self._target_organization(connection, finding["target_id"]))
            if outcome not in REVIEW_OUTCOMES:
                raise ValidationError("outcome 必须是 confirmed 或 rejected")
            note = self._optional_text(note, "note", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                if finding["status"] != "open":
                    raise ConflictError("发现项已完成复核")
                connection.execute(
                    "UPDATE findings SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                    "WHERE finding_id=?",
                    (outcome, actor_id, self._now(), note, finding_id))
                append_event(connection, actor_id=actor_id, action="finding.reviewed",
                             resource_type="finding", resource_id=finding_id,
                             detail={"outcome": outcome, "severity": finding["severity"],
                                     "target_id": finding["target_id"]},
                             occurred_at=self._now())
                return "finding", finding_id, {"finding_id": finding_id, "status": outcome}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_finding", payload=payload, create=create)

    def resolve_finding(self, *, request_id: str, actor_id: str, finding_id: str,
                        evidence_id: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "finding_id": finding_id,
                   "evidence_id": evidence_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            finding = self._finding(connection, finding_id)
            self._check_org(actor, self._target_organization(connection, finding["target_id"]))
            note = self._optional_text(note, "note", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                if finding["status"] != "confirmed":
                    raise ConflictError("只有已确认的发现项可以标记处置")
                evidence = connection.execute("SELECT * FROM evidence WHERE evidence_id=?",
                                              (evidence_id,)).fetchone()
                if evidence is None:
                    raise NotFoundError("证据不存在")
                evidence_campaign = self._campaign(connection, evidence["campaign_id"])
                if evidence_campaign["target_id"] != finding["target_id"]:
                    raise ValidationError("证据与发现项不属于同一测试目标")
                connection.execute(
                    "UPDATE findings SET status='resolved', resolution_evidence_id=?, "
                    "resolution_note=?, resolved_by=?, resolved_at=? WHERE finding_id=?",
                    (evidence_id, note, actor_id, self._now(), finding_id))
                append_event(connection, actor_id=actor_id, action="finding.resolved",
                             resource_type="finding", resource_id=finding_id,
                             detail={"evidence_id": evidence_id,
                                     "evidence_campaign_id": evidence_campaign["campaign_id"],
                                     "evidence_round_no": evidence_campaign["round_no"]},
                             occurred_at=self._now())
                return "finding", finding_id, {"finding_id": finding_id, "status": "resolved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_finding", payload=payload, create=create)

    def attach_evidence(self, *, request_id: str, actor_id: str, campaign_id: str,
                        evidence_id: str, kind: str, uri: str, content_hash: str,
                        finding_id: str | None = None, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "campaign_id": campaign_id, "evidence_id": evidence_id,
                   "kind": kind, "uri": uri, "content_hash": content_hash,
                   "finding_id": finding_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            campaign = self._campaign(connection, campaign_id)
            self._check_org(actor, self._campaign_organization(connection, campaign_id))
            evidence_id = self._identifier(evidence_id, "evidence_id")
            if kind not in EVIDENCE_KINDS:
                raise ValidationError("kind 不在允许范围内")
            uri = self._text(uri, "uri", 500)
            content_hash = self._text(content_hash, "content_hash", 128)
            note = self._optional_text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if campaign["status"] not in ("running", "failed"):
                    raise ConflictError("活动未在执行中，不能补充证据")
                if self._snapshot_row(connection, campaign_id) is not None:
                    raise ConflictError("活动已冻结，不能补充证据")
                if finding_id:
                    finding = self._finding(connection, finding_id)
                    if finding["target_id"] != campaign["target_id"]:
                        raise ValidationError("证据关联的发现项不属于同一测试目标")
                try:
                    connection.execute(
                        "INSERT INTO evidence(evidence_id,campaign_id,finding_id,kind,uri,content_hash,"
                        "note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (evidence_id, campaign_id, finding_id, kind, uri, content_hash, note,
                         actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("证据编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="evidence.attached",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"campaign_id": campaign_id, "finding_id": finding_id,
                                     "kind": kind, "content_hash": content_hash},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="attach_evidence", payload=payload, create=create)

    # ---------- 开放决定 ----------

    def decide_release(self, *, request_id: str, actor_id: str, target_id: str,
                       decision: str, rationale: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_id": target_id,
                   "decision": decision, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            self._target(connection, target_id)
            self._check_org(actor, self._target_organization(connection, target_id))
            if decision not in RELEASE_DECISIONS:
                raise ValidationError("decision 不在允许范围内")
            rationale = self._text(rationale, "rationale", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                readiness = self._readiness(connection, target_id)
                if decision == "approved" and not readiness["approvable"]:
                    details = "；".join(f"{reason['code']}:{reason['count']}"
                                       for reason in readiness["reasons"])
                    raise ConflictError(f"不能批准开放（{details}）")
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO release_decisions(decision_id,target_id,decision,rationale,"
                    "open_findings,blocking_findings,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (decision_id, target_id, decision, rationale, readiness["counts"]["open"],
                     len(readiness["blocking_findings"]), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="release.decided",
                             resource_type="target", resource_id=target_id,
                             detail={"decision_id": decision_id, "decision": decision,
                                     "open_findings": readiness["counts"]["open"],
                                     "blocking_findings": len(readiness["blocking_findings"])},
                             occurred_at=self._now())
                return "release_decision", decision_id, {"decision_id": decision_id,
                                                         "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_release", payload=payload, create=create)

    # ---------- 查询与解释 ----------

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        connection = self.database.connection
        campaign = self._campaign(connection, campaign_id)
        steps = [dict(row) for row in connection.execute(
            "SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY sequence",
            (campaign_id,)).fetchall()]
        dependencies = [dict(row) for row in self._dependency_rows(connection, campaign_id)]
        allocation = connection.execute(
            "SELECT 1 FROM environment_allocations WHERE campaign_id=? AND status='active'",
            (campaign_id,)).fetchone()
        snapshot = self._snapshot_row(connection, campaign_id)
        findings = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM findings WHERE campaign_id=? GROUP BY status",
            (campaign_id,)).fetchall()}
        return {"campaign_id": campaign["campaign_id"], "target_id": campaign["target_id"],
                "environment_id": campaign["environment_id"],
                "owner_team_id": campaign["owner_team_id"], "name": campaign["name"],
                "round_no": campaign["round_no"], "status": campaign["status"],
                "created_by": campaign["created_by"], "created_at": campaign["created_at"],
                "started_at": campaign["started_at"], "completed_at": campaign["completed_at"],
                "steps": steps, "dependencies": dependencies,
                "environment_held": allocation is not None,
                "frozen": snapshot is not None,
                "snapshot_hash": snapshot["snapshot_hash"] if snapshot else None,
                "findings": findings}

    def explain_campaign(self, campaign_id: str) -> dict[str, Any]:
        """解释活动当前状态：为何等待、谁在阻塞、哪个团队负责下一步。"""

        connection = self.database.connection
        campaign = self._campaign(connection, campaign_id)
        status = campaign["status"]
        dependencies = [dict(row) for row in self._dependency_rows(connection, campaign_id)]
        incomplete = [row for row in dependencies if row["status"] != "completed"]
        holder = self._active_allocation(connection, campaign["environment_id"])
        holder_info = None
        if holder is not None and holder["campaign_id"] != campaign_id:
            holder_campaign = self._campaign(connection, holder["campaign_id"])
            holder_info = {"campaign_id": holder_campaign["campaign_id"],
                           "name": holder_campaign["name"],
                           "round_no": holder_campaign["round_no"],
                           "owner_team_id": holder_campaign["owner_team_id"]}
        waiting_reasons: list[dict[str, Any]] = []
        if status == "planned":
            for row in incomplete:
                waiting_reasons.append({"code": "dependency_incomplete",
                                        "campaign_id": row["depends_on"], "name": row["name"],
                                        "round_no": row["round_no"], "status": row["status"],
                                        "owner_team_id": row["owner_team_id"]})
            if holder_info is not None:
                waiting_reasons.append({"code": "environment_occupied",
                                        "environment_id": campaign["environment_id"],
                                        "holder": holder_info})
        next_step = None
        if status in ("running", "failed"):
            statuses = ("pending",) if status == "running" else ("failed", "pending")
            marks = ",".join("?" * len(statuses))
            row = connection.execute(
                f"SELECT * FROM campaign_steps WHERE campaign_id=? AND status IN ({marks}) "
                "ORDER BY sequence LIMIT 1", (campaign_id, *statuses)).fetchone()
            if row is not None:
                next_step = {"step_id": row["step_id"], "sequence": row["sequence"],
                             "name": row["name"], "scenario_id": row["scenario_id"],
                             "owner_team_id": row["owner_team_id"], "status": row["status"]}
        open_findings = connection.execute(
            "SELECT COUNT(*) AS count FROM findings WHERE campaign_id=? AND status='open'",
            (campaign_id,)).fetchone()["count"]
        snapshot = self._snapshot_row(connection, campaign_id)
        responsible = None
        if status == "planned":
            if incomplete:
                first = incomplete[0]
                responsible = {**self._describe_team(connection, first["owner_team_id"]),
                               "action": "完成依赖活动", "campaign_id": first["depends_on"]}
            elif holder_info is not None:
                responsible = {**self._describe_team(connection, holder_info["owner_team_id"]),
                               "action": "释放环境", "campaign_id": holder_info["campaign_id"]}
            else:
                responsible = {**self._describe_team(connection, campaign["owner_team_id"]),
                               "action": "启动活动"}
        elif status == "running" and next_step is not None:
            responsible = {**self._describe_team(connection, next_step["owner_team_id"]),
                           "action": f"执行步骤 {next_step['sequence']}",
                           "step_id": next_step["step_id"]}
        elif status == "failed":
            responsible = {**self._describe_team(connection, campaign["owner_team_id"]),
                           "action": "恢复活动或冻结归档"}
        elif status == "completed" and open_findings:
            row = connection.execute(
                "SELECT review_team_id FROM findings WHERE campaign_id=? AND status='open' "
                "ORDER BY created_at, finding_id LIMIT 1", (campaign_id,)).fetchone()
            responsible = {**self._describe_team(connection, row["review_team_id"]),
                           "action": "复核发现项"}
        state = status
        if status == "planned":
            state = "waiting" if waiting_reasons else "ready"
        return {"campaign_id": campaign_id, "name": campaign["name"],
                "round_no": campaign["round_no"], "status": status, "state": state,
                "target_id": campaign["target_id"], "environment_id": campaign["environment_id"],
                "owner_team_id": campaign["owner_team_id"],
                "waiting_reasons": waiting_reasons, "next_step": next_step,
                "responsible": responsible, "open_findings": open_findings,
                "dependencies": dependencies,
                "environment": {"environment_id": campaign["environment_id"],
                                "held_by_this": holder is not None
                                and holder["campaign_id"] == campaign_id,
                                "holder": holder_info},
                "frozen": snapshot is not None,
                "snapshot_hash": snapshot["snapshot_hash"] if snapshot else None}

    def schedule_target(self, target_id: str) -> dict[str, Any]:
        """按依赖关系给出目标下各轮活动的可执行顺序。"""

        connection = self.database.connection
        target = self._target(connection, target_id)
        campaigns = connection.execute(
            "SELECT * FROM campaigns WHERE target_id=? ORDER BY round_no, campaign_id",
            (target_id,)).fetchall()
        by_id = {row["campaign_id"]: row for row in campaigns}
        if not by_id:
            return {"target_id": target_id, "model_version": target["model_version"], "order": []}
        marks = ",".join("?" * len(by_id))
        dep_rows = connection.execute(
            f"SELECT campaign_id, depends_on FROM campaign_dependencies WHERE campaign_id IN ({marks})",
            tuple(by_id)).fetchall()
        dependencies: dict[str, list[str]] = {cid: [] for cid in by_id}
        dependents: dict[str, list[str]] = {}
        indegree = {cid: 0 for cid in by_id}
        for row in dep_rows:
            dependencies[row["campaign_id"]].append(row["depends_on"])
            if row["depends_on"] in by_id:
                dependents.setdefault(row["depends_on"], []).append(row["campaign_id"])
                indegree[row["campaign_id"]] += 1

        def sort_key(cid: str) -> tuple[int, str]:
            return (by_id[cid]["round_no"], cid)

        available = sorted([cid for cid, degree in indegree.items() if degree == 0], key=sort_key)
        order: list[str] = []
        while available:
            current = available.pop(0)
            order.append(current)
            for nxt in dependents.get(current, ()):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    available.append(nxt)
            available.sort(key=sort_key)
        items = []
        for cid in order:
            campaign = by_id[cid]
            blocked_by: list[dict[str, Any]] = []
            if campaign["status"] == "planned":
                for row in self._incomplete_dependencies(connection, cid):
                    blocked_by.append({"code": "dependency_incomplete",
                                       "campaign_id": row["depends_on"],
                                       "status": row["status"]})
                holder = self._active_allocation(connection, campaign["environment_id"])
                if holder is not None and holder["campaign_id"] != cid:
                    blocked_by.append({"code": "environment_occupied",
                                       "holder_campaign_id": holder["campaign_id"]})
            items.append({"campaign_id": cid, "name": campaign["name"],
                          "round_no": campaign["round_no"], "status": campaign["status"],
                          "depends_on": dependencies[cid],
                          "runnable": campaign["status"] == "planned" and not blocked_by,
                          "blocked_by": blocked_by})
        return {"target_id": target_id, "model_version": target["model_version"], "order": items}

    def get_snapshot(self, campaign_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._campaign(connection, campaign_id)
        row = self._snapshot_row(connection, campaign_id)
        if row is None:
            raise NotFoundError("活动尚未冻结快照")
        return {"snapshot_id": row["snapshot_id"], "campaign_id": row["campaign_id"],
                "target_id": row["target_id"], "round_no": row["round_no"],
                "snapshot_hash": row["snapshot_hash"], "frozen_by": row["frozen_by"],
                "frozen_at": row["frozen_at"], "snapshot": json.loads(row["snapshot_json"])}

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        """返回发现项详情，并解释各轮次证据如何被引用。"""

        connection = self.database.connection
        finding = self._finding(connection, finding_id)
        campaign = self._campaign(connection, finding["campaign_id"])
        review_team = self._team(connection, finding["review_team_id"])
        evidence = []
        referenced_rounds: set[int] = set()
        rows = connection.execute(
            "SELECT e.*, c.round_no AS round_no, c.name AS campaign_name FROM evidence e "
            "JOIN campaigns c ON c.campaign_id=e.campaign_id WHERE e.finding_id=? "
            "ORDER BY c.round_no, e.created_at, e.evidence_id", (finding_id,)).fetchall()
        for row in rows:
            snapshot = self._snapshot_row(connection, row["campaign_id"])
            referenced_rounds.add(row["round_no"])
            evidence.append({"evidence_id": row["evidence_id"], "kind": row["kind"],
                             "uri": row["uri"], "content_hash": row["content_hash"],
                             "note": row["note"], "campaign_id": row["campaign_id"],
                             "campaign_name": row["campaign_name"], "round_no": row["round_no"],
                             "frozen": snapshot is not None,
                             "snapshot_hash": snapshot["snapshot_hash"] if snapshot else None})
        resolution = None
        if finding["resolution_evidence_id"]:
            row = connection.execute(
                "SELECT e.*, c.round_no AS round_no, c.name AS campaign_name FROM evidence e "
                "JOIN campaigns c ON c.campaign_id=e.campaign_id WHERE e.evidence_id=?",
                (finding["resolution_evidence_id"],)).fetchone()
            if row is not None:
                referenced_rounds.add(row["round_no"])
                resolution = {"evidence_id": row["evidence_id"], "kind": row["kind"],
                              "uri": row["uri"], "content_hash": row["content_hash"],
                              "campaign_id": row["campaign_id"],
                              "campaign_name": row["campaign_name"], "round_no": row["round_no"],
                              "note": finding["resolution_note"],
                              "resolved_by": finding["resolved_by"],
                              "resolved_at": finding["resolved_at"]}
        return {"finding_id": finding["finding_id"], "target_id": finding["target_id"],
                "campaign_id": finding["campaign_id"], "step_id": finding["step_id"],
                "scenario_id": finding["scenario_id"], "title": finding["title"],
                "severity": finding["severity"], "description": finding["description"],
                "status": finding["status"], "created_by": finding["created_by"],
                "created_at": finding["created_at"],
                "campaign": {"campaign_id": campaign["campaign_id"], "name": campaign["name"],
                             "round_no": campaign["round_no"]},
                "review": {"status": finding["status"], "team_id": review_team["team_id"],
                           "team_name": review_team["name"],
                           "reviewed_by": finding["reviewed_by"],
                           "reviewed_at": finding["reviewed_at"],
                           "note": finding["review_note"]},
                "evidence": evidence, "resolution": resolution,
                "referenced_rounds": sorted(referenced_rounds)}

    def list_findings(self, target_id: str | None = None, campaign_id: str | None = None,
                      status: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        query = "SELECT * FROM findings WHERE 1=1"
        if target_id:
            query += " AND target_id=?"
            parameters.append(target_id)
        if campaign_id:
            query += " AND campaign_id=?"
            parameters.append(campaign_id)
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY created_at, finding_id"
        return [dict(row) for row in
                self.database.connection.execute(query, parameters).fetchall()]

    def list_campaigns(self, target_id: str | None = None,
                       status: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        query = "SELECT * FROM campaigns WHERE 1=1"
        if target_id:
            query += " AND target_id=?"
            parameters.append(target_id)
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY target_id, round_no, campaign_id"
        return [dict(row) for row in
                self.database.connection.execute(query, parameters).fetchall()]

    def release_readiness(self, target_id: str) -> dict[str, Any]:
        """汇总发现项复核结论如何影响当前开放决定。"""

        connection = self.database.connection
        target = self._target(connection, target_id)
        readiness = self._readiness(connection, target_id)
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM release_decisions WHERE target_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 10", (target_id,)).fetchall()]
        return {"target_id": target["target_id"], "name": target["name"],
                "model_version": target["model_version"],
                "approvable": readiness["approvable"], "reasons": readiness["reasons"],
                "counts": readiness["counts"], "open_findings": readiness["open_findings"],
                "blocking_findings": readiness["blocking_findings"],
                "latest_decision": readiness["latest_decision"], "decisions": decisions}

    def list_environments(self, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        items = []
        for row in connection.execute(
                "SELECT * FROM environments WHERE site_id=? ORDER BY environment_id",
                (site_id,)).fetchall():
            holder = self._active_allocation(connection, row["environment_id"])
            holder_info = None
            if holder is not None:
                campaign = self._campaign(connection, holder["campaign_id"])
                holder_info = {"campaign_id": campaign["campaign_id"], "name": campaign["name"],
                               "round_no": campaign["round_no"],
                               "owner_team_id": campaign["owner_team_id"],
                               "acquired_at": holder["acquired_at"]}
            items.append({"environment_id": row["environment_id"], "name": row["name"],
                          "kind": row["kind"], "occupied": holder is not None,
                          "holder": holder_info})
        return items

    def list_teams(self, organization_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.database.connection.execute(
            "SELECT * FROM teams WHERE organization_id=? ORDER BY team_id",
            (organization_id,)).fetchall()]

    def list_targets(self, site_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.database.connection.execute(
            "SELECT * FROM targets WHERE site_id=? ORDER BY created_at, target_id",
            (site_id,)).fetchall()]

    def list_scenarios(self, site_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.database.connection.execute(
            "SELECT * FROM scenarios WHERE site_id=? ORDER BY created_at, scenario_id",
            (site_id,)).fetchall()]
