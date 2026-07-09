"""AI 辅助能力：从简历文本抽取画像 + 针对单个职位生成投递话术/面试题/技能差距。

复用统一的 complete() 文本补全入口（按配置走 Cursor 或 DeepSeek）。
"""

from __future__ import annotations

from typing import Any, Dict

from . import complete
from ._common import LLMError, _extract_json

# 画像字段与门户表单/services._PROFILE_LABELS 对齐
_PROFILE_KEYS = [
    "years",
    "education",
    "current_role",
    "target_role",
    "skills",
    "industry",
    "expected_salary",
    "work_mode",
    "relocate",
    "company_size",
    "avoid",
    "goal",
]

_PROFILE_PROMPT = """你是一名资深招聘顾问。请从下面的简历文本中提取求职者画像，
严格输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记），字段如下：
{{
  "years": "工作年限，如 应届/1-3年/3-5年/5-10年/10年以上，判断不出留空",
  "education": "最高学历：大专/本科/硕士/博士，判断不出留空",
  "current_role": "当前或最近职位名",
  "target_role": "目标/期望职位，没写则据经历推断，仍不确定留空",
  "skills": "核心技能，逗号分隔",
  "industry": "所在或目标行业",
  "expected_salary": "期望薪资，没写留空",
  "work_mode": "远程/现场/混合，没写留空",
  "relocate": "可异地/不可异地/可考虑，没写留空",
  "company_size": "公司规模偏好：初创/中型/大型/外企/国企，没写留空",
  "avoid": "想避开的，逗号分隔，没写留空",
  "goal": "一句话职业目标"
}}
缺失字段一律用空字符串，不要编造。

# 简历文本
{resume}
"""

_ASSIST_PROMPTS = {
    "cover_letter": (
        "你是一名求职辅导专家。请根据【求职者画像】与【职位信息】，"
        "为该求职者撰写一段中文投递话术/求职信，用于投递或打招呼：\n"
        "- 开头自然，突出与该职位最相关的亮点与匹配点；\n"
        "- 结合 JD 要求，结合画像里的技能与经历给出有说服力的理由；\n"
        "- 语气真诚、专业，控制在 200-300 字；\n"
        "只输出话术正文本身，不要额外说明、不要使用工具。"
    ),
    "interview": (
        "你是一名资深面试官。请根据【求职者画像】与【职位信息】，"
        "列出该职位可能的面试题，并给出简要答题思路：\n"
        "- 按『技术/项目经历/软技能』分类，每类 3-5 题；\n"
        "- 每题后用一行给出回答要点或考察点；\n"
        "只输出题目与思路本身，不要额外说明、不要使用工具。"
    ),
    "skill_gap": (
        "你是一名职业规划师。请对比【求职者画像】与【职位信息】的要求，"
        "做技能差距分析：\n"
        "- 指出求职者已具备的、与该职位匹配的能力；\n"
        "- 明确为胜任该职位还需补强的技能/知识点，按优先级分点，"
        "每点给出简短学习方向；\n"
        "只输出分析本身，不要额外说明、不要使用工具。"
    ),
}

_JOB_BLOCK = """# 求职者画像
{profile}

# 职位信息
- 职位名称: {title}
- 公司: {company}
- 薪资: {salary}
- 地点: {location}
- 经验要求: {experience}
- 学历要求: {education}
- 技能标签: {tags}
- 职位描述: {description}
"""


async def extract_profile_from_text(text: str) -> Dict[str, Any]:
    """从简历文本抽取画像字典；缺失字段补空串。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("简历内容为空")
    prompt = _PROFILE_PROMPT.format(resume=text[:8000])
    content = await complete(prompt, json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能从简历解析出画像，请稍后重试或手动填写") from exc
    if not isinstance(parsed, dict):
        raise LLMError("简历解析结果格式异常")
    out: Dict[str, Any] = {}
    for k in _PROFILE_KEYS:
        v = parsed.get(k, "")
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v if x)
        out[k] = str(v).strip() if v is not None else ""
    return out


async def job_assistant(job: dict, profile_text: str, kind: str) -> str:
    """针对单个职位生成 投递话术/面试题/技能差距 文本。"""
    instruction = _ASSIST_PROMPTS.get(kind)
    if instruction is None:
        raise LLMError(f"未知的助手类型: {kind}")
    block = _JOB_BLOCK.format(
        profile=(profile_text or "").strip() or "（求职者未提供画像，请基于职位本身给出通用内容）",
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", "") or "未提供",
        location=job.get("location", "") or "未提供",
        experience=job.get("experience", "") or "未提供",
        education=job.get("education", "") or "未提供",
        tags=job.get("tags", "") or "未提供",
        description=(job.get("description", "") or "未提供")[:4000],
    )
    prompt = f"{instruction}\n\n{block}"
    return (await complete(prompt, json_mode=False)).strip()


# ===================== 简历优化能力 =====================

_DIAGNOSE_PROMPT = """你是一名资深简历顾问与 ATS（简历筛选系统）专家。请基于【简历原文】，
必要时参考【求职者画像】，对这份简历做体检式诊断。
严格只输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记），结构如下：
{{
  "overall_score": 0到100的整数,综合评分,
  "summary": "两三句话的总体评价",
  "sections": [
    {{"name": "板块名,如 个人信息/工作经历/项目经历/技能/教育/整体表达",
      "score": 0到100的整数,
      "issues": ["该板块存在的具体问题"],
      "suggestions": ["可执行的改进建议"]}}
  ],
  "keywords_missing": ["相对目标岗位可能缺失、建议补充的关键词/技能"],
  "ats_issues": ["影响机器筛选/排版规范性的问题,如格式、图片、特殊字符、缺少量化等"],
  "quick_wins": ["最高性价比、能立刻改的几条"]
}}
要求：建议要具体、可落地，针对中文求职场景；不编造简历里没有的事实。

# 求职者画像
{profile}

# 简历原文
{resume}
"""

_TAILOR_INSTRUCTION = (
    "你是一名资深简历顾问。请基于【简历原文】（必要时参考【求职者画像】），"
    "针对下面这条【职位信息】给出'如何改简历去贴合这个 JD'的定制建议：\n"
    "- 该突出/前置哪些与 JD 最相关的经历与成果；\n"
    "- 建议补充或显式写出的关键词/技能（对齐 JD 用词，利于 ATS 命中）；\n"
    "- 针对性的经历改写建议（可给出'原描述→改写后'示例）；\n"
    "- 与 JD 仍有差距、需要弱化或诚实说明的点。\n"
    "用清晰的小标题分点输出中文正文，不要使用工具、不要输出多余说明。"
)

_TAILOR_BLOCK = """# 求职者画像
{profile}

# 简历原文
{resume}

# 职位信息
- 职位名称: {title}
- 公司: {company}
- 薪资: {salary}
- 地点: {location}
- 经验要求: {experience}
- 学历要求: {education}
- 技能标签: {tags}
- 职位描述: {description}
"""

_REWRITE_PROMPT = """你是一名简历写作专家。请把下面【待改写内容】里的工作/项目经历描述，
逐条改写成更有竞争力的版本：使用 STAR 思路、以强动词开头、尽量量化成果（数字/比例/规模），
去掉空话套话，保持真实、不编造数据（无法量化处用占位提示，如'(可补充：提升X%)'）。
{role_hint}
严格只输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记），结构如下：
{{
  "pairs": [
    {{"original": "原始的一条描述", "improved": "改写后的版本"}}
  ]
}}
按内容里可识别的条目逐条给出；若原文是整段，请先合理拆分为若干条再改写。

# 待改写内容
{content}
"""


def _norm_str_list(v: Any) -> list[str]:
    """把 LLM 返回的字段统一成字符串列表。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    s = str(v).strip()
    return [s] if s else []


async def diagnose_resume(resume_text: str, profile_text: str = "") -> Dict[str, Any]:
    """对简历原文做整体诊断打分，返回结构化结果。"""
    resume_text = (resume_text or "").strip()
    if not resume_text:
        raise LLMError("没有可诊断的简历，请先上传简历")
    prompt = _DIAGNOSE_PROMPT.format(
        profile=(profile_text or "").strip() or "（未提供画像）",
        resume=resume_text[:8000],
    )
    content = await complete(prompt, json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能解析诊断结果，请稍后重试") from exc
    if not isinstance(parsed, dict):
        raise LLMError("诊断结果格式异常")
    try:
        score = int(float(parsed.get("overall_score", 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    sections = []
    for sec in parsed.get("sections", []) or []:
        if not isinstance(sec, dict):
            continue
        try:
            ss = max(0, min(100, int(float(sec.get("score", 0)))))
        except (TypeError, ValueError):
            ss = 0
        sections.append(
            {
                "name": str(sec.get("name", "") or "").strip(),
                "score": ss,
                "issues": _norm_str_list(sec.get("issues")),
                "suggestions": _norm_str_list(sec.get("suggestions")),
            }
        )
    return {
        "overall_score": score,
        "summary": str(parsed.get("summary", "") or "").strip(),
        "sections": sections,
        "keywords_missing": _norm_str_list(parsed.get("keywords_missing")),
        "ats_issues": _norm_str_list(parsed.get("ats_issues")),
        "quick_wins": _norm_str_list(parsed.get("quick_wins")),
    }


async def tailor_resume_for_job(
    resume_text: str, profile_text: str, job: dict
) -> str:
    """针对单条职位，给出如何改简历去贴合该 JD 的定制建议文本。"""
    resume_text = (resume_text or "").strip()
    if not resume_text:
        raise LLMError("没有可优化的简历，请先在『画像』里上传简历")
    block = _TAILOR_BLOCK.format(
        profile=(profile_text or "").strip() or "（求职者未提供画像）",
        resume=resume_text[:6000],
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", "") or "未提供",
        location=job.get("location", "") or "未提供",
        experience=job.get("experience", "") or "未提供",
        education=job.get("education", "") or "未提供",
        tags=job.get("tags", "") or "未提供",
        description=(job.get("description", "") or "未提供")[:4000],
    )
    prompt = f"{_TAILOR_INSTRUCTION}\n\n{block}"
    return (await complete(prompt, json_mode=False)).strip()


async def rewrite_bullets(text: str, target_role: str = "") -> Dict[str, Any]:
    """把经历描述逐条改写成 STAR/动词+量化 的版本，返回 {pairs:[{original,improved}]}。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("没有可改写的内容，请粘贴经历描述或先上传简历")
    role_hint = (
        f"改写时围绕目标岗位『{target_role.strip()}』的相关性来组织重点。"
        if (target_role or "").strip()
        else ""
    )
    prompt = _REWRITE_PROMPT.format(role_hint=role_hint, content=text[:8000])
    content = await complete(prompt, json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能解析改写结果，请稍后重试") from exc
    raw_pairs = []
    if isinstance(parsed, dict):
        raw_pairs = parsed.get("pairs", []) or []
    elif isinstance(parsed, list):
        raw_pairs = parsed
    pairs = []
    for p in raw_pairs:
        if not isinstance(p, dict):
            continue
        orig = str(p.get("original", "") or "").strip()
        imp = str(p.get("improved", "") or "").strip()
        if imp:
            pairs.append({"original": orig, "improved": imp})
    if not pairs:
        raise LLMError("未能生成改写内容，请稍后重试")
    return {"pairs": pairs}


# ===================== 公司多维打分 =====================

_COMPANY_PROMPT = """你是一名熟悉中国职场的行业与雇主分析师。请从求职者视角，对下面这家【公司】做多维评估。
严格只输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记），结构如下：
{{
  "overall": 0到100的整数（综合推荐度）,
  "summary": "一句话总评",
  "business": "主营业务与行业地位简述",
  "promotion": "职业晋升空间与成长性",
  "pay": "薪资待遇与福利水平",
  "culture": "工作氛围/人文关怀/加班文化",
  "score_business": 0到100,
  "score_promotion": 0到100,
  "score_pay": 0到100,
  "score_culture": 0到100,
  "pros": ["求职亮点1", "亮点2"],
  "cons": ["风险或槽点1", "槽点2"]
}}
要求：基于公开认知客观评估，分项文字简洁（每项一两句）。若你对该公司了解有限，
请在 summary 里明确写“信息有限，仅供参考”，给保守估计，绝不编造具体数字、融资或事件。

# 公司
{company}
"""


def _clamp100(v: Any) -> int:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return 0


def _str_list(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


_INTERVIEW_PROMPT = """你是一名资深技术面试官，熟悉中国互联网/科技公司的招聘考察风格。
请针对【公司】的【岗位】，整理一份高频面试题与笔试题（结合该公司一贯的考察侧重）。
严格只输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记），结构如下：
{{
  "questions": [
    {{
      "qtype": "interview 或 written 或 system_design 或 behavioral",
      "question": "题目",
      "answer": "参考答案/解题思路（简洁要点）",
      "tags": ["知识点1", "知识点2"],
      "difficulty": "简单 或 中等 或 困难"
    }}
  ]
}}
要求：覆盖技术基础、项目深挖、系统设计、行为面等；贴近该公司岗位的真实考察方向，
但这是你归纳整理的高频题，并非官方原题，不要声称是真题或泄露具体试卷。共给 {count} 道。

# 公司
{company}
# 岗位
{role}
"""


async def generate_interview_questions(
    company: str, role: str, count: int = 12
) -> list[dict]:
    """按公司+岗位生成面试/笔试题，返回规整后的题目列表。"""
    company = (company or "").strip()
    role = (role or "").strip()
    if not role and not company:
        raise LLMError("请至少提供公司或岗位")
    count = max(1, min(30, int(count or 12)))
    prompt = _INTERVIEW_PROMPT.format(
        company=company or "（不限公司，通用大厂风格）",
        role=role or "（通用技术岗）",
        count=count,
    )
    content = await complete(prompt, json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能解析题库生成结果，请稍后重试") from exc
    raw = []
    if isinstance(parsed, dict):
        raw = parsed.get("questions", []) or []
    elif isinstance(parsed, list):
        raw = parsed
    _types = {"interview", "written", "system_design", "behavioral"}
    out: list[dict] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        question = str(q.get("question", "") or "").strip()
        if not question:
            continue
        qtype = str(q.get("qtype", "interview") or "interview").strip()
        if qtype not in _types:
            qtype = "interview"
        tags = q.get("tags")
        tags = "、".join(_str_list(tags)) if not isinstance(tags, str) else tags.strip()
        out.append(
            {
                "qtype": qtype,
                "question": question,
                "answer": str(q.get("answer", "") or "").strip(),
                "tags": tags,
                "difficulty": str(q.get("difficulty", "") or "").strip(),
            }
        )
    if not out:
        raise LLMError("未能生成题目，请稍后重试")
    return out


async def score_company(name: str) -> Dict[str, Any]:
    """对一家公司做主营/晋升/待遇/人文关怀四维 + 综合评分，返回结构化结果。"""
    name = (name or "").strip()
    if not name:
        raise LLMError("公司名为空")
    content = await complete(_COMPANY_PROMPT.format(company=name), json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能解析公司评分结果，请稍后重试") from exc
    if not isinstance(parsed, dict):
        raise LLMError("公司评分结果格式异常")

    def s(k: str) -> str:
        return str(parsed.get(k, "") or "").strip()

    return {
        "overall": _clamp100(parsed.get("overall", 0)),
        "summary": s("summary"),
        "business": s("business"),
        "promotion": s("promotion"),
        "pay": s("pay"),
        "culture": s("culture"),
        "score_business": _clamp100(parsed.get("score_business", 0)),
        "score_promotion": _clamp100(parsed.get("score_promotion", 0)),
        "score_pay": _clamp100(parsed.get("score_pay", 0)),
        "score_culture": _clamp100(parsed.get("score_culture", 0)),
        "pros": _str_list(parsed.get("pros")),
        "cons": _str_list(parsed.get("cons")),
        "raw": content,
    }
