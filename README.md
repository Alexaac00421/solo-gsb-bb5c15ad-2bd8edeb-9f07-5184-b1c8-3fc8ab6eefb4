# 博物馆展厅灯光场景编排 API

为博物馆展厅编排**可定时切换的灯光场景**的 HTTP 服务。输入灯具（通道）与场景（亮度、渐变时长、生效时间窗），服务会：

1. **校验**
   - 场景引用的灯具必须存在（`unknown_lamp`）；
   - 亮度必须在 **0–100**（百分比，对应 DMX 0–255）范围内（`level_out_of_range`）；
   - 场景时间窗合法（`end_at > start_at`，`invalid_time_window`）、同一灯具在一个场景内只能出现一次（`duplicate_channel_in_scene`）；
   - **同一通道上，普通场景的渐变区间 `[start_at, start_at + fade_seconds)` 不得重叠**（端点相接合法），冲突返回具体通道、时间区间和涉及的两个场景（`fade_interval_overlap`）；
   - 紧急场景彼此的生效时间窗不得重叠（`emergency_overlap`）。
2. **合成可执行时间线**：按通道输出排序后的指令序列（`fade` / `emergency_enter` / `emergency_exit`），每条指令带 `at`、`from_level`、`to_level`、`fade_end_at`，可直接下发 DMX/调光网关。
3. **紧急抢占**：紧急场景（`priority: "emergency"`）在 `start_at` 立即抢占普通场景；在 `end_at` 释放通道时，**只恢复到"抢占前已经开始、且释放时仍在生效时间窗内"的那个普通场景**；在紧急期间才到点的普通场景标记为 `skipped`，不会被恢复执行。
4. 校验失败返回 **HTTP 422** 和结构化冲突明细（通道/区间/原因），**不保存无效编排**。
5. **网关投递**：已发布编排按通道、按执行时间顺序向灯光网关发放到期指令（`POST /commands/claim`），带稳定任务标识、发布版本与租约令牌；网关凭当前令牌确认（`POST /commands/ack`），租约内不重发、超时可重领换令牌，重新发布/删除使旧版本任务失效且新版本不补发发布之前的指令（详见下文「网关投递」一节）。

---

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8080
```

### 本地 Python

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开交互文档：<http://localhost:8080/docs>（Swagger UI）、<http://localhost:8080/redoc>。

### 测试

```bash
pip install pytest httpx
pytest -q
```

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `PUT` | `/halls/{hall_id}` | 校验并全量保存一个展厅编排（成功 200，冲突 422 且不写入） |
| `POST` | `/halls/batch-publish` | **一次原子发布多个展厅编排**（全成功或全不写入，乐观版本控制） |
| `POST` | `/validate` | 只校验/试编译（dry run），不落库 |
| `GET` | `/halls` | 列出已保存展厅 |
| `GET` | `/halls/{hall_id}` | 获取编译后的完整计划 |
| `GET` | `/halls/{hall_id}/timeline` | 只取每通道可执行时间线 |
| `GET` | `/halls/{hall_id}/state?at=...` | 模拟某时刻各通道的实际亮度/生效场景 |
| `POST` | `/halls/{hall_id}/commands/claim` | **网关按通道领取已到期指令**（租约 + 稳定任务标识） |
| `POST` | `/halls/{hall_id}/commands/ack` | **凭当前租约令牌确认一条指令**（重复确认幂等） |
| `GET` | `/halls/{hall_id}/delivery` | 查看每通道投递/租约进度（运维视图） |
| `DELETE` | `/halls/{hall_id}` | 删除编排 |

数据为**内存存储**（线程安全，单 worker），重启清空；如需多实例持久化可将 `app/store.py` 替换为 Redis/数据库实现，`app/engine.py` 是纯函数、与存储无关。

### 数据模型要点

- `lamp`：`id` 即通道号；`default_level` 是无场景生效时的保持亮度。
- `scene`：
  - `priority`：`normal`（默认）或 `emergency`；
  - `start_at` / `end_at`：ISO 8601（建议带 `Z` 或时区偏移，无时区按 UTC 处理），区间左闭右开；
  - `items[]`：`lamp_id` + `level`(0–100) + `fade_seconds`（渐变时长，0 为瞬切）；紧急项可选 `restore_fade_seconds` 指定释放回普通场景时的渐变时长（默认沿用 `fade_seconds`）。

---

## 调用示例

示例文件：[`examples/bronze-gallery.json`](examples/bronze-gallery.json)。

### 1. 提交编排（校验通过 → 保存并返回时间线）

```bash
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' \
  --data @examples/bronze-gallery.json | jq
```

响应节选（`timeline` 为每个通道的指令序列；`preemptions` 描述每次抢占与恢复；`skipped_activations` 是被紧急场景跳过的普通场景）：

```json
{
  "hall_id": "bronze-gallery",
  "version": 1,
  "summary": {"lamps": 3, "scenes": 4, "normal_scenes": 3, "emergency_scenes": 1, "commands": 13, "preemptions": 2, "skipped_scene_activations": 0},
  "timeline": {
    "lamp-case-1": [
      {"type": "fade", "at": "2026-10-06T08:00:00+00:00", "from_level": 0, "to_level": 60.0, "fade_end_at": "2026-10-06T08:05:00+00:00", "fade_seconds": 300.0, "scene_id": "morning-open", "priority": "normal"},
      {"type": "emergency_enter", "at": "2026-10-06T10:00:00+00:00", "from_level": 60.0, "to_level": 5.0, "fade_end_at": "2026-10-06T10:00:00+00:00", "fade_seconds": 0.0, "scene_id": "conservation-alert", "priority": "emergency"},
      {"type": "emergency_exit", "at": "2026-10-06T10:30:00+00:00", "from_level": 5.0, "to_level": 60.0, "fade_end_at": "2026-10-06T10:33:00+00:00", "fade_seconds": 180.0, "scene_id": "conservation-alert", "priority": "emergency", "restored_scene_id": "morning-open"}
    ]
  },
  "preemptions": [
    {"emergency_scene_id": "conservation-alert", "channel": "lamp-case-1", "from": "2026-10-06T10:00:00+00:00", "to": "2026-10-06T10:30:00+00:00", "restored_scene_id": "morning-open", "restored_level": 60.0, "skipped_scene_ids": []}
  ]
}
```

说明：10:00 紧急场景把展柜灯从 **60%** 瞬切到保护值 **5%**；10:30 释放时，`morning-open`（08:00–12:00）在抢占前已开始且仍有效，因此用 180s 渐变恢复到 **60%**。若普通场景此时正在渐变中途，恢复目标是按抢占时刻插值出的亮度。

### 2. 查询某时刻的实际输出

```bash
# 紧急期间
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:10:00Z" | jq '.channels'
# => { "lamp-case-1": {"level": 5, "active_scene_id": "conservation-alert", "priority": "emergency", "in_emergency": true},
#      "lamp-wash":   {"level": 35, "active_scene_id": "morning-open",       "priority": "normal",    "in_emergency": false}, ... }
# 注意：紧急场景只引用了两盏展柜灯，wall wash 通道不受影响，继续运行普通场景。

# 释放恢复后
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:40:00Z" | jq '.channels."lamp-case-1"'
# => {"level": 60, "active_scene_id": "morning-open", ...}
```

### 3. 冲突：同一通道渐变区间重叠 → 422，且不保存

```bash
curl -sS -X POST http://localhost:8080/validate -H 'Content-Type: application/json' -d '{
  "id": "bad",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "a", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 50, "fade_seconds": 600}]},
    {"id": "b", "start_at": "2026-10-06T08:09:59Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 70, "fade_seconds": 10}]}
  ]
}'
```

```json
{
  "ok": false,
  "hall_id": "bad",
  "message": "Orchestration rejected; no timeline was saved.",
  "errors": [
    {
      "code": "fade_interval_overlap",
      "message": "Fade intervals on channel 'L1' overlap for scenes 'a' and 'b' between 2026-10-06T08:09:59+00:00 and 2026-10-06T08:10:00+00:00.",
      "scene_id": "b",
      "channel": "L1",
      "interval": ["2026-10-06T08:09:59+00:00", "2026-10-06T08:10:00+00:00"],
      "between_scenes": ["a", "b"]
    }
  ],
  "warnings": []
}
```

`a` 的渐变在 08:10:00 结束，`b` 在 08:09:59 开始——半开区间相交 1 秒即拒绝；若 `b` 从 **08:10:00** 开始则合法（端点相接）。此时 `GET /halls/bad` 返回 404，证明未保存。

### 4. 冲突：引用不存在的灯具 / 亮度越界

```bash
curl -sS -X PUT http://localhost:8080/halls/bad2 -H 'Content-Type: application/json' -d '{
  "id": "bad2",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "s", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T09:00:00Z",
     "items": [{"lamp_id": "L9", "level": 120, "fade_seconds": 0}]}
  ]
}'
```

- `level: 120` 由 Pydantic 以 422 拦截（字段级错误）；
- 改为合法亮度但引用未声明灯具时，返回业务错误 `unknown_lamp`，同样 422 且不写入。

### 5. 抢占恢复边界语义

- 场景 B 在紧急窗口**之内**到点：B 出现在 `skipped_activations` 中，释放时不会执行 B，只恢复抢占前仍有效的场景。
- 抢占前已开始、但在释放时刻**之前结束**的场景不可恢复：`emergency_exit.restored_scene_id` 为 `null`，通道保持紧急亮度直到下一个已排定场景接管，指令带 `note` 说明。
- 新普通场景恰好在释放的同一时刻开始：它直接接管通道，`emergency_exit` 带 `taken_over_by`，不产生可见的亮度跳变。
- 紧急场景只影响自己 `items` 中列出的通道，其他通道时间线完全不变。

---

## 批量发布多个展厅（原子提交）

运营一次发布**多个展厅的完整灯光编排**，用 `POST /halls/batch-publish`。请求体是一个**非空**列表 `halls[]`，每项：

| 字段 | 说明 |
| --- | --- |
| `hall_id` | 目标展厅 ID（同一批内不得重复） |
| `expected_version` | 乐观锁：你认为当前正在替换的版本；**尚未保存的新展厅传 `null`** |
| `orchestration` | 该展厅的完整编排，结构与 `PUT /halls/{hall_id}` 的请求体完全一致 |

语义保证：

- **逐项沿用**单展厅的校验与时间线合成规则（灯具引用、亮度范围、渐变区间不重叠、紧急抢占等）。
- 任一项出现下列问题时，**整批拒绝、不写入任何展厅，所有版本保持不变**，响应里给出每个问题展厅的 `index`、`hall_id` 与 `reason`：
  - `duplicate_in_batch`（同批 ID 重复，422）；
  - `id_mismatch`（`hall_id` 与 `orchestration.id` 不符，422）；
  - `orchestration_invalid`（编排未通过校验，422，`reason.errors` 为单展厅同款结构化冲突明细）；
  - `version_mismatch`（`expected_version` 与当前版本不符，**409**，返回 `expected_version` 与 `current_version`）。一批中既有内容错误又有版本冲突时，状态码取 **409** 且所有问题一次返回。
- 全部通过时**原子替换整批展厅**，每个展厅版本恰好 **+1**（新建为 `1`），按提交顺序返回各自的新版本、完整计划与时间线。
- **并发**发布涉及同一展厅且使用**相同 `expected_version`** 时，至多一方成功（另一方得到 409，需重新 `GET` 最新版本后再发布）。版本比对与整批写入在同一临界区完成。
- 原有单展厅 HTTP API（`PUT`/`GET`/`DELETE` 等）与响应结构保持不变、继续可用。

示例文件：[`examples/batch-publish.json`](examples/batch-publish.json)（第 1 项更新已存在的 `bronze-gallery`（期望版本 1），第 2 项以 `null` 创建新展厅 `porcelain-hall`）。

### 6. 批量发布：成功（各自版本 +1，返回新版本与时间线）

```bash
# 先放入 bronze-gallery，使其处于版本 1
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' --data @examples/bronze-gallery.json >/dev/null

# 一次发布一批：更新 bronze-gallery（expected_version=1）+ 新建 porcelain-hall（null）
curl -sS -X POST http://localhost:8080/halls/batch-publish \
  -H 'Content-Type: application/json' \
  --data @examples/batch-publish.json | jq '{ok, count, versions: [.published[] | {hall_id, version, lamps: .summary.lamps, commands: .summary.commands}]}'
```

```json
{
  "ok": true,
  "count": 2,
  "versions": [
    {"hall_id": "bronze-gallery", "version": 2, "lamps": 3, "commands": 13},
    {"hall_id": "porcelain-hall",  "version": 1, "lamps": 2, "commands": 4}
  ]
}
```

`published[]` 每一项就是单展厅 `GET /halls/{hall_id}` 的完整响应（含 `timeline`、`preemptions`、`skipped_activations`、`warnings` 等）。

### 7. 批量发布：版本冲突 → 409，整批不写入

```bash
# bronze-gallery 已被别人更新到版本 2，本批仍按版本 1 提交
curl -sS -X POST http://localhost:8080/halls/batch-publish \
  -H 'Content-Type: application/json' --data @examples/batch-publish.json
```

```json
{
  "ok": false,
  "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
  "failures": [
    {
      "index": 0,
      "hall_id": "bronze-gallery",
      "reason": {
        "code": "version_mismatch",
        "hall_id": "bronze-gallery",
        "message": "Expected hall 'bronze-gallery' at version 1, but current version is 2.",
        "expected_version": 1,
        "current_version": 2
      }
    }
  ]
}
```

此时同批的 `porcelain-hall` **也不会被创建**（整批原子）；`GET /halls/porcelain-hall` 返回 404，`bronze-gallery` 仍是版本 2。调用方应先 `GET` 取到最新版本、按最新编排重新校验后再发布。

> 新建展厅请传 `expected_version: null`；若该 ID 已存在，会得到 `expected_version=null / current_version=<n>` 的 409。对不存在的展厅传非 `null` 版本，同样得到 409（`current_version: null`）。

### 8. 批量发布：内容错误（ID 不符 / 编排无效 / 同批重复）→ 422

整批中不同展厅可以同时出错，响应一次性列出，各自带 `index` 便于定位：

```json
{
  "ok": false,
  "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
  "failures": [
    {"index": 0, "hall_id": "hall-x", "reason": {"code": "id_mismatch", "message": "Item hall id 'hall-x' does not match orchestration id 'different-id'."}},
    {"index": 1, "hall_id": "hall-y", "reason": {"code": "orchestration_invalid", "message": "Orchestration rejected; no timeline was saved.",
        "errors": [{"code": "fade_interval_overlap", "channel": "L1", "between_scenes": ["a", "b"], "...": "..."}], "warnings": []}}
  ]
}
```

空列表 `{"halls": []}`、缺字段、亮度越界等请求体形状错误由 Pydantic 直接返回 422（与单展厅一致），同样不写入。

### 9. 并发发布相同预期版本：至多一方成功

版本比较与整批写入在**同一个锁临界区**内完成。两个运营人员同时对版本 1 的同一展厅发起批量发布（都带 `expected_version: 1`）时：一方 `200`（版本变为 2），另一方 `409`（`current_version: 2`、其整批不写入）。失败方重新拉取版本 2 的当前编排、合并自己的修改后再提交即可。

---

## 网关投递：按通道领取 / 确认到期指令（租约队列）

灯光网关（DMX/调光网关）通过两个接口把**已发布版本**的指令拉到设备上执行。投递是**按通道（灯具）**、**按执行时间顺序**的严格 FIFO，并在其外叠加一层「租约 / 可见性超时」：

- **领取** `POST /halls/{hall_id}/commands/claim`：请求体指定数量上限 `max_count` 与租约时长 `lease_seconds`，服务端只返回**已到执行时刻（`at <= now`）**的指令，**每个通道至多返回一条**（该通道尚未确认的队首任务）。
- 返回每通道的队首任务，带 **稳定任务标识 `task_id`**、所属 **发布版本 `version`**、可直接下发的 **`command`**（与 `/timeline` 中单条指令结构一致）以及 **`lease.token`**。
- **前一任务未确认，不得领取后一任务**：通道队首处于活动租约内时，该通道本次不产出任何任务（队头阻塞）；不同通道相互独立。
- **租约内不重复发放**：租约未到期前重复领取不会再次给出同一任务（响应 `delivered` 为空）。
- **超时可重领并换令牌**：租约到期后可再次领取**同一个稳定任务**，但会签发一个**新令牌**；旧令牌立刻失效。
- **确认** `POST /halls/{hall_id}/commands/ack`：必须同时带上 `task_id` 与**当前** `lease_token`。
  - 令牌匹配且仍在租约内 → `200`，通道队首前移一条；
  - 用**同一个令牌重复确认** → `200` 且 `idempotent: true`（幂等，不重复前移）；
  - 用**旧令牌**（已超时 / 已被重领替换）确认 → `409`，**不改变任何状态**；
  - 令牌属于已被**重新发布或删除**的版本 → `410 version_expired`，并回传 `current_version`。
- **版本失效与水位**：
  - 展厅**重新发布（单展厅或批量）或删除**后，旧版本所有**未确认任务立即失效**；拿旧租约确认一律得到 `version_expired`（删除时 `current_version` 为 `null`）。
  - 每个版本以其**发布时刻为水位**：**不补发**发布时间之前的指令（`at < published_at` 一律不投递）；恰好在发布时刻到点的指令属于新版本（半开区间）。初始发布同样适用该水位。
  - **失败的发布不改变队列**：校验失败（422）或乐观版本冲突（409）都不会产生新版本，旧租约继续有效。
- 领取 / 确认 / 发布共享同一把锁、在同一临界区完成，因此**并发**操作下要么确认落在旧版本（`200`）、要么先被重新发布（`410`），**绝不会跨版本确认**。

> 数据为内存存储，重启会清空租约与队列；重新发布即按当前时刻建立新水位。

### 领取：`POST /halls/{hall_id}/commands/claim`

请求体（[`examples/gateway-claim.json`](examples/gateway-claim.json)）：

```json
{ "max_count": 10, "lease_seconds": 30 }
```

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `max_count` | 1–256，默认 10 | 本次最多返回的任务数；每通道至多一条，按 `(执行时间, 通道)` 排序返回 |
| `lease_seconds` | 1–86400，默认 30 | 租约可见性时长（秒）；到期未确认可重领 |

已发布 `bronze-gallery`（版本 1，指令从 08:00 起）后，在 08:00 之后领取：

```bash
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/commands/claim \
  -H 'Content-Type: application/json' --data @examples/gateway-claim.json | jq
```

```json
{
  "hall_id": "bronze-gallery",
  "version": 1,
  "published_at": "2026-10-06T07:00:00+00:00",
  "server_time": "2026-10-06T08:00:00+00:00",
  "max_count": 10,
  "lease_seconds": 30,
  "count": 3,
  "delivered": [
    {
      "task_id": "bronze-gallery:v1:lamp-case-1:000000",
      "hall_id": "bronze-gallery",
      "version": 1,
      "channel": "lamp-case-1",
      "sequence": 0,
      "at": "2026-10-06T08:00:00+00:00",
      "command": {"type": "fade", "at": "2026-10-06T08:00:00+00:00", "from_level": 0, "to_level": 60.0,
                  "fade_end_at": "2026-10-06T08:05:00+00:00", "fade_seconds": 300.0,
                  "scene_id": "morning-open", "priority": "normal"},
      "lease": {
        "token": "Wq8...随机令牌...",
        "lease_seconds": 30,
        "issued_at": "2026-10-06T08:00:00+00:00",
        "expires_at": "2026-10-06T08:00:30+00:00"
      }
    },
    { "task_id": "bronze-gallery:v1:lamp-case-2:000000", "channel": "lamp-case-2", "sequence": 0, "...": "..." },
    { "task_id": "bronze-gallery:v1:lamp-wash:000000",   "channel": "lamp-wash",   "sequence": 0, "...": "..." }
  ]
}
```

- `task_id` 形如 `{hall}:v{version}:{channel}:{sequence}`，其中 `sequence` 是该通道在「本版本、水位之后」时间线中的下标，重领保持不变。
- 此时三盏灯的 08:00 队首同时到点，一次领取最多拿到 3 条（每通道 1 条）；09:00 的指令仍被各自队首阻塞。没有任何任务可领时返回 `200` 且 `count: 0`（正常空轮询，不是错误）。
- 展厅不存在 → `404 hall_not_found`；`max_count` / `lease_seconds` 越界或类型错误由 Pydantic 返回 `422`。

### 10. 确认成功 / 重复确认幂等 / 旧令牌冲突

```bash
curl -sS -X POST http://localhost:8080/halls/bronze-gallery/commands/ack \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"bronze-gallery:v1:lamp-case-1:000000","lease_token":"Wq8...当前令牌..."}'
```

```json
{"ok": true, "idempotent": false, "hall_id": "bronze-gallery", "version": 1,
 "task_id": "bronze-gallery:v1:lamp-case-1:000000", "channel": "lamp-case-1",
 "sequence": 0, "confirmed_at": "2026-10-06T08:00:01+00:00"}
```

- 确认成功后，该通道下一条到点指令才会在后续 `claim` 中出现。
- 用**同一令牌再确认一次**：HTTP 仍为 `200`，响应 `idempotent: true`，队首不会重复前移。
- 租约超时后已重领、换发新令牌，再拿**旧令牌**确认：

```json
HTTP/409
{"code": "lease_token_conflict", "hall_id": "bronze-gallery",
 "task_id": "bronze-gallery:v1:lamp-case-1:000000",
 "message": "This lease timed out and a newer lease token was issued for the task; confirm with the current token."}
```

- 租约已超时但**尚未重领**，拿原令牌确认：`409 {"code": "lease_expired", ...}`（应重新领取再确认）。
- 令牌从未签发 / 服务重启后丢失：`404 lease_not_found`；令牌与 `task_id` 或展厅不匹配：`409 ack_target_mismatch`。上述失败都**不改变队列状态**。

### 11. 重新发布 / 删除后旧租约确认报版本过期，且新版本不补发历史指令

```
08:00 网关领取 v1 的 08:00 指令（租约 30s，未及时确认）
...
10:00 运营重新发布 bronze-gallery（PUT 或 batch-publish）→ 版本变为 2，发布水位=10:00
```

- 网关拿 v1 旧令牌确认：

```json
HTTP/410
{"code": "version_expired", "hall_id": "bronze-gallery",
 "task_id": "bronze-gallery:v1:lamp-case-1:000000", "task_version": 1,
 "current_version": 2,
 "message": "Hall 'bronze-gallery' was republished or deleted after this task was leased ..."}
```

- 网关应丢弃本地缓存的 v1 任务，重新 `claim`：新队列只包含 `at >= 10:00` 的指令，08:00/09:00 这些**水位之前的指令不会补发**。
- `DELETE /halls/bronze-gallery` 同理使旧租约失效，确认得到 `410 version_expired`（`current_version: null`）；此后对该展厅 `claim` 返回 `404`。
- 若重新发布的编排**校验失败**（422）或批量发布**版本冲突**（409），则不产生新版本、水位与队列保持不变，网关手里的旧租约仍可正常确认。

### 12. 单展厅与批量发布遵循同一规则

`PUT /halls/{id}` 与 `POST /halls/batch-publish` 的每一项在成功落库时都会：① 让该厅旧版本的全部令牌转为 `version_expired`；② 以提交时刻为水位重建投递队列。批量中任一厅失败则**整批不写入**，所有相关队列与租约维持原状。

### 13. 并发：领取、确认与发布不跨版本

所有判定与写入在同一把可重入锁的同一临界区内：

- 8 个网关并发 `claim` 同一条到点任务：恰好 1 个拿到（其余 `count: 0`）；
- 确认与重新发布并发：每个确认结果只能是 `200`（抢到在发布前提交）或 `410 version_expired`（发布先提交），不会出现「确认了 v1、队首却已在 v2」的跨版本状态。

### 14. 运维视图：`GET /halls/{hall_id}/delivery`

查看每个通道「水位之后的指令数、当前队首下标、剩余未确认数、活动/已过期租约」，便于排查卡住的队头：

```bash
curl -sS http://localhost:8080/halls/bronze-gallery/delivery | jq '.channels["lamp-case-1"]'
# {"eligible_commands": 3, "head_sequence": 0, "pending": 3,
#  "leased": {"task_id": "bronze-gallery:v1:lamp-case-1:000000", "state": "live",
#             "issued_at": "...", "expires_at": "..."}}
```

### 网关轮询伪代码

```text
loop:
  tasks = POST /halls/{id}/commands/claim {"max_count": 10, "lease_seconds": 30}
  for t in tasks.delivered:           # 已按执行时间排序，每通道至多一条
      send_to_dmx(t.command)
      ack = POST /halls/{id}/commands/ack {"task_id": t.task_id, "lease_token": t.lease.token}
      if ack.status == 410:           # version_expired：本厅已重新发布/删除
          break                       # 放弃旧任务，下一轮 claim 自动切到新版本
      if ack.status == 409:           # lease_expired/token_conflict
          break                       # 令牌旧了：下一轮重新领取同一任务
  sleep(poll_interval)
```

---

## 目录结构

```
app/
  models.py     # Pydantic 输入模型（含 BatchPublishIn、ClaimIn、AckIn）与时间归一化
  clock.py      # 可注入 UTC 时钟（生产读墙钟，测试可冻结/推进时间）
  engine.py     # 纯函数：校验 + 时间线合成（抢占/恢复/跳过）
  simulator.py  # 任意时刻的通道状态模拟
  store.py      # 线程安全内存存储：单展厅 put 与批量原子 batch_commit（乐观版本检查）
  delivery.py   # 网关投递队列：按通道 FIFO、租约/令牌、发布水位与版本失效
  main.py       # FastAPI 路由（单展厅 + 批量发布 + commands/claim、commands/ack、delivery）
tests/
  test_api.py       # 34 个端到端用例（单展厅 + 批量发布，含并发）
  test_delivery.py  # 23 个网关投递用例（领取/租约/确认/版本失效/并发）
examples/
  bronze-gallery.json
  batch-publish.json   # 批量发布请求示例（更新 + 新建）
  gateway-claim.json   # 网关领取请求体
  gateway-ack.json     # 网关确认请求体（task_id + lease_token）
Dockerfile
docker-compose.yml
requirements.txt
```
