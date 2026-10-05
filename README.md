# 监管冻结解冻流程

本项目维护监管冻结解冻流程的领域约定、角色边界与样例数据，并提供一套**仅依赖 Python 标准库**的服务端：登记冻结案件（额度范围、来源批次条件、期限、审批链），计算可用余额时动态扣除有效冻结；扩大、缩减、续期、解冻均走审批并生成版本与分录；冻结与交易并发时由数据库事务串行化；历史成交永久保留当时的检查结果；案件证据仅授权角色可见。

## 角色与边界

| 角色（请求头代码） | 主要权限 |
| --- | --- |
| 企业申报员 `enterprise` | 登记/提交冻结案件 |
| 核算专员 `accounting` | 审批初审；发起扩大/缩减/续期/解冻；查看证据 |
| 监管审计员 `auditor` | 审批终审；发起变更/解冻；查看证据；到期批处理 |
| 交易运营员 `trading` | 余额检查、交易扣款、查询成交与检查记录 |

案件证据（`evidence_refs`）仅核算专员、监管审计员可见；其他角色读取案件时 `evidence=null`，只返回 `evidence_count`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/freeze_service/`：冻结/解冻服务端。
  - `store.py`：SQLite 存储（WAL + `BEGIN IMMEDIATE`，写事务进入即加保留锁）。
  - `models.py`：角色、状态码、审批链、错误类型。
  - `service.py`：案件登记、审批链、版本/分录、余额计算、交易、到期批处理。
  - `api.py`：标准库 `http.server` 实现的 JSON API。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域服务、并发一致性、HTTP API 回归测试。

## 核心规则

- **条件化额度冻结**：每条冻结可指定 `source_batch`（来源批次）；不指定即整户额度冻结。可用余额 = 账户余额 − 当前有效冻结。批次冻结只影响该批次资金，无争议业务照常进行。多案件对同一批次的冻结为相互独立的法律冻结，累加并封顶于批次剩余。
- **额度范围**：登记时 `amount` 为初始冻结额，`cap_amount` 为可扩大上限（缺省等于初始额）；扩大超过上限被拒绝。
- **审批版本链**：案件登记链默认 `核算专员 → 监管审计员`；扩大/缩减/续期/解冻使用独立的变更审批链。每一步留痕（角色、决定、操作人、时间、意见），每次状态变化生成不可变版本，版本含当时案件完整快照并通过 `parent_id` 串链。
- **分录**：激活 `freeze_activate`、扩大 `freeze_expand`、缩减 `freeze_shrink`、续期 `freeze_renew`、审批解冻 `freeze_release`、到期失效 `freeze_expire`，逐笔记账，带符号变动。
- **交易并发一致性**：余额检查与扣款在同一个立即型写事务内完成；并发冻结变更与交易被数据库串行化。交易以 `txn_ref` 幂等。成交（含被拒绝交易）永久保留当时的余额、冻结构成快照。
- **期限**：未到生效时间审批通过的案件为 `confirmed`，到期批处理 `/admin/sweep-expired` 负责激活与过期失效。

## 运行

```bash
PYTHONPATH=src python3 -m freeze_service.api --host 127.0.0.1 --port 8080 --db freeze.sqlite3
```

所有接口需请求头 `X-Actor-Id` 与 `X-Role`（角色代码见表）。主要接口：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /accounts` | 开户 |
| `POST /accounts/{e}/deposits` | 充值（可带 `batch_no` 登记来源批次池） |
| `GET  /accounts/{e}/available` | 当前余额、有效冻结、可用余额及构成 |
| `POST /accounts/{e}/checks` | 只检查不扣款 |
| `POST /accounts/{e}/transactions` | 交易扣款（原子检查+扣款，返回当时检查快照） |
| `GET  /accounts/{e}/transactions` | 历史成交及当时检查结果 |
| `GET  /accounts/{e}/checks` | 全部余额检查记录（含拒绝） |
| `POST /cases` | 登记冻结案件（`freezes`、期限、审批链、证据） |
| `POST /cases/{id}/submit` `/approve` `/reject` | 提交与审批 |
| `POST /cases/{id}/changes` | `kind=amend`（扩大/缩减/续期）或 `unfreeze` |
| `POST /changes/{id}/approve` `/reject` | 变更审批 |
| `GET  /cases/{id}/versions` `/entries` | 审批版本链与冻结分录 |
| `GET  /cases?enterprise_id={e}` | 案件列表（证据按角色脱敏） |
| `POST /admin/sweep-expired` | 到期/到生效时间批处理 |

登记示例：

```json
{
  "enterprise_id": "E-7",
  "title": "批次0910033调查",
  "freezes": [
    {"amount": 200, "cap_amount": 400, "source_batch": "B-2026-09"}
  ],
  "effective_from": "2026-10-05T00:00:00+00:00",
  "effective_to": "2026-11-05T00:00:00+00:00",
  "evidence_refs": ["ev://case/0910033-007"],
  "freeze_chain": ["核算专员", "监管审计员"],
  "amend_chain": ["核算专员", "监管审计员"]
}
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
