from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class Site(str, Enum):
    zhilian = "zhilian"
    linkedin = "linkedin"
    boss = "boss"
    liepin = "liepin"
    job51 = "job51"


class SendSlot(str, Enum):
    """订阅可选的固定发送/抓取时段（实现为 cron 触发）。"""

    daily_09 = "daily_09"          # 每天 10:00（键名保留以兼容历史数据）
    daily_21 = "daily_21"          # 每天 21:00
    weekday_09 = "weekday_09"      # 工作日 10:00
    weekly_mon_09 = "weekly_mon_09"  # 每周一 10:00


class ScheduleUnit(str, Enum):
    minutes = "minutes"
    hours = "hours"
    days = "days"


class DateRange(str, Enum):
    """职位发布时间筛选。"""

    any = "any"
    day = "day"
    week = "week"
    month = "month"


class User(SQLModel, table=True):
    """门户用户：邮箱+密码登录，持有用户级自画像。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, description="登录邮箱")
    password_hash: str = Field(default="", description="pbkdf2 哈希(算法$迭代$盐$摘要)")
    name: str = Field(default="", description="昵称")
    # 用户级自画像（结构化 JSON 字符串）；订阅画像为空时回退到此画像做分析
    profile_json: str = Field(default="", description="自画像结构化数据(JSON)")
    # 最近一次上传的简历原文与元信息；供简历诊断/按岗位优化/经历改写复用，无需重传
    resume_text: str = Field(default="", description="简历原始文本")
    resume_filename: str = Field(default="", description="简历原始文件名")
    resume_updated_at: Optional[datetime] = Field(default=None, description="简历更新时间")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = Field(default=None)


class SearchConfig(SQLModel, table=True):
    """一条可配置的搜索任务（职位完全可配置）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="便于识别的名称，如 'C++ 上海'")
    site: Site = Field(index=True)
    keyword: str = Field(description="职位关键词，如 C++")
    city: str = Field(default="", description="城市，空表示不限")
    salary: str = Field(default="", description="薪资筛选（站点相关，可留空）")
    date_range: DateRange = Field(
        default=DateRange.any, description="发布时间：不限/近一天/近一周/近一月"
    )

    # 定时：每 interval 个 unit 跑一次
    interval: int = Field(default=6)
    unit: ScheduleUnit = Field(default=ScheduleUnit.hours)
    enabled: bool = Field(default=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_run_at: Optional[datetime] = Field(default=None)


class JobPosting(SQLModel, table=True):
    """抓取到的职位。fingerprint 用于去重。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    fingerprint: str = Field(index=True, unique=True)

    site: Site = Field(index=True)
    config_id: Optional[int] = Field(default=None, foreign_key="searchconfig.id")

    title: str = ""
    company: str = ""
    salary: str = ""
    location: str = ""
    experience: str = ""
    education: str = ""
    tags: str = Field(default="", description="技能标签，逗号分隔")
    description: str = ""
    url: str = ""

    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    analyzed: bool = Field(default=False, index=True)


class EmailSetting(SQLModel, table=True):
    """邮件推送配置（单行，id 固定为 1）。SMTP 服务器/凭据在 .env。"""

    id: Optional[int] = Field(default=1, primary_key=True)
    recipients: str = Field(default="", description="收件人，逗号或换行分隔")
    enabled: bool = Field(default=False, description="是否在抓取后自动推送新职位")
    include_analysis: bool = Field(default=True, description="是否附带 AI 分析建议")
    min_score: int = Field(default=0, description="仅推送匹配度≥该值的职位(0=不限)")

    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Analysis(SQLModel, table=True):
    """LLM 对某职位的分析结果。按 (job_id, profile_hash) 缓存复用。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobposting.id", index=True)
    # 画像指纹：同一职位在不同求职者画像下分析结果不同；空串=无画像的通用分析
    profile_hash: str = Field(default="", index=True)

    match_score: int = Field(default=0, description="0-100 匹配度")
    summary: str = ""
    advice: str = ""
    skills_to_learn: str = Field(default="", description="为胜任该职位需补强的技能")
    resume_tips: str = Field(default="", description="针对该职位的简历改进建议")
    raw: str = Field(default="", description="模型原始返回，便于排查")

    created_at: datetime = Field(default_factory=datetime.utcnow)


class Subscription(SQLModel, table=True):
    """一条扁平订阅：邮箱 + 自画像 + 过滤条件 + 发送时段（自助管理）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", index=True, description="所属用户(可空，兼容历史按邮箱订阅)"
    )
    email: str = Field(index=True, description="收件邮箱")
    name: str = Field(default="", description="订阅名，如 'C++ 上海'")

    # 过滤条件
    sites: str = Field(default="", description="目标站点，逗号分隔（空=全部可用站点）")
    keywords: str = Field(default="", description="关键词，逗号/空格分隔（OR 命中）")
    job_type: str = Field(default="", description="工作类型关键词，如 全职/实习/远程")
    location: str = Field(default="", description="期望城市/地点（包含匹配）")
    salary_min: int = Field(default=0, description="期望月薪下限(单位k，0=不限)")
    salary_max: int = Field(default=0, description="期望月薪上限(单位k，0=不限)")

    # 自画像（结构化 JSON 字符串）与个性化
    profile_json: str = Field(default="", description="自画像结构化数据(JSON)")
    min_score: int = Field(default=0, description="仅推送匹配度≥该值的职位(0=不限)")
    include_analysis: bool = Field(default=True, description="邮件是否附带 AI 分析")
    max_jobs: int = Field(default=10, description="每次最多推送条数(按匹配度取最匹配的前 N 条)")
    notify_empty: bool = Field(default=False, description="无新匹配时也发送提醒邮件")

    # 发送时段（SendSlot 值，逗号分隔）
    send_slots: str = Field(default=SendSlot.daily_09.value)

    enabled: bool = Field(default=True)
    manage_token: str = Field(index=True, description="自助管理/退订令牌")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_sent_at: Optional[datetime] = Field(default=None)


class Referral(SQLModel, table=True):
    """内推信息：由用户发布，供其他用户按订阅匹配后自行联系发布者。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, description="发布者")

    title: str = Field(description="职位名")
    company: str = Field(default="", description="公司")
    location: str = Field(default="", description="工作地点")
    salary: str = Field(default="", description="薪资（自由文本）")
    keywords: str = Field(default="", description="关键词，逗号分隔，便于匹配订阅")
    description: str = Field(default="", description="职位描述/要求")
    contact: str = Field(default="", description="联系方式，留空则用发布者邮箱")
    url: str = Field(default="", description="职位/内推链接")

    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class JobFavorite(SQLModel, table=True):
    """用户收藏的职位：当下不想投/暂不满足条件，但日后可能想回看。"""

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_jobfavorite_user_job"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    job_id: int = Field(foreign_key="jobposting.id", index=True)
    note: str = Field(default="", description="收藏备注，便于回想当时为何收藏")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Delivery(SQLModel, table=True):
    """某订阅已推送过的职位记录，避免重复推送。"""

    __table_args__ = (
        UniqueConstraint("subscription_id", "job_id", name="uq_delivery_sub_job"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="subscription.id", index=True)
    job_id: int = Field(foreign_key="jobposting.id", index=True)
    sent_at: datetime = Field(default_factory=datetime.utcnow)


class CrawlStatus(str, Enum):
    """预抓取阶段每个抓取组合的状态。"""

    pending = "pending"   # 尚未执行
    ok = "ok"             # 抓取成功
    failed = "failed"     # 抓取异常（网络/解析等）
    blocked = "blocked"   # 被反爬/登录墙拦截，需人工干预重跑


class CrawlTask(SQLModel, table=True):
    """某发送时段一次预抓取批次里的单个抓取组合 (site, keyword, city)。

    批次由 (slot, run_date) 标识；同组合重跑时按唯一约束 upsert，便于
    “只重跑出错的组合”。
    """

    __table_args__ = (
        UniqueConstraint(
            "slot", "run_date", "site", "keyword", "city", name="uq_crawltask_combo"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    slot: str = Field(index=True, description="发送时段（SendSlot 值）")
    run_date: str = Field(index=True, description="批次日期，本地 YYYY-MM-DD")

    site: str = Field(default="", description="站点")
    keyword: str = Field(default="", description="关键词")
    city: str = Field(default="", description="城市")

    status: CrawlStatus = Field(default=CrawlStatus.pending, index=True)
    error: str = Field(default="", description="失败原因，便于人工排查")
    scraped: int = Field(default=0, description="本组合抓到的职位数")
    new: int = Field(default=0, description="本组合新增入库数")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
