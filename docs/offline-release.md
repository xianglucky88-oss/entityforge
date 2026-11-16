# 离线构建发布链：准备与运行

交付环境**不能联网**。本目录的 `vendor/wheels/` 已包含全部构建工具制品，整条链路只用 `--no-index` + 本地 `--find-links`，任何缺失或被改动的制品都是显式失败，绝不回退到包索引。

## 1. 固定了什么

| 对象 | 固定值 | 校验位置 |
|---|---|---|
| 解释器 | CPython 3.11.2，`/usr/bin/python3.11`，二进制 sha256 见 `releng/pins.json` | `common.verify_interpreter`（版本 + 平台 + 二进制摘要） |
| pip | 23.3.2（py3-none-any wheel，sha256 固定） | `common.verify_vendor` |
| setuptools | 68.1.2（满足原 `setuptools>=68`，改为精确钉版） | 同上，并与 `pyproject.toml` 精确钉版交叉核对 |
| wheel | 0.41.3 | 同上 |
| 归档时间 | `SOURCE_DATE_EPOCH=1700000000` | `pins.json` + `clean_env` 注入 |

`pyproject.toml` 的 `build-system.requires` 已从 `setuptools>=68` 改为 `["setuptools==68.1.2", "wheel==0.41.3"]`：下限约束会让离线构建在不同时间解析到不同工具版本，与可复现目标冲突。

## 2. 在联网准备机上（一次性，交付环境永不执行）

```bash
bash vendor/fetch-vendor.sh https://mirrors.aliyun.com/pypi/simple
python3 vendor/write-manifest.py
```

`fetch-vendor.sh` 做三件事：从 simple 索引解析 `pins.json` 钉定的三个 wheel；把索引 URL fragment 中公布的 sha256 与 `pins.json` 核对（不一致即退出）；下载后再对落盘字节复核一次摘要。`vendor/MANIFEST.json` 是离线制品摘要清单，可独立复核：

```bash
sha256sum -c <(python3 -c "import json;[print(w['sha256'],w['file'].replace('wheels/','vendor/wheels/')) for w in json.load(open('vendor/MANIFEST.json'))['wheels']]")
```

## 3. 离线引导（无 ensurepip/pip 的机器也能用）

本机系统 Python 没有 ensurepip，因此引导不依赖它：

1. `python -m venv --without-pip` 用标准库建 venv；
2. 在 venv 解释器下用 `runpy.run_module('pip')` 直接执行 vendor 里的 pip wheel（`-I` 下脚本目录不在 sys.path，必须显式 insert wheel 路径）；
3. pip 以 `PIP_NO_INDEX=1 --find-links vendor/wheels` 自举并安装 setuptools/wheel；
4. 导入探测确认四个版本与 pins 完全一致。

## 4. 运行入口（Make）

```bash
make release-build   # 冻结清单 → 原始 sdist/wheel → 标准化 → sdist 重建 wheel
                     # → 成员级 diff（代码/METADATA/RECORD）→ 隔离安装验收
make release-repro   # 两个不同绝对路径、不同 mtime 的副本，给原始+标准化摘要与结论
make release         # release-build 后按内容寻址提交到 release/，原子切换版本引用
make release-test    # 14 个证明测试：双进程竞争、替换暂存、提交前退出、磁盘失败、离线强制
make release-accept  # 仅对已构建 wheel 重跑隔离验收
make clean-release   # 清理 release-work/ 与 release/
```

## 5. 验收为什么无法借用源码或开发环境

- 每个验收都新建 `--without-pip` venv，只从 vendor 装 pip/setuptools，再用**显式 wheel 路径**安装，使 pip 写 PEP 610 `direct_url.json`（含该 wheel 的 `file://` URL 与 sha256）；
- 黑盒运行前把源码树所有目录自底向上 `chmod 000`（不可读、不可遍历），运行结束自顶向下还原；
- 验收沙箱（venv、脚本、cwd）位于被锁源码树**之外**的系统临时目录，避免自锁；
- 子进程使用 `python -I`、清理后的最小环境（无 `PYTHONPATH`/`PYTHONHOME`/代理/`PIP_CONFIG_FILE`），并在无关 cwd 启动；
- 黑盒断言：导入路径位于 `site-packages`、`sys.flags.isolated` 为真、cwd 不在 `sys.path`、`INSTALLER=pip`、`direct_url` 绑定摘要等于被验收 wheel、RECORD 逐文件复核、控制台脚本与 Python API 行为正确。
- 构建 venv 从不安装 entityforge（后端仅进程内调用），因此不存在“借用开发环境已安装副本”的可能（测试显式断言 build-venv 内无 entityforge）。

## 6. 标准化是显式步骤，不掩盖差异

实测（setuptools 68.1.2）：wheel 的 zip 时间受 `SOURCE_DATE_EPOCH` 约束，但 sdist 的 tar/PAX 头记录构建机墙钟时间、源文件自身 mtime、uid/gid/uname，原始 `.tar.gz` 跨副本必然不同。

`releng/normalize.py` 因此作为**独立的显式构建步骤**：

- wheel：按名排序重写 zip 条目，固定 epoch、mode、无 extra field；重写后用 RECORD 复核全部载荷；
- sdist：重写为 `gzip(USTAR tar)`，固定排序/epoch/uid=0/无 uname/gname，gzip 头 mtime=0；
- 两者重写前后逐成员比对载荷摘要，任何代码或元数据变化都会让构建失败，而不是被“相同归档字节”掩盖。

报告同时保留 `raw_sha256` 与 `normalized_sha256`；代码、METADATA、RECORD 的成员级 diff 单独成节。

## 7. 离线失败必须显式

| 故障 | 行为 |
|---|---|
| vendor 缺 wheel | `missing offline artifact`，构建中止 |
| vendor wheel 被改 | `hash mismatch`，构建中止 |
| 解释器缺失/摘要不符/版本不符 | 分别报 `pinned interpreter not available` / `digest mismatch` / `version mismatch` |
| 试图 `pip install <包索引上的包>` | `PIP_NO_INDEX` 下报 `No matching distribution found`，即使显式给 `--index-url` 也被压制 |

## 8. 领域代码边界

`src/entityforge/` 的实体规则、修订存储与 CLI 行为保持黑盒，本链路不修改其领域逻辑；验收只通过已安装的控制台入口与公开 Python API 调用。`examples/smoke.py` 仍按原样自行设置 `PYTHONPATH=src` 服务于开发演示（`make demo`），与离线验收路径完全分离。
