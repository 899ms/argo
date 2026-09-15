# 设计：跨平台路径与惯例（macOS / Linux / BSD / Windows）

> 状态：已实施（2026-09-15）。`python3 scripts/argo_paths.py [--json]`（或 `argo paths`）
> 会直接打印下列路径的**实际来源**——支持类问题的第一问是「你到底在读哪个文件」。
> 想让存量安装改用惯例目录：`argo paths --migrate`（默认只报告，见 §4）。

## 0. 问题重定义

argo 需要在四类系统上找到两样东西：**状态目录**（搜索缓存、配额计数、准入记录、
健康库）与**密钥文件**。此前两处都是写死的：

- 状态目录兜底 `~/.cache/unified-search`：Windows 上不符合 `%LOCALAPPDATA%` 惯例，
  Linux 上无视 `XDG_CACHE_HOME`；
- 密钥文件写死 `~/.config/argo/env`：Windows 该用 `%APPDATA%`，自定义
  `XDG_CONFIG_HOME` 的用户无处安放密钥。

写死的代价不是「不够好看」，而是**平台约定的两处收益同时丢失**：备份/清理工具
按惯例知道该动哪个目录；容器与 CI 镜像按惯例只挂载一个路径。

**同等重要的一个前提**：`config.yaml` 里曾经写死 `cache.db_path`。它优先级高于平台
惯例，等于让上面这套解析永远轮不到生效——所以那一行现在是注释状态（要固定位置再
启用，见文件内说明）。


## 0.1 一条命令核对（任何平台）

```sh
argo paths --check          # 人读；有 fail 时退出码非 0（可直接进脚本/CI）
argo paths --check --json   # 机器读
```

它在这台机器上**实测**七件事：平台与惯例变量、状态目录解析（含来源）、状态目录
**可写性**、密钥文件候选与可解析性（只报键数量，不回显值）、配置加载、解释器候选
最终落到谁，以及**跨进程锁是否真的互斥**（起一个子进程握住锁，父进程再抢一次并测量
等待时间——Windows 的 msvcrt 分支在开发机上只能用假模块测契约，只有真机跑这一条才算
验过）。

真机验证清单（Windows / BSD / 容器）：

```sh
argo paths --check                 # 目录解析 + 锁 + 解释器，一条够
argo paths --check --json | ...    # CI 里断言 status 没有 fail
```

## 1. 状态目录的解析顺序（存量优先）

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `ARGO_STATE_DIR` | 显式覆盖：测试隔离、只读环境、想换目录的用户都用它 |
| 2 | `config.yaml` 的 `cache.db_path` 父目录 | 与主缓存同域，保持既有语义 |
| 3 | 历史目录 `~/.cache/unified-search`（**已存在**） | 继续用，**不迁移** |
| 4 | 平台惯例目录 | Windows `%LOCALAPPDATA%/unified-search`；POSIX `$XDG_CACHE_HOME/unified-search` |
| 5 | 兜底 `~/.cache/unified-search` | 即 POSIX 的 XDG 默认值，与历史一致 |

**为什么「已存在的历史目录」排在平台惯例之前**：`LOCALAPPDATA` 在 Windows 上恒有值，
`XDG_CACHE_HOME` 在部分 Linux 桌面也常被设置。若它们无条件优先，存量用户升级后会
发现搜索缓存、配额计数、准入记录**全部「归零」**——那比「目录不够惯例」严重得多。
想换目录的用户用 `ARGO_STATE_DIR` 或 `cache.db_path` 显式指定，一步到位且可预期。

## 2. 密钥文件的解析顺序（候选合并）

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `ARGO_ENV_FILE` | 显式指定，**只读这一个**，不合并 |
| 2 | 平台惯例 | Windows `%APPDATA%/argo/env`；POSIX `$XDG_CONFIG_HOME/argo/env`（设置了才有） |
| 3 | 历史路径 `~/.config/argo/env` | POSIX 上它就是 XDG 默认值 |

读取时**合并全部候选**（靠前者逐键优先），而不是只认第一个存在的文件。理由是本仓
反复踩过的一类故障：`同几个变量的两套名字/两处位置` 会导致「一部分读得到、一部分
读不到」——引擎按状态层判定可用、实际拿不到 key，表现为静默 0 结果（「装了没通电」）。
缓存签名覆盖全部候选的 `(路径, mtime_ns, size)`，改任意一份都会触发重读。

## 3. 其它平台细节

| 事项 | 做法 |
|------|------|
| 跨进程文件锁 | POSIX `fcntl.flock`；Windows `msvcrt.locking`（锁首字节，空文件先写占位）。都不可用时 fail-open，绝不阻断主路径 |
| 解释器探测 | `bin/argo`：`python3.x` → `py -3` / `py` / `python` / `python.exe`；`py` 是启动器，先解析成真实路径再入缓存 |
| 配置缓存指纹 | config.yaml 用**内容摘要**（blake2b/16，0.47 ms）：`mtime` 可被 `cp -p`/`touch -r` 还原，`st_ctime` 在 Windows 是**创建时间**，两者都不能作为跨平台的失效判据 |
| 归档脱敏 | 家目录三家平台都认：`/Users/<x>`、`/home/<x>`、`C:\Users\<x>` |
| 临时文件 | `tempfile.gettempdir()`；不用 `/tmp` 字面量 |
| 编码 | 显式 UTF-8（`PYTHONUTF8=1` + 全部 `read_text(encoding="utf-8")`），规避 Windows GBK |
| 控制台 | 不依赖 ANSI 颜色；无 TTY 时输出纯文本/JSON |

## 4. 存量安装怎么改成惯例目录（可选）

`argo paths --migrate` 把历史目录里的状态搬到平台惯例目录，并让惯例**立即生效**：

```sh
argo paths --migrate --dry-run     # 先看会搬什么（只读，随时可跑）
argo paths --migrate --yes         # 确认执行（不使用 TTY 探测做交互确认）
```

安全边界（这是会动用户数据的命令，默认什么都不做）：

- 只在根目录由**历史默认**决定时才迁移；`ARGO_STATE_DIR` 或 `cache.db_path` 说了算时
  直接拒绝——那是用户明确指定的位置。
- 目标目录已有内容 → 拒绝（绝不覆盖）。
- 不用 TTY 探测做确认：无论交互与否都要求显式 `--yes`——数据搬迁命令在脚本里
  该有完全一致的行为（本仓也有专门门禁挡 `isatty`）。
- 搬完清掉空的历史目录。这一步是关键：留着空目录会让 `state_root()` 继续判
  「历史存在」而停在旧路径，用户会以为数据丢了。个别文件搬不动时保留目录并写入
  `MIGRATED_TO.txt` 说明去处，绝不假装成功。

## 5. 给贡献者的两条约束

1. **状态路径只能由 `argo_paths` 派生**（各模块只声明文件名）。历史上 11 个模块各自
   拼 `~/.cache/unified-search`，导致 `config.yaml` 管不住这些文件、测试无法整体隔离。
2. **候选位置只能由 `argo_paths._platform_cache_root` / `engine_env._envfile_paths`
   解析**。任何新增候选都改这两个函数，不要在别处再写一份判断——那正是「两套口径」
   的开端，而两套口径最终一定会漂移。
