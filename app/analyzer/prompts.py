SYSTEM_PROMPT = """你是一名资深的技术招聘顾问和职业规划师。
你会根据求职者画像，对一条招聘职位进行匹配度分析，给出务实、可执行的求职建议，
并明确指出求职者为胜任该职位需要补强的技能、以及针对该职位投递时的简历改进方向。
务必只输出一个 JSON 对象，不要包含任何额外文字或 Markdown 代码块标记。"""

USER_PROMPT_TEMPLATE = """# 求职者画像
{profile}

# 职位信息
- 职位名称: {title}
- 公司: {company}
- 薪资: {salary}
- 地点: {location}
- 经验要求: {experience}
- 学历要求: {education}
- 技能标签: {tags}
- 来源: {site}
- 职位描述: {description}

# 任务
请基于以上信息分析该职位与求职者的匹配度，并给出建议。
严格输出如下 JSON（不要输出其它内容）：
{{
  "match_score": 0-100 的整数,
  "summary": "一句话概括该职位是否值得投递",
  "advice": "给求职者的整体建议：匹配亮点、潜在风险、投递与面试准备要点，分点说明",
  "skills_to_learn": "为胜任该职位、求职者需要重点学习或补强的技能/知识点，结合 JD 要求与其画像差距，按优先级分点列出（每点可附简短理由或学习方向）；若已完全胜任则说明无明显短板",
  "resume_tips": "针对该职位投递时的简历改进建议：应突出/补充哪些项目与经历、用哪些关键词与量化成果对齐 JD、需要弱化或调整的内容，分点说明"
}}"""


def build_single_prompt(profile: str, job: dict) -> str:
    """把 system + user 合并成一段提示词（用于 Agent.prompt 这类单字符串接口）。"""
    msgs = build_messages(profile, job)
    system = msgs[0]["content"]
    user = msgs[1]["content"]
    return (
        f"{system}\n\n{user}\n\n"
        "只输出 JSON 对象本身，不要使用任何工具，不要读写文件，不要任何额外说明。"
    )


def build_messages(profile: str, job: dict) -> list[dict]:
    profile_text = profile.strip() or "（求职者未提供画像，请基于职位本身做通用分析）"
    user = USER_PROMPT_TEMPLATE.format(
        profile=profile_text,
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", "") or "未提供",
        location=job.get("location", "") or "未提供",
        experience=job.get("experience", "") or "未提供",
        education=job.get("education", "") or "未提供",
        tags=job.get("tags", "") or "未提供",
        site=job.get("site", ""),
        description=(job.get("description", "") or "未提供")[:4000],
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
