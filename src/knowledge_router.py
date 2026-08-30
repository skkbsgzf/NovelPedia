# -*- coding: utf-8 -*-
"""knowledge_router.py —— 知识路由器: 决定"何时引入外部通识知识库"。

设计原则（对应用户要求）:
  1. **默认不加载**: 遵守"global 库默认不对外开放"策略, 不指定 --genre 时完全不引入外部知识。
  2. **观察 ≠ 加载**: 用 `webnovel_lexicon.peek()`(不受作用域限制)静静观察抽取过程,
     但不写入任何 wn_* 字段、不影响抽取结果 —— 观察本身零污染。
  3. **中途发现可用**: 跑着跑着发现"这本书跟序列体系有关", 达到阈值才引入对应域（增量发现 → 动态加载）。
  4. **阈值触发搜索**: 归类困难 / 定义反复冲突 / 缺档过多 时, 判定"该搜一下这是什么梗了"。
  5. **可解释**: 每次触发都记录 rule + evidence, 能回答"为什么引入了这个库"。

触发规则（阈值可在 settings.json 或构造参数调整）:
  R0 explicit       显式 --genre 指定(最高优先级, 跳过阈值)
  R1 domain_hits    同一域累计命中 >= 3 个词条  -> 判定本书属于该流派
  R2 unknown_pile   无法归类(category 空/other/行为)累计 >= 8 -> 需要外部参照
  R3 missing_gaps   某有序体系缺档 >= 2 -> 加载该域辅助补全
  R4 def_conflict   同一词条定义反复被追加不同表述 >= 3 次 -> 需要外部澄清
  R5 meme_signal    疑似"梗"信号累计 >= 2 -> 需要外部查梗(外部搜索层)

外部搜索分层(L0-L3), 见 search_external():
  L0 不搜(默认) → L1 本文/local 库 → L2 global 通识库(按域) → L3 联网(可插拔钩子, 默认关闭)
"""
import os
import re

import webnovel_lexicon

DEFAULT_THRESHOLDS = {
    "domain_hits": 3,     # R1
    "unknown_pile": 8,    # R2
    "missing_gaps": 2,    # R3
    "def_conflict": 3,    # R4
    "meme_signal": 2,     # R5
}

# 归类困难: 这些 category 视为"未归类"(LLM 自由发挥时的兜底分类)
UNKNOWN_CATEGORIES = {"", "其他", "other", "行为", "未知", None}

# 疑似"梗"的信号: 口语化/网络化表述(非世界观体系词, 更像圈内黑话)
MEME_PATTERNS = [
    r"^[A-Za-z]{2,8}$",                    # 纯英文缩写(如 gg/ntr/mo)
    r".*(梗|玩梗|名场面|名台词|老梗|烂梗)$",
    r"^(退婚|废柴|扮猪|打脸|迪化|狗粮|发糖|虐心|毒点|爽点|金手指)$",
    r".*(套路|操作|骚操作|神展开|反套路)$",
]


class KnowledgeRouter:
    """观察抽取过程 → 按需唤醒外部通识知识。"""

    def __init__(self, thresholds=None, genre=None, verbose=True):
        self.th = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.th.update(thresholds)
        self.verbose = verbose
        self.domain_hits = {}     # 域 -> 命中词条数
        self.unknown = 0          # 无法归类计数
        self.meme = 0             # 疑似梗计数
        self.def_count = {}       # term -> 定义追加次数
        self.seen_terms = []      # 观察窗口(限量)
        self.log = []             # 触发日志
        self.loaded_domain = None
        self.triggered_rule = None

        # R0: 显式指定类别 -> 立即加载, 跳过阈值
        if genre:
            dom = webnovel_lexicon.resolve_genre(genre)
            self._load(dom, "R0 explicit", f"命令行指定 --genre={genre}")

    # ---------------- 观察 ----------------
    def observe(self, name, category=None, definition=None):
        """每抽到一个设定词条就喂进来。观察阶段不写任何 wn_* 字段(零污染)。"""
        if not name:
            return
        n = str(name).strip()
        self.seen_terms.append(n)
        if len(self.seen_terms) > 400:
            self.seen_terms = self.seen_terms[-400:]

        # R1: 探测是否属于某个已知体系
        hit = webnovel_lexicon.peek(n)
        if hit:
            self.domain_hits[hit["domain"]] = self.domain_hits.get(hit["domain"], 0) + 1

        # R2: 无法归类
        if category in UNKNOWN_CATEGORIES:
            self.unknown += 1

        # R4: 定义反复追加(用 "；" 切分后条数近似追加次数)
        if definition:
            parts = [x for x in str(definition).split("；") if x.strip()]
            if len(parts) >= 2:
                self.def_count[n] = max(self.def_count.get(n, 0), len(parts))

        # R5: 疑似梗
        if self._looks_like_meme(n):
            self.meme += 1

    @staticmethod
    def _looks_like_meme(name):
        for pat in MEME_PATTERNS:
            try:
                if re.match(pat, str(name)):
                    return True
            except re.error:
                continue
        return False

    # ---------------- 决策 ----------------
    def decide(self):
        """返回是否该引入外部知识库 + 该加载哪个域 + 触发原因。"""
        if self.loaded_domain:
            return {"triggered": True, "domain": self.loaded_domain,
                    "rule": self.triggered_rule, "reason": "已加载", "evidence": {}}

        # R1 体系命中
        if self.domain_hits:
            dom, cnt = max(self.domain_hits.items(), key=lambda x: x[1])
            if cnt >= self.th["domain_hits"]:
                return {"triggered": True, "domain": dom, "rule": "R1 domain_hits",
                        "reason": f"中途发现本书属于「{dom}」体系(累计命中 {cnt} 个词条)",
                        "evidence": dict(self.domain_hits)}

        # R3 缺档(需要体系参照才能判断)
        gaps = self._detect_gaps()
        if gaps:
            best = max(gaps, key=lambda g: len(g["missing"]))
            if len(best["missing"]) >= self.th["missing_gaps"]:
                return {"triggered": True, "domain": best["domain"], "rule": "R3 missing_gaps",
                        "reason": f"「{best['domain']}」等级序列缺档 {len(best['missing'])} 个, 需体系参照补全",
                        "evidence": {"missing": best["missing"], "have": best["have"]}}

        # R2 归类困难
        if self.unknown >= self.th["unknown_pile"]:
            dom = self._best_domain_guess()
            if dom:
                return {"triggered": True, "domain": dom, "rule": "R2 unknown_pile",
                        "reason": f"{self.unknown} 个词条无法归类, 引入最相近体系作参照",
                        "evidence": {"unknown": self.unknown, "domain_hits": dict(self.domain_hits)}}

        # R4 定义冲突
        conflicted = {k: v for k, v in self.def_count.items() if v >= self.th["def_conflict"]}
        if conflicted:
            dom = self._best_domain_guess()
            if dom:
                return {"triggered": True, "domain": dom, "rule": "R4 def_conflict",
                        "reason": f"{len(conflicted)} 个词条定义反复不一致, 需外部澄清",
                        "evidence": {"terms": list(conflicted)[:5]}}

        # R5 梗信号(走外部搜索层, 不一定有对应域)
        if self.meme >= self.th["meme_signal"]:
            return {"triggered": True, "domain": None, "rule": "R5 meme_signal",
                    "reason": f"检测到 {self.meme} 个疑似'梗'信号, 建议走外部搜索层查证",
                    "evidence": {"meme_count": self.meme}, "needs_external": True}

        return {"triggered": False, "domain": None, "rule": None,
                "reason": "未达任何触发阈值, 继续使用纯本文知识"}

    def _detect_gaps(self):
        return webnovel_lexicon.suggest_missing(self.seen_terms)

    def _best_domain_guess(self):
        if not self.domain_hits:
            return None
        return max(self.domain_hits.items(), key=lambda x: x[1])[0]

    # ---------------- 执行 ----------------
    def _load(self, domain, rule, reason):
        if not domain:
            return False
        webnovel_lexicon.set_scope(enabled=True, domains=[domain])
        self.loaded_domain = domain
        self.triggered_rule = rule
        self.log.append({"rule": rule, "domain": domain, "reason": reason})
        if self.verbose:
            print(f"[知识路由器] {rule} 触发 → 加载域「{domain}」")
            print(f"            原因: {reason}")
            print(f"            用途: 补档/分类/候选参考; 与原文冲突一律以本次阅读到的为准")
        return True

    def step(self, name=None, category=None, definition=None, min_interval=1):
        """观察(可选) + 决策 + 按需加载。返回本次决策。"""
        if name:
            self.observe(name, category, definition)
        d = self.decide()
        if d.get("triggered") and d.get("domain") and not self.loaded_domain:
            self._load(d["domain"], d["rule"], d["reason"])
        return d

    def explain(self):
        """可解释性: 返回观察统计 + 触发日志。"""
        return {
            "thresholds": dict(self.th),
            "observed_terms": len(self.seen_terms),
            "domain_hits": dict(self.domain_hits),
            "unknown_pile": self.unknown,
            "meme_signal": self.meme,
            "def_conflicts": len([1 for v in self.def_count.values() if v >= self.th["def_conflict"]]),
            "loaded_domain": self.loaded_domain,
            "triggered_rule": self.triggered_rule,
            "log": self.log,
        }


# ======================================================================
# 外部搜索分层 (L0 → L3)
# ======================================================================
def search_external(query, layer=2, domain=None, local_terms=None):
    """分层外部搜索。**结果一律视为参考, 不覆盖本文事实。**

    L1 local    : 在本次已抽取的词条里找(零成本, 最先查)
    L2 global   : 在通识知识库按域检索(需已加载或临时指定 domain)
    L3 internet : 联网查证(可插拔钩子; 默认关闭, 通过环境变量 EXTERNAL_SEARCH=1 + 自定义接口启用)

    返回 {layer, hits:[{term,domain,slot,rank,score,how}], note}
    """
    q = str(query or "").strip()
    if not q:
        return {"layer": 0, "hits": [], "note": "空查询"}

    # L1 本文
    if layer >= 1 and local_terms:
        for t in local_terms:
            if str(t).strip() == q:
                return {"layer": 1, "hits": [{"term": q, "domain": "本书", "slot": "local",
                                              "rank": None, "score": 1.0, "how": "exact"}],
                        "note": "本文已抽取, 优先使用本文事实"}

    # L2 通识库
    if layer >= 2:
        prev_scope = webnovel_lexicon.get_scope()
        if domain:
            webnovel_lexicon.set_scope(enabled=True, domains=[domain])
        hits = webnovel_lexicon.search(q, topk=5)
        if domain:
            webnovel_lexicon.set_scope(enabled=prev_scope["enabled"], domains=prev_scope["domains"])
        if hits:
            return {"layer": 2, "hits": hits, "note": "通识库参考(与原文冲突以原文为准)"}

    # L3 联网(钩子)
    import config_schema as _CS
    if layer >= 3 and _CS.get("rag.external_search"):
        # TODO: 接入联网搜索接口(WebSearch API / 百科 API)。
        # 约定: 返回结构同 L2, 且 note 必须标注"来源: 联网"。
        return {"layer": 3, "hits": [], "note": "联网搜索钩子未接入(设置 rag.external_search 并实现本分支)"}

    return {"layer": 0, "hits": [], "note": "未命中任何层"}


if __name__ == "__main__":
    print("=== 模拟: 跑着跑着发现是诡秘体系 ===")
    r = KnowledgeRouter()
    for n, c in [("医生", "角色"), ("孕妇", "角色"), ("接生工具", "物品")]:
        r.step(n, c)
    print("  前3个词后:", r.decide()["reason"])
    for n, c in [("序列9", "力量体系"), ("魔药", "世界观"), ("非凡者", "角色"), ("扮演法", "规则")]:
        d = r.step(n, c)
    print("  加入体系词后:", d["rule"], "->", d["reason"][:50])
    print("  已加载域:", r.loaded_domain)
    print("  explain:", r.explain()["domain_hits"], r.explain()["log"])
    print()
    print("=== 模拟: 显式指定(跳过阈值) ===")
    r2 = KnowledgeRouter(genre="宫斗")
    print("  立即加载:", r2.loaded_domain)
    print()
    print("=== 模拟: 缺档触发 ===")
    r3 = KnowledgeRouter()
    for n in ["炼气", "元婴", "渡劫"]:
        r3.step(n, "力量体系")
    d3 = r3.decide()
    print("  ", d3["rule"], "->", d3["reason"][:60])
    print()
    print("=== 外部搜索分层 ===")
    print("  L2 查'魔药':", search_external("魔药", layer=2, domain="诡秘神秘学")["hits"][:2])
    print("  L1 优先:", search_external("医生", layer=2, local_terms=["医生", "孕妇"])["note"])
