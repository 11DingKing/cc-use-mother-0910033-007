# 监管冻结解冻流程

本项目维护监管冻结解冻流程的领域约定、角色边界、样例数据与 **Python 服务端**，供后端服务、接口和自动化验证统一使用。契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并落实**条件化额度冻结、审批版本链、交易并发一致性、案件证据隔离**四个关键约束。

## 解决的问题

调查期间监管人员只需冻结企业的**部分积分**或**特定来源批次**：

- 旧系统只能整户停用，无争议业务被误伤；本服务支持「额度区间冻结」与「按来源批次条件冻结」，计算可用余额时**动态扣除有效冻结**，其余业务正常成交。
- 扩大、缩减、续期、解冻均生成**新版本与不可变分录**，完整**审批链**记录每一步由谁在何时批准。
- 冻结变更与交易**并发提交时保持一致**（账户级互斥锁 + SQLite `BEGIN IMMEDIATE` 单写事务，检查与扣款同事务）。
- 每笔成交/检查**留存当时的余额快照**，事后可还原"当时为什么放行/拦截"。
- 案件证据仅向授权角色开放，交易运营员只见冻结对余额的影响、不见证据。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/reg_freeze/`：监管冻结服务端
  - `models.py`：案件、版本、分录、审批步骤、冻结作用域等领域模型；
  - `security.py`：角色、动作权限矩阵、证据可见性；
  - `repository.py`：SQLite 仓储（WAL、线程独立连接、只追加台账）；
  - `service.py`：领域服务（登记/提交/审批/变更/解冻/到期/余额/交易）；
  - `api.py`：标准库 HTTP API（Bearer 令牌鉴权）；
  - `server.py`：服务启动入口。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tests/`：契约、领域行为、并发一致性、HTTP API 回归测试。

## 角色与动作矩阵

| 动作 | 企业申报员 | 核算专员 | 交易运营员 | 监管审计员 |
| --- | :---: | :---: | :---: | :---: |
| 登记/提交冻结案件 | ✅ | | | |
| 审批 / 驳回（审批链环节） | | ✅ | | ✅ |
| 申请扩大/缩减/续期/解冻 | ✅ | ✅ | | |
| 执行交易、查看余额 | | | ✅ | ✅ |
| 紧急解冻 | | | | ✅ |
| 查看案件证据/审批链 | 仅本人案件 | ✅ | ❌ | ✅ |

审批链在登记时通过 `approval_chain` 指定（默认 `["核算专员"]`，可配多级，如
`["核算专员","监管审计员"]`），**解冻沿用案件完整审批链**，其余变更由核算专员单级审批。

## 案件状态

```
草稿 ──提交──▶ 待核算 ──全部审批环节通过──▶ 已确认(生效时间未到) ──到点──▶ 执行中
                  │                         生效时间已到则直接进入执行中 ▲
                  └──驳回──▶ 草稿                                           │
执行中/已确认 ──解冻(完整链批准)──▶ 已封存                                   │
执行中/已确认 ──冻结期限届满(sweep)──▶ 已封存（写到期版本+释放分录） ─────────┘
```

## 冻结作用域与余额计算

- **账户级冻结**：`amount_limit`，占用账户额度区间。
- **来源条件冻结**：`sources` 为 `{批次号: 上限}`（上限可填一个超大数表示按批次当前全额）。
- 有效冻结 = 状态为「已确认/执行中」且在最新版本期限内、**按分录净额求和**的结果；草稿、待核算、已封存、已过期均不计入。
- `可用余额 = 总余额 − 有效冻结`；来源批次冻结优先占用对应批次额度，账户级冻结占用剩余部分。
- 扩大/缩减/续期/解冻各生成一个**版本**；扩大、缩减、解冻、到期生成对应**带符号分录**，续期生成零金额留痕分录。

## HTTP API

鉴权：所有请求需 `Authorization: Bearer <token>`。内置演示令牌：
`token-filer` / `token-accountant` / `token-operator` / `token-auditor`。

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /admin/accounts` `{account_id, balance}` | 维护账户 |
| `POST /admin/sources` `{account_id, source_id, amount}` | 维护来源批次额度 |
| `POST /cases` | 登记案件（`amount_limit` 或 `sources`、`expire_at`、`effective_from`、`evidence_ref`、`evidence_text`、`approval_chain`） |
| `POST /cases/{id}/submit` `/approve` `/reject` `/emergency-unfreeze` | 提交/审批/驳回/紧急解冻 |
| `GET  /cases` `/cases/{id}` | 案件列表/详情（证据按角色脱敏） |
| `GET  /cases/{id}/approval-chain` `/evidence` `/amendments` | 审批链/证据/变更申请 |
| `POST /cases/{id}/amendments` `{action: 扩大\|缩减\|续期\|解冻, ...}` | 发起变更申请 |
| `POST /amendments/{pid}/approve` `/reject` | 变更审批（解冻走完整链） |
| `GET  /accounts/{id}/balance` | 当前可用余额（动态扣除有效冻结） |
| `POST /accounts/{id}/checks` `{amount, sources?}` | 只检查不扣款，留存快照 |
| `POST /accounts/{id}/transactions` `{amount, sources?}` | 交易执行（检查+扣款同事务） |
| `GET  /accounts/{id}/checks` `/checks/{check_id}` `/checks` | 历史检查（含当时快照） |
| `POST /sweep` `{at?}` | 到点激活/到期封存（可注入时间便于测试） |

错误码：`403` 无权限（无令牌同样 403）、`404` 不存在或无权查看证据（统一 404 防侧信道）、
`409` 状态/业务/并发冲突、`400` 请求格式错误。

### 快速试用

```bash
PYTHONPATH=src python3 -m reg_freeze.server --port 8080          # 内存库
PYTHONPATH=src python3 -m reg_freeze.server --db ./data/freeze.db # 持久化
```

```bash
# 账户 10000，冻结其中 3000
curl -s -X POST localhost:8080/admin/accounts -H "Authorization: Bearer token-operator" \
  -H "Content-Type: application/json" -d '{"account_id":"E-1","balance":10000}'
CID=$(curl -s -X POST localhost:8080/cases -H "Authorization: Bearer token-filer" \
  -H "Content-Type: application/json" \
  -d '{"account_id":"E-1","amount_limit":3000,"evidence_ref":"ev://x"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['case_id'])")
curl -s -X POST localhost:8080/cases/$CID/submit   -H "Authorization: Bearer token-filer"   -d '{}'
curl -s -X POST localhost:8080/cases/$CID/approve  -H "Authorization: Bearer token-accountant" -d '{}'
curl -s localhost:8080/accounts/E-1/balance -H "Authorization: Bearer token-operator"
# => frozen=3000, available=7000；无争议的 7000 以内交易正常
```

## 并发一致性

- 写事务一律 `BEGIN IMMEDIATE`：SQLite 同一时刻只允许一个写事务，冻结变更与交易检查/扣款在数据库层**串行提交**，配合进程内按账户互斥锁缩小竞争。
- 交易执行把「读余额→读有效冻结→判定→扣款→写历史快照」放在**同一事务**，不会出现两笔交易都读到旧可用余额而超支。
- `freeze_entries`、`case_versions`、`approval_steps`、`balance_checks` 全部**只追加**，历史不可变。
- `tests/test_concurrency.py` 用线程池验证：50 笔并发交易不会超支、扩大/解冻与交易并发后 `余额 ≥ 有效冻结`、单案件版本号连续且分录与版本对应。

## 验证

```bash
python3 -m unittest discover -s tests -v          # 全部回归（契约+领域+并发+HTTP）
python3 -m compileall -q src tools tests          # 编译检查
python3 tools/check_contract.py domain/contract.json
```

金额一律使用**整数积分**，不使用浮点。时间统一 ISO 8601（带时区）。
