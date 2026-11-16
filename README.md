# EntityForge · 可解释实体解析与主数据修订

把 CRM、支持系统等来源中的同一实体归并为主数据，保留每个字段从哪条记录选出、哪些匹配规则生效，以及人工修改和回滚的历史。

## 已实现

- `source + id` 为来源记录键，重复导入更新同一条记录。
- Unicode NFKC、大小写、空白、电话数字与可配置别名标准化。
- 邮箱、电话和城市阻塞召回；确定性标识与姓名相似度/同城匹配。
- 不同非空邮箱/电话视为冲突；合并候选簇前检查所有跨簇记录，阻止矛盾标识的传递合并。
- 按来源优先级选择非空字段，并记录字段来源。默认同优先级按记录键确定顺序。
- 人工合并、完整分区拆分、历史快照读取、回滚为新修订。
- SQLite 写事务和 `expected` 修订号防止并发覆盖；失败修改不提交。

## 快速运行

Python 3.11+，在项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite ingest examples/records.json
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite resolve
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite history
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite rollback 1
python3 -m unittest discover -s tests -v
python3 examples/smoke.py
```

样例的 3 条来源记录解析为 2 个实体。CLI 每次运行都重新打开数据库；端到端验证检查重启后状态及回滚历史。

## 模块与扩展

| 模块 | 职责 | 扩展方式 |
|---|---|---|
| `matching.py` | 归一化、匹配证据、字段存续 | 注入 `Normalizer`、实现 `Matcher.compare`、替换 `PrioritySurvivor.select` |
| `storage.py` | SQLite 状态修订、审计、事务与乐观锁 | 保持 `read/edit/history` 边界增加存储适配器 |
| `service.py` | 候选聚类、人工操作、快照与回滚 | 由应用服务或 HTTP 层调用 |
| `cli.py` | JSON 输入输出与错误退出码 | 添加文件流/连接器时复用领域服务 |

```python
from entityforge import EntityService
from entityforge.matching import Normalizer, PrioritySurvivor

service = EntityService("master.sqlite",
    normalizer=Normalizer({"sh": "Shanghai"}),
    survivor=PrioritySurvivor({"crm": 0, "support": 10}))
try:
    revision = service.ingest([
        {"source": "crm", "id": "1", "fields": {"name": "Alice", "city": "SH"}},
        {"source": "support", "id": "2", "fields": {"name": "Alice", "city": "Shanghai"}}
    ])
    result = service.resolve(expected=revision)
    entity_id, entity = next(iter(result["entities"].items()))
    service.split(entity_id, [[m] for m in entity["members"]], expected=result["revision"])
finally:
    service.close()
```

## 一致性和当前边界

字段仅接受字符串或 null；邮箱/电话为业务身份假设，不能保证现实世界中不存在共享联系方式。自动规则偏保守，冲突可由人工合并覆盖。人工合并/拆分保存在修订中，再次执行全量 `resolve` 会重新按自动规则计算；导入修改会清空派生实体，需重新解析。实体 ID 由成员集合派生，成员变化时 ID 可能改变。

当前全量状态修订保存在 SQLite JSON 中；城市大块候选最坏为 O(n²)，未验证百万级解析、增量聚类或外部数据库。快照是工程可回滚状态，不是人工判定工作流。

## 独立关系层级计算

主数据归并之后，上游仍可能对“哪个实体属于哪个上级”给出互相矛盾、有效期不同的关系声明。关系层级在**独立产物**中计算，与实体的 ingest/resolve/merge/split、成员约束和修订保留规则完全隔离：实体管线不读写关系数据，关系引擎只通过只读连接固定某个实体修订的成员映射，之后实体 HEAD 前进也不会替换该映射。

### 合同

- 每条断言带唯一 `assertion_id`，端点用来源记录键（`source + id`）标识；`parent` 为 `null` 是显式根声明。
- 有效期是带明确时区的半开区间 `[start, end)`；偏好分数为非负整数。
- 端点按固定快照的成员映射解析为实体。同一实体内部的边解释为**自环并排除**；未知端点、或同一记录 id 携带与固定快照不一致的内容摘要，都会**整批拒绝**。
- `assertion_id` 是断言身份：同 id 重复提交且内容一致是幂等重复，不增加分数；同 id 改内容拒绝整批。不同 id 支持同一条父子边时是独立来源，折叠为最高分并保留全部最高分来源，分数不会累加。

### 全局求解（禁止贪心删环边）

每个时段把相同父子边的有效支持折叠为最高分边，再为每个实体选至多一个上级，结果必须无环，并使所选边分数之和最大；分数相同时按固定实体身份排列的父节点序列稳定裁决。没有显式根声明时，每个实体都有零分虚拟根边兜底，森林始终可行。

这是最大权树形图（branching）问题，提供两个精确求解器并相互交叉验证：

| 求解器 | 方法 | 预算行为 |
|---|---|---|
| `edmonds` | Chu–Liu/Edmonds 带环收缩，稳定裁决编码进复合整数权重 | 无预算，始终精确 |
| `branch_and_bound` | 分支限界约束搜索，带显式节点/解预算 | 预算耗尽抛 `BudgetExhausted`，绝不把局部结果当最优 |

逐点选最高分再随手删掉环里最弱边是错误做法。对 `B→A=5、C→B=6、A→C=7`，根声明 `根→A=0、根→B=0、根→C=6`（箭头为父→子），精确最优森林是 `根→C、C→B、B→A`，总分 17；贪心成环后删最弱边 `B→A` 只能得到 13。小图通过独立枚举全部合法森林的 oracle 校验该反例、并列分数、重复断言、归并后自环、区间端点和预算拒绝的组合。

### 时间与证据

- 只在断言生效/失效边界重新求解；同刻撤销与新增按半开语义处理（失效者在该刻不活动，生效者活动）。
- 相邻时段森林相同时可合并呈现，但每个组成时段及其来源证据都保留，点查询会定位到准确的组成时段。
- 覆盖范围之外的查询返回全零分基线森林，而不是过期结果。
- 每个结果携带固定输入 `revision`、断言摘要（id 与内容摘要）、求解规则版本 `rule_version`，以及每条采用/排除边的依据：`lower_score`、`tie_lost`、`acyclicity_constraint` 或自环 `self_loop_excluded`。排除只表示该边未参与最优森林，**不**声称它是错误关系。
- 独立验证器重建折叠模型，复核一父约束、无环、分数和、支持摘要完整性与每条边的理由，并用枚举 oracle（小图）或另一精确求解器（大图）复核最优性。

### 用法

```bash
export PYTHONPATH="$PWD/src"
python3 -m entityforge.cli --db /tmp/entities.sqlite ingest examples/records.json
python3 -m entityforge.cli --db /tmp/entities.sqlite resolve
# 关系产物是独立 SQLite 文件；--method 可切换 branch_and_bound 并设置预算
python3 -m entityforge.cli --db /tmp/entities.sqlite \
  relations --artifact /tmp/relations.sqlite submit examples/relation_assertions.json
python3 -m entityforge.cli --db /tmp/entities.sqlite \
  relations --artifact /tmp/relations.sqlite verify
python3 -m entityforge.cli --db /tmp/entities.sqlite \
  relations --artifact /tmp/relations.sqlite at "2026-06-01T00:00:00Z"
python3 examples/relation_smoke.py
```

```python
from entityforge.relations import RelationEngine

engine = RelationEngine("relations.sqlite", "master.sqlite",
                        method="edmonds")  # 或 method="branch_and_bound",
                                          # node_budget=..., solution_budget=...
view = engine.submit([{
    "assertion_id": "ba",
    "child": {"source": "crm", "id": "a"},
    "parent": {"source": "crm", "id": "b"},
    "score": 5,
    "start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z",
}])
engine.verify()       # 独立校验全部持久化时段
engine.close()
```

关系产物的批处理同样具备审计表与整批原子性：验证或求解失败时不会留下半写状态，拒绝批次会记录原因但不改变既有断言与区间。
