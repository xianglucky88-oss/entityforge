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
- **幂等批次入口 `apply_batch`**：同一批的更新与显式删除、调用方批次 id、`expected` 修订号一次提交；来源记录、候选索引、成员关系与字段来源始终属于同一修订。
- **增量解析**：按失效闭包局部重算，结果与同一固定规则下的全量解析逐项一致（成员、字段值、来源、冲突证据）；无关候选块不重新调用匹配器。
- **明确的全量回退**：仅当规则指纹变化或派生状态不可作增量基线时整批重算，并在回执与审计中报告原因，绝不每批偷偷走全量。

## 快速运行

Python 3.11+，在项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite ingest examples/records.json
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite resolve
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite batch examples/batch-ops.json --batch-id b1 --expected 2
python3 -m entityforge.cli --db /tmp/entityforge-demo.sqlite batch examples/batch-ops.json --batch-id b1 --expected 2   # 重试 → "status": "replayed"，不写审计
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
| `storage.py` | SQLite 状态修订、批次回执、候选索引、事务与乐观锁 | 保持 `read/edit/history` 边界增加存储适配器 |
| `service.py` | 批次幂等、失效闭包增量聚类、人工操作、快照与回滚 | 由应用服务或 HTTP 层调用 |
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

## 批次入口 `apply_batch`

```python
receipt = service.apply_batch("batch-42", [
    {"op": "upsert", "source": "crm", "id": "1", "fields": {"name": "Alice", "city": "SH"}},
    {"op": "delete", "source": "web", "id": "9"},
], expected=7)
```

- 操作列表先整体校验（无效记录出现在任何位置都会整批拒绝，不留下半套实体，也不占用批次 id），再按记录键折叠为最后操作生效；同一事务内依次：识别已完成批次 → 校验 `expected` → 应用变更 → 增量解析 → 提交修订、索引与回执。
- **重放**：批次 id 已提交且内容（折叠后的有效操作）相同 → 返回原回执（`status: "replayed"`、原修订号），不写审计、不推进 HEAD。识别批次优先于 `expected` 检查，因此成功回执丢失后的重试即使带着已陈旧的 `expected` 也返回原修订。
- **冲突**：同 id 换内容抛 `BatchConflict`；新批次 `expected` 陈旧抛 `RevisionConflict`。CLI 分别以 `error: batch_conflict:` / `error: revision_conflict:` 退出码 2 报告。
- **回执**含 `status`、`revision`、`fallback`/`fallback_reason`、`changed_records`、`affected_records`、`matcher_calls`、`entity_count` 等，与修订同事务持久化，可跨进程重启重放。
- **全量回退**只在两种情况下发生并明确报告：`rules_changed`（匹配器/存续策略/算法模式的规则指纹与产生当前实体的指纹不同）或 `entities_not_resolved`（实体被 ingest 清空或被人工合并/拆分偏离自动解析不动点）。索引本身不一致时总是安全重建（索引是记录的纯函数），不视为回退。

## 增量解析设计

**索引身份**：`record_index` 表保存 `记录键 → (归一化字段, 阻塞键列表)`，`index_meta.revision` 绑定其对应的 HEAD 修订。索引与记录、实体在同一事务提交，读者只能看到提交前或提交后的完整状态；崩溃（含索引已写但修订未提交）经 WAL 回滚，不留半套状态。

**失效闭包**：变更记录（upsert 后归一化或原始字段变化、删除）的旧阻塞块 ∪ 新阻塞块为种子，不动点扩展为整组件（HEAD 实体）与整块的并集。闭包边界上没有任何候选块、实体或证据对跨越：块不跨界 ⇒ 全量重跑时闭包内外的分组互不影响，闭包内按同一确定性 `_cluster`（相同排序、相同完整链接冲突检查）重算的结果与全量解析逐项相等；闭包外实体与证据原样保留，匹配器零调用。记录删除、联系方式更换、别名变化同时改变旧候选与新候选，因此种子必须包含新旧两侧的块，组件拆分与跨簇冲突都在闭包内重现。

**批次回执**：`batches` 表（批次 id → 修订、内容哈希、回执）与修订、索引同一事务提交；重放路径在事务内只读并回滚，不可能产生可误认成功的回执。

**索引放在修订快照 vs 独立索引表**：快照内索引让每个修订自包含、回滚零重建，但每批要重写整个索引（O（索引规模） 写放大），状态 JSON 与每次快照读取都随之膨胀。独立索引表按行更新（O（变更规模）），代价是回滚/人工操作后需要重建或与 HEAD 对齐——重建总是安全的，且历史修订本就不需要携带索引。当前选择独立索引表 + 同事务提交 + `index_meta.revision` 校验。

## 一致性和当前边界

字段仅接受字符串或 null；邮箱/电话为业务身份假设，不能保证现实世界中不存在共享联系方式。自动规则偏保守，冲突可由人工合并覆盖。人工合并/拆分保存在修订中，之后的批次或全量 `resolve` 会重新按自动规则计算（回执以 `entities_not_resolved` 报告此次全量回退）。旧式 `ingest` 仍会在导入后清空派生实体，需重新解析或走一次回退批次。实体 ID 由成员集合派生，成员变化时 ID 可能改变。

当前全量状态修订保存在 SQLite JSON 中；城市大块候选最坏为 O(n²)。增量批次把昂贵的匹配器调用限制在失效闭包内（回执中的 `matcher_calls`/`affected_records` 可验证），但阻塞成员关系等簿记仍是每批 O(n) 扫描，未验证百万级解析或外部数据库。快照是工程可回滚状态，不是人工判定工作流。
