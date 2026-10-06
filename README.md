# 红队测试活动编排服务

本项目在人工智能治理基础能力（角色权限、请求幂等、SQLite 事务、哈希链审计）之上，提供模型开放前的红队测试活动编排：登记测试目标、隔离环境、攻击场景与负责团队，按依赖关系调度多轮测试活动，互斥管理环境占用，支持失败恢复与逐轮冻结快照，并让发现项的复核结论直接约束后续开放决定。

## 领域概念

- **测试目标 target**：待开放的模型版本，挂在业务场所下。
- **隔离环境 environment**：执行攻击场景的隔离资源；同一时刻只允许一个活动占用（数据库部分唯一索引保证），失败或完成时自动释放。
- **攻击场景 scenario**：可复用的攻击手法登记（如提示注入、数据外泄）。
- **测试活动 campaign**：针对目标的一轮测试，状态机为 `planned → running → failed/completed`。创建时声明步骤与依赖活动；轮次号按目标自动递增。
- **步骤 step**：活动内按顺序执行，只允许处理最早的待办步骤；步骤失败使活动失败并释放环境。
- **发现项 finding**：`open → confirmed/rejected`，已确认的可用同目标任意轮次的证据标记 `resolved`。
- **证据 evidence**：带内容哈希的工件引用，可关联到同目标的跨轮次发现项。
- **冻结快照 snapshot**：活动完成时自动冻结（失败活动可手动冻结归档），固化当轮步骤、发现与证据清单及哈希，之后该轮不可再变更。
- **开放决定 release decision**：存在未复核发现或未处置的高危/严重发现时，不能记录 `approved`。

## 行为规则

- 环境冲突时只有一个活动获准运行，其余活动保持等待，`explain` 接口给出占用者与负责团队。
- 活动启动前要求所有依赖活动已完成；依赖关系拒绝成环。
- 失败的活动恢复（`resume`）后重新获得环境，已完成的步骤保留，从首个未完成步骤继续。
- 每轮完成后冻结快照，历史轮次的证据通过发现项详情中的轮次号与快照哈希被引用。
- 所有写接口幂等（`request_id`），并写入可校验的审计哈希链。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /teams` `/targets` `/environments` `/scenarios` | 登记团队、目标、环境、场景 |
| `POST /campaigns` | 创建活动（含步骤与依赖） |
| `POST /campaigns/{id}/dependencies` | 追加依赖（仅计划中） |
| `POST /campaigns/{id}/start` `/resume` `/complete` `/freeze` | 活动状态流转 |
| `POST /campaigns/{id}/steps/{sid}/complete` `/fail` | 步骤执行 |
| `POST /findings` | 登记发现项 |
| `POST /findings/{id}/review` `/resolve` | 复核确认/驳回、标记处置 |
| `POST /evidence` | 登记证据（可跨轮次关联发现项） |
| `POST /targets/{id}/release-decisions` | 记录开放决定 |
| `GET /campaigns/{id}/explain` | 解释为何等待、哪个团队负责下一步 |
| `GET /campaigns/{id}/snapshot` | 查看冻结快照 |
| `GET /targets/{id}/schedule` | 按依赖关系排出可执行顺序 |
| `GET /targets/{id}/release-readiness` | 复核结论如何影响开放决定 |
| `GET /findings/{id}` | 发现项详情与跨轮次证据引用 |
| `GET /environments?site_id=` | 环境占用情况 |

写接口需要在请求头携带 `X-Actor-Id`，角色分工：`admin/operator` 执行登记与测试，`reviewer/admin` 复核与开放决定，`auditor` 只读。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.api --database ai_governance.sqlite3 --host 127.0.0.1 --port 8080
```

服务提供健康检查和带操作者身份的业务请求，重启后 SQLite 中的状态与审计历史继续保留。
