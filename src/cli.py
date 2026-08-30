# -*- coding: utf-8 -*-
"""
cli.py ——小说拆书工程统一命令行入口
所有路径/章节数/模型都从 settings.json + models.json 读取（见 config.py / llm_client.py），
这里只做任务编排与目录约定。
  - data/     中间产物：Stage1 数据库、向量缓存、LLM 直出 JSON、调试缓存（可跨次运行复用）
  - outputs/  每次运行的可视化产物：outputs/<小说名>_<日期>/  下全是自包含 HTML + 数据 JSON

用法:
  python src/cli.py <任务> [--backend 预设名] [--src DIR] [--chapters N] [--model M] [--key K]
  python src/cli.py --list-backends                    # 列出全部模型预设
  python src/cli.py mine --role review=glm-4-flash     # 评述阶段用更强模型

任务:
  extract       Stage1 分场景抽取(读小说TXT -> SQLite)
  feel          Stage1 主agent读感(资深读者) + 设定子agent图谱 (含 doubt_index 思考深度)
  collect       Stage1 四维并行收集(设定/暗线/人物/文风采样)
  mine          Stage2 四维并行挖掘(剧情推理/设定强化/人物简历/文风分析)
  annotate      创作解析标注(关键场景 plot_function/note, 拉片画布🎬)
  review        全板块编辑评述(专业编辑视角 habits/strengths/weaknesses/summary)
  build         Stage2 LLM 直出 章纲/人物/设定/总结 (JSON 产物)
  personality   生成人物性格六维向量 (雷达图数据 需 LLM)
  graph         知识图谱 (角色关系 + 设定, 自包含HTML)
  detail        拆书详情页(四Tab 全景, 自包含HTML)
  report        本地 vs 云端 对比报告
  rag "<问题>"  RAG 问答
  viz        一键：详情页 + 图谱 + 报告 + 导航页 -> outputs/<小说名>_<日期>/
  analyze       跑批日志健康度分析(H1-H5)

全局参数:
  --backend <预设名>   选择主 LLM 预设 (枚举见 models.json; --list-backends 可查)
  --role role=预设     角色级模型覆盖, 可多次 (如 --role review=glm-4-flash)
  --model   覆盖模型名       --key 覆盖 API key (云端)
  --chapters N             处理章数 (默认从 settings.json 读 chapters)
  --out-dir DIR            产物输出目录 (中间产物默认 data/llm_50/<backend>)
  --src DIR                读哪个 LLM 产物目录 (可视化任务专用, 默认跟随 settings.json 的 run.src_dir)

无参运行: 进入交互菜单 (选择后端 + 任务)。"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import llm_client
import webnovel_lexicon
import style_baseline

ROOT = C.PROJECT_ROOT
SRC = os.path.join(ROOT, "src")
DATA = C.DATA_DIR
OUTPUT = C.OUTPUT_DIR                                   # 本次运行的可视化产物目录
MODELS = os.path.join(ROOT, "models.json")

# 预设兜底(与 models.json 一致; 注册表缺失时用, 保证 cli 仍可跑)
DEFAULTS = {
    "dots3-note": {"backend": "openai", "base_url": "https://note3-prev-api.askdiandian.com",
                   "model": "dots3-note-prev", "auth_scheme": "apikey", "enable_thinking": False,
                   "name": "小红书 dots.ai (默认)"},
    "glm-4-flash": {"backend": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-4.7-flash", "auth_scheme": "bearer", "enable_thinking": False,
                    "name": "GLM-4.7-Flash (智谱, 免费)"},
    "qwen3-8b": {"backend": "ollama", "base_url": "http://localhost:11434",
                 "model": "qwen3:8b", "auth_scheme": "none", "enable_thinking": False,
                 "name": "本地 qwen3:8b (零成本/离线)"},
}


def load_cfg():
    """读 models.json presets + routing（注册表缺失时用 DEFAULTS）。
    models.json 优先覆盖 DEFAULTS(同名字段以注册表为准)。"""
    presets = dict(DEFAULTS)
    routing = {}
    try:
        with open(MODELS, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in (cfg.get("presets") or {}).items():
            merged = dict(presets.get(k, {}))
            merged.update(v)          # 注册表字段覆盖 DEFAULTS
            presets[k] = merged
        routing = cfg.get("routing") or {}
    except Exception:
        pass
    return {"presets": presets, "routing": routing}


def _resolve_backend_name(backend):
    """预设名归一化: 兼容历史名(local/cloud/xiaohongshu/glm/qwen3)。"""
    return llm_client.LEGACY_ALIAS.get(backend, backend)


def backend_env(backend, model=None, key=None, preset=None, role_models=None):
    """根据 backend 构造LLM_* 环境变量字典(不污染os.environ)。
    role_models: {"review": "glm-4-flash"} -> 序列化进 LLM_ROLE_MODELS 供子进程角色路由。"""
    cfg = preset or load_cfg()
    name = _resolve_backend_name(backend)
    p = (cfg.get("presets") or {}).get(name) or DEFAULTS.get(name)
    env = dict(os.environ)
    env["LLM_BACKEND"] = p.get("backend", "ollama")
    env["LLM_BASE_URL"] = p.get("base_url", DEFAULTS["dots3-note"]["base_url"])
    env["LLM_MODEL"] = model or p.get("model", C.EXTRACT_MODEL)
    # 深度思考开关: 默认关闭(省 token/加速); 预设可显式开启
    env["LLM_ENABLE_THINKING"] = "true" if p.get("enable_thinking", False) else "false"
    scheme = p.get("auth_scheme", "none")
    if scheme != "none":
        env["LLM_AUTH_SCHEME"] = scheme
        key = key or os.environ.get("LLM_API_KEY", C.LLM_API_KEY or "")
        if not key:
            print(f"⚠️ 后端 [{name}] 需要API key:")
            print(f"   方式1: 环境变量  set LLM_API_KEY=你的key")
            print(f"   方式2: 参数      python src/cli.py ... --key 你的key")
            print(f"   方式3: settings.json 里的 llm.api_key")
            sys.exit(2)
        env["LLM_API_KEY"] = key
    # 角色级模型覆盖(透传子进程)
    if role_models:
        env["LLM_ROLE_MODELS"] = ",".join(f"{r}={p2}" for r, p2 in role_models.items())
    # 运行时清晰提示: 当前后端/模型/能力摘要
    cost_tag = {"free": "🆓免费", "paid": "💰付费"}.get(p.get("cost"), "")
    ctx_tag = f"{p.get('ctx')}" if p.get("ctx") else "?"
    print(f"▶ 后端 [{name}] {p.get('name','')} {cost_tag}")
    print(f"   model={p.get('model')} | 后端类型={p.get('backend')} | "
          f"上下文={ctx_tag} | 并发={p.get('concurrency','?')} | "
          f"JSON模式={'开' if p.get('json_mode', True) else '关'}")
    if role_models:
        print(f"   角色覆盖: " + ", ".join(f"{r}→{pp}" for r, pp in role_models.items()))
    return env


def run(script, args, env):
    cmd = [sys.executable, "-u", os.path.join(SRC, script)] + args
    print(f"\n$ {os.path.relpath(cmd[2], ROOT)} {' '.join(args)}")
    t0 = time.time()
    rc = subprocess.call(cmd, env=env)
    print(f"\n⏱ 子任务 [{script}] 结束, 耗时 {time.time()-t0:.1f}s, exit={rc}")
    return rc


# ---------------- 任务实现 ----------------
def cmd_extract(args, env):
    return run("main.py", ["--chapters", str(args.chapters),
                           "--extract-mode", args.extract_mode], env)


def cmd_feel(args, env):
    """Stage1 主agent读感 + 设定子agent图谱。doubt_index 从 llm.config.json 顶层读取。"""
    argv = ["--chapters", str(args.chapters)]
    if args.doubt_index is not None:
        argv += ["--doubt-index", str(args.doubt_index)]
    return run("stage1_feel.py", argv, env)


def cmd_collect(args, env):
    """Stage1 四维度并行收集(设定/暗线/人物/文风采样)。--fresh: 全量重抽设定(删旧 settings_graph 增量)。"""
    argv = ["--chapters", str(args.chapters), "--parallel", str(args.parallel or 4)]
    if args.doubt_index is not None:
        argv += ["--doubt-index", str(args.doubt_index)]
    if getattr(args, "fresh", False):
        env = dict(env) if env else {}
        env["COLLECT_FRESH"] = "1"
        print("[collect] --fresh: 删除旧 settings_graph, 设定将全量重抽(不再增量归并)")
    return run("stage1_collect.py", argv, env)


def cmd_mine(args, env):
    """Stage2 四维度并行挖掘(剧情推理/设定强化/人物简历/文风分析)。"""
    argv = ["--chapters", str(args.chapters), "--parallel", str(args.parallel or 4)]
    if args.doubt_index is not None:
        argv += ["--doubt-index", str(args.doubt_index)]
    return run("stage2_mine.py", argv, env)


def cmd_build(args, env):
    # stage2 产物默认写到 outputs/<书>_<日期>/stage2/ (含 rag_answers)
    out = args.out_dir or C.STAGE2_DIR
    return run("stage2.py",
               ["--chapters", str(args.chapters), "--all-outlines", "--characters",
                "--settings", "--summary", "--out-dir", out], env)


def cmd_annotate(args, env):
    """创作解析标注(P2): 挑关键场景 -> LLM 标 plot_function/note -> stage1/scenes_annotations.json"""
    import scene_annotator
    s1 = args.src if args.src and os.path.isdir(args.src) else None
    if not s1:
        import stage2_mine as M
        s1 = M._latest_stage1_dir()
    print(f"标注目标 stage1: {s1}")
    r = scene_annotator.annotate_file(
        os.path.join(s1, "scenes_meta.json"),
        os.path.join(s1, "clue_graph.json"),
        os.path.join(s1, "scenes_annotations.json"),
        None, None)
    print(f"✅ 标注完成: {len(r['scenes'])} 个关键场景 -> scenes_annotations.json")
    return 0


def cmd_review(args, env):
    """全板块编辑评述(P3): 专业编辑视角给每个板块 habits/strengths/weaknesses/summary"""
    import editor_review
    s1 = args.src if args.src and os.path.isdir(args.src) else None
    if not s1:
        import stage2_mine as M
        s1 = M._latest_stage1_dir()
    print(f"评述目标 stage1: {s1}")
    r = editor_review.review_all(s1)
    for k, v in r["reviews"].items():
        print(f"  [{k}] {v.get('summary', '')[:60]}")
    print(f"✅ 评述完成: {len(r['reviews'])} 个板块 -> editor_reviews.json")
    return 0


def cmd_personality(args, env):
    return run("gen_personality.py", ["--src", args.src or args.backend], env)


def cmd_graph(args, env):
    base = args.out_dir or C.STAGE2_DIR
    return run("export_graph.py",
               ["--db", C.DB_PATH,
                "--src", base, "--out", OUTPUT], env)


def cmd_detail(args, env):
    base = args.out_dir or C.STAGE2_DIR
    run("gen_personality.py", ["--src", args.src or args.backend], env)   # 性格六维(有缓存则秒过)
    return run("build_detail.py",
               ["--src", args.src or args.backend, "--out", os.path.join(OUTPUT, "detail_data.json")], env)


def cmd_report(args, env):
    return run("final_report.py", [], env)


def cmd_rag(args, env):
    return run("rag_qa.py", ["--chapters", str(args.chapters), args.question], env)


def cmd_stage3(args, env):
    """一键：拆人/拆章/设定关系 -> outputs/<小说名_<日期>/ + index.html 单页工作台"""
    src_dir = args.src or args.backend
    os.makedirs(OUTPUT, exist_ok=True)
    print(f"═══一键生成可视化产物 -> {os.path.relpath(OUTPUT, ROOT)} ═══")
    run("build_detail.py", ["--src", C.STAGE2_DIR, "--out", os.path.join(OUTPUT, "detail_data.json")], env)
    run("export_graph.py",
        ["--db", C.DB_PATH,
         "--src", C.STAGE2_DIR, "--out", OUTPUT], env)
    run("gen_personality.py", ["--src", C.STAGE2_DIR], env)
    # RAG 结果聚合到 rag/ (若存在)
    try:
        import shutil, glob as _glob
        os.makedirs(C.RAG_DIR, exist_ok=True)
        for src in _glob.glob(os.path.join(C.STAGE2_DIR, "rag_answers.json")):
            shutil.copy2(src, os.path.join(C.RAG_DIR, "rag_answers.json"))
    except Exception as e:
        print(f"[warn] RAG 聚合失败: {e}")
    return archive_stage3()


def archive_stage3():
    """生成 outputs/<小说名_<日期>/index.html —— 单页 Tab 版拆书工作台(四维)。
    模块: 剧情推理(结论→证据簇→原文) / 设定体系 / 人物简历 / 文风分析。
    内嵌 detail_data + graph_data + personality + clue_graph(剥向量) + 四维产物,
    单文件自包含, 零外部依赖。模板: src/index_template.html (占位符注入)。"""
    import re as _re
    # stage1 产物目录: 优先当前时间戳; 否则取最新含 clue_graph 的目录
    # 注: 原为 `s1_dir = s1_dir`(自赋值, 无全局定义时会抛 UnboundLocalError),
    #     使本函数无法脱离 cmd_stage3 独立调用。改为显式默认 C.STAGE1_DIR。
    s1_dir = C.STAGE1_DIR
    if not os.path.exists(os.path.join(s1_dir, "clue_graph.json")):
        outputs = os.path.join(C.PROJECT_ROOT, "outputs")
        cands = []
        if os.path.exists(outputs):
            for d in os.listdir(outputs):
                p = os.path.join(outputs, d, "stage1")
                if os.path.exists(os.path.join(p, "clue_graph.json")):
                    try:
                        mt = os.path.getmtime(os.path.join(p, "clue_graph.json"))
                    except Exception:
                        mt = 0
                    cands.append((mt, p))
        cands.sort(key=lambda x: -x[0])
        if cands:
            # 优先当前小说
            s1_dir = next((p for _mt, p in cands if C.NOVEL_NAME in p), cands[0][1])
    # ---- 读取数据 ----
    detail = {}
    graph = {}
    personality = {}
    feelings = {"entries": []}
    try:
        with open(os.path.join(OUTPUT, "detail_data.json"), encoding="utf-8") as f:
            detail = json.load(f)
    except Exception:
        pass
    try:
        with open(os.path.join(OUTPUT, "graph_data.json"), encoding="utf-8") as f:
            graph = json.load(f)
    except Exception:
        pass
    try:
        with open(os.path.join(OUTPUT, "personality.json"), encoding="utf-8") as f:
            for p in json.load(f):
                personality[p.get("name")] = p.get("dims", {})
    except Exception:
        pass
    # 读感(stage1 产物, 可选)
    feel_path = os.path.join(s1_dir, "reader_feelings.json")
    if os.path.exists(feel_path):
        try:
            with open(feel_path, encoding="utf-8") as f:
                feelings = json.load(f)
        except Exception:
            pass
    # 线索图谱(结论/证据/簇/实体) —— 剥掉向量(98%体积), 只留结构
    clue_graph = {"schema": 1, "evidence": [], "clusters": [], "conclusions": [],
                  "entities": [], "relations": []}
    clue_path = os.path.join(s1_dir, "clue_graph.json")
    if os.path.exists(clue_path):
        try:
            with open(clue_path, encoding="utf-8") as f:
                cg = json.load(f)
            for e in cg.get("evidence", []):
                e.pop("vector", None)
            clue_graph = cg
        except Exception:
            pass
    # 拉片画布数据: scenes_meta(截断原文/预计算情绪/证据锚点) + 伏笔-回收链
    scenes_meta = []
    sm_path = os.path.join(s1_dir, "scenes_meta.json")
    if os.path.exists(sm_path):
        try:
            with open(sm_path, encoding="utf-8") as f:
                scenes_meta = json.load(f).get("scenes", [])
        except Exception:
            pass
    ev_by_scene = {}
    for _e in clue_graph.get("evidence", []):
        ev_by_scene.setdefault(str(_e.get("scene_id")), []).append(_e.get("id"))
    try:
        import style_sampler as _ss
        for _s in scenes_meta:
            rt = (_s.get("raw_text") or "")
            _s["raw_text"] = rt[:150]                       # 控制注入体积
            _s["sentiment"] = _ss._sentiment(rt)
            _s["n_ev"] = len(ev_by_scene.get(str(_s.get("scene_id")), []))
            _s["ev_ids"] = ev_by_scene.get(str(_s.get("scene_id")), [])[:3]
    except Exception:
        pass
    # 伏笔-回收链: 同一簇证据跨章 >=5 章
    foreshadow = []
    for _c in clue_graph.get("clusters", []):
        mids = _c.get("member_ids") or []
        chs = sorted({e.get("chapter_no") for e in clue_graph.get("evidence", [])
                      if e.get("id") in mids and e.get("chapter_no")})
        if len(chs) >= 2 and (chs[-1] - chs[0]) >= 5:
            foreshadow.append({"cluster_id": _c.get("id"),
                               "span": [chs[0], chs[-1]],
                               "n_ev": len(mids),
                               "weight": _c.get("weight", 0)})
    foreshadow.sort(key=lambda x: -x["weight"])

    # 文风分析
    style_data = None
    style_path = os.path.join(s1_dir, "style_analysis.json")
    if os.path.exists(style_path):
        try:
            with open(style_path, encoding="utf-8") as f:
                style_data = json.load(f)
        except Exception:
            pass
    # 人物简历(四维)
    resumes = []
    resumes_path = os.path.join(s1_dir, "characters_resume.json")
    if os.path.exists(resumes_path):
        try:
            with open(resumes_path, encoding="utf-8") as f:
                resumes = json.load(f).get("characters", [])
        except Exception:
            pass
    # 设定体系(四维)
    settings_system = None
    sys_path = os.path.join(s1_dir, "settings_system.json")
    if os.path.exists(sys_path):
        try:
            with open(sys_path, encoding="utf-8") as f:
                settings_system = json.load(f)
        except Exception:
            pass
    # 设定图谱(全量词条+关系) —— 剥掉向量(占 95%+ 体积), 只留释义与结构
    settings_graph = {"schema": 1, "terms": [], "relations": []}
    sg_path = os.path.join(s1_dir, "settings_graph.json")
    if os.path.exists(sg_path):
        try:
            with open(sg_path, encoding="utf-8") as f:
                sg = json.load(f)
            for t in sg.get("terms", []):
                t.pop("vector", None)
            settings_graph = {"schema": 1,
                              "terms": sg.get("terms", []),
                              "relations": sg.get("relations", [])}
            # 公版体系图 RAG 打标(幂等, 零 LLM 成本): 让已有产物也完成双库互链
            try:
                webnovel_lexicon.annotate_terms(settings_graph["terms"])
            except Exception:
                pass
        except Exception:
            pass
    # 人物事实(语录/行为/共现) —— 简历页的"言行"素材
    char_facts = {}
    cf_path = os.path.join(s1_dir, "character_facts.json")
    if os.path.exists(cf_path):
        try:
            with open(cf_path, encoding="utf-8") as f:
                char_facts = json.load(f).get("characters", {})
        except Exception:
            pass
    # 实体注册表(别名→规范名, 供内链跳转与去重)
    entity_reg = {}
    er_path = os.path.join(s1_dir, "entity_registry.json")
    if os.path.exists(er_path):
        try:
            with open(er_path, encoding="utf-8") as f:
                er = json.load(f)
            for _ch, lst in (er.items() if isinstance(er, dict) else []):
                for e in lst:
                    canon = e.get("canonical")
                    if not canon:
                        continue
                    rec = entity_reg.setdefault(
                        canon, {"canonical": canon, "aliases": set(),
                                "category": e.get("category", "")})
                    for a in (e.get("aliases") or []):
                        if a and a != canon:
                            rec["aliases"].add(a)
                    if not rec["category"]:
                        rec["category"] = e.get("category", "")
            entity_reg = {k: {"canonical": v["canonical"],
                              "aliases": sorted(v["aliases"]),
                              "category": v["category"]}
                          for k, v in entity_reg.items()}
        except Exception:
            pass
    # 公版通用名词词库(跨书共享资产, 与 setting_agent 用同一份文件)
    generic_lexicon = {}
    lex_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generic_lexicon.json")
    if os.path.exists(lex_path):
        try:
            with open(lex_path, encoding="utf-8") as f:
                generic_lexicon = json.load(f)
        except Exception:
            pass

    chapters = detail.get("chapters") or graph.get("chapters") or []
    characters = detail.get("characters") or []
    settings = detail.get("settings") or []
    edges = graph.get("edges") or []
    book = detail.get("book") or {}
    total_chars = book.get("totalChapters", len(chapters))
    n_concl = len(clue_graph.get("conclusions", []))
    n_ev = len(clue_graph.get("evidence", []))
    n_sg_term = len(settings_graph.get("terms", []))
    n_sg_rel = len(settings_graph.get("relations", []))
    meta = (f"{total_chars} 章 · {len(resumes)} 人物简历 · {n_sg_term} 设定词条 · "
            f"{n_sg_rel} 设定关系 · {n_concl} 推理结论 · {n_ev} 暗线证据 · "
            f"{len(settings_system.get('layers', [])) if settings_system else 0} 设定层级")

    def _js(v):
        return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")

    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_template.html")
    html = open(tpl, encoding="utf-8").read()
    html = html.replace("__DETAIL_DATA__", _js(detail))
    html = html.replace("__GRAPH_DATA__", _js(graph))
    html = html.replace("__PERSONALITY_DATA__", _js(personality))
    html = html.replace("__FEELINGS_DATA__", _js(feelings))
    html = html.replace("__CLUE_GRAPH__", _js(clue_graph))
    html = html.replace("__STYLE_DATA__", _js(style_data))
    html = html.replace("__RESUMES_DATA__", _js(resumes))
    html = html.replace("__SETTINGS_SYSTEM__", _js(settings_system))
    html = html.replace("__SETTINGS_GRAPH__", _js(settings_graph))
    html = html.replace("__CHAR_FACTS__", _js(char_facts))
    html = html.replace("__ENTITY_REG__", _js(entity_reg))
    html = html.replace("__GENERIC_LEXICON__", _js(generic_lexicon))
    # 公版体系图 RAG: 词表映射(供面板打"网文体系"标签) + 补档建议(供概览页提示缺档)
    wn_map, wn_suggest = {}, []
    try:
        for _n, _r in webnovel_lexicon.match_terms(
                [x.get("name") for x in settings_graph.get("terms", [])]).items():
            wn_map[_n] = _r
        wn_suggest = webnovel_lexicon.suggest_missing(
            [x.get("name") for x in settings_graph.get("terms", [])])
    except Exception:
        pass
    html = html.replace("__WEBNOVEL_LEXICON__", _js(wn_map))
    html = html.replace("__WN_SUGGEST__", _js(wn_suggest))
    # 文风跨书对比基准(W3): genre 桶 + all 桶; 无数据时为空 dict, 面板显示"待积累"
    try:
        _bl = style_baseline.get(os.getenv("NOVEL_GENRE", "").strip() or None)
    except Exception:
        _bl = {}
    html = html.replace("__STYLE_BL_OBJ__", _js(_bl))
    html = html.replace("__SCENES_META__", _js(scenes_meta))
    html = html.replace("__FORESHADOW__", _js(foreshadow))
    # 创作解析标注(P2): 关键场景 plot_function/note
    scene_annot = []
    sa_path = os.path.join(s1_dir, "scenes_annotations.json")
    if os.path.exists(sa_path):
        try:
            with open(sa_path, encoding="utf-8") as f:
                scene_annot = json.load(f).get("scenes", [])
        except Exception:
            pass
    html = html.replace("__SCENE_ANNOTATIONS__", _js(scene_annot))
    # 全板块编辑评述(P3)
    editor_reviews = {}
    er_path = os.path.join(s1_dir, "editor_reviews.json")
    if os.path.exists(er_path):
        try:
            with open(er_path, encoding="utf-8") as f:
                editor_reviews = json.load(f).get("reviews", {})
        except Exception:
            pass
    html = html.replace("__EDITOR_REVIEWS__", _js(editor_reviews))
    html = html.replace("__NOVEL_NAME__", C.NOVEL_NAME)
    html = html.replace("__META__", meta)
    with open(os.path.join(OUTPUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅已生成单页 Tab 工作台 -> {os.path.join(OUTPUT, 'index.html')}")


def cmd_analyze(args, env):
    """跑批日志分析: 读 logs/run_latest.jsonl 输出健康报告(阶段耗时/失败/进度/指标曲线)。"""
    import analyze_log
    return analyze_log.analyze(args.log if getattr(args, "log", None) else None)


def interactive():
    cfg = load_cfg()
    presets = cfg["presets"]
    print("══ 小说拆书工程 · 交互菜单 ══\n")
    print("选择 LLM 后端:")
    names = list(presets.keys())
    for i, k in enumerate(names, 1):
        p = presets[k]
        tag = {"free": "🆓", "paid": "💰"}.get(p.get("cost"), "")
        local = "🏠本地" if p.get("backend") == "ollama" else ""
        print(f"  {i}. {k} —{p.get('name', '')}{tag}{local}")
    try:
        sel = int(input("> ").strip() or "1")
        backend = names[sel - 1] if 1 <= sel <= len(names) else names[0]
    except Exception:
        backend = names[0]
    print(f"\n选择任务:")
    tasks = [("extract", "Stage1 抽取"), ("collect", "Stage1 四维收集(设定/暗线/人物/文风)"),
             ("mine", "Stage2 四维挖掘(推理/设定强化/简历/文风)"),
             ("annotate", "创作解析标注(关键场景🎬)"),
             ("review", "全板块编辑评述(专业编辑视角)"),
             ("build", "Stage2 直出(章纲/人物/设定)"),
             ("personality", "性格六维"), ("graph", "知识图谱"),
             ("detail", "拆书详情页"), ("viz", "一键生成可视化"),
             ("report", "对比报告"), ("rag", "RAG 问答")]
    for i, (_, label) in enumerate(tasks, 1):
        print(f"  {i}. {label}")
    try:
        t = int(input("> ").strip() or "6")
        task, _ = tasks[t - 1] if 1 <= t <= len(tasks) else tasks[5]
    except Exception:
        task = "viz"
    args = argparse.Namespace(backend=backend, chapters=C.CHAPTERS, model=None,
                              key=None, out_dir=None, question="", src=None,
                              doubt_index=None, parallel=4)
    print(f"\n→后端: {backend} | 任务: {task}")
    env = backend_env(backend, args.model, args.key, cfg)
    HANDLERS[task](args, env)


# ---------------- 入口 ----------------
HANDLERS = {
    "extract": cmd_extract, "feel": cmd_feel,
    "collect": cmd_collect, "mine": cmd_mine,
    "annotate": cmd_annotate, "review": cmd_review,
    "build": cmd_build, "personality": cmd_personality,
    "graph": cmd_graph, "detail": cmd_detail, "report": cmd_report,
    "rag": cmd_rag, "viz": cmd_stage3, "analyze": cmd_analyze,
}

def list_backends():
    """打印全部模型预设与能力字段(--list-backends)。"""
    cfg = load_cfg()
    print("══ 模型预设注册表 (models.json) ══\n")
    for name, p in cfg["presets"].items():
        tag = {"free": "🆓免费", "paid": "💰付费"}.get(p.get("cost"), "")
        print(f"  [{name}] {p.get('name', '')} {tag}")
        print(f"      backend={p.get('backend')}  model={p.get('model')}  "
              f"ctx={p.get('ctx')}  json={p.get('json_mode')}  "
              f"thinking_off={not p.get('enable_thinking', False)}  "
              f"并发={p.get('concurrency')}")
        roles = p.get("roles") or []
        print(f"      适用角色: {', '.join(roles) if roles else '(未标注)'}")
        if p.get("notes"):
            print(f"      备注: {p['notes']}")
        print()
    r = cfg.get("routing") or {}
    print("角色路由(推荐默认, 可用 --role role=预设 覆盖):")
    for role, preset in r.items():
        if role.startswith("_"):
            continue
        print(f"  {role:10s} -> {preset}")

def main():
    if len(sys.argv) == 1:
        interactive()
        return
    p = argparse.ArgumentParser(description="小说拆书工程统一入口 (本地 Ollama / 云端 API 一键切换)")
    p.add_argument("task", nargs="?", default="viz", choices=list(HANDLERS) + [None],
                   help="任务(缺省 viz)")
    p.add_argument("question", nargs="?", default="", help="rag 问题")
    p.add_argument("--backend", default=C.LLM_BACKEND,
                   help="主 LLM 预设名(枚举见 models.json; 兼容旧名 local/cloud/xiaohongshu/glm/qwen3)")
    p.add_argument("--list-backends", action="store_true", help="列出全部模型预设与能力字段后退出")
    p.add_argument("--role", action="append", default=None, metavar="ROLE=PRESET",
                   help="角色级模型覆盖, 可多次 (如 --role review=glm-4-flash 评述用强模型)")
    p.add_argument("--chapters", type=int, default=C.CHAPTERS)
    p.add_argument("--extract-mode", dest="extract_mode", default="v2",
                   choices=["two", "single", "v2", "attention"],
                   help="抽取模式: v2(默认) | attention(读者注意力动态切分)")
    p.add_argument("--model", default=None, help="覆盖模型名)")
    p.add_argument("--key", default=None, help="云端 API key(优先于环境变量LLM_API_KEY)")
    p.add_argument("--out-dir", default=None, help="产物输出目录(中间产物默认 data/llm_50/<backend>)")
    p.add_argument("--src", default=None, help="读哪个 LLM 产物目录(可视化任务专用, 默认跟随 --backend 与 settings.json 的 run.src_dir)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None,
                   help="质疑指数 0-1 (collect/mine 任务: 控制簇触发阈值/思考深度; 默认 llm.config.json 顶层 doubt_index)")
    p.add_argument("--parallel", type=int, default=None,
                   help="场景级/简历并发数 (collect/mine 任务, 默认4)")
    p.add_argument("--fresh", action="store_true",
                   help="collect: 全量重抽设定(删除旧 settings_graph 增量, 全书重跑必须加)")
    p.add_argument("--log", default=None, help="analyze 任务: 指定日志 jsonl 路径(默认读最近会话 run_latest.jsonl)")
    p.add_argument("--genre", default=None,
                   help="小说类别(宫斗/修仙/诡秘/系统/灵异/都市…)。★global 通识库默认不加载; "
                        "指定后才按类别加载对应域作参考(补档/分类/候选), 与原文冲突一律以本次阅读到的为准")
    a = p.parse_args()
    if a.list_backends:
        list_backends()
        return
    if not a.task:
        interactive()
        return
    # analyze 不需要 LLM 后端(纯日志分析), 提前分发
    if a.task == "analyze":
        sys.exit(cmd_analyze(a, {}) or 0)
    cfg = load_cfg()
    resolved = _resolve_backend_name(a.backend)
    if resolved not in (cfg.get("presets") or {}):
        print(f"未知 backend: {a.backend} (可选: {', '.join(llm_client.preset_names())})")
        print("查看预设详情: python src/cli.py --list-backends")
        sys.exit(2)
    # 角色级模型覆盖: --role review=glm-4-flash -> env LLM_ROLE_MODELS
    role_models = {}
    if a.role:
        for item in a.role:
            if "=" not in item:
                print(f"[warn] 忽略无效 --role: {item} (格式 role=预设)")
                continue
            role, preset = item.split("=", 1)
            if _resolve_backend_name(preset) not in (cfg.get("presets") or {}):
                print(f"[warn] 忽略未知预设: {preset} (--role {item})")
                continue
            role_models[role.strip()] = _resolve_backend_name(preset)
        if role_models:
            print("[角色路由] " + ", ".join(f"{r}->{pp}" for r, pp in role_models.items()))
    env = backend_env(a.backend, a.model, a.key, cfg, role_models)
    # global 通识库作用域: 默认关闭, 指定 --genre 才按类别加载(经环境变量透传给子进程)
    if a.genre:
        try:
            dom = webnovel_lexicon.resolve_genre(a.genre)
            webnovel_lexicon.set_scope(enabled=True, domains=[dom])
            env["NOVEL_GENRE"] = dom
            print(f"[global 通识库] 按类别加载: {dom}  (输入: {a.genre})")
            print("               用途限定: 补档/分类/候选参考; 与原文冲突一律以本次阅读到的为准")
        except Exception as e:
            print(f"[warn] 类别库加载失败({e}), 本次不使用 global 通识库")
    else:
        print("[global 通识库] 未指定 --genre, 本次不加载(默认不对外开放)")
    sys.exit(HANDLERS[a.task](a, env) or 0)


if __name__ == "__main__":
    main()
