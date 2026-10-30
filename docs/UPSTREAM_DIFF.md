# 上游仓库差异清单

- 本地：`main` @ `6d0daf3`（2026-10-25），工作区有 **43 个未提交改动**。
- 上游：`Lumorian/xianyu-auto-reply` `main` @ `4bff0dd`（2026-10-30）。
- 对比方式：GitHub API 读取上游提交/文件树；**未执行 git merge / pull**。
- 本地 origin 是 `wan-hua/xianyu-auto-reply`，上游是另一个仓库，历史不共享。
- 上游自报测试规模：**713 passed**；本地当前：**348 passed**。

## 一、上游新增/修复块

| # | 提交 | 类型 | 主要改动 | 与本地关系 | 建议 |
|---|---|---|---|---|---|
| 1 | `4bff0dd` | 修复 | 后台显示卖家在闲鱼客户端发出的消息；from_me 归一化；付款卡片恢复买家；outgoing 去重/自消息防护 | 本地消息流水有自定义 msg_type/社交，但未完整支持卖家侧消息 | 适配后合并 |
| 2 | `ead5b20` | 依赖 | requirements.lock，安装哈希校验 | 本地 requirements 含 2018K/MiniDaily 依赖 | 重新生成锁文件后再用 |
| 3 | `f70e252` | 安全修复 | 发货前强制核实订单归属；原子认领发送 | 本地有订单核实与 inflight，但不是数据库级原子认领 | 高优先适配 |
| 4 | `04e5ab1` | 新功能 | 闲鱼订单历史同步；backup/inspect/restore 迁移包；恢复前 SQLite 完整性校验 | 本地无 backup/order sync 模块 | 可独立移植 |
| 5 | `968a921` | 性能/安全 | 512MB 小内存适配；Pillow 惰性导入、媒体并发限制/流式响应、缓存上限、RSS；XSS/CSRF/请求体/上游路径注入/扫码日志脱敏 | 本地模板和媒体能力不同，改动面很大 | 分批移植 |
| 6 | `0830d24` | 安全大改 | Cookie AES-256-GCM 加密落库；登录态真实自检/重登预警；Cookie 清洗与入参硬化；new-api/recharge/redeem/account 等大量功能 | 与本地 2018K/MiniDaily 架构直接冲突；runner/db/web 改动巨大 | 先评审，按模块移植 |
| 7 | `b4d1f74` / `45ba924` | 文档 | 管理后台安全边界、ALLOWED_HOSTS、#token、测试数量、发货鉴权口径 | 本地 README 已定制 | 手工合并文档 |
| 8 | `520ca40` | 修复 | new-api 建好令牌但取不到 Key 时回滚令牌 | 本地上游是 2018K，不是 new-api | 暂不适用 |
| 9 | `b291e3a` | 安全 | 管理后台口令限流、Host/CSRF 校验、安全响应头、ALLOW_REMOTE 失败关闭 | 本地有管理 token，但缺少这些加固 | 建议合并 |
| 10 | `08b67ac` | 修复 | IPv6 端口探测、启停互斥锁、站点货币缓存 | 本地 daemon 较旧 | 建议适配 |
| 11 | `e90c2d7` | 修复 | orders 写入原子化、消息处理失败可重试、消息流水按天清理 | 本地 record_order 为普通 upsert | 建议合并 |
| 12 | `40d5b97` | 修复 | XFF 绕过、模型白名单热更新、二维码 401 | 本地管理接口部分已有，需对照 | 适配 |
| 13 | `3bbea03` | 行为变更 | 消息发出后不再退回卡密；确认发货失败也不回滚整单 | **与本地“先确认成功才发卡、确认失败不发卡”直接冲突** | **不要合并** |
| 14 | `f148638` | 安全 | 卡片形态与订单号来源分开判断，防伪造交易卡片白送卡密 | 本地已有 system card/order verified，但需对照补强 | 安全对照后适配 |
| 15 | `3758e5d` | 初始 | 上游初始版本 | 本地基于更早的初始快照并已大幅定制 | 不适用 |

## 二、上游新增文件

`app/backup.py`、`app/media.py`、`app/memory.py`、`app/rate.py`、`app/secrets.py`、`app/template.py`、`requirements.lock`，以及大量新测试（cookie、login、backup、order sync、topup、media 等）。

## 三、本地独有、不能被上游覆盖

- `app/k2018.py`、`app/minidaily.py`、`app/social.py`
- 2018K 建卡/删卡与实例绑定
- MiniDaily 邀请链接发货
- 求花/提醒收货/好评/自动领取小红花
- 旧会话 1 天保护
- 先确认发货、失败不发卡的本地语义
- 本地 `order_actions`、消息 `msg_type`、面板 UI

## 四、结论

不建议直接 `git pull` 或整体 merge。上游已经把项目扩展成 new-api/充值/兑换码/账号模式，和本地的 2018K/MiniDaily 分支是两套架构；直接合并会在 `runner.py`、`db.py`、`web.py`、`templates/index.html`、`upstream.py`、`config.py` 产生大量冲突，并可能覆盖本地业务语义。

建议按优先级 cherry-pick/移植：

1. **P0 安全**：`f70e252` 订单归属与原子认领、`b291e3a` 后台加固、`f148638` 卡片伪造防护、`0830d24` 的 Cookie 加密/登录态模块（拆分移植）。
2. **P1 稳定性/迁移**：`04e5ab1` 备份与订单同步、`e90c2d7` 订单原子写入、`08b67ac` daemon 健壮性、`4bff0dd` 卖家消息展示。
3. **P2**：`ead5b20` 依赖锁定、文档更新、`968a921` 的小内存/媒体部分。
4. **跳过**：`3bbea03`（与本地确认发货语义冲突）、`520ca40`（new-api 专属）。
