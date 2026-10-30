"""运行期配置，全部来自环境变量。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """把 .env 里的键值对塞进 os.environ（已存在的变量优先）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


#: 2018K 卡密验证平台的默认地址（公开 API / 后台 API 是两套）
DEFAULT_K2018_API_BASE = "https://api.2018k.cn/v3"
DEFAULT_K2018_ADMIN_BASE = "https://admin.2018k.cn"

_OPENID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(slots=True, frozen=True)
class K2018Instance:
    """一个 2018K 实例：后台显示的名称 + 32 位 softwareId。"""

    name: str
    software_id: str

    @property
    def tail(self) -> str:
        return self.software_id[-8:]


def parse_k2018_instances(raw: str) -> list[K2018Instance]:
    """解析 XY_2018K_INSTANCES。

    支持三种写法，条目之间用逗号 / 分号 / 竖线 / 换行分隔：
        Miracles=B4ADC92FDF7240398D8A80A4176C80BD
        Miracles:B4ADC92FDF7240398D8A80A4176C80BD
        B4ADC92FDF7240398D8A80A4176C80BD
    没有名字时用「实例 + 后 8 位」自动命名；ID 不是 32 位十六进制的条目直接跳过，
    不让一条写错的配置把整个进程带崩。
    """
    items: list[K2018Instance] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;|\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        name = ""
        sid = chunk
        for sep in ("=", ":"):
            if sep in chunk:
                name, sid = chunk.split(sep, 1)
                name, sid = name.strip(), sid.strip()
                break
        if not _OPENID_RE.match(sid):
            continue
        if sid.upper() in seen:
            continue
        seen.add(sid.upper())
        items.append(K2018Instance(name=name or ("实例" + sid[-8:]), software_id=sid.upper()))
    return items


def _float(name: str, default: float) -> float:
    """读一个浮点配置；没配或者填错就用默认值。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    """读一个整数配置；没配、填空或者填错都退回默认值。

    以前这里直接 int(os.getenv(...))，一个 XY_PORT=abc 就会让整个进程
    在 import 期崩掉，而报错里只有 invalid literal for int()，
    看不出是哪个变量写错了。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    """进程级配置。

    所有字段都可以通过 XY_ 前缀的环境变量覆盖，方便 Docker / systemd 部署。
    """

    db_path: Path = field(default_factory=lambda: Path(os.getenv("XY_DB_PATH", str(BASE_DIR / "data" / "app.db"))))
    host: str = field(default_factory=lambda: os.getenv("XY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("XY_PORT", 8000))
    admin_token: str = field(default_factory=lambda: os.getenv("XY_ADMIN_TOKEN", ""))
    #: 没配 XY_ADMIN_TOKEN 时，是否允许非本机来源访问管理接口。
    #: 默认 False：管理接口能改 Cookie、读卡密、删规则，绝不能默认敞开。
    admin_allow_remote: bool = field(default_factory=lambda: _bool("XY_ADMIN_ALLOW_REMOTE", False))
    log_level: str = field(default_factory=lambda: os.getenv("XY_LOG_LEVEL", "INFO"))
    #: 日志文件路径。留空则只输出到控制台（交给 systemd / docker 收走）
    log_file: str = field(default_factory=lambda: os.getenv("XY_LOG_FILE", "./logs/server.log"))
    #: 单个日志文件上限（字节），默认 10 MB
    log_max_bytes: int = field(default_factory=lambda: _int("XY_LOG_MAX_BYTES", 10 * 1024 * 1024))
    #: 保留几份历史日志，默认 5
    log_backup_count: int = field(default_factory=lambda: _int("XY_LOG_BACKUP_COUNT", 5))

    # 发货行为
    delivery_cooldown: int = field(default_factory=lambda: _int("XY_DELIVERY_COOLDOWN", 600))
    send_retry: int = field(default_factory=lambda: _int("XY_SEND_RETRY", 2))
    dry_run: bool = field(default_factory=lambda: _bool("XY_DRY_RUN", False))

    # 上游联动 2018K 卡密验证平台（按订单金额换成天数卡密）
    #: OpenId 同时是 AES 密钥；后台个人中心可见，32 位十六进制。
    k2018_openid: str = field(default_factory=lambda: os.getenv("XY_2018K_OPENID", "").strip())
    k2018_api_base: str = field(
        default_factory=lambda: os.getenv("XY_2018K_API_BASE", DEFAULT_K2018_API_BASE)
    )
    #: 实例列表：Miracles=xxx,MiraclesX64=yyy；没有名字时自动用「实例+后8位」
    k2018_instances: list[K2018Instance] = field(
        default_factory=lambda: parse_k2018_instances(os.getenv("XY_2018K_INSTANCES", ""))
    )
    #: 可选：后台账号，用于发货失败时删除已建卡密。不配就只能人工删。
    k2018_admin_base: str = field(
        default_factory=lambda: os.getenv("XY_2018K_ADMIN_BASE", DEFAULT_K2018_ADMIN_BASE)
    )
    k2018_admin_username: str = field(
        default_factory=lambda: os.getenv("XY_2018K_ADMIN_USERNAME", "").strip()
    )
    k2018_admin_password: str = field(
        default_factory=lambda: os.getenv("XY_2018K_ADMIN_PASSWORD", "").strip()
    )

    # MiniDailyReward 扫码入池（同一台服务器上的控制台）
    #: 只监听本机即可，例如 http://127.0.0.1:8790
    minidaily_api_base: str = field(
        default_factory=lambda: os.getenv("XY_MINIDAILY_API_BASE", "http://127.0.0.1:8790").rstrip("/")
    )
    #: MiniDailyReward 控制台令牌；只在本机 .env 保存，不回显
    minidaily_token: str = field(
        default_factory=lambda: os.getenv("XY_MINIDAILY_TOKEN", "").strip()
    )

    # 订单社交自动化：求小红花 / 提醒收货 / 自动评价
    social_auto_flower: bool = field(default_factory=lambda: _bool("XY_SOCIAL_AUTO_FLOWER", False))
    social_auto_remind: bool = field(default_factory=lambda: _bool("XY_SOCIAL_AUTO_REMIND", False))
    social_auto_rate: bool = field(default_factory=lambda: _bool("XY_SOCIAL_AUTO_RATE", False))
    #: 买家送小红花时自动领取；这件事通常是正向互动，默认开启。
    social_auto_receive_flower: bool = field(
        default_factory=lambda: _bool("XY_SOCIAL_AUTO_RECEIVE_FLOWER", True)
    )
    social_rate_content: str = field(
        default_factory=lambda: os.getenv("XY_SOCIAL_RATE_CONTENT", "很好，合作愉快！").strip()
    )
    #: 开启自动化时写入；只处理这个时间之后创建的订单，避免一开就把历史订单全部打扰一遍。
    social_since: str = field(default_factory=lambda: os.getenv("XY_SOCIAL_SINCE", "").strip())

    # WebSocket 心跳间隔（秒）。对齐网页端：首跳立即发，之后每 10 秒一次。
    heartbeat_interval: float = field(default_factory=lambda: _float("XY_HEARTBEAT_INTERVAL", 10.0))
    # ackDiff 同步位点重发间隔（秒）。0 = 只发一次（不推荐）。
    # 增量推送是服务端对 ackDiff 的响应，只发一次就基本收不到新消息。
    sync_poll_interval: float = field(default_factory=lambda: _float("XY_SYNC_POLL_INTERVAL", 30.0))
    # /reg 与 ackDiff 之间的间隔（秒）。紧凑连发会让注册在服务端不生效，
    # 表现为「能连上、能收框架帧、但永远收不到私信正文」。
    register_gap: float = field(default_factory=lambda: _float("XY_REGISTER_GAP", 1.0))

    # 会话列表轮询间隔（秒）。0 = 关闭。
    # 私信正文不一定会走 WebSocket 推送（线上实测经常只有「未读标记」），
    # 所以另外按这个节奏拉一次「最近会话」，把最后一条消息当新消息处理。
    message_poll_interval: float = field(default_factory=lambda: _float("XY_MESSAGE_POLL_INTERVAL", 20.0))
    # 每次拉多少个会话（1~50）
    message_poll_limit: int = field(default_factory=lambda: _int("XY_MESSAGE_POLL_LIMIT", 20))
    # 发现会话有新消息时，往回拉多少条历史（1~50）。
    # 只看「最后一条」会漏掉连发或多条之间的系统消息，所以按会话补一段。
    message_history_limit: int = field(default_factory=lambda: _int("XY_MESSAGE_HISTORY_LIMIT", 20))
    # 每轮最多给几个会话补拉历史（每次是一趟 WS 往返）。0 = 永远只看最后一条。
    message_history_conversations: int = field(
        default_factory=lambda: _int("XY_MESSAGE_HISTORY_CONVERSATIONS", 5)
    )
    # 旧会话保护：会话最后一条消息距现在超过这么多秒，整段历史都不处理。
    # 0 = 关闭保护（不推荐）；默认 1 天。
    message_stale_seconds: int = field(
        default_factory=lambda: _int("XY_MESSAGE_STALE_SECONDS", 86400)
    )

    # 自动回复总开关。关掉后商品 / 关键词自动回复全部不生效（只发货）。
    auto_reply_enabled: bool = field(default_factory=lambda: _bool("XY_AUTO_REPLY", True))
    # 自动回复前先等几秒，避免「秒回」显得像机器人（也顺便错开连发）。0 = 立刻回。
    auto_reply_delay: float = field(default_factory=lambda: _float("XY_AUTO_REPLY_DELAY", 1.0))

    # 订单轮询间隔（秒）。0 = 关闭。IM 推送丢了时靠它兜底发货。
    # 这是「一直没有推送」时的兜底节奏；有推送时下面那项会把轮询提前叫醒，
    # 所以实际发货延迟通常是几秒，而不是这个值。
    order_poll_interval: int = field(default_factory=lambda: _int("XY_ORDER_POLL_INTERVAL", 30))
    # 收到任何 IM 推送就提前补一次订单轮询（秒级节流）。0 = 关闭这个加速。
    # 推送本身常解析不出内容，但「有推送」是「可能刚有人拍下」最便宜的信号。
    order_hint_poll_gap: float = field(default_factory=lambda: _float("XY_ORDER_HINT_POLL_GAP", 5.0))
    # 一轮轮询里同时处理多少张待发货订单。1 = 串行。
    order_poll_concurrency: int = field(default_factory=lambda: _int("XY_ORDER_POLL_CONCURRENCY", 3))
    # 订单详情缓存时长（秒）：补 itemId 与取金额共用同一次查询。0 = 不缓存。
    order_cache_ttl: float = field(default_factory=lambda: _float("XY_ORDER_CACHE_TTL", 30.0))
    # 1 = 把收到的每条原始消息负载写进日志（排查「为什么没发货」时打开）
    debug_payload: bool = field(default_factory=lambda: _bool("XY_DEBUG_PAYLOAD", False))

    # 订单金额单位：auto 会按数值大小猜（整数且很大时视为「分」），也可强制 yuan / fen
    order_amount_unit: str = field(default_factory=lambda: os.getenv("XY_ORDER_AMOUNT_UNIT", "auto"))
    # 单笔订单允许的最大金额（元），超过则不自动发货，防止标错价被刷
    max_order_cny: float = field(default_factory=lambda: float(os.getenv("XY_MAX_ORDER_CNY", "0") or 0))

    # ---- 登录态保活（参考 xianyu-pilot）----
    # accessToken 进程内缓存时长（秒）。0 = 不缓存。缓存能显著减少重连时的
    # token 接口调用，也就少一次触发风控的机会。
    im_token_ttl: float = field(default_factory=lambda: _float("XY_IM_TOKEN_TTL", 4 * 3600))
    # _m_h5_tk 主动刷新间隔（秒），每次在 [min, max] 内随机，避免固定节奏像机器人。
    # 0 = 关闭主动刷新（仍然会在遇到「令牌过期」时被动刷新）。
    m_h5_tk_refresh_min: float = field(default_factory=lambda: _float("XY_MH5TK_REFRESH_MIN", 1.5 * 3600))
    m_h5_tk_refresh_max: float = field(default_factory=lambda: _float("XY_MH5TK_REFRESH_MAX", 2.5 * 3600))
    # Cookie 保活间隔（秒），0 = 关闭。调用 passport hasLogin.do 让登录态续期。
    session_keepalive_interval: float = field(
        default_factory=lambda: _float("XY_SESSION_KEEPALIVE_INTERVAL", 30 * 60)
    )

    # 连接参数（一般不需要改）
    ws_url: str = "wss://wss-goofish.dingtalk.com/"
    app_key: str = "34839810"
    im_app_key: str = "444e9908a51d1cb236a27862abc769c9"
    mtop_base: str = "https://h5api.m.goofish.com/h5"



def k2018_ready() -> bool:
    """2018K 是否配置齐全：OpenId + 至少一个实例。

    后台账号不是发货必需项，只影响「发货失败时能不能自动删卡」。
    """
    return bool(_OPENID_RE.match(settings.k2018_openid)) and bool(settings.k2018_instances)


def k2018_admin_ready() -> bool:
    """是否配置了 2018K 后台账号，可用于删除未送达的卡密。"""
    return bool(settings.k2018_admin_username and settings.k2018_admin_password)


def find_k2018_instance(software_id: str) -> K2018Instance | None:
    """按 softwareId 找配置里的实例（大小写不敏感）。"""
    target = (software_id or "").strip().upper()
    for item in settings.k2018_instances:
        if item.software_id.upper() == target:
            return item
    return None


settings = Settings()


def apply_saved_k2018_config(values: dict[str, str]) -> None:
    """把后台保存的 2018K 配置覆盖到当前 settings。

    数据库里没有的 key 保持 .env 的值；这样老部署仍然能用 .env，
    面板保存过之后以面板为准（包括把后台账号清空这种显式操作）。
    """
    if "k2018_openid" in values:
        settings.k2018_openid = str(values["k2018_openid"] or "").strip()
    if "k2018_instances" in values:
        settings.k2018_instances = parse_k2018_instances(str(values["k2018_instances"] or ""))
    if "k2018_admin_username" in values:
        settings.k2018_admin_username = str(values["k2018_admin_username"] or "").strip()
    if "k2018_admin_password" in values:
        settings.k2018_admin_password = str(values["k2018_admin_password"] or "")


def social_any_enabled() -> bool:
    """是否有任一订单社交自动化开关打开。"""
    return bool(
        settings.social_auto_flower
        or settings.social_auto_remind
        or settings.social_auto_rate
        or settings.social_auto_receive_flower
    )


def apply_saved_social_config(values: dict[str, str]) -> None:
    """把面板保存的订单社交开关覆盖到当前 settings。"""
    truthy = {"1", "true", "yes", "on"}
    for key, attr in (
        ("social_auto_flower", "social_auto_flower"),
        ("social_auto_remind", "social_auto_remind"),
        ("social_auto_rate", "social_auto_rate"),
        ("social_auto_receive_flower", "social_auto_receive_flower"),
    ):
        if key in values:
            setattr(settings, attr, str(values[key]).strip().lower() in truthy)
    if "social_rate_content" in values:
        settings.social_rate_content = str(values["social_rate_content"] or "").strip()
    if "social_since" in values:
        settings.social_since = str(values["social_since"] or "").strip()
