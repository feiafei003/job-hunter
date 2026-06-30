"""按职能划分的软件岗位关键词预设（六大类 / 约 30 个角色）。

供三处复用：
- 订阅表单「推荐关键词」：前端拉 all_presets() 按大类分组渲染，点角色即填关键词；
- AI 助手 recommend_keywords 工具的静态兜底来源（match_preset）；
- 系统提示里给模型的岗位词库参考。

每个角色字段：
    role          中文角色名（也作为 pill 文案 / category）
    en            常见英文称呼（list）
    keywords      搜索关键词（list，直接可填进订阅）
    skills        核心技能（list）
    levels        级别梯度（应届 → 管理，list）
    job_type      建议的职类（订阅 job_type 字段）
    suggest_sites 推荐站点（Site 枚举字符串：zhilian/boss/liepin/job51/linkedin）

站点取值与 scrapers 注册一致：zhilian / linkedin / boss / liepin / job51。
"""

from __future__ import annotations

from typing import Optional

# 国内综合站覆盖面广；算法/AI/架构等高端或外企方向额外加领英
_CN = ["boss", "zhilian", "liepin", "job51"]
_CN_INTL = ["boss", "zhilian", "liepin", "linkedin"]


# 六大类 → 角色。每个大类含 group(类名)/note(说明)/roles(角色列表)。
_GROUPS: list[dict] = [
    {
        "group": "研发/开发",
        "note": "招聘量最大",
        "roles": [
            {
                "role": "前端开发",
                "en": ["Frontend Engineer", "Web Developer", "Front-end Developer"],
                "keywords": ["前端", "Web前端", "React", "Vue", "TypeScript", "前端工程化", "Webpack"],
                "skills": ["JavaScript/TypeScript", "React/Vue", "HTML/CSS", "工程化构建", "性能优化"],
                "levels": ["前端实习", "初级前端", "高级前端", "前端专家/架构", "前端负责人"],
                "job_type": "前端开发工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "后端开发",
                "en": ["Backend Engineer", "Server-side Developer"],
                "keywords": ["后端", "服务端", "Java", "Go", "Python", "微服务", "高并发", "分布式"],
                "skills": ["Java/Go/Python", "微服务/分布式", "数据库", "缓存/消息队列", "高并发设计"],
                "levels": ["后端实习", "初级后端", "高级后端", "后端专家", "技术负责人"],
                "job_type": "后端开发工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "全栈开发",
                "en": ["Full Stack Engineer", "Full-stack Developer"],
                "keywords": ["全栈", "Full Stack", "Node.js", "React", "后端", "数据库"],
                "skills": ["前后端通吃", "Node.js", "React/Vue", "REST/GraphQL", "数据库"],
                "levels": ["初级全栈", "中级全栈", "高级全栈", "全栈架构"],
                "job_type": "全栈工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "移动端开发",
                "en": ["Mobile Engineer", "Android/iOS Developer"],
                "keywords": ["Android", "iOS", "移动开发", "Kotlin", "Swift", "Flutter", "React Native", "鸿蒙"],
                "skills": ["Android/Kotlin 或 iOS/Swift", "跨平台(Flutter/RN)", "性能/包体优化", "组件化"],
                "levels": ["移动实习", "初级移动", "高级移动", "移动端专家", "移动负责人"],
                "job_type": "移动开发工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "客户端/桌面",
                "en": ["Client Engineer", "Desktop Developer"],
                "keywords": ["客户端", "桌面开发", "C++", "C#", "Qt", "Windows 客户端", "Electron", "跨平台"],
                "skills": ["C++/C#", "Qt/Electron", "跨平台", "系统调用", "性能优化"],
                "levels": ["初级客户端", "高级客户端", "客户端专家"],
                "job_type": "客户端开发工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "嵌入式/底层",
                "en": ["Embedded Engineer", "Firmware Engineer"],
                "keywords": ["嵌入式", "单片机", "RTOS", "驱动开发", "固件", "C", "Linux", "底层"],
                "skills": ["C/C++", "RTOS/Linux 内核", "驱动/固件", "硬件协议(I2C/SPI/UART)"],
                "levels": ["嵌入式实习", "初级嵌入式", "高级嵌入式", "嵌入式架构"],
                "job_type": "嵌入式软件工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "游戏开发",
                "en": ["Game Developer", "Unity/Unreal Engineer"],
                "keywords": ["游戏开发", "Unity", "Unreal", "C++", "游戏客户端", "游戏服务器", "图形渲染"],
                "skills": ["Unity/Unreal", "C++/C#", "图形/渲染", "物理/网络同步"],
                "levels": ["游戏实习", "初级游戏", "高级游戏", "主程"],
                "job_type": "游戏开发工程师",
                "suggest_sites": _CN,
            },
        ],
    },
    {
        "group": "数据/算法/AI",
        "note": "门槛与薪资偏高",
        "roles": [
            {
                "role": "算法工程师",
                "en": ["Algorithm Engineer", "Search/Reco/Ads Algorithm"],
                "keywords": ["算法", "推荐算法", "搜索", "广告", "C++", "数据结构", "策略"],
                "skills": ["数据结构与算法", "推荐/搜索/广告", "C++/Python", "建模/调优"],
                "levels": ["算法实习", "初级算法", "高级算法", "算法专家", "算法负责人"],
                "job_type": "算法工程师",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "机器学习",
                "en": ["Machine Learning Engineer", "MLE"],
                "keywords": ["机器学习", "深度学习", "PyTorch", "TensorFlow", "模型训练", "特征工程"],
                "skills": ["机器学习/深度学习", "PyTorch/TF", "特征工程", "模型部署"],
                "levels": ["ML 实习", "初级 ML", "高级 ML", "ML 专家"],
                "job_type": "机器学习工程师",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "大模型/LLM",
                "en": ["LLM Engineer", "GenAI Engineer", "NLP Engineer"],
                "keywords": ["大模型", "LLM", "AIGC", "RAG", "微调", "Prompt", "Agent", "NLP", "向量数据库"],
                "skills": ["LLM/Transformer", "RAG/向量检索", "微调(SFT/LoRA)", "Prompt/Agent", "推理优化"],
                "levels": ["LLM 实习", "初级 LLM", "高级 LLM", "LLM 专家/负责人"],
                "job_type": "大模型算法工程师",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "数据工程",
                "en": ["Data Engineer", "Big Data Engineer"],
                "keywords": ["大数据", "数据开发", "数仓", "ETL", "Spark", "Flink", "Hive"],
                "skills": ["Spark/Flink", "数仓建模", "ETL/调度", "SQL", "实时计算"],
                "levels": ["数据实习", "初级数据开发", "高级数据开发", "数据架构"],
                "job_type": "数据开发工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "数据分析",
                "en": ["Data Analyst", "BI Analyst"],
                "keywords": ["数据分析", "SQL", "BI", "Python", "统计", "数据可视化", "指标体系"],
                "skills": ["SQL", "统计分析", "BI 工具(Tableau/PowerBI)", "Python/pandas", "业务洞察"],
                "levels": ["分析实习", "初级分析师", "高级分析师", "分析专家"],
                "job_type": "数据分析师",
                "suggest_sites": _CN,
            },
            {
                "role": "数据科学",
                "en": ["Data Scientist"],
                "keywords": ["数据科学", "建模", "机器学习", "统计", "Python", "实验设计", "A/B 测试"],
                "skills": ["统计建模", "机器学习", "实验/AB 测试", "Python/R", "业务建模"],
                "levels": ["DS 实习", "初级数据科学家", "高级数据科学家", "首席数据科学家"],
                "job_type": "数据科学家",
                "suggest_sites": _CN_INTL,
            },
        ],
    },
    {
        "group": "测试/质量",
        "note": "",
        "roles": [
            {
                "role": "测试工程师",
                "en": ["QA Engineer", "Test Engineer"],
                "keywords": ["测试", "功能测试", "QA", "测试用例", "缺陷管理"],
                "skills": ["测试用例设计", "缺陷管理", "接口测试", "测试流程"],
                "levels": ["测试实习", "初级测试", "高级测试", "测试负责人"],
                "job_type": "测试工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "自动化测试(SDET)",
                "en": ["SDET", "Automation Test Engineer"],
                "keywords": ["自动化测试", "SDET", "Selenium", "Appium", "Pytest", "测试框架"],
                "skills": ["自动化框架", "Selenium/Appium", "Python/Java", "CI 集成"],
                "levels": ["初级自动化", "高级自动化", "测试架构"],
                "job_type": "自动化测试工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "性能/测试开发",
                "en": ["Performance Test Engineer", "Test Development Engineer"],
                "keywords": ["性能测试", "测试开发", "压测", "JMeter", "全链路压测", "稳定性"],
                "skills": ["性能压测(JMeter/Locust)", "测试平台开发", "调优", "监控"],
                "levels": ["初级测开", "高级测开", "测开专家"],
                "job_type": "测试开发工程师",
                "suggest_sites": _CN,
            },
        ],
    },
    {
        "group": "运维/基础设施",
        "note": "",
        "roles": [
            {
                "role": "运维/SRE",
                "en": ["SRE", "Operations Engineer"],
                "keywords": ["运维", "SRE", "稳定性", "监控", "Linux", "故障处理"],
                "skills": ["Linux", "监控告警", "稳定性保障", "脚本(Shell/Python)", "故障应急"],
                "levels": ["运维实习", "初级运维", "高级运维/SRE", "稳定性负责人"],
                "job_type": "运维工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "DevOps",
                "en": ["DevOps Engineer", "Platform Engineer"],
                "keywords": ["DevOps", "CI/CD", "Docker", "Kubernetes", "Jenkins", "GitOps", "自动化部署"],
                "skills": ["CI/CD", "Docker/K8s", "IaC(Terraform)", "自动化运维"],
                "levels": ["初级 DevOps", "高级 DevOps", "平台架构"],
                "job_type": "DevOps工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "云架构",
                "en": ["Cloud Architect", "Cloud Engineer"],
                "keywords": ["云计算", "云架构", "AWS", "阿里云", "Kubernetes", "云原生", "容器"],
                "skills": ["公有云(AWS/阿里云)", "云原生/K8s", "网络/存储", "成本与高可用"],
                "levels": ["云工程师", "高级云工程师", "云架构师"],
                "job_type": "云计算工程师",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "安全工程师",
                "en": ["Security Engineer", "Penetration Tester"],
                "keywords": ["安全", "网络安全", "渗透测试", "漏洞", "安全开发", "攻防", "等保"],
                "skills": ["渗透/漏洞挖掘", "安全开发", "攻防对抗", "合规(等保)"],
                "levels": ["安全实习", "初级安全", "高级安全", "安全专家"],
                "job_type": "安全工程师",
                "suggest_sites": _CN,
            },
            {
                "role": "DBA",
                "en": ["Database Administrator", "DBA"],
                "keywords": ["DBA", "数据库", "MySQL", "Oracle", "PostgreSQL", "数据库优化", "高可用"],
                "skills": ["MySQL/Oracle/PG", "SQL 调优", "高可用/备份", "性能诊断"],
                "levels": ["初级 DBA", "高级 DBA", "数据库架构"],
                "job_type": "数据库管理员",
                "suggest_sites": _CN,
            },
        ],
    },
    {
        "group": "产品/设计",
        "note": "技术相关但非纯编码",
        "roles": [
            {
                "role": "产品经理",
                "en": ["Product Manager", "PM"],
                "keywords": ["产品经理", "需求分析", "原型", "Axure", "数据驱动", "B端产品", "C端产品"],
                "skills": ["需求分析", "原型设计", "数据分析", "项目协调"],
                "levels": ["产品助理", "产品经理", "高级产品经理", "产品总监"],
                "job_type": "产品经理",
                "suggest_sites": _CN,
            },
            {
                "role": "UI 设计",
                "en": ["UI Designer", "Visual Designer"],
                "keywords": ["UI", "视觉设计", "Figma", "Sketch", "界面设计", "设计规范"],
                "skills": ["Figma/Sketch", "视觉/界面", "设计规范", "切图标注"],
                "levels": ["初级 UI", "高级 UI", "设计负责人"],
                "job_type": "UI设计师",
                "suggest_sites": _CN,
            },
            {
                "role": "交互/UX",
                "en": ["UX Designer", "Interaction Designer"],
                "keywords": ["交互设计", "UX", "用户体验", "用户研究", "原型", "可用性"],
                "skills": ["交互设计", "用户研究", "信息架构", "原型/可用性测试"],
                "levels": ["初级交互", "高级交互", "体验负责人"],
                "job_type": "交互设计师",
                "suggest_sites": _CN,
            },
        ],
    },
    {
        "group": "架构/管理",
        "note": "资深进阶方向",
        "roles": [
            {
                "role": "架构师",
                "en": ["Software Architect", "Solution Architect"],
                "keywords": ["架构师", "系统架构", "高并发", "分布式", "技术选型", "中台"],
                "skills": ["系统设计", "高并发/分布式", "技术选型", "性能与可用性"],
                "levels": ["高级工程师", "架构师", "首席架构师"],
                "job_type": "系统架构师",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "技术经理",
                "en": ["Engineering Manager", "Tech Lead"],
                "keywords": ["技术经理", "team leader", "研发管理", "技术负责人", "团队管理"],
                "skills": ["团队管理", "项目把控", "技术规划", "人才培养"],
                "levels": ["技术组长", "技术经理", "研发总监"],
                "job_type": "技术经理",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "技术总监/CTO",
                "en": ["Director of Engineering", "VP Engineering", "CTO"],
                "keywords": ["技术总监", "CTO", "研发总监", "技术战略", "技术管理"],
                "skills": ["技术战略", "组织管理", "业务理解", "跨团队协作"],
                "levels": ["研发总监", "技术总监", "VP/CTO"],
                "job_type": "技术总监",
                "suggest_sites": _CN_INTL,
            },
            {
                "role": "项目经理",
                "en": ["Project Manager", "Technical Project Manager", "PMO"],
                "keywords": ["项目经理", "PMP", "项目管理", "交付", "敏捷", "Scrum"],
                "skills": ["项目计划", "风险/进度管理", "敏捷/Scrum", "干系人协调"],
                "levels": ["项目助理", "项目经理", "高级项目经理", "PMO负责人"],
                "job_type": "项目经理",
                "suggest_sites": _CN,
            },
        ],
    },
]


def _flat_role(role: dict, group: str) -> dict:
    """把角色规整成对外/消费方使用的扁平结构（category=角色名，便于复用旧调用）。"""
    return {
        "group": group,
        "category": role["role"],
        "role": role["role"],
        "en": list(role.get("en", [])),
        "keywords": list(role["keywords"]),
        "skills": list(role.get("skills", [])),
        "levels": list(role.get("levels", [])),
        "job_type": role.get("job_type", ""),
        "suggest_sites": list(role["suggest_sites"]),
    }


def all_groups() -> list[dict]:
    """返回按六大类分组的完整预设（供前端分组渲染）。"""
    return [
        {
            "group": g["group"],
            "note": g.get("note", ""),
            "roles": [_flat_role(r, g["group"]) for r in g["roles"]],
        }
        for g in _GROUPS
    ]


def all_roles() -> list[dict]:
    """返回拉平后的全部角色（约 30 条）。"""
    return [_flat_role(r, g["group"]) for g in _GROUPS for r in g["roles"]]


def all_presets() -> list[dict]:
    """向后兼容：返回拉平后的全部角色预设（旧调用方按 category/keywords/suggest_sites 使用）。"""
    return all_roles()


def match_preset(text: str) -> Optional[dict]:
    """按自由文本匹配最相关的角色预设。

    打分：角色名命中权重最高；其次英文称呼；再次 keywords 命中数。无命中返回 None。
    """
    t = (text or "").lower().strip()
    if not t:
        return None
    best: Optional[dict] = None
    best_score = 0
    for g in _GROUPS:
        for r in g["roles"]:
            score = 0
            role_l = r["role"].lower()
            if role_l in t or t in role_l:
                score += 6
            for en in r.get("en", []):
                if en.lower() in t:
                    score += 3
            for kw in r["keywords"]:
                if kw.lower() in t:
                    score += 1
            jt = r.get("job_type", "").lower()
            if jt and (jt in t or t in jt):
                score += 4
            if score > best_score:
                best_score = score
                best = _flat_role(r, g["group"])
    if best is None or best_score == 0:
        return None
    return best
