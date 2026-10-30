# 闲鱼自动发货 · 端到端工作流

> 依据当前仓库实现整理：`app/runner.py`、`app/engine.py`、`app/client.py`、`app/social.py`、`app/web.py`。  
> 配图：![工作流总览](workflow.png)

## 1. 总览图

```mermaid
flowchart TD
    subgraph BOOT["① 启动 / 登录 / 接入"]
        B1["start.sh / systemd"]
        B2["FastAPI 管理后台 + SQLite"]
        B3["扫码登录 / 手工 Cookie"]
        B4["WorkerManager 同步启用账号"]
        B5["AccountWorker：心跳 → /reg → ackDiff"]
        B1 --> B2 --> B3 --> B4 --> B5
    end

    subgraph ING["② 消息入口"]
        I1["WebSocket 推送"]
        I2["会话列表轮询 + 历史补拉"]
        I3["messageId 去重"]
        I4["解析买家 / 商品 / 订单号，补买家名"]
        I5["写 messages 流水（chat / trade）"]
        I1 --> I3
        I2 --> I3
        I3 --> I4 --> I5
    end

    subgraph ROUTE["③ 分流"]
        R1{"系统交易消息？"}
        R2["关闭自动回复"]
        R3["社交事件：领花 / 提醒 / 求花 / 好评"]
        R4["自动回复：商品 / 关键词规则"]
        R5{"付款触发？"}
        R6["订单核实 / 去重 / 规则匹配"]
        R7["blocked 或 skip"]
        I5 --> R1
        R1 -- 是 --> R2 --> R3
        R1 -- 否 --> R4
        I5 --> R5
        R5 -- 是 --> R6
        R5 -- 否 --> X1["结束"]
        R6 -- 不通过 --> R7
    end

    subgraph SHIP["④ 发货执行"]
        D1{"规则模式"}
        D2["2018K：查金额 → 算天数 → 建卡"]
        D3["MiniDaily：生成一次性邀请链接"]
        D4["本地卡池：锁定卡密"]
        D5["固定文案：变量渲染"]
        D6{"auto_confirm？"}
        D7["确认发货"]
        D8["发送卡密 / 文案"]
        D9["delivered + 写 outgoing 流水"]
        D10["confirm_failed：不发送"]
        D11["send_failed：回滚卡密"]
        D12["upstream_failed / invite_failed"]
        R6 -- 通过 --> D1
        D1 --> D2
        D1 --> D3
        D1 --> D4
        D1 --> D5
        D2 --> D6
        D3 --> D6
        D4 --> D6
        D5 --> D6
        D6 -- 是 --> D7
        D7 -- "SUCCESS / ORDER_ALREADY_DELIVERY" --> D8
        D7 -- "ORDER_STATUS_ERROR / 其他失败" --> D10
        D6 -- 否 --> D8
        D8 -- 成功 --> D9
        D8 -- 失败 --> D11
        D2 -- 建卡失败 --> D12
        D3 -- 生成失败 --> D12
    end

    subgraph POLL["⑤ 订单兜底"]
        P1["订单轮询（默认 30s）"]
        P2["收到推送 → 提前唤醒轮询"]
        P3["待发货订单列表"]
        P4["inflight / delivered 去重"]
        P2 --> P1 --> P3 --> P4 --> D1
    end

    B5 --> I1
    B5 --> I2
    B5 --> P1
    R3 --> S1["order_actions 去重"]
    S1 --> S2["自动领取小红花"]
    S1 --> S3["求花 / 提醒收货 / 好评"]
    D9 --> DB["SQLite：accounts / rules / cards / orders / messages / order_actions"]
    D10 --> DB
    D11 --> DB
    D12 --> DB
    R4 --> DB
```

## 2. 发货关键时序

```mermaid
sequenceDiagram
    autonumber
    actor Buyer
    participant XY as 闲鱼
    participant W as AccountWorker
    participant DB as SQLite
    participant UP as 2018K / MiniDaily

    Buyer->>XY: 拍下并付款
    XY-->>W: 系统付款卡片 / 未读推送
    W->>DB: messageId 去重 + delivered 去重
    W->>DB: 读规则 / 金额 / 卡池
    W->>UP: 建 2018K 卡密或生成 MiniDaily 邀请
    W->>XY: 确认发货（confirm_shipping）
    alt SUCCESS / ORDER_ALREADY_DELIVERY
        W->>XY: 发送卡密 / 文案
        W->>DB: 记 delivered + outgoing 流水
    else ORDER_STATUS_ERROR / 其他失败
        W->>UP: 尝试撤销刚创建的卡密
        W->>DB: 记 confirm_failed（不发送）
    end
    opt 确认后发送失败
        W->>UP: 回滚卡密（可删时）
        W->>DB: 记 send_failed
    end
```

## 3. 各环节说明

| 环节 | 入口 / 实现 | 关键动作 |
| --- | --- | --- |
| 启动 | `start.sh`、`main.py`、`app/web.py` | 单实例检查、FastAPI、SQLite、加载面板配置 |
| 账号接入 | `app/runner.py` 的 `AccountWorker._once()` | 扫码 Cookie、WebSocket、心跳、`/reg`、ackDiff |
| 规则配置 | `app/engine.py`、后台规则页 | 商品匹配、实例绑定、固定天数 / SKU / 按金额、四种发货模式 |
| 消息接入 | `_process_incoming()` | 推送 + 轮询双通道、`messageId` 去重、旧会话保护、买家名回填、流水 |
| 自动回复 | `app/autoreply.py`、`_maybe_auto_reply()` | 商品 / 关键词匹配；系统交易消息强制跳过；冷却与会话去重 |
| 发货决策 | `DeliveryEngine.decide()` / `decide_order()` | 付款触发、订单核实、`delivered` 去重、规则匹配、卡密出库 |
| 发货执行 | `_deliver_once_inner()` | 建卡 / 邀请 / 卡池 / 文案；**先确认发货，成功后才发送** |
| 异常回滚 | 同上 | `confirm_failed` 不发卡；`send_failed` 回滚；`upstream_failed` / `invite_failed` 明确终态 |
| 订单兜底 | `_order_poll_loop()` | 默认 30 秒拉待发货；推送唤醒；多单并发；已发货去重 |
| 社交自动化 | `app/social.py`、`_handle_social_event()` | 送花自动领取；发货提醒/求花；确认收货求花/好评；按订单去重 |
| 面板与数据 | `app/web.py`、`app/db.py` | 规则、订单、消息、账号、2018K 配置、健康检查 |

## 4. 订单状态

| 状态 | 含义 |
| --- | --- |
| `delivered` | 卡密/文案已成功发送 |
| `confirm_failed` | 确认发货失败，**未发送卡密**；2018K 卡密尝试撤销 |
| `send_failed` | 确认后发送失败；卡密回滚或保留卡密 ID 供人工处理 |
| `upstream_failed` | 2018K 建卡或金额/实例校验失败 |
| `invite_failed` | MiniDaily 邀请生成失败 |
| `blocked` | 真人文本疑似触发发货但订单核实未通过 |

## 5. 需要知道的边界

- 系统交易消息（收货、评价、付款卡片等）**不会触发自动回复**。
- 自动发货必须可核实：系统卡片直接信任；真人文本要查订单状态与买家归属。
- 订单轮询和 WebSocket 共用 `delivered` / `messageId` 去重，不会重复发货或重复处理。
- **旧会话保护**：会话最后一条消息距当前超过 `XY_MESSAGE_STALE_SECONDS`（默认 1 天）时，整段历史都不处理；即使后来会话重新活跃，也只处理新消息，历史消息按同一时间窗口过滤。
- 社交自动化只由交易消息事件触发，不做订单轮询；提醒收货按自然日去重。
- 2018K 健康检查只在启动、保存配置或手动点击时执行，不做定时轮询。
