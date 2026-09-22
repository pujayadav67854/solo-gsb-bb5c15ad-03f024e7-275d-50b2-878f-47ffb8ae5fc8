# Rule decision service

一个基于 **FastAPI + SQLite + Docker** 的规则决策服务：

- 用户创建**规则集**，规则可通过表达式引用其他规则的命中结果，形成**条件依赖图**；
- 提交文档（任意 JSON）进行**决策计算**，按依赖图迭代到**稳定（定点）结果**；
- 返回命中的规则顺序、决策值以及**冲突解释**（为什么某条命中规则被压制）；
- 保存时**检测循环依赖**并拒绝保存（HTTP 400，错误码 `CYCLIC_DEPENDENCY`）；
- 规则集**版本化**：每次修改生成不可变新版本并自动重算最新版，旧版本与旧结果永久保留可查。

## 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 应用入口 / HTTP API
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # SQLite 连接、建表、初始化
│   ├── crud.py            # 持久化层（规则集 / 版本 / 决策结果）
│   ├── engine/
│   │   ├── ast_eval.py    # 基于 AST 的安全表达式求值（沙箱）
│   │   ├── compiler.py    # 规则编译：引用、依赖、循环、冲突策略校验
│   │   └── evaluator.py   # 拓扑排序 + 定点迭代 + 冲突处理
│   └── seed.py            # 演示规则集初始化
├── tests/                 # pytest 单元/接口测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，自动建库并写入演示规则集 demo-promotions
```

### Docker 直接运行（不使用 Compose）

```bash
# 构建镜像
docker build -t rule-decision-service:latest .

# 启动容器；用命名卷持久化 SQLite 数据
docker run -d \
  --name rule-decision-service \
  -p 8000:8000 \
  -e SEED_DEMO=1 \
  -e DATABASE_URL='sqlite:////data/rules.db' \
  -v rule-data:/data \
  rule-decision-service:latest

# 查看日志 / 停止 / 重新启动
docker logs -f rule-decision-service
docker stop rule-decision-service
docker start rule-decision-service
```

镜像说明：

- 容器内监听 `8000`，用 `-p 8000:8000` 映射到宿主机；
- 数据库默认写容器内 `/data/rules.db`（声明为 VOLUME），生产环境务必挂载
  命名卷或宿主机目录，否则容器删除后数据丢失；
- 环境变量：
  - `SEED_DEMO=1`：首次启动写入演示规则集 `demo-promotions`（默认已开启）；
  - `DATABASE_URL`：SQLite 路径，形如 `sqlite:////data/rules.db`；
- 镜像内置 `/health` 健康检查（`HEALTHCHECK`），可用
  `docker ps` 或 `docker inspect` 观察健康状态。


健康检查与交互文档：

- 健康检查：<http://localhost:8000/health>
- Swagger UI：<http://localhost:8000/docs>
- OpenAPI：<http://localhost:8000/openapi.json>

### 本地运行

```bash
pip install -r requirements.txt
SEED_DEMO=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件默认 `./data/rules.db`，可用环境变量 `DATABASE_URL` 修改。

## 规则模型

规则集（ruleset）：

| 字段 | 说明 |
| --- | --- |
| `name` / `description` | 名称与描述 |
| `default_conflict_policy` | 默认冲突策略：`collect_all` / `priority` / `first_hit` |
| `rules` | 规则列表（见下） |

规则（rule）：

| 字段 | 说明 |
| --- | --- |
| `code` | 规则编码，`[a-zA-Z_][a-zA-Z0-9_]*`，规则集内唯一 |
| `name` / `description` | 名称与描述 |
| `priority` | 优先级，数值越大越优先（默认 0） |
| `conflict_group` | 冲突组标识；同一组内命中的规则互相冲突 |
| `conflict_policy` | 本组冲突策略，缺省继承规则集默认值 |
| `depends_on` | 依赖的其他规则编码（计算顺序保证 + 循环检测依据） |
| `condition` | 条件表达式，求值为真则命中。空表达式视为恒真 |
| `action` | 动作表达式，命中时计算，作为该规则的决策值 |

### 表达式语言

条件/动作是受限的 Python 表达式，由 AST 白名单沙箱执行：

- 文档通过变量 `doc` 访问，例如 `doc["amount"] >= 100`；
- 其他规则的命中结果可通过 `规则编码.命中` 访问，例如：
  - `is_vip.hit` —— 规则 `is_vip` 是否命中（布尔）；
  - `is_vip.value` —— 规则 `is_vip` 动作的返回值；
- 支持 `and / or / not`、比较运算、算术、三元表达式、列表/字典字面量、
  `in / not in`；
- 允许的函数：`len / str / int / float / bool / round / min / max / sum / abs / lower / upper / contains`；
  允许的字符串方法：`lower / upper / startswith / endswith / contains`。
- **禁止**属性穿透、导入、调用任意函数、下标越界（缺省键一律得到 `None`）。

> 条件中引用其他规则时，被引用的规则编码必须出现在 `depends_on` 中，
> 否则保存时报 `UNDECLARED_REFERENCE`。

### 冲突策略

同一 `conflict_group` 中多条规则同时命中时：

- `collect_all`（默认）：全部保留，决策值按字段合并（字段互相覆盖时给出解释）；
- `priority`：只保留 `priority` 最高者，其余标记为 `suppressed`（附原因）；
- `first_hit`：只保留依赖拓扑序中的第一条，其余标记为 `suppressed`。

## 决策语义：依赖图与定点

1. 按 `depends_on` 做拓扑排序得到确定的评估顺序（无法排序即存在环，拒绝保存）；
2. 每一轮按拓扑序评估全部规则，上一轮结果作为 `rule.hit / rule.value` 输入；
3. 重复评估直到所有规则的命中状态与动作值不再变化（定点）；
4. 在命中集合上执行冲突处理，输出最终决策、命中顺序与冲突解释。

响应示例：

```json
{
  "id": "9f1c...",
  "ruleset_id": "demo-promotions",
  "version": 2,
  "document": {"amount": 1500, "customer": {"vip": true}},
  "fired_rule_codes": ["is_vip", "large_order", "vip_discount", "free_shipping"],
  "hit_rule_codes": ["vip_discount", "free_shipping"],
  "evaluation_order": ["is_vip", "large_order", "vip_discount", "free_shipping"],
  "iterations": 2,
  "converged": true,
  "decision": {"discount": 0.15, "free_shipping": true},
  "conflicts": [
    {
      "group": "discount",
      "policy": "priority",
      "winner": "vip_discount",
      "suppressed": [
        {"rule_code": "large_order", "reason": "lower_priority", "priority": 10}
      ]
    }
  ],
  "traces": [ {"rule_code": "vip_discount", "hit": true, "value": {"discount": 0.15}} ],
  "created_at": "2026-09-21T00:00:00"
}
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 健康检查 |
| `POST` | `/api/rulesets` | 创建规则集（编译校验，含循环依赖检测） |
| `GET`  | `/api/rulesets` | 列出全部规则集 |
| `GET`  | `/api/rulesets/{id}` | 获取规则集元数据 |
| `GET`  | `/api/rulesets/{id}/versions` | 列出全部版本 |
| `GET`  | `/api/rulesets/{id}/versions/{version}` | 获取指定版本快照 |
| `POST` | `/api/rulesets/{id}/impact` | 静态依赖影响分析（不求值、不落库） |
| `PUT`  | `/api/rulesets/{id}` | 修改规则集 → 生成新版本并自动重算 |
| `POST` | `/api/rulesets/{id}/evaluate` | 用最新版（或 `?version=n` 指定旧版）决策，默认持久化结果 |
| `POST` | `/api/rulesets/{id}/evaluate-batch` | 批量决策（≤100 项），整批固定同一版本，逐项隔离错误 |
| `GET`  | `/api/rulesets/{id}/evaluations` | 列出该规则集的历史决策结果 |
| `GET`  | `/api/rulesets/{id}/evaluation-stats` | 聚合已持久化决策：规则命中率与决策字段统计（支持 `version` / `since` / `until` 筛选） |
| `GET`  | `/api/evaluations/{eval_id}` | 按 ID 查询单次决策结果 |
| `POST` | `/api/rulesets/{id}/recompute` | 用旧文档对最新版重算（保留旧结果） |

### 三分钟试用

```bash
# 1) 对演示规则集提交文档（大额 + VIP）
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"document": {"amount": 1500, "customer": {"vip": true}, "region": "north"}}' | python3 -m json.tool

# 2) 普通小单
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"document": {"amount": 80, "region": "north"}}' | python3 -m json.tool

# 3) 查询历史结果
curl -s localhost:8000/api/rulesets/demo-promotions/evaluations | python3 -m json.tool
```

### 批量评估

`POST /api/rulesets/{id}/evaluate-batch` 一次提交多份文档：

- 请求体 `documents` 为 JSON 数组，**单次最多 100 项**（超出返回 HTTP 422）；
- 可选 `version`（请求体内）指定规则版本，缺省使用最新版；**整批固定使用
  同一规则版本**，只解析编译一次；
- 每项**独立求值**：某项求值失败只记录该项错误，**不中止整批**；
- 响应按输入 `index` 逐项返回，并汇总 `success_count` / `error_count`；
- 与单次评估一致，默认持久化各项成功结果；加 `?persist=false` 只算不存
  （失败项不会落库）；
- 规则集或指定版本不存在属于整批级错误，直接返回 HTTP 404。

请求示例：

```bash
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/evaluate-batch \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "documents": [
      {"amount": 1500, "customer": {"vip": true}, "region": "north"},
      {"amount": 80, "region": "north"},
      {"amount": 10, "coupons": ["SAVE10", "SAVE20"]}
    ]
  }' | python3 -m json.tool
```

响应示例（每项结构与单次 `evaluate` 一致；此处 `result` 有省略）：

```json
{
  "ruleset_id": "demo-promotions",
  "version": 1,
  "total": 3,
  "success_count": 3,
  "error_count": 0,
  "results": [
    {
      "index": 0,
      "status": "success",
      "result": {
        "id": "9f1c...",
        "ruleset_id": "demo-promotions",
        "version": 1,
        "decision": {"discount": 0.15, "free_shipping": true},
        "fired_rule_codes": ["is_vip", "large_order", "vip_discount", "free_shipping"],
        "hit_rule_codes": ["vip_discount", "free_shipping"],
        "converged": true,
        "iterations": 2
      },
      "error": null
    },
    {
      "index": 1,
      "status": "success",
      "result": {"decision": {}, "fired_rule_codes": [], "converged": true},
      "error": null
    },
    {
      "index": 2,
      "status": "success",
      "result": {"decision": {"coupon": "SAVE10"}, "fired_rule_codes": ["coupon_save10", "coupon_save20"]},
      "error": null
    }
  ]
}
```

某项求值失败时，该项 `status` 为 `error`、`result` 为 `null`，错误信息在
`error` 中，其他项照常求值：

```json
{
  "index": 2,
  "status": "error",
  "result": null,
  "error": {
    "code": "NOT_CONVERGED",
    "message": "规则评估在 100 轮内未达到稳定状态",
    "details": {"max_iterations": 100}
  }
}
```

> 注意：批量接口在存在失败项时仍返回 **HTTP 200**，通过每项的
> `status` / `error` 与顶层计数区分成败；仅当规则集或版本不存在等
> 整批级问题发生时才返回 4xx。

### 评估统计

`GET /api/rulesets/{id}/evaluation-stats` 聚合该规则集**已持久化**的决策结果
（`persist=false` 的试算与求值失败项均不计入）：

- 可选筛选参数：
  - `version`：只统计指定规则版本的决策；
  - `since` / `until`：ISO-8601 时间，按决策创建时间过滤，**含边界**；
    无时区的输入按 UTC 处理，带偏移的输入会归一到 UTC；
- `total_evaluations`：筛选后命中的决策条数；
- `rule_stats`：按规则 `code` 排序。`hit_count` 为 traces 中 `hit=true` 的
  决策条数（**被冲突策略压制的命中也计入**）；`hit_rate =
  hit_count / total_evaluations`，保留 4 位小数；
- `decision_field_counts`：按字段名排序，统计最终 `decision` 的顶层字段
  在多少条决策中出现；
- 规则集不存在返回 404（`NOT_FOUND`）；无匹配记录返回零值结构
  （`total_evaluations` 为 0、两个列表为空）；非法时间参数返回 422。

```bash
curl -s 'localhost:8000/api/rulesets/demo-promotions/evaluation-stats?since=2026-01-01T00:00:00Z' \
  | python3 -m json.tool
```

响应示例：

```json
{
  "ruleset_id": "demo-promotions",
  "version": null,
  "since": "2026-01-01T00:00:00+00:00",
  "until": null,
  "total_evaluations": 2,
  "rule_stats": [
    {"code": "free_shipping", "hit_count": 1, "hit_rate": 0.5},
    {"code": "is_vip", "hit_count": 1, "hit_rate": 0.5},
    {"code": "large_order", "hit_count": 1, "hit_rate": 0.5},
    {"code": "vip_discount", "hit_count": 1, "hit_rate": 0.5}
  ],
  "decision_field_counts": [
    {"field": "discount", "count": 1},
    {"field": "free_shipping", "count": 1}
  ]
}
```

### 循环依赖被拒绝

```bash
curl -i -X POST localhost:8000/api/rulesets -H 'Content-Type: application/json' -d '{
  "name": "cyclic",
  "rules": [
    {"code": "a", "condition": "b.hit",        "depends_on": ["b"]},
    {"code": "b", "condition": "a.hit == true","depends_on": ["a"]}
  ]
}'
# HTTP/1.1 400 Bad Request
# {"error": {"code": "CYCLIC_DEPENDENCY", "message": "规则依赖存在循环：a -> b -> a", "cycle": ["a","b","a"]}}
```

### 静态依赖影响分析

`POST /api/rulesets/{id}/impact` 用于在**改动规则前评估影响面**：提交
`version` 与一个或多个 `rule_codes`，服务基于该版本快照中**已编译**的
`depends_on` 与条件表达式引用，静态计算反向传递依赖闭包——**不执行任何
文档求值，也不会写入 evaluations**。

请求体：

| 字段 | 说明 |
| --- | --- |
| `version` | **必填**，分析所基于的规则版本（不可变快照） |
| `rule_codes` | 一个或多个待分析的规则编码（至少 1 个；重复编码去重保序） |

响应字段：

- `rule_codes`：去重保序后的输入回显；
- `direct_dependents`：**直接依赖**任一输入规则的规则（反向边 1 跳）；
- `transitive_dependents`：**传递依赖**（反向边 ≥2 跳）的规则；多路径可达时
  按最短路径计层级，因此已计入 direct 的规则不会再出现在这里；
- `affected`：输入规则（`level=0`）与上述两类受影响规则的并集，按
  `evaluation_order` 排列，每项含 `level`（0/1/≥2）与 `relation`
  （`input` / `direct_dependent` / `transitive_dependent`）；
- `coupled_rules`：与任一受影响规则（含输入）处于**同一 `conflict_group`**
  的其余规则。它们不经依赖边被波及（`level` 恒为 `null`），但运行时可能因
  冲突策略联动（被压制或反向胜出）；`coupled_with` 指出 evaluation_order 上
  第一个触发联动的受影响规则。若某条联动规则本身已在依赖闭包中，则依赖分类
  优先，不在此重复出现；联动关系也不会继续向外传播。

所有规则列表均按该版本的 `evaluation_order` 排列，保证输出确定。

错误情况：

- 规则集不存在 → **404** `NOT_FOUND`；
- 版本不存在 → **404** `VERSION_NOT_FOUND`；
- 规则编码在该版本中不存在 → **422** `RULE_NOT_FOUND`
  （`details.rule_codes` 列出全部缺失编码）；
- 请求体缺字段、`version<1`、`rule_codes` 为空 → **422**。

请求示例（演示规则集 v1 中，`is_vip` 被 `vip_discount` 引用，后者又被
`manual_review` 引用；`vip_discount` 与 `large_order` 同属 discount 组）：

```bash
curl -s -X POST localhost:8000/api/rulesets/demo-promotions/impact \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "rule_codes": ["is_vip"]}' | python3 -m json.tool
```

响应示例：

```json
{
  "ruleset_id": "demo-promotions",
  "version": 1,
  "rule_codes": ["is_vip"],
  "affected": [
    {"rule_code": "is_vip", "level": 0, "relation": "input"},
    {"rule_code": "vip_discount", "level": 1, "relation": "direct_dependent"},
    {"rule_code": "manual_review", "level": 2, "relation": "transitive_dependent"}
  ],
  "direct_dependents": ["vip_discount"],
  "transitive_dependents": ["manual_review"],
  "coupled_rules": [
    {
      "rule_code": "large_order",
      "conflict_group": "discount",
      "coupled_with": "vip_discount",
      "level": null
    }
  ]
}
```

一次提交多个规则时，各规则的闭包取并集；例如同时分析
`["large_order", "is_vip"]` 会得到直接依赖方
`["vip_discount", "free_shipping"]` 与传递依赖方 `["manual_review"]`，
而 discount 组双方都已在闭包内，不再产生联动项。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试覆盖：AST 沙箱安全性、编译校验（重复编码/未知依赖/隐式引用/循环/冲突策略冲突）、
拓扑排序、定点迭代、三种冲突策略、版本化重算、评估统计聚合（筛选/排序/零值/非法参数）、
静态影响分析（反向闭包/层级/多输入去重/冲突组联动/404 与 422），
以及完整 HTTP 接口。
