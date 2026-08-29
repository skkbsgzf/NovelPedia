# -*- coding: utf-8 -*-
"""
prompts.py —— 两轮抽取提示词模板 + JSON Schema 说明
第一轮:必填 5W1H(稳)
第二轮:叙事层 + 文学层 + 分镜 beats(可空)
"""

SYSTEM_PROMPT = (
    "你是一位严谨的中文小说结构化分析助手。任务:把给定小说片段拆成"
    "事件要素(5W1H)、叙事信息与文学要素,严格按指定 JSON Schema 输出。"
    "规则:1) 只输出 JSON,不要任何解释性文字;2) 字段缺失时填空字符串\"\"或空数组[],"
    "不要输出 null;3) who 中 role 只能是 主事/参与/提及 之一;"
    "4) when.timeline 只能是 顺叙/倒叙/插叙 之一;5) 不编造原文没有的信息。"
)

# ---------- 第一轮:5W1H ----------
ROUND1_FEWSHOT = {
    "who": [{"name": "林晚", "role": "主事"}, {"name": "守阁弟子", "role": "提及"}],
    "what": "林晚以幻术潜入藏书阁顶层寻找与师父失踪有关的残页",
    "when": {"story_time": "入夜三更", "timeline": "顺叙"},
    "where": "宗门·藏书阁顶层",
    "why": "查明师父失踪的真相",
    "how": "以幻术骗过巡夜弟子",
}

ROUND1_USER_TMPL = """下面是一段小说原文(每行前缀 [段落号] 是原文坐标,用于你理解位置,不要抄进结果)。

{text}

【任务】抽取本片段的核心事件,严格按以下 JSON 输出:
{{
  "who": [{{"name": "人物名", "role": "主事|参与|提及"}}],
  "what": "一句话概述发生了什么事",
  "when": {{"story_time": "故事内时间(如'入夜三更')", "timeline": "顺叙|倒叙|插叙"}},
  "where": "地点",
  "why": "动机/原因",
  "how": "达成方式/手段"
}}

要求:
- where 必填。原文未明写地点时,依据上下文给出最可能的场所(如"室内·卧床""街道");
  确实无从判断才填"未明示"。禁止留空。
- who 只收本片段真实出现或被提及的人物,不要把物品/组织当人物。
- what 一句话,20-40 字,必须包含主事人物与核心动作。

参考示例:
```json
{example}
```

只输出 JSON。"""

# ---------- 第二轮:叙事 + 文学 + 分镜 ----------
ROUND2_FEWSHOT = {
    "pov": "林晚",
    "emotion": {"label": "紧张", "intensity": 4},
    "plot_function": "推进",
    "rhetoric": ["拟人:'灯火在昏暗中窃窃私语'"],
    "key_sentences": ["她隐入书架阴影,心跳如鼓。"],
    "summary": "林晚深夜以幻术潜入藏书阁顶层,寻找与师父失踪有关的残页。",
    "keywords": ["林晚", "藏书阁", "幻术", "残页", "师父失踪"],
    "beats": [
        {"seq": 1, "anchor": "她借着夜色靠近阁楼", "content": "潜入准备:确认无人后接近藏书阁"},
        {"seq": 2, "anchor": "指尖拂过一排排书脊", "content": "搜寻残页:逐架排查目标位置"},
        {"seq": 3, "anchor": "一道幻光自袖中荡开", "content": "骗过巡夜:以幻术遮蔽自身气息"},
    ],
}

ROUND2_USER_TMPL = """这是同一段小说原文:
{text}

这是第一轮已抽出的 5W1H:
{round1}

【任务】在之上补充叙事/文学要素与分镜,严格按以下 JSON 输出:
{{
  "pov": "视角人物的姓名(只写人名,如'周明瑞';无法判断填\\"未明示\\",禁止写'第三人称'这类描述)",
  "emotion": {{"label": "情绪标签", "intensity": 1-5 的整数}},
  "plot_function": "铺垫|推进|转折|高潮|收束|揭示",
  "rhetoric": ["确凿的修辞手法+原文短引,宁缺毋滥"],
  "key_sentences": ["原文关键句短引(逐字,便于定位)"],
  "summary": "本片段一句话摘要(服务检索)",
  "keywords": ["检索关键词数组"],
  "beats": [
    {{"seq": 1, "anchor": "该分镜起始的原文短句(逐字,用于定位段落)", "content": "该分镜一句话概括"}}
  ]
}}
说明(务必遵守条数限制,超量视为错误):
- rhetoric: **最多 3 条**,只收确凿的修辞(比喻/拟人/排比/夸张等),格式"手法:'原文短引'";
  没有明显修辞就给空数组 []。不要把普通叙述当修辞。
- key_sentences: **最多 3 条**,只挑本片段最有信息量或最具文学性的整句原文(逐字);
  禁止摘录一堆短句(如"痛!""好痛!"),禁止把整段对话全抄进来。
- beats 是'分镜'(叙事节拍),按场景节奏列 **2-5 个**,seq 从 1 递增,并按原文出现顺序排列;
- 每个 beat 的 anchor 与 content **都不得为空**;anchor 必须是原文中真实存在、
  且属于该节拍起始位置的短句(逐字,10-20 字为宜),程序会据此定位段落坐标;
  若某个节拍给不出真实 anchor,就不要输出这一条。
- 文学要素不确定就留空数组/空串,不要硬编。

参考示例:
```json
{example}
```

只输出 JSON。"""


def build_round1_user(text: str) -> str:
    import json
    return ROUND1_USER_TMPL.format(text=text, example=json.dumps(ROUND1_FEWSHOT, ensure_ascii=False))


def build_round2_user(text: str, round1: dict) -> str:
    import json
    return ROUND2_USER_TMPL.format(
        text=text,
        round1=json.dumps(round1, ensure_ascii=False),
        example=json.dumps(ROUND2_FEWSHOT, ensure_ascii=False),
    )


# ---------- 单轮合并抽取(5W1H + 叙事 + 文学 + 分镜一次出,提速用) ----------
# 2026-08-20 小样实测: 11/11 成功, 单场景 24.8s vs 两轮 55s, 提速 ~2.2x, 质量样例过硬
SINGLE_FEWSHOT = {
    "who": [{"name": "林晚", "role": "主事"}, {"name": "守阁弟子", "role": "提及"}],
    "what": "林晚以幻术潜入藏书阁顶层寻找与师父失踪有关的残页",
    "when": {"story_time": "入夜三更", "timeline": "顺叙"},
    "where": "宗门·藏书阁顶层",
    "why": "查明师父失踪的真相",
    "how": "以幻术骗过巡夜弟子",
    "pov": "林晚",
    "emotion": {"label": "紧张", "intensity": 4},
    "plot_function": "推进",
    "rhetoric": ["拟人:'灯火在昏暗中窃窃私语'"],
    "key_sentences": ["她隐入书架阴影,心跳如鼓。"],
    "summary": "林晚深夜以幻术潜入藏书阁顶层,寻找与师父失踪有关的残页。",
    "keywords": ["林晚", "藏书阁", "幻术", "残页", "师父失踪"],
    "beats": [
        {"seq": 1, "anchor": "她借着夜色靠近阁楼", "content": "潜入准备:确认无人后接近藏书阁"},
        {"seq": 2, "anchor": "指尖拂过一排排书脊", "content": "搜寻残页:逐架排查目标位置"},
        {"seq": 3, "anchor": "一道幻光自袖中荡开", "content": "骗过巡夜:以幻术遮蔽自身气息"},
    ],
}

SINGLE_USER_TMPL = """下面是一段小说原文(每行前缀 [段落号] 是原文坐标,用于你理解位置,不要抄进结果)。

{text}

【任务】抽取本片段的事件要素(5W1H)、叙事信息、文学要素与分镜,一次完成,严格按以下 JSON 输出:
{{
  "who": [{{"name": "人物名", "role": "主事|参与|提及"}}],
  "what": "一句话概述发生了什么事",
  "when": {{"story_time": "故事内时间(如'入夜三更')", "timeline": "顺叙|倒叙|插叙"}},
  "where": "地点",
  "why": "动机/原因",
  "how": "达成方式/手段",
  "pov": "视角人物的姓名(只写人名;无法判断填\\"未明示\\",禁止写'第三人称'这类描述)",
  "emotion": {{"label": "情绪标签", "intensity": 1-5 的整数}},
  "plot_function": "铺垫|推进|转折|高潮|收束|揭示",
  "rhetoric": ["确凿的修辞手法+原文短引,宁缺毋滥"],
  "key_sentences": ["原文关键句短引(逐字,便于定位)"],
  "summary": "本片段一句话摘要(服务检索)",
  "keywords": ["检索关键词数组"],
  "beats": [
    {{"seq": 1, "anchor": "该分镜起始的原文短句(逐字,用于定位段落)", "content": "该分镜一句话概括"}}
  ]
}}

要求:
- who 只收本片段真实出现或被提及的人物,不要把物品/组织当人物;role 只能是 主事/参与/提及。
- what 一句话,20-40 字,必须包含主事人物与核心动作。
- where 必填。原文未明写地点时,依据上下文给出最可能的场所;确实无从判断才填"未明示"。禁止留空。
- when.timeline 只能是 顺叙/倒叙/插叙 之一。
- rhetoric: **最多 3 条**,只收确凿的修辞(比喻/拟人/排比/夸张等),格式"手法:'原文短引'";没有就给空数组 []。
- key_sentences: **最多 3 条**,只挑本片段最有信息量或最具文学性的整句原文(逐字);禁止摘录碎句。
- beats 是'分镜'(叙事节拍),按场景节奏列 **2-5 个**,seq 从 1 递增,按原文出现顺序排列;
  每个 beat 的 anchor 与 content 都不得为空;anchor 必须是原文中真实存在、
  且属于该节拍起始位置的短句(逐字,10-20 字为宜),程序会据此定位段落坐标;
  若某个节拍给不出真实 anchor,就不要输出这一条。
- 文学要素不确定就留空数组/空串,不要硬编;不编造原文没有的信息;字段缺失填空串""或空数组,不要 null。

参考示例:
```json
{example}
```

只输出 JSON。"""


def build_single_user(text: str) -> str:
    import json
    return SINGLE_USER_TMPL.format(
        text=text, example=json.dumps(SINGLE_FEWSHOT, ensure_ascii=False))


# ======================================================================
# v2 薄 schema(2026-08-20): 把书读薄,输出 ≤ 原文
# 顶层: who / when / where / actinfo(有序列表) / notes(免压缩安全阀)
# actinfo 条目:
#   act   -> {type缺省, channel, who, content}  channel ∈ see/hear/feel/do/say/recall/reason
#   event -> {type:"event", content, scope}      客观事件,无执行者,scope=受影响角色
# 砍掉: what/why/how/pov/emotion/rhetoric/key_sentences/summary/keywords/beats
# ======================================================================
SYSTEM_PROMPT_V2 = (
    "你是一位小说结构化抽取助手。任务:把给定小说片段\"读薄\","
    "抽取出场景锚点(who/when/where)、有序事件列表(actinfo)与备注(notes),"
    "严格按指定 JSON Schema 输出,输出长度必须小于原文。"
    "规则:1) 只输出 JSON,不要任何解释性文字;2) 字段缺失填空串\"\"或空数组[],不要 null;"
    "3) who.role 只能是 主事/参与/提及 之一;4) when.timeline 只能是 顺叙/倒叙/插叙 之一;"
    "5) actinfo 按原文先后顺序排列;6) 不编造原文没有的信息。"
)

V2_FEWSHOT = {
    "who": [
        {"name": "林晚", "role": "主事"},
        {"name": "守阁弟子", "role": "提及"},
    ],
    "when": {"story_time": "入夜三更", "timeline": "顺叙"},
    "where": "宗门·藏书阁顶层",
    "actinfo": [
        {"type": "act", "channel": "do", "who": "林晚", "content": "借着夜色靠近藏书阁；隐入书架阴影等待"},
        {"type": "act", "channel": "see", "who": "林晚", "content": "巡夜弟子提灯走过转角"},
        {"type": "act", "channel": "feel", "who": "林晚", "content": "心跳如鼓,暗自紧张"},
        {"type": "event", "content": "远处传来三声钟响", "scope": ["林晚"]},
        {"type": "act", "channel": "recall", "who": "林晚", "content": "想起师父失踪前一晚也是这个时辰"},
        {"type": "act", "channel": "reason", "who": "林晚", "content": "钟响是换岗信号,须趁隙上楼"},
    ],
    "notes": "守阁弟子全程未察觉林晚,幻术设定可作后续伏笔;师父失踪疑点尚无线索。",
}

V2_USER_TMPL = """下面是一段小说原文(每行前缀 [段落号] 是原文坐标,用于你理解位置,不要抄进结果)。

{text}

【任务】把本片段"读薄"成结构化数据,严格按以下 JSON 输出:
{{
  "who": [{{"name": "人物名", "role": "主事|参与|提及"}}],
  "when": {{"story_time": "故事内时间(如'入夜三更')", "timeline": "顺叙|倒叙|插叙"}},
  "where": "地点",
  "actinfo": [
    {{"type": "act", "channel": "see|hear|feel|do|say|recall|reason", "who": "人物名", "content": "短语"}},
    {{"type": "event", "content": "客观事件", "scope": ["受影响人物"]}}
  ],
  "notes": "补充信息"
}}

要求:
- who 只收本片段真实出现或被提及的人物,不要把物品/组织当人物;role 只能是 主事/参与/提及。
- where 必填。原文未明写地点时,依据上下文给出最可能的场所;确实无从判断才填"未明示"。禁止留空。
- when: story_time 原文未明写填"未明示";timeline 只能是 顺叙/倒叙/插叙 之一。
- actinfo 是按原文先后顺序排列的**有序事件列表**,总条数上限按场景规模:
  长场景(>600字)最多 12 条,中等(300-600字)最多 8 条,短场景(<300字)最多 4 条。
  * act 条目 = 某角色的行为/感知/话语/想法,必填 who(执行者)与 channel;
    channel 只能是:
      see=看见什么, hear=听见什么, feel=感受(痛/冷等体感 + 情绪吐槽、内心戏),
      do=做了什么, say=说了什么, recall=回想/回忆(串联过去线索), reason=推理(得出结论)
  * event 条目 = 场景中客观发生的事件(非某角色行为),**没有 who**;
    必填 scope=受影响角色列表,波及所有人填 ["*"]。
  * act 条目的 who、event 条目的 scope,出现的名字都必须能在顶层 who 数组中找到。
  * **同一角色同一 channel 最多 2 条**,连续同类(同角色同channel)必须合并成一条,
    content 用"；"连接多个动作/感受;宁精勿滥,每条 content 用短语概括 10-25 字,不要抄整句。
- notes: 本片段无法塞入 actinfo 但重要的信息(伏笔、氛围铺垫、作者暗示、异常点),至少 1-2 条,内容不限。
- 不编造原文没有的信息;字段缺失填空串""或空数组[],不要 null。

参考示例:
```json
{example}
```

只输出 JSON。"""


def build_v2_user(text: str) -> str:
    import json
    return V2_USER_TMPL.format(
        text=text, example=json.dumps(V2_FEWSHOT, ensure_ascii=False))
