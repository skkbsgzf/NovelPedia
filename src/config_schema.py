# -*- coding: utf-8 -*-
"""
config_schema.py —— 统一配置注册表（全工程行为开关的单一真相源）v2

分层来源(优先级 高→低): 运行时覆盖(set) > 环境变量 > settings.json > 内置默认

可插拔: 任何模块 import 本文件后调用 register() 自注册, get()/export_env() 自动生效。
新增一个开关 = 一行 register, 零改中心代码。

API:
  register(key, ...)       注册配置项(可插拔扩展点)
  get(key)                 读(合并优先级 + 类型转换 + 校验)
  set(key, value)          运行时覆盖(如 --genre 动态作用域 / set_scope)
  export_env(extra=None)   生成子进程环境变量 dict(自动收集带 env 的项)
  snapshot()               全部生效配置 dict(运行快照)
  validate_all()           fail-fast 校验(类型/枚举)
  markdown_table()         配置总览表格(自动生成文档, 防漂移)

零业务依赖: 只依赖 stdlib, 可被任何模块安全 import。
"""
import os
import json
import datetime

# ---------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------
_REGISTRY = {}          # key -> spec
_OVERRIDES = {}         # 运行时覆盖(优先级最高)
_ORDER = []             # 注册顺序(文档/快照排序)
_SETTINGS_CACHE = None  # settings.json 内容缓存


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_settings():
    """懒加载 settings.json(与 config.py 共享同一文件)。"""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        path = os.path.join(_project_root(), "settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                _SETTINGS_CACHE = json.load(f)
        except Exception:
            _SETTINGS_CACHE = {}
    return _SETTINGS_CACHE


def _cast(value, typ):
    """按注册类型转换(env/settings 读出的都是字符串/原值)。"""
    if value is None:
        return None
    if typ == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if typ == "int":
        return int(float(str(value).strip())) if str(value).strip() else None
    if typ == "float":
        return float(str(value).strip()) if str(value).strip() else None
    if typ == "json":
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return None
    return str(value)


def register(key, *, typ="str", default=None, env=None, cli=None,
             settings=None, group="misc", desc="", choices=None):
    """注册一个配置项(可插拔扩展点)。key 用点分命名空间(组.名)。"""
    spec = {
        "key": key, "typ": typ, "default": default, "env": env, "cli": cli,
        "settings": settings or [], "group": group, "desc": desc,
        "choices": choices,
    }
    if key not in _REGISTRY:
        _ORDER.append(key)
    _REGISTRY[key] = spec
    return spec


def _lookup_settings(spec):
    """沿 settings 路径取原值(无路径/无命中则 None)。"""
    path = spec.get("settings") or []
    if not path:
        return None
    s = _load_settings()
    for k in path:
        if not isinstance(s, dict) or k not in s:
            return None
        s = s[k]
    return s


def get(key, default=None):
    """合并读取: 覆盖 > env > settings > default。"""
    spec = _REGISTRY.get(key)
    if spec is None:
        raise KeyError(f"未注册的配置项: {key} (先 register())")
    val = None
    src = "default"
    if key in _OVERRIDES:
        val, src = _OVERRIDES[key], "override"
    else:
        env = spec.get("env")
        if env and os.getenv(env) is not None:
            val, src = os.getenv(env), "env"
        else:
            sv = _lookup_settings(spec)
            if sv is not None:
                val, src = sv, "settings"
    if val is None:
        return spec["default"] if default is None else default
    try:
        return _cast(val, spec["typ"])
    except Exception:
        return spec["default"]


def set(key, value):
    """运行时覆盖(优先级最高; 用于 CLI 参数注入 / 动态作用域)。"""
    spec = _REGISTRY.get(key)
    if spec is None:
        raise KeyError(f"未注册的配置项: {key}")
    _OVERRIDES[key] = value


def unset(key):
    _OVERRIDES.pop(key, None)


def export_env(extra=None):
    """生成子进程环境变量 dict: 收集所有带 env 的注册项(合并后最终值)。
    bool -> "1"/"0"; json -> 紧凑 JSON; extra 覆盖同名项(如临时注入)。"""
    env = dict(os.environ)
    for key in _ORDER:
        spec = _REGISTRY[key]
        e = spec.get("env")
        if not e:
            continue
        val = get(key)
        if val is None:
            continue
        if spec["typ"] in ("str", "json") and val == "":
            continue  # 空字符串不导出(如未配 api_key)
        if spec["typ"] == "bool":
            env[e] = "1" if val else "0"
        elif spec["typ"] == "json":
            env[e] = json.dumps(val, ensure_ascii=False)
        else:
            env[e] = str(val)
    if extra:
        env.update(extra)
    return env


def snapshot(only_set=False):
    """全部生效配置(运行快照/调试)。only_set=True 时只含非默认项。"""
    out = {}
    for key in _ORDER:
        val = get(key)
        spec = _REGISTRY[key]
        if only_set and val == spec["default"] and key not in _OVERRIDES:
            continue
        out[key] = val
    return out


def validate_all():
    """fail-fast: 类型/枚举校验, 返回错误列表(空=全过)。"""
    errs = []
    for key in _ORDER:
        spec = _REGISTRY[key]
        val = get(key)
        if val is None:
            continue
        if spec["typ"] in ("int", "float") and not isinstance(val, (int, float)):
            errs.append(f"{key}: 应为 {spec['typ']}, 得到 {val!r}")
        if spec.get("choices") and val not in spec["choices"]:
            errs.append(f"{key}: 取值 {val!r} 不在 {spec['choices']}")
    return errs


def markdown_table():
    """自动生成配置总览 Markdown(写 docs/ 防文档漂移)。"""
    groups = {}
    for key in _ORDER:
        s = _REGISTRY[key]
        groups.setdefault(s["group"], []).append(s)
    lines = ["# 配置总览(自动生成 · config_schema.py 注册表)",
             "", "分层来源(优先级 高→低): **运行时覆盖 > 环境变量 > settings.json > 内置默认**", ""]
    for g in sorted(groups):
        lines.append(f"## {g}")
        lines.append("| 配置项 | 类型 | 默认 | 环境变量 | settings 路径 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for s in groups[g]:
            env = s["env"] or ""
            sp = "/".join(s["settings"]) or ""
            lines.append(f"| `{s['key']}` | {s['typ']} | `{s['default']}` | `{env}` | `{sp}` | {s['desc']} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------
# 内置核心注册(全工程行为开关; 模块级扩展开关由各模块 register)
# ---------------------------------------------------------------
def _register_core():
    _G = "novel"
    register("novel.name", typ="str", default="小说", env="NOVEL_NAME",
             settings=["novel", "name"], group=_G, desc="书名(决定 db/产物命名)")
    register("novel.path", typ="str", default="", env="BOOK_PATH",
             settings=["novel", "path"], group=_G, desc="小说 txt 路径(相对项目根或绝对)")
    register("novel.chapters", typ="int", default=50, env="NOVEL_CHAPTERS",
             settings=["novel", "chapters"], cli="--chapters", group=_G,
             desc="处理章数(决定 db 与产物规模)")
    register("novel.genre", typ="str", default="", env="NOVEL_GENRE",
             cli="--genre", group=_G, desc="小说类别, 触发加载对应公版体系域")

    _G = "llm"
    register("llm.backend", typ="str", default="local",
             settings=["llm", "backend"], cli="--backend", group=_G,
             desc="主 LLM 预设名(models.json 枚举)")
    register("llm.api_key", typ="str", default="", env="LLM_API_KEY",
             settings=["llm", "api_key"], cli="--key", group=_G,
             desc="云端 API key(敏感, 不在命令行明文回显)")
    register("llm.base_url", typ="str", default="http://localhost:11434",
             env="LLM_BASE_URL", settings=["llm", "base_url"], group=_G,
             desc="LLM 服务地址")
    register("llm.model", typ="str", default="", env="LLM_MODEL",
             settings=["llm", "model"], cli="--model", group=_G, desc="覆盖模型名")
    register("llm.auth_scheme", typ="str", default="", env="LLM_AUTH_SCHEME",
             group=_G, desc="认证方式(bearer/apikey; 空=按 backend 推导)")
    register("llm.enable_thinking", typ="bool", default=False,
             env="LLM_ENABLE_THINKING", group=_G, desc="开启深度思考(默认关, 省 token)")
    register("llm.num_ctx", typ="int", default=4096, env="LLM_NUM_CTX",
             group=_G, desc="上下文窗口")
    register("llm.temperature", typ="float", default=0.3, env="LLM_TEMPERATURE",
             group=_G, desc="采样温度")
    register("llm.doubt_index", typ="float", default=0.5, env="LLM_DOUBT_INDEX",
             cli="--doubt-index", group=_G, desc="质疑指数 0-1(簇触发阈值/思考深度)")
    register("llm.role_models", typ="json", default={}, env="LLM_ROLE_MODELS",
             cli="--role", group=_G, desc='角色路由 {"review":"glm-4-flash"}')
    register("llm.embed_model", typ="str", default="bge-m3",
             settings=["llm", "embed_model"], group=_G, desc="本地向量化模型(Ollama)")

    _G = "run"
    register("run.src_dir", typ="str", default="cloud_fixed",
             settings=["run", "src_dir"], cli="--src", group=_G,
             desc="可视化任务读哪个 LLM 产物目录")
    register("run.date_suffix", typ="str", default="",
             settings=["run", "date_suffix"], group=_G,
             desc="输出目录固定后缀(留空=自动时间戳)")
    register("run.out_dir", typ="str", default="", cli="--out-dir", group=_G,
             desc="产物输出目录覆盖")
    register("run.parallel", typ="int", default=4, cli="--parallel", group=_G,
             desc="场景级/简历并发数")
    register("run.log", typ="str", default="", cli="--log", group=_G,
             desc="analyze 任务指定日志 jsonl 路径")
    for _k, _d, _desc in (("w_recent", 30, "最近 N 章出现过→活跃"),
                          ("k_freq", 3, "在 ≥N 章出现→活跃"),
                          ("k_confirm", 2, "被后文证实 N 次→永久活跃"),
                          ("every", 20, "每 N 章做一次代际压缩")):
        register(f"run.archive.{_k}", typ="int", default=_d,
                 settings=["run", "archive", _k], group=_G, desc=_desc)

    _G = "extract"
    register("extract.mode", typ="str", default="v2", cli="--extract-mode",
             choices=["two", "single", "v2", "attention"], group=_G,
             desc="场景抽取模式")
    register("extract.seg_mode", typ="str", default="rule", cli="--seg-mode",
             choices=["rule", "vector"], group=_G, desc="场景切分模式")

    _G = "collect"
    register("collect.fresh", typ="bool", default=False, env="COLLECT_FRESH",
             cli="--fresh", group=_G,
             desc="全量重抽设定(删旧 settings_graph 增量)")

    _G = "embed"
    register("embed.off", typ="bool", default=False, env="EMBED_OFF", group=_G,
             desc="跳过本地 bge-m3 向量化(省本地 CPU; FTS 检索不受影响)")

    _G = "rag"
    register("rag.external_search", typ="bool", default=False,
             env="EXTERNAL_SEARCH", group=_G, desc="启用联网搜索钩子(knowledge_router L3)")
    register("rag.top", typ="int", default=10, cli="--top", group=_G,
             desc="RAG 问答返回条数")

    _G = "knowledge"
    register("knowledge.scope", typ="json", default={"enabled": False, "domains": []},
             group=_G, desc="公版体系域作用域(默认不开放; --genre 动态加载)")


_register_core()
