# 场馆疏散通道编排服务

基于 **Python 3.11 / FastAPI / SQLite** 的疏散路线编排（scheduling）服务。
输入场馆的有向通道、封闭时段、出口与人群组，按优先级逐组安排**截止前最早到达**
的路线与出发时间，并保证通道每分钟容量不被超用、封闭时段不被占用。
支持推进时间、增量新增人群组/封闭，且只重排未出发组。
支持现场负责人提交**人群组纠正清单**：批量修正未出发组的人数、起点、
最早出发分钟、优先级或截止分钟，整批原子生效并整体重排未出发组。
支持按通道登记**容量管制时段**（临时限流）：管制时段内该通道每分钟有效上限
收紧为指定值，编排按通行期间每一分钟的有效上限核算。
支持现场指挥按组号、所在节点、预计恢复分钟报告已出发组的**节点滞留**：
保留已走前缀、释放该组从当前分钟起的旧预约并从滞留节点重找余程，无余程
则标记**待救援**，其他未出发组随之整体重排。
支持按唯一编号与正整数载员量**登记人工救援队**，现场指挥可把待救援组
**交接派遣**给空闲队伍（校验所见分钟、滞留节点、载员量、队伍空闲），
随后凭任务号与既有出口、实际送达分钟**结案核销**：成功派遣/结案与编排
修订在同一事务原子落库，实际出口与时刻单列、不当作规划到达。
支持现场为尚未出发的组登记**先导组与非负放行间隔**（每组至多一项、再次
登记替换旧关系）：后继最早出发不早于自身最早出发与"先导规划到达+间隔"，
先导已人工送达时改用实际送达分钟；先导待救援、救援中或无可行路线时后继

标记**前置未满足**（不占通道），多级关系按依赖顺序安排。
支持现场在出口按人群组**清点实际撤离人数**：每条清点凭非空唯一流水号
幂等登记（同内容重试返回原记录、异内容拒绝），仅已按固定路线抵达该出口
或已由人工救援实际送达该出口的组可登记，同组累计不超组人数；可凭原
流水号、记录版本与非空原因**更正**为非负人数（保留原值与更正历史），
并按组查询应到/已清点/未清点人数与逐条记录。
每次成功的编排变更都会落库一条**全局递增修订**（前后完整快照 + 逐组差异），
供指挥员追溯"每次疏散变更为何改动编排"。

## 1. 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| SQLite 数据文件 | 容器内 `/data/evacuation.db`（命名卷 `evac_data` 持久化） |

首次启动且数据库为空时，会自动写入一份演示场馆数据并完成编排，直接
`curl http://localhost:8000/plan` 即可看到结果。

后台运行：`docker compose up --build -d`；停止：`docker compose down`
（保留数据）；清空数据重来：`docker compose down -v`。

### 本地（无 Docker）运行

```bash
pip install -r requirements.txt
EVAC_DB_PATH=./evacuation.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVAC_DB_PATH` | `/data/evacuation.db` | SQLite 数据库文件路径，目录自动创建 |
| `EVAC_SEED` | `true` | 启动时若库为空，是否自动初始化演示数据（`true`/`false`） |

服务固定单 uvicorn worker 运行：所有编排变更由进程内 `asyncio` 锁串行化，
多副本水平扩展不在设计范围内（会绕过互斥锁）。

## 3. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查，返回当前分钟 |
| POST | `/scenario` | 全量重置场景（通道/出口/封闭/人群组），立即编排；同时清除全部容量管制、人工救援队与救援任务、先导关系与出口清点记录（旧修订保留） |
| GET | `/plan` | 查询每组路线、出发/到达分钟或无法安排的原因 |
| POST | `/groups` | 新增一组或多组，**仅重排未出发组** |
| POST | `/group-corrections` | 批量纠正未出发组的属性（非空清单，整批原子生效）；目标组已出发则 **409 整批拒绝** |
| POST | `/closures` | 新增左闭右开封闭时段；触及已出发组剩余路线则 **409 整次拒绝** |
| POST | `/capacity-controls` | 登记左闭右开容量管制时段；重叠或使已出发组超载则 **409 整次拒绝** |
| GET | `/capacity-controls` | 列出当前全部容量管制时段 |
| POST | `/group-prerequisites` | 为未出发组登记先导组+非负放行间隔（每组一项，再登记替换）；目标已出发、自依赖/成环 **409**，编号或间隔无效 **422** |
| GET | `/group-prerequisites` | 列出全部先导关系及实时放行依据（先导状态、采用的到达分钟、最早放行分钟） |
| POST | `/groups/{group_id}/delay-report` | 现场报告已出发组在某节点滞留（按节点+恢复分钟重找余程）；组未出发、节点或时机不符 **409 整次拒绝** |
| POST | `/rescue-teams` | 登记人工救援队（唯一编号+正整数载员量）；编号重复 **409 整次拒绝** |
| GET | `/rescue-teams` | 列出全部救援队（载员量、当前进行中任务号） |
| POST | `/rescue-tasks` | 把待救援组派遣给人工救援队（组号/队号/滞留节点/所见分钟）；分钟失步、状态或节点不符、超载、队伍忙均 **409 整次拒绝** |
| GET | `/rescue-tasks` | 列出全部救援任务，可加 `?status=in_progress|completed` |
| GET | `/rescue-tasks/{task_id}` | 查询单个任务当前状态；未知任务号 **404** |
| POST | `/rescue-tasks/{task_id}/completion` | 结案核销（既有出口+实际送达分钟）；重复结案/送达时序冲突 **409** |
| POST | `/exit-counts` | 登记一条出口清点（非空唯一流水号幂等）；同内容重试返回原记录，异内容/组未抵达/出口不符/分钟越窗/累计超额 **409**，参数或编号无效 **422** |
| POST | `/exit-counts/{serial}/correction` | 凭原流水号+记录版本+非空原因更正为非负人数（保留原值与历史）；版本失配/重复更正/超额 **409**，未知流水号 **404** |
| GET | `/exit-counts` | 按组列出应到/已清点/未清点人数及逐条记录（含更正历史） |
| GET | `/exit-counts/{group_id}` | 单组清点台账；未知组号 **404** |
| POST | `/tick` | 推进当前分钟（body 可传 `{"minutes": n}`，默认 1） |
| GET | `/passages/{edge_id}/load` | 通道逐分钟占用与各分钟有效上限，可加 `?minute=n` |
| GET | `/revisions` | 列出全部修订（修订号/触发操作/当时分钟），重置与重启后保留 |
| GET | `/revisions/{rev}` | 按修订号查询当时编排快照与各组前后差异；未知修订号 **404** |

### 请求体

**POST /scenario**

```json
{
  "passages": [
    {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30}
  ],
  "exits": [5, 6],
  "closures": [
    {"edge_id": 1, "start_minute": 3, "end_minute": 10}
  ],
  "groups": [
    {"id": 1, "size": 30, "origin": 1,
     "earliest_departure": 0, "priority": 10, "deadline": 30}
  ]
}
```

* `passages`：有向通道，`id` 唯一正整数；`minutes` 为正整数通行分钟数；
  `capacity` 为每分钟可同时容纳的人数（正整数）。
* `closures`：左闭右开 `[start_minute, end_minute)`。
* `groups`：整组不可拆分；`priority` 数值越大越优先。

**POST /groups**：请求体为上述 group 对象的数组。

**POST /group-corrections**：请求体为非空纠正项数组，每项给出已有组编号
及至少一个待纠正字段。

```json
[
  {"group_id": 3, "size": 12},
  {"group_id": 4, "origin": 2, "priority": 25}
]
```

* 可纠正字段：`size`（人数）、`origin`（起点）、`earliest_departure`
  （最早出发分钟）、`priority`（优先级）、`deadline`（截止分钟）；
  未指定字段沿用原值，同批组编号不得重复。
* 仅未出发（含此前编排失败）的组可纠正；每项最终字段须满足与新增组
  相同的校验（正数人数、非负分钟、截止不早于最早出发），起点须为
  场馆节点。
* 任一项无效（422），或目标组已出发（409，含并发时间推进使其在受理
  前出发）时，整批拒绝且状态与修订历史不变；全部有效时整批一次生效，
  整体重排未出发组（已出发组路线与占用保持固定）并返回完整编排。

**POST /closures**：单个 closure 对象。
**POST /capacity-controls**：单个容量管制对象。

```json
{"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}
```

* 左闭右开 `[start_minute, end_minute)`，起点须早于终点且不早于当前分钟；
* `capacity` 为正整数，表示管制时段内该通道每分钟人数上限；
* 同一通道的管制时段不得重叠，时段外仍用原容量；
* 管制时段内每分钟有效上限为 `min(原容量, 管制上限)`，编排按通行期间
  每一分钟的有效上限核算，封闭规则照常生效。

**POST /tick**：`{"minutes": 3}` 或空 body（默认推进 1 分钟）。

**POST /groups/{group_id}/delay-report**：现场指挥报告某个**已出发**组的
节点滞留。请求体给出该组按现行计划已到达的滞留节点与预计恢复分钟：

```json
{"node_id": 2, "recovery_minute": 6}
```

> 另有等价的集合式写法 `POST /delay-reports`，请求体为
> `{"group_id": 1, "node_id": 2, "recovery_minute": 6}`，语义完全相同。

* 仅当该组按现行计划**已到达该节点**、**尚未进入下一通道**且**未抵达
  出口**时受理；恢复分钟不得早于当前分钟（相等允许，表示当分钟即可恢复）。
  下一通道的进入分钟恰为当前分钟时，该组仍在节点上、可以被报告（与
  `/tick` 时"出发分钟 == 当前分钟仍属未出发"的口径一致）。
* 下列情形整次拒绝、**不改编排、不产生修订**：组未出发（`scheduled`/
  `failed`/`pending`，409 `NOT_DEPARTED`）、报告节点与现行所在节点不符
  （409 `NODE_MISMATCH`）、组已进入下一通道正在通行中（409
  `GROUP_IN_TRANSIT`）、组已抵达出口（409 `ALREADY_AT_EXIT`）；组不存在
  或恢复分钟早于当前分钟返回 422。
* 受理后在**同一事务**内：
  1. **保留已走前缀**及其逐段通行时间，删除并释放该组从当前分钟起的旧
     预约（旧预约不会因后续无解而恢复）；
  2. 以滞留节点为起点、恢复分钟为最早出发，依据现有封闭、容量管制与
     **其他已出发组的固定占用**，按既有"最早到达 / 出口编号 / 通道序列
     字典序"规则寻找截止前余程；
  3. 有解：组保持 `fixed`，路线为**已走前缀 + 新余程**（新余程作为固定
     占用，随后与其他已出发组同等受封闭/限流保护）；无解：组置为
     `awaiting_rescue`（待救援），`reason` 沿用既有失败原因码并附具体
     说明，只保留已走前缀；
  4. 其他**未出发**组随之整体重排（让出的固定占用可被复用）；其他
     **已出发**组路线与占用不变。
* 待救援组不参与后续时间推进的自动重排；只能在其**滞留节点**以不早于
  当前分钟的恢复分钟**再次报告**（节点不符仍 409），由现场决定恢复或
  继续等待救援。待救援组不可再被 `/group-corrections` 纠正（409
  `GROUP_ALREADY_DEPARTED`）。

报告成功（含无解待救援）产生一条 `delay-reports` 修订，`detail` 记录
`group_id/node_id/recovery_minute/outcome/prefix_step_count`，其中
`outcome` 为 `resumed`（找到余程）或 `awaiting_rescue`（待救援）。拒绝
响应形如：

```json
{
  "error": "DELAY_REPORT_NOT_ACCEPTABLE",
  "message": "报告节点与该组按现行计划所在节点不符，整次拒绝",
  "conflicts": [
    {"group_id": 1, "status": "fixed", "node_id": 1,
     "current_minute": 4, "recovery_minute": 4,
     "reason": "NODE_MISMATCH", "expected_node_id": 2}
  ]
}
```

**人工救援队登记 / 派遣 / 结案核销**

* `POST /rescue-teams` 登记救援队：`{"team_id": 1, "capacity": 12}`。
  编号须为正整数且全库唯一，载员量为正整数；重复登记返回 409
  `RESCUE_TEAM_ALREADY_REGISTERED`（`reason=DUPLICATE_TEAM_ID`），原队伍
  载员量不变。登记不改编排、不产生修订；`GET /rescue-teams` 返回各队
  载员量与其当前进行中任务号（空闲为 `null`）。
* `POST /rescue-tasks` 派遣：请求体为
  `{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4}`。
  **仅当**下列条件全部成立时受理（HTTP 201，返回任务号）：

  1. 所见当前分钟 `observed_minute` 恰为服务端当前分钟（分钟未变）；
  2. 该组状态仍为 `awaiting_rescue`，且其待救援滞留节点就是 `node_id`；
  3. 组人数不超过队伍载员量；
  4. 队伍空闲、同组同队均无进行中任务（各只允许一项）。

  受理后组标记 `rescuing`（救援中）：**保留已走通道前缀**，不写入任何
  未来通道占用；时间推进不自动重排该组；其既有节点滞留报告接口被拒绝
  （409 `GROUP_RESCUING`），组纠正同样拒绝。拒绝统一返回 409
  `RESCUE_DISPATCH_NOT_ACCEPTABLE`，`conflicts[0].reason` 区分：

  | reason | 触发条件 |
  | --- | --- |
  | `OBSERVED_MINUTE_STALE` | 所见分钟与当前分钟不一致（含并发 `/tick` 抢占） |
  | `GROUP_NOT_AWAITING_RESCUE` | 组不在待救援状态（未出发/已出发等） |
  | `GROUP_RESCUE_IN_PROGRESS` | 该组已有进行中救援任务 |
  | `GROUP_ALREADY_RESCUED` | 该组已由人工结案送达，不可再派 |
  | `NODE_MISMATCH` | 滞留节点不符（响应给 `expected_node_id`） |
  | `TEAM_CAPACITY_EXCEEDED` | 组人数超队伍载员量（附 `group_size`/`team_capacity`） |
  | `TEAM_BUSY` | 队伍已有进行中任务（附 `current_task_id`） |

  无效组号/队号返回 422；任何拒绝均整次不改编排、不产生修订。成功派遣
  原子写入一条 `rescue-dispatches` 修订。
* 任务查询：`GET /rescue-tasks/{task_id}` 返回单个任务（未知任务号
  404）；`GET /rescue-tasks` 列出全部任务，可加
  `?status=in_progress|completed` 过滤（其他取值 422）。
* `POST /rescue-tasks/{task_id}/completion` 结案：请求体为
  `{"exit_id": 6, "delivery_minute": 8}`。`exit_id` 必须是当前场景的
  **既有出口**（否则 422）；实际送达分钟须**不早于派遣分钟且不晚于当前
  分钟**（与派遣分钟相等允许；晚于该组原规划截止也**如实记录**）。成功
  后任务置 `completed`、队伍立即释放空闲，组标记 `rescued`（已救援），
  实际出口与送达时刻在任务及编排中单列（`rescued_exit_id` /
  `delivery_minute`），**不**回写规划到达字段（`exit_id` /
  `arrival_minute` 保持为空，人工送达不当作规划到达）。成功结案原子写入
  一条 `rescue-completions` 修订。时序/状态冲突统一返回 409
  `RESCUE_COMPLETION_NOT_ACCEPTABLE`，`conflicts[0].reason` 区分
  `TASK_ALREADY_COMPLETED`（附首次结案的出口与分钟）、
  `DELIVERY_BEFORE_DISPATCH`、`DELIVERY_IN_FUTURE`；未知任务号 404。
* **场景重置**（`POST /scenario`）清空全部救援队与救援任务；旧修订保留、
  修订号继续递增，重置后可重新登记同编号队伍。

**出口清点（按组清点实际撤离人数）**

* `POST /exit-counts` 登记一条清点：

  ```json
  {"serial": "C-0001", "group_id": 1, "exit_id": 6,
   "count": 4, "counted_minute": 2}
  ```

  * `serial`：**非空**唯一流水号（幂等键）；`count` 为**正整数**人数；
    `counted_minute` 为**非负**清点分钟（非法取值 422）。
  * **仅**已按固定路线抵达该出口（`fixed` 且到达分钟不晚于当前分钟）或
    已由人工救援实际送达该出口（`rescued`）的组可登记；救援送达组采用
    **实际出口与实际送达分钟**作为到达锚点。清点分钟不得早于相应到达/
    送达分钟，也不得晚于当前分钟。
  * 组不存在、出口非当前场景既有出口返回 422。
  * **幂等**：同一流水号**同内容**（组号/出口号/人数/清点分钟均一致）
    重试返回 HTTP 200 与原记录，不产生重复记录；**异内容**返回 409
    `EXIT_COUNT_SERIAL_CONFLICT`（`reason=SERIAL_CONTENT_MISMATCH`，
    附已登记内容），整次拒绝。
  * **累计上限**：同组全部记录的当前生效人数之和不得超过组人数，超额
    整次拒绝（409，`reason=COUNT_EXCEEDS_GROUP_SIZE`，附组人数、已清点
    累计与本次提交人数）。
  * 其余 409 拒绝（`EXIT_COUNT_NOT_ACCEPTABLE`，`conflicts[0].reason`
    区分）：`GROUP_NOT_ARRIVED`（组尚未抵达任何出口）、`EXIT_MISMATCH`
    （清点出口与抵达/送达出口不符，附 `expected_exit_id`）、
    `COUNTED_MINUTE_BEFORE_ARRIVAL`（清点分钟早于到达/送达分钟，附
    `arrival_minute`）、`COUNTED_MINUTE_IN_FUTURE`（清点分钟晚于当前
    分钟）。任何拒绝均不登记记录、不产生修订。
  * 首次登记成功返回 HTTP 201 与记录（版本自 1 起）；**登记不改编排、
    不产生修订**。
* `POST /exit-counts/{serial}/correction` 更正一条清点：

  ```json
  {"version": 1, "count": 3, "reason": "复核实为 3 人"}
  ```

  * 凭**原流水号**、**原记录版本**（须为记录当前版本）与**非空原因**
    把人数更正为**非负**整数；更正保留原值与历史——记录内
    `submitted_count` 恒为首次提交人数，每次更正追加一条历史（原值、
    新值、原因与受理分钟），记录版本 +1。
  * 按更正后的同组累计数复核组人数上限。版本失配
    （`VERSION_MISMATCH`）、对同一版本重复更正
    （`VERSION_ALREADY_CORRECTED`，提交版本小于当前版本）或更正后累计
    超额（`COUNT_EXCEEDS_GROUP_SIZE`）均整次拒绝（409
    `EXIT_COUNT_CORRECTION_NOT_ACCEPTABLE`），记录与历史不变；未知
    流水号 404；空原因/负人数/非法版本 422。成功返回 HTTP 200 与更新后
    记录；**更正不改编排、不产生修订**。
* `GET /exit-counts/{group_id}` 按组查询清点台账（未知组号 404），
  `GET /exit-counts` 汇总全部组：

  ```json
  {
    "current_minute": 3,
    "group_id": 1,
    "expected": 10,
    "counted": 3,
    "remaining": 7,
    "arrival": {"basis": "planned_arrival", "exit_id": 6, "minute": 2},
    "records": [
      {"serial": "C-0001", "group_id": 1, "exit_id": 6, "count": 3,
       "submitted_count": 4, "counted_minute": 2, "version": 2,
       "created_minute": 3,
       "corrections": [
         {"from_version": 1, "to_version": 2, "old_count": 4,
          "new_count": 3, "reason": "复核实为 3 人", "minute": 3}]}
    ]
  }
  ```

  * `expected`/`counted`/`remaining`：应到（组人数）、已清点（全部记录
    当前生效人数之和）、未清点人数。
  * `arrival` 为可清点到达锚点：`planned_arrival`（按固定路线抵达的
    规划出口与分钟）或 `actual_delivery`（人工救援**实际送达**的出口与
    分钟）；尚未抵达的组三个字段均为 `null`。
* **场景重置**（`POST /scenario`）清空全部清点记录与更正历史；旧修订
  保留，流水号可在新场景重新使用。

### 响应（GET /plan 与所有变更接口）

```json
{
  "current_minute": 0,
  "groups": [
    {
      "group_id": 3,
      "status": "scheduled",
      "exit_id": 6,
      "departure_minute": 0,
      "arrival_minute": 2,
      "route": [5],
      "steps": [
        {"edge_id": 5, "from_node": 2, "to_node": 6,
         "enter_minute": 0, "exit_minute": 2}
      ],
      "reason": null,
      "detail": null
    }
  ]
}
```

组状态：`scheduled`（已安排未出发）、`fixed`（已出发，路线与占用固定）、
`awaiting_rescue`（已出发但现场报告后截止前再无可行余程，待救援；后续
时间推进不自动重排）、`rescuing`（待救援组已交接人工救援队、救援中；
保留已走前缀、无未来通道占用、不自动重排）、`rescued`（已由人工救援队
结案送达；实际出口与时刻单列，不作规划到达）、`blocked`（从未出发但
先导前置未满足：先导待救援/救援中/尚未安排/无可行路线/先导自身前置未
满足；不写路线、不占通道，先导落定后随重排自动转正）、`failed`（从未
出发且在前置已满足时仍无法在截止前安排）。

被人工救援队交接过的组（`rescuing`/`rescued`）在编排中额外给出：
`rescue_task_id`（救援任务号）、`rescue_team_id`（接手队伍）、
`rescued_exit_id`（结案单列的实际送达出口，仅 `rescued`）、
`delivery_minute`（结案单列的实际送达分钟，仅 `rescued`）；未交接组这些
字段恒为 `null`，原有字段语义保持兼容。

被现场滞留报告影响过的组（`fixed` 或 `awaiting_rescue`）额外给出
`prefix_step_count`：`steps` 的前 `prefix_step_count` 段为报告时已走完、
固定保留的**已走前缀**，其后为按恢复分钟重找的**新余程**；未报告组该
字段恒为 `null`，其余字段与规划语义保持兼容。

登记过先导关系的组额外给出 `leader_group_id`、`release_gap` 与
`release_basis`（放行依据，实时计算）：`satisfied` 表示先导是否已有可用
到达锚点；`basis_type` 为 `planned_arrival`（先导 `scheduled`/`fixed`
的规划到达分钟）或 `actual_delivery`（先导 `rescued` 的人工实际送达
分钟）；`leader_arrival_minute` 为采用的到达分钟、
`earliest_release_minute` 为其加间隔后的后继最早放行分钟。前置未满足时
`satisfied=false` 且 `reason` 为 `LEADER_PENDING` /
`LEADER_AWAITING_RESCUE` / `LEADER_RESCUING` / `LEADER_FAILED` /
`LEADER_BLOCKED`，最早放行分钟为 `null`。未登记关系的组这三个字段恒为
`null`，原有字段语义保持兼容。

失败原因码（`failed` 组的失败原因，或 `awaiting_rescue` 组自滞留节点
重找余程无解的原因）：

| reason | 含义 |
| --- | --- |
| `BLOCKED_BY_CLOSURE` | 忽略封闭后可到达——封闭时段阻断了截止前所有路线 |
| `INSUFFICIENT_CAPACITY` | 忽略容量后可到达（或单组人数超通道容量）——容量竞争阻断 |
| `UNREACHABLE_EXIT` | 起点与任何出口之间不存在连通路径 |
| `DEADLINE_PASSED` | 当前分钟已超过该组规划截止分钟 |

封闭被拒绝时返回 HTTP 409：

```json
{
  "error": "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP",
  "message": "封闭时段与已出发组的剩余路线相交，整次拒绝",
  "conflicts": [
    {"group_id": 3, "edge_id": 5, "enter_minute": 0,
     "exit_minute": 2, "closure": [1, 3]}
  ]
}
```

纠正清单中任一目标组已出发时返回 HTTP 409（整批拒绝，状态与修订
历史不变）：

```json
{
  "error": "GROUP_ALREADY_DEPARTED",
  "message": "目标组已出发，不可纠正，整批拒绝",
  "conflicts": [
    {"group_id": 3, "status": "fixed", "departure_minute": 0,
     "arrival_minute": 2}
  ]
}
```

容量管制被拒绝时同样整次拒绝、不改动任何状态：

* 通道不存在、起止无效（`end_minute <= start_minute`、上限非正整数）
  或起点早于当前分钟：HTTP 422；
* 与同通道已有管制时段重叠：HTTP 409
  `CAPACITY_CONTROL_OVERLAP`，`conflicts` 给出相交的已有时段；
* 新上限使已出发组剩余路线在任一分钟超载：HTTP 409
  `CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP`，`conflicts` 逐分钟给出
  冲突分钟、占用量、新有效上限与相关组：

```json
{
  "error": "CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP",
  "message": "新上限使已出发组剩余路线在部分分钟超载，整次拒绝",
  "conflicts": [
    {"edge_id": 5, "minute": 1, "used": 10, "effective_capacity": 5,
     "groups": [{"group_id": 3, "size": 10}]}
  ]
}
```

登记成功后仅重排未出发组（已出发组路线与占用保留），返回重排后的
完整编排（同 GET /plan）。

**POST /group-prerequisites**：为**尚未出发**的人群组登记先导组与非负
放行间隔。

```json
{"group_id": 2, "leader_group_id": 1, "release_gap": 3}
```

* `group_id`：后继（目标）组，必须是**尚未出发**的组
  （`scheduled`/`failed`/`pending`/`blocked`）；
* `leader_group_id`：先导组编号，**必须存在**（可以是任意状态的组，
  含已出发或已救援组）；
* `release_gap`：**非负整数**放行间隔。同一组至多一项关系，再次登记
  **替换**旧关系（修订 `detail.replaced` 记录被替换的旧关系）。
* 后继组的可出发分钟不早于自身最早出发分钟及
  **先导组规划到达分钟 + 间隔**；先导组已由人工救援队结案送达时改用
  **实际送达分钟 + 间隔**（放行依据中 `basis_type` 分别为
  `planned_arrival` / `actual_delivery`）。
* 先导组待救援、救援中、尚未安排、无可行路线（`failed`）或先导自身前置
  未满足时，后继标记 **`blocked`（前置未满足）**：不写路线、不占通道，
  `/plan` 中原因码为 `LEADER_AWAITING_RESCUE` / `LEADER_RESCUING` /
  `LEADER_PENDING` / `LEADER_FAILED` / `LEADER_BLOCKED`，待先导落定后的
  下一次重排（关系变更、`/tick`、现场滞留报告、救援结案等触发）自动转正。
* **多级关系按依赖顺序安排**：先导先于后继得到路线与放行依据；每轮在
  可安排组中沿用现有优先级、最早出发分钟、组编号与路线并列规则，截止、
  封闭与容量约束继续生效（前置满足但自身赶不上截止仍为普通 `failed`）。
* 下列情形**整次拒绝**（HTTP 409 `PREREQUISITE_NOT_ACCEPTABLE`，
  旧关系、编排与修订历史不变），`conflicts[0].reason` 区分：

| reason | 触发条件 |
| --- | --- |
| `TARGET_ALREADY_DEPARTED` | 目标组已出发（`fixed`/`awaiting_rescue`/`rescuing`/`rescued`） |
| `SELF_DEPENDENCY` | 先导组就是目标组自身（禁止自依赖） |
| `CYCLE_DETECTED` | 登记后形成环（含多级关系），响应给出 `cycle` 组号序列 |

* 目标组或先导组不存在（422）、间隔为负（422，pydantic 校验）同样整次
  拒绝。成功登记在同一事务内重排全部未出发组、写入一条
  `group-prerequisites` 修订，HTTP 201 返回完整编排。
* **场景重置**（`POST /scenario`）清空全部先导关系；旧修订保留。

**GET /group-prerequisites** 按目标组编号升序列出全部关系及其实时放行
依据：

```json
{
  "current_minute": 0,
  "relations": [
    {"group_id": 2, "leader_group_id": 1, "release_gap": 3,
     "release_basis": {
       "satisfied": true,
       "basis_type": "planned_arrival",
       "leader_status": "scheduled",
       "leader_arrival_minute": 6,
       "earliest_release_minute": 9,
       "reason": null}}
  ]
}
```

前置未满足时 `satisfied=false`、`basis_type`/`leader_arrival_minute`/
`earliest_release_minute` 为 `null`，`reason` 给出上述 `LEADER_*` 原因码。

**GET /passages/{edge_id}/load** 的响应逐分钟给出占用与该分钟的
有效上限（管制时段内为 `min(原容量, 管制上限)`，时段外为原容量）；
管制覆盖的分钟即使无占用也会列出（`used=0`），顶层 `capacity_controls`
给出该通道当前生效的管制时段：

```json
{
  "edge_id": 1, "capacity": 30, "traversal_minutes": 4,
  "capacity_controls": [
    {"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}
  ],
  "minutes": [
    {"minute": 10, "used": 0, "effective_capacity": 10, "groups": []},
    {"minute": 14, "used": 30, "effective_capacity": 30,
     "groups": [{"group_id": 1, "size": 30, "status": "scheduled"}]}
  ]
}
```

### 修订历史（变更追溯）

场景重置、新增人群组、纠正人群组、登记封闭、登记容量管制、**登记先导
关系**、现场节点滞留报告、**人工救援派遣与结案核销**、时间推进**成功**
时，各产生一条修订：
全局递增修订号（按实际提交顺序编号）、触发操作及参数摘要、提交后的当前
分钟、提交前后完整编排快照。**被拒绝的请求（409/422）不产生修订。**修订
和编排变更在同一 SQLite 事务提交，查询不会看到只写入一方的状态；场景
重置与服务重启后历史保留、编号继续递增。快照为落库时的完整编排（含当时
固定组、救援中/已救援组状态与逐段通行时刻），按原样返回，不按当前场景
重算。救援队登记与**出口清点/清点更正**均不改编排，不产生修订。

**GET /revisions**

```json
{
  "revisions": [
    {"rev": 1, "operation": "scenario", "minute": 0,
     "detail": {"passages": 7, "exits": [5, 6], "closures": 1, "groups": 4}},
    {"rev": 2, "operation": "tick", "minute": 1,
     "detail": {"minutes": 1}}
  ]
}
```

`operation`：`scenario`（场景重置）/ `groups`（新增人群组）/
`group-corrections`（纠正人群组）/ `closures`（登记封闭）/
`capacity-controls`（登记容量管制）/
`group-prerequisites`（登记先导关系）/ `delay-reports`（现场节点滞留
报告）/ `rescue-dispatches`（人工救援派遣）/
`rescue-completions`（救援结案核销）/ `tick`（时间推进）。
纠正类修订的 `detail` 逐项记录纠正前后的字段值，形如
`{"corrections": [{"group_id": 3, "changed_fields": ["size"],
"before": {...}, "after": {...}}]}`；先导关系修订的 `detail` 形如
`{"group_id": 2, "leader_group_id": 1, "release_gap": 3}`（替换旧关系时
另附 `"replaced": {"leader_group_id": 4, "release_gap": 0}}`）；节点滞留
报告的 `detail` 形如
`{"group_id": 1, "node_id": 2, "recovery_minute": 6, "outcome": "resumed",
"prefix_step_count": 1}`；派遣修订形如
`{"task_id": 1, "group_id": 1, "team_id": 1, "node_id": 2,
"observed_minute": 4, "dispatch_minute": 4}`；结案修订形如
`{"task_id": 1, "group_id": 1, "team_id": 1, "exit_id": 6,
"delivery_minute": 8, "dispatch_minute": 4}`。

**GET /revisions/{rev}**

```json
{
  "rev": 2,
  "operation": "tick",
  "minute": 1,
  "detail": {"minutes": 1},
  "snapshot": {"current_minute": 1, "groups": ["…提交后的完整编排…"]},
  "changes": [
    {"group_id": 3, "change": "changed", "changed_fields": ["status"],
     "before": {"status": "scheduled", "departure_minute": 0,
                "arrival_minute": 2, "route": [5], "steps": ["…"]},
     "after":  {"status": "fixed", "departure_minute": 0,
                "arrival_minute": 2, "route": [5],
                "steps": [{"edge_id": 5, "from_node": 2, "to_node": 6,
                           "enter_minute": 0, "exit_minute": 2}]}}
  ]
}
```

每组差异 `change`：`added`（新增，`before` 为 null）/ `removed`
（移除，`after` 为 null）/ `changed`（`changed_fields` 列出变化的字段，
`before`/`after` 给出状态、出发与到达分钟、路线、逐段时刻、已走前缀步数
`prefix_step_count`、失败原因、人工救援字段
`rescue_task_id/rescue_team_id/rescued_exit_id/delivery_minute` 及先导
关系字段 `leader_group_id/release_gap/release_basis` 的前后完整值）。
未知修订号返回 HTTP 404 `{"detail": "修订号 N 不存在"}`。

## 4. 调用示例

```bash
# curl 版（依次演示初始化、新增组、新增封闭、推进分钟、409 拒绝、
# 容量管制登记/重叠/超载拒绝、有效上限核验、现场滞留报告、人工救援
# 派遣/结案、先导关系、出口清点登记/幂等/更正/查询、修订追溯）
bash examples/curl_examples.sh

# Python 版（仅标准库）
python3 examples/python_client.py
```

最小示例：

```bash
curl -s http://localhost:8000/plan | python3 -m json.tool

curl -s -X POST http://localhost:8000/tick \
  -H 'Content-Type: application/json' -d '{"minutes": 5}'

# 登记容量管制：通道 1 第 10..14 分钟限流至 10 人/分钟
curl -s -X POST http://localhost:8000/capacity-controls \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}'

# 查看通道 1 逐分钟占用与各分钟有效上限
curl -s http://localhost:8000/passages/1/load | python3 -m json.tool

# 现场报告：组 1 已到达节点 2、预计第 6 分钟恢复，按现状重找余程
curl -s -X POST http://localhost:8000/groups/1/delay-report \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' | python3 -m json.tool

# 登记一支载员量 12 的人工救援队
curl -s -X POST http://localhost:8000/rescue-teams \
  -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 12}'

# 把待救援组 1（滞留节点 2、所见当前分钟 6）派遣给救援队 1
curl -s -X POST http://localhost:8000/rescue-tasks \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 6}' \
  | python3 -m json.tool

# 查询任务 1 当前状态
curl -s http://localhost:8000/rescue-tasks/1 | python3 -m json.tool

# 结案核销：实际由出口 6 在第 8 分钟送达
curl -s -X POST http://localhost:8000/rescue-tasks/1/completion \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 8}' | python3 -m json.tool

# 登记先导关系：组2 须在先导组1 规划到达（或人工实际送达）3 分钟后才能放行
curl -s -X POST http://localhost:8000/group-prerequisites \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 1, "release_gap": 3}' \
  | python3 -m json.tool

# 查询全部先导关系与实时放行依据
curl -s http://localhost:8000/group-prerequisites | python3 -m json.tool

# 出口清点：组1 已按固定路线抵达出口6（到达分钟2，当前分钟3），清点 4 人
curl -s -X POST http://localhost:8000/exit-counts \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0001", "group_id": 1, "exit_id": 6,
       "count": 4, "counted_minute": 2}' | python3 -m json.tool

# 凭流水号+版本+原因更正为 3 人（保留原值与更正历史）
curl -s -X POST http://localhost:8000/exit-counts/C-0001/correction \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "count": 3, "reason": "复核实为 3 人"}' \
  | python3 -m json.tool

# 按组查询清点台账：应到/已清点/未清点与逐条记录
curl -s http://localhost:8000/exit-counts/1 | python3 -m json.tool
```

## 5. 编排语义（约定）

1. **时间**：当前分钟从 0 起按整数推进。组在第 `t` 分钟进入通行分钟数为
   `w` 的通道，占用分钟区间 `[t, t+w)`（左闭右开），第 `t+w` 分钟到达。
   到达分钟 `<= deadline` 即视为在截止前（第 `deadline` 分钟末）完成。
2. **封闭**：通行区间与任一封闭 `[a,b)` 相交（`t < b 且 a < t+w`）即不可
   进入；可在节点等待，封闭结束分钟 `b` 当分钟即可进入。
3. **容量**：同一通道同一分钟，所有在途组人数之和不得超过该通道该分钟
   的有效上限。有效上限默认为通道容量；登记容量管制后，管制时段
   `[a,b)` 内收紧为 `min(原容量, 管制上限)`，时段外仍为原容量。
   同一通道的管制时段互不重叠。
4. **排序**（逐组贪婪）：优先级从高到低；同级按最早出发分钟升序；再相同
   按组编号升序。高优先级组先占容量，低优先级组只能使用剩余容量。
5. **择优**：在允许出发范围内取到达分钟最早；同到达取出口编号最小；
   再相同取通道编号序列字典序最小。算法为多标签时变 FIFO 最短路
   （等待只发生在节点，边到达时间关于进入时间单调，标签集合保证字典序
   并列情形正确）。
6. **推进与固定**：`/tick` 推进后，旧计划中出发分钟 `< 当前分钟` 的组
   立即视为已出发（`fixed`），路线与容量占用永久固定；其余未出发组
   （含此前失败与前置未满足的组）以当前分钟为最早出发时间整体重排。被
   现场报告置为待救援（`awaiting_rescue`）的组不参与重排，时间推进不会
   自动为其重新规划；已交接人工救援（`rescuing`）与已人工送达
   （`rescued`）的组同样不参与重排，由救援派遣/结案接口管理。
7. **增量变更**：新增人群组或封闭时只重排未出发组；重排在单个 SQLite
   事务内“先整体删除旧路线、再写入新路线”，不会留下部分重排。
8. **组纠正**：纠正清单整批校验、整批生效——任一项目标组不存在、
   同批编号重复、最终字段不满足既有组校验、起点非场馆节点（422），
   或目标组已出发（409，含并发时间推进使其在受理前出发）时，整批
   拒绝且状态与修订历史不变；全部有效时在同一事务内应用全部最终值
   并整体重排未出发组，已出发组路线与占用保持固定。
9. **封闭拒绝**：新封闭若与任一已出发组尚未走完的剩余路线相交，整次请求
   拒绝（409），不写入封闭、不触发重排、不改动任何编排。
10. **容量管制**：登记管制时段时，通道不存在、起止无效、起点早于当前分钟
   （422）或与同通道已有管制重叠（409）均整次拒绝；若新上限使已出发组
   剩余路线在任一分钟超载，同样整次拒绝（409）并返回冲突分钟、占用量
   与相关组。成功后仅重排未出发组，已出发组路线与占用保留；场景重置
   清除全部管制记录。
11. **并发**：全部变更接口由同一把异步互斥锁串行化，容量逐组累加校验，
    并发变更不会超用容量。
12. **修订**：十类变更（场景重置/新增组/纠正组/登记封闭/登记容量管制/
    登记先导关系/现场节点滞留报告/人工救援派遣/救援结案核销/推进时间）
    成功时，在同一事务内追加修订记录（全局递增修订号、触发操作、当时
    分钟、前后完整编排快照）；被拒绝的请求不产生修订，救援队登记也不
    产生修订。修订不随场景重置或服务重启丢失，历史快照按落库内容原样
    返回，不按当前场景重算。
13. **现场节点滞留报告**：仅当该组按现行计划已到达所报节点、尚未进入
    下一通道且未抵达出口时受理（下一通道进入分钟 == 当前分钟仍视为在
    节点；恢复分钟不得早于当前分钟）；组未出发、节点或时机不符一律拒绝
    且不改编排。受理后保留已走前缀及其时间、释放该组从当前分钟起的旧
    预约，再以滞留节点为起点、恢复分钟为最早出发，按现有封闭、限流与
    其他已出发组固定占用重找截止前余程（沿用最早到达/出口/通道序列
    择优）：有解则保持 `fixed`（已走前缀 + 新余程，新余程同为固定占用），
    无解则置 `awaiting_rescue` 并说明原因、不恢复旧预约；其他未出发组
    随之整体重排，其他已出发组路线与占用不变。待救援组不参与后续时间
    推进的自动重排，只能在其滞留节点由新的报告重新受理；当前编排以
    `prefix_step_count` 区分已走前缀与新余程。
14. **人工救援队交接**：救援队按唯一正整数编号与正整数载员量登记，重复
    登记整次拒绝。派遣提交组号、队号、滞留节点与所见当前分钟，仅当所见
    分钟未变、组仍在该节点处于 `awaiting_rescue`、人数不超载员量、队伍
    空闲且同组同队各无进行中任务时受理并返回任务号；任一条件不符（无效
    编号、状态/时序冲突、并发时间推进抢占）整次拒绝，不改编排与修订。
    受理后组置 `rescuing`：保留已走通道前缀、不产生未来通道占用、不参与
    时间推进自动重排，其节点滞留报告与组纠正被拒绝。结案提交任务号、
    **既有出口编号**与实际送达分钟，送达分钟须不早于派遣且不晚于当前
    分钟（晚于原截止如实记录）；成功后队伍释放、组置 `rescued`，实际
    出口与时刻单列（不回写规划到达字段）。成功派遣与结案均与修订在同一
    事务原子写入。场景重置清空全部救援队与任务，旧修订保留、编号继续
    递增。
15. **先导组放行间隔**：现场可为尚未出发的组登记一个先导组与非负放行
    间隔，同一组至多一项关系、再次登记替换旧关系；先导组须存在，禁止
    自依赖与成环（登记时以替换后的关系图做环检测），目标组已出发、编号
    或间隔无效、形成环时整次拒绝，计划与修订历史不变。后继组的可出发
    分钟不早于自身最早出发分钟及**先导组规划到达分钟 + 间隔**；先导组
    已由人工救援结案送达时改用**实际送达分钟 + 间隔**。先导待救援、救援
    中、尚未安排、无可行路线或先导自身前置未满足时，后继标记
    `blocked`（前置未满足）：不写路线、不占通道；阻塞沿多级关系传递，
    先导落定后的下一次重排自动转正。多级关系**按依赖顺序安排**：每轮先
    在可安排组（无先导或先导已有可用到达锚点）中沿用优先级、最早出发
    分钟、组编号与路线并列规则逐组贪婪安排；截止、封闭与容量约束照常
    生效，前置满足但自身赶不上截止的组仍为普通 `failed`。关系变更、
    时间推进、现场滞留及救援结案后立即重排未出发组；已出发组路线及既有
    放行决定不追溯。成功登记在同一事务原子写入一条
    `group-prerequisites` 修订（替换时 `detail.replaced` 记录旧关系）；
    场景重置清空全部先导关系。
16. **出口清点**：现场在出口按组清点实际撤离人数。每条清点凭非空唯一
    流水号幂等登记：同内容重试返回原记录（HTTP 200），异内容整次拒绝
    （409）；仅已按固定路线抵达该出口（`fixed` 且到达分钟不晚于当前
    分钟）或已由人工救援实际送达该出口（`rescued`，采用实际出口与实际
    送达分钟）的组可登记；清点分钟不得早于相应到达/送达分钟、不得晚于
    当前分钟；同组累计人数不得超过组人数（登记与更正均按更正后口径
    复核）。更正凭原流水号、记录当前版本与非空原因把人数更正为非负
    整数：保留原值与更正历史（`submitted_count` 恒为首次提交人数，
    每次更正追加一条历史并使版本 +1）；版本失配、对同一版本重复更正
    或更正后累计超额均整次拒绝（409），记录与历史不变。清点与更正
    不改编排、不产生修订；场景重置清空全部清点记录与更正历史。

## 6. 项目结构

```
app/
  main.py       FastAPI 路由、事务/互斥、增量重排（含先导依赖多轮安排）与拒绝逻辑、先导关系登记/查询、现场滞留报告、人工救援队派遣/结案、出口清点登记/更正/查询、修订记录与差异
  planner.py    编排核心：时变 FIFO 多标签最短路 + 容量/封闭可行性
  db.py         SQLite 建表、世界状态加载、整组事务化持久化、先导关系/滞留报告/出口清点与修订落库
  schemas.py    Pydantic 输入/输出模型
  seed.py       启动演示数据
examples/       curl 与 Python 调用示例
tests/          算法单元测试与 API/并发/修订历史/先导关系/出口清点集成测试（pytest）
```

本地运行测试：

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest -q
```
