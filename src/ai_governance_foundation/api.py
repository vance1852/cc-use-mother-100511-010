"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .redteam import RedTeamService
from .storage import Database


def route(service: RedTeamService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if method == "POST" and parsed.path == "/teams":
            receipt = service.register_team(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/targets":
            receipt = service.register_target(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/environments":
            receipt = service.register_environment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scenarios":
            receipt = service.register_scenario(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/campaigns":
            receipt = service.create_campaign(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/findings":
            receipt = service.report_finding(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/evidence":
            receipt = service.attach_evidence(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "dependencies":
            receipt = service.add_campaign_dependency(actor_id=actor_id, campaign_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "start":
            receipt = service.start_campaign(actor_id=actor_id, campaign_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "resume":
            receipt = service.resume_campaign(actor_id=actor_id, campaign_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "complete":
            receipt = service.complete_campaign(actor_id=actor_id, campaign_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "freeze":
            receipt = service.freeze_campaign(actor_id=actor_id, campaign_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 5 and segments[0] == "campaigns" \
                and segments[2] == "steps" and segments[4] == "complete":
            receipt = service.complete_step(actor_id=actor_id, campaign_id=segments[1],
                                            step_id=segments[3], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 5 and segments[0] == "campaigns" \
                and segments[2] == "steps" and segments[4] == "fail":
            receipt = service.fail_step(actor_id=actor_id, campaign_id=segments[1],
                                        step_id=segments[3], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "findings" \
                and segments[2] == "review":
            receipt = service.review_finding(actor_id=actor_id, finding_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "findings" \
                and segments[2] == "resolve":
            receipt = service.resolve_finding(actor_id=actor_id, finding_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(segments) == 3 and segments[0] == "targets" \
                and segments[2] == "release-decisions":
            receipt = service.decide_release(actor_id=actor_id, target_id=segments[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/teams":
            query = parse_qs(parsed.query)
            organization_id = query.get("organization_id", [""])[0]
            if not organization_id:
                raise ValidationError("organization_id 不能为空")
            return 200, {"items": service.list_teams(organization_id)}
        if method == "GET" and parsed.path == "/targets":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_targets(site_id)}
        if method == "GET" and parsed.path == "/environments":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_environments(site_id)}
        if method == "GET" and parsed.path == "/scenarios":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_scenarios(site_id)}
        if method == "GET" and parsed.path == "/campaigns":
            query = parse_qs(parsed.query)
            return 200, {"items": service.list_campaigns(
                target_id=query.get("target_id", [None])[0],
                status=query.get("status", [None])[0])}
        if method == "GET" and len(segments) == 2 and segments[0] == "campaigns":
            return 200, service.get_campaign(segments[1])
        if method == "GET" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "explain":
            return 200, service.explain_campaign(segments[1])
        if method == "GET" and len(segments) == 3 and segments[0] == "campaigns" \
                and segments[2] == "snapshot":
            return 200, service.get_snapshot(segments[1])
        if method == "GET" and len(segments) == 3 and segments[0] == "targets" \
                and segments[2] == "schedule":
            return 200, service.schedule_target(segments[1])
        if method == "GET" and len(segments) == 3 and segments[0] == "targets" \
                and segments[2] == "release-readiness":
            return 200, service.release_readiness(segments[1])
        if method == "GET" and parsed.path == "/findings":
            query = parse_qs(parsed.query)
            return 200, {"items": service.list_findings(
                target_id=query.get("target_id", [None])[0],
                campaign_id=query.get("campaign_id", [None])[0],
                status=query.get("status", [None])[0])}
        if method == "GET" and len(segments) == 2 and segments[0] == "findings":
            return 200, service.get_finding(segments[1])
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: RedTeamService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动红队测试活动编排服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = RedTeamService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
