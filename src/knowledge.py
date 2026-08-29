# -*- coding: utf-8 -*-
"""
knowledge.py —— 卷积式知识库(Stage1)
在逐场景抽取后, 识别该场景涉及的设定实体(人物/势力/世界观/物品),
提取每个实体的信息增量(动机/人设/心理状态/设定铺陈), 卷积更新到独立知识库。

设计要点:
  - 独立 JSON 文件存储(非 db 表), 符合"rag 不通过 db 形式"的要求。
  - 逐场景增量累积(fold/卷积): 先检索知识库, 命中则合并增量, 未命中则新建。
  - 保信息不丢: 记录每个实体每次出现的位置(appearance_scenes), 动机/人设/心理完整铺陈。
  - 实体 category: 人物 / 势力 / 世界观 / 物品。
"""
import sys
import os
import json
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import prompts as P
import llm_client


# ======================================================================
# 知识库数据结构
# ======================================================================
EMPTY_KB = {"entities": [], "meta": {"schema": 1, "created_at": None, "updated_at": None}}


def new_entity(name, category):
    return {
        "name": name,
        "category": category,          # 人物 | 势力 | 世界观 | 物品
        "aliases": [],                  # 别名/异名(用于归并)
        "description": "",              # 一句话描述
        "motivation": "",               # 动机(人物)
        "persona": "",                  # 人设/性格(人物)
        "psychology": "",               # 心理状态/思想(人物)
        "related": [],                  # 相关实体名列表
        "first_seen": None,             # 首次出现章
        "appearance_scenes": [],        # 出现过的场景 [scene_id]
        "evolution": "",                # 发展轨迹/伏笔(全类通用)
    }


def load_kb(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                kb = json.load(f)
            # 兼容旧/缺字段
            kb.setdefault("entities", [])
            kb.setdefault("meta", EMPTY_KB["meta"])
            return kb
        except Exception:
            return json.loads(json.dumps(EMPTY_KB))
    return json.loads(json.dumps(EMPTY_KB))


def save_kb(path, kb):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kb["meta"]["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    if not kb["meta"].get("created_at"):
        kb["meta"]["created_at"] = kb["meta"]["updated_at"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


def _norm_aliases(entity):
    """实体的可匹配名集合: name + aliases。"""
    names = {str(entity.get("name", "")).strip()}
    for a in entity.get("aliases", []) or []:
        if a:
            names.add(str(a).strip())
    names.discard("")
    return names


def find_entity(kb, name, category=None):
    """按 name/alias 检索知识库里的实体。返回 (idx, entity) 或 (None, None)。"""
    name = (name or "").strip()
    if not name:
        return None, None
    for i, e in enumerate(kb["entities"]):
        if name in _norm_aliases(e):
            # category 限定则校验
            if category and e.get("category") != category:
                continue
            return i, e
    return None, None


def _merge_delta(entity, delta):
    """把 LLM 抽出的增量 delta 合并进实体(卷积: 非空增量拼接/追加, 不覆盖已有)。"""
    # 标量字段: 非空则追加描述(保留历史, 信息不丢)
    for f in ("description", "motivation", "persona", "psychology", "evolution"):
        dv = (delta.get(f) or "").strip()
        if dv:
            old = (entity.get(f) or "").strip()
            # 去重: 若已有内容包含新内容则不重复
            if old and dv in old:
                continue
            entity[f] = (old + "；" + dv) if old else dv
    # aliases 合并去重
    for a in delta.get("aliases", []) or []:
        a = str(a).strip()
        if a and a not in entity["aliases"] and a != entity["name"]:
            entity["aliases"].append(a)
    # related 合并去重
    for r in delta.get("related", []) or []:
        r = str(r).strip()
        if r and r not in entity["related"] and r != entity["name"]:
            entity["related"].append(r)
    # first_seen: 取最早
    fs = delta.get("first_seen")
    if fs:
        try:
            fs = int(fs)
        except Exception:
            fs = None
        if fs and (not entity.get("first_seen") or fs < entity["first_seen"]):
            entity["first_seen"] = fs
    # appearance_scenes 追加去重
    for s in delta.get("appearance_scenes", []) or []:
        if s not in entity["appearance_scenes"]:
            entity["appearance_scenes"].append(s)


# ======================================================================
# LLM: 从场景抽取实体信息增量
# ======================================================================
KB_SYSTEM = (
    "你是小说拆书师, 负责从每个场景中识别'设定实体'并抽取其信息增量。"
    "设定实体分为: 人物/势力/世界观(规则,力量体系,地点)/物品。"
    "只输出 JSON。")

KB_USER = """下面是第{chapter_no}章一个场景的抽取结果(人物/who, 地点/where, 行为/actinfo, 备注/notes)。

【场景】第{chapter_no}章 scene{scene_id}: {where}
【人物】{who}
【行为事件】{actinfo}
【备注】{notes}

【任务】分析本场景, 识别涉及的设定实体, 抽取每个实体的**信息增量**:
- 人物: 出场角色; 抽取其 动机(motivation)/人设(persona)/心理状态(psychology)/与其他实体的关系(related)
- 势力: 出现的组织/势力; 抽取其 描述(description)/成员/目的/关系
- 世界观: 出现的规则/力量体系/地点/历史; 抽取其 描述(description)/机制
- 物品: 出现的道具/物品; 抽取其 描述(description)/来历/功能

【输出】严格 JSON 数组, 每个元素一个实体的增量, 字段可缺省(不清楚就空):
{{
  "name": "实体名",
  "category": "人物|势力|世界观|物品",
  "aliases": ["别名/异名, 无则[]"],
  "description": "一句话描述(该场景新增的信息)",
  "motivation": "人物动机(该场景体现的)",
  "persona": "人物性格/人设(该场景体现的)",
  "psychology": "人物心理状态/思想(该场景体现的)",
  "related": ["本场景中与其互动的实体名, 无则[]"],
  "evolution": "本场景体现的发展/伏笔线索, 无则空串"
}}

要求:
- 只抽本场景真实出现的实体; 信息只能来自输入, 不编造。
- category 要准确判断(人物/势力/世界观/物品)。
- 每个实体尽量填足已知字段, 不要只给 name; 信息不足就留空, 不要臆造。
只输出 JSON 数组。"""


def extract_entities(scene, base, model, who, where, actinfo, notes):
    """调 LLM 从场景抽实体信息增量。返回 [(delta_dict), ...], 失败返回 []。"""
    import json as _j
    if not (who or actinfo or where):
        return []
    user = KB_USER.format(
        chapter_no=scene.get("chapter_no", "?"),
        scene_id=scene.get("scene_id", "?"),
        who=_j.dumps(who or [], ensure_ascii=False)[:800],
        actinfo=_j.dumps(actinfo or [], ensure_ascii=False)[:1500],
        notes=str(notes or "")[:300],
        where=str(where or "未明示"),
    )
    try:
        raw = llm_client.chat(KB_SYSTEM, user, json_mode=True, num_predict=1500,
                              temperature=0.2)[0]
        d = _j.loads(raw)
        if isinstance(d, dict):
            for k in ("entities", "data", "result"):
                if isinstance(d.get(k), list):
                    d = d[k]
                    break
        if not isinstance(d, list):
            return []
        return [x for x in d if isinstance(x, dict)]
    except Exception:
        return []


# ======================================================================
# 卷积: 场景 -> 更新知识库
# ======================================================================
def convolve_scene(scene, kb, deltas):
    """把一个场景的实体增量卷积进知识库。"""
    scene_id = scene.get("scene_id")
    chapter_no = scene.get("chapter_no")
    for delta in deltas:
        name = str(delta.get("name", "")).strip()
        if not name:
            continue
        category = str(delta.get("category", "")).strip() or "人物"
        idx, entity = find_entity(kb, name, category)
        if entity is None:
            entity = new_entity(name, category)
            kb["entities"].append(entity)
            idx = len(kb["entities"]) - 1
        # 记录出现场景
        if scene_id not in entity["appearance_scenes"]:
            entity["appearance_scenes"].append(scene_id)
        delta = dict(delta)
        delta["appearance_scenes"] = entity["appearance_scenes"]
        _merge_delta(entity, delta)
        if not entity.get("first_seen"):
            entity["first_seen"] = chapter_no
    return kb


def build_kb_from_scenes(scenes, base, model, kb_path):
    """对场景列表做卷积式知识库累积。返回 (kb, 处理场景数, 实体数)。"""
    kb = load_kb(kb_path)
    processed = 0
    for sc in scenes:
        deltas = extract_entities(sc, base, model,
                                  sc.get("who"), sc.get("where"),
                                  sc.get("actinfo"), sc.get("notes"))
        if deltas:
            convolve_scene(sc, kb, deltas)
        processed += 1
        if processed % 10 == 0:
            print(f"  知识库卷积: 已处理 {processed} 场景, 实体 {len(kb['entities'])} 个")
    save_kb(kb_path, kb)
    return kb, processed, len(kb["entities"])