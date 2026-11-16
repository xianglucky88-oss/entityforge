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
