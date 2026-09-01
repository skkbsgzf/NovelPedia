# -*- coding: utf-8 -*-
"""
llm_client.py —— 统一 LLM 调用层（本地 Ollama / 云端 OpenAI 兼容 / 多预设角色路由）

三个概念：
  1. 预设(preset)：models.json 里的一组连接参数（backend/base_url/model/auth），
     cli.py 用 --backend <预设名> 选择主模型。
  2. 角色(role)：管线阶段标签（extract/collect/mine/review/summary/embed）。
     --role review=glm-4-flash 可让"评述类"阶段用更强模型，其余阶段仍走主模型。
     角色覆盖经环境变量 LLM_ROLE_MODELS（"role=preset,role=preset"）传给子进程。
  3. 环境变量（兜底，不配置 models.json 也能用）：
     LLM_BACKEND(ollama|openai) LLM_BASE_URL LLM_API_KEY LLM_MODEL
     LLM_AUTH_SCHEME(bearer|apikey) LLM_ENABLE_THINKING LLM_NUM_CTX LLM_TEMPERATURE

调用:
  content, p_tokens, c_tokens = chat(system, user, model=..., num_ctx=...,
                                      temperature=..., json_mode=..., num_predict=...,
                                      base=..., role=...)
"""
import os
import sys
import json
import time
import urllib.request
import threading

import config as C

# ---- 预设注册表 ----
_MODELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models.json")
# 旧名 → 新预设名 兼容映射（历史 settings.json/llm.config.json 里写的名字）
LEGACY_ALIAS = {
    "local": "qwen3-8b", "cloud": "dots3-note",
    "xiaohongshu": "dots3-note", "glm": "glm-4-flash", "qwen3": "qwen3-8b",
}


def _load_presets():
    try:
        with open(_MODELS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        presets = cfg.get("presets") or {}
        default = cfg.get("default_backend") or next(iter(presets), "dots3-note")
        # 校验每个预设必需的字段
        for name, p in list(presets.items()):
            for k in ("backend", "base_url", "model"):
                if not p.get(k):
                    presets.pop(name)
                    break
        return presets, default
    except Exception:
        return {}, "dots3-note"


PRESETS, DEFAULT_PRESET = _load_presets()


def preset_names():
    """全部可用预设名（含旧别名，便于提示）。"""
    return sorted(set(PRESETS) | set(LEGACY_ALIAS))


def get_preset(name):
    """按名取预设 dict（含旧名别名解析），找不到返回 None。"""
    if not name:
        return None
    return PRESETS.get(LEGACY_ALIAS.get(name, name))


# ---- 环境变量兜底（无 models.json 时仍可全 env 驱动）----
BACKEND = os.getenv("LLM_BACKEND", "ollama").lower()
BASE_URL = os.getenv("LLM_BASE_URL", C.OLLAMA_BASE).rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
AUTH_SCHEME = os.getenv("LLM_AUTH_SCHEME", "bearer").lower()
# 默认关闭思考(节省 token/加速): dots.ai 等深度思考模型必须关, 否则只返回 reasoning_content
ENABLE_THINKING = os.getenv("LLM_ENABLE_THINKING", "false").lower() in ("1", "true", "yes")
NUM_CTX = int(os.getenv("LLM_NUM_CTX", "4096"))
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
MAX_ATTEMPTS = 6

# 质疑指数 (0-1), 控制思考深度和思维链触发频率
DOUBT_INDEX = float(os.getenv("LLM_DOUBT_INDEX", "0.7"))

# ---- 角色路由（cli.py --role 注入: "review=glm-4-flash,mine=deepseek"）----
ROLE_MODELS = {}


def _parse_role_models(env_str):
    out = {}
    for item in (env_str or "").split(","):
        item = item.strip()
        if "=" in item:
            role, preset = item.split("=", 1)
            role, preset = role.strip(), preset.strip()
            if role and get_preset(preset):
                out[role] = preset
    return out


ROLE_MODELS = _parse_role_models(os.getenv("LLM_ROLE_MODELS", ""))


def load_config(config_path=None):
    """读取 llm.config.json, 返回顶层配置(如 doubt_index)。
    兼容旧路径: 默认读项目根 llm.config.json(如存在); 否则读 models.json 顶层。
    """
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "llm.config.json")
    if not os.path.exists(config_path):
        # 新注册表: 顶层只有 doubt_index 时用其默认值
        try:
            with open(_MODELS_PATH, encoding="utf-8") as f:
                return {"doubt_index": json.load(f).get("doubt_index", 0.7)}
        except Exception:
            return {"doubt_index": 0.7}
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---- token 计量钩子 ----
# 两层: (1) 持久累加器 TOKEN_TOTAL 始终落盘 logs/token_total.json(不依赖 panel 注入,
#         供全量跑批真实采集 token 消耗量); (2) 可选 TOKEN_METER 注入(panel 实时显示用)。
# 签名: TOKEN_METER(model, prompt_tokens, completion_tokens)
TOKEN_METER = None

# 进程内累加器(线程安全), 每次 chat 后原子落盘到 logs/token_total.json
_TOKEN_LOCK = threading.Lock()
TOKEN_TOTAL = {"prompt_total": 0, "completion_total": 0, "by_model": {}, "calls": 0, "runs": 0}


def _token_persist():
    """原子写 logs/token_total.json (崩溃安全, 全量跑批可随时读取累计)。"""
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "token_total.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(TOKEN_TOTAL, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def reset_token_meter():
    """清空累计(开始一次新的全量测试前调用), 使 token_total.json 仅含本次运行数据。"""
    global TOKEN_TOTAL
    with _TOKEN_LOCK:
        TOKEN_TOTAL = {"prompt_total": 0, "completion_total": 0, "by_model": {}, "calls": 0, "runs": 0}
        _token_persist()


def set_token_meter(fn):
    """panel 启动后注入：fn(model, prompt_tokens, completion_tokens)。"""
    global TOKEN_METER
    TOKEN_METER = fn


def _record_usage(model, prompt_tok, completion_tok):
    pt = prompt_tok or 0
    ct = completion_tok or 0
    # 始终累加并落盘(持久计量, 支撑全量跑批 token 采集)
    with _TOKEN_LOCK:
        TOKEN_TOTAL["prompt_total"] += pt
        TOKEN_TOTAL["completion_total"] += ct
        TOKEN_TOTAL["calls"] += 1
        bm = TOKEN_TOTAL["by_model"].setdefault(
            model or "unknown", {"prompt": 0, "completion": 0, "calls": 0})
        bm["prompt"] += pt
        bm["completion"] += ct
        bm["calls"] += 1
        _token_persist()
    if TOKEN_METER is not None:
        try:
            TOKEN_METER(model, pt, ct)
        except Exception:
            pass


def _resolve_model(model):
    # 优先级: LLM_MODEL 环境变量 > 调用方传入 > 配置默认
    return os.getenv("LLM_MODEL") or model or C.EXTRACT_MODEL


def chat(system, user, *, model=None, num_ctx=None, temperature=None,
         json_mode=True, num_predict=2048, base=None, timeout=120, role=None):
    """统一对话接口, 返回 (content, prompt_tokens, completion_tokens)。

    role: 管线阶段标签。命中 LLM_ROLE_MODELS 时, 该次调用切换到对应预设
    (优先级: 角色预设 > 环境变量 > 调用方参数 > 配置默认)。
    base: 仅 ollama 后端生效, 覆盖 LLM_BASE_URL(兼容 main.py --base 透传)。
    """
    backend, api_key, scheme = BACKEND, API_KEY, AUTH_SCHEME
    rp = get_preset(ROLE_MODELS.get(role)) if role else None
    # 修复①: 直接传 model 名也应驱动对应 preset (原本只有 role 能切 backend)
    if rp is None and model and get_preset(model):
        rp = get_preset(model)
    # 修复②: model 为 None 时复活 models.json 的 default_backend (消除死配置);
    #        若该默认是云端且缺 key, 透明降级到本地 ollama 并打印告警(不再静默)
    if rp is None and model is None and not role:
        dp = get_preset(DEFAULT_PRESET)
        if dp:
            need_key = dp.get("backend") != "ollama" and not (dp.get("api_key") or API_KEY)
            if need_key:
                print(f"\u26a0\ufe0e default_backend={DEFAULT_PRESET!r} 需要 API key 但当前缺失，"
                      f"已透明降级到本地 ollama（非静默）。设置 LLM_API_KEY 或显式 --model 可切换。",
                      file=sys.stderr)
            else:
                rp = dp
                model = DEFAULT_PRESET
    if rp:
        backend = rp.get("backend", BACKEND)
        base = rp.get("base_url", base or BASE_URL)
        # dots3-note 用独立凭据变量, 避免与 GLM 的 LLM_API_KEY 互相污染
        api_key = rp.get("api_key") or (os.getenv("DOTSAI_API_KEY") if model == "dots3-note" else API_KEY)
        scheme = rp.get("auth_scheme", AUTH_SCHEME)
        model = rp.get("model", model)
        if num_ctx is None and rp.get("ctx"):
            num_ctx = int(rp["ctx"])
    model = _resolve_model(model)
    # 修复③: 显式指定了云端模型/角色却缺 key -> 直接报错, 绝不静默降级到本地小模型
    if backend != "ollama" and not api_key:
        raise RuntimeError(
            f"目标模型 {model!r} 走 {backend} 后端但缺少 API key。"
            "请设置环境变量 LLM_API_KEY 或在 preset 中配置 api_key；"
            "若要用本地模型请显式传入 model='qwen3-8b'。"
        )
    num_ctx = num_ctx or NUM_CTX
    temperature = temperature if temperature is not None else TEMPERATURE
    if backend == "openai":
        url = _openai_url(base or BASE_URL)
        out = _post_openai(url, system, user, model, temperature,
                           json_mode, num_predict, timeout, api_key, scheme)
    elif backend == "anthropic":
        out = _post_anthropic(base or BASE_URL, system, user, model, temperature,
                              json_mode, num_predict, timeout, api_key)
    elif backend == "google":
        out = _post_google(base or BASE_URL, system, user, model, temperature,
                           json_mode, num_predict, timeout, api_key)
    else:
        url = (base or BASE_URL).rstrip("/") + "/api/chat"
        out = _post_ollama(url, system, user, model, num_ctx, temperature,
                           json_mode, num_predict, timeout)
    _record_usage(model, out[1], out[2])
    return out


def _openai_url(base):
    b = base.rstrip("/")
    if b.endswith("/chat/completions"):
        return b                      # base 已是完整 chat 端点
    if b.endswith("/v1") or b.endswith("/v4"):
        return b + "/chat/completions"  # 已是版本根(openai/v1, glm/v4)
    return b + "/v1/chat/completions"   # 裸根(自动补 /v1)


def _post(url, payload, headers, timeout, ctx=""):
    import urllib.error
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(MAX_ATTEMPTS):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 限流(429)/服务端错误(5xx): 退避后重试, 不立即失败
            if e.code in (429, 500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                time.sleep(min(60, 10 * (attempt + 1)))
                continue
            _hint = {
                400: "请求格式被厂商拒绝(模型不支持 JSON 模式时会降级重试)",
                401: "API key 无效或未授权(检查 LLM_API_KEY / --key)",
                403: "无访问权限(检查 key 权限范围或套餐额度)",
                404: "端点或模型名不存在(检查预设的 base_url / model)",
                429: "触发限流(可降低 --parallel 并发或稍后重试)",
            }.get(e.code, "")
            raise RuntimeError(
                f"[LLM {ctx or 'request'}] HTTP {e.code} {'：' + _hint if _hint else '服务端返回错误'}\n"
                f"    URL: {url}") from e
        except Exception:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(5 * (attempt + 1))


def _post_anthropic(base, system, user, model, temperature,
                    json_mode, num_predict, timeout, api_key):
    """Anthropic Claude 原生 API: POST /v1/messages (x-api-key 认证)。"""
    if not api_key:
        raise RuntimeError("Anthropic 后端需要 API key(环境变量 LLM_API_KEY 或 --key)")
    url = base.rstrip("/") + "/v1/messages"
    payload = {
        "model": model,
        "max_tokens": num_predict,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": temperature,
        "stream": False,
    }
    # json_mode: Anthropic 无 response_format, 靠提示词约束 + 调用方 _extract 容错
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    resp = _post(url, payload, headers, timeout, ctx=f"anthropic:{model}")
    content = "".join(b.get("text", "") for b in (resp.get("content") or [])
                      if b.get("type") == "text")
    usage = resp.get("usage", {}) or {}
    return (content, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


def _post_google(base, system, user, model, temperature,
                 json_mode, num_predict, timeout, api_key):
    """Google Gemini 原生 API: POST /v1beta/models/{model}:generateContent (x-goog-api-key)。"""
    if not api_key:
        raise RuntimeError("Gemini 后端需要 API key(环境变量 LLM_API_KEY 或 --key)")
    url = base.rstrip("/") + f"/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": temperature,
                             "maxOutputTokens": num_predict},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    headers = {"x-goog-api-key": api_key}
    resp = _post(url, payload, headers, timeout, ctx=f"gemini:{model}")
    cands = resp.get("candidates") or [{}]
    content = "".join(p.get("text", "") for p in
                      (cands[0].get("content", {}).get("parts") or []) if p.get("text"))
    um = resp.get("usageMetadata", {}) or {}
    return (content, um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))


def _post_ollama(url, system, user, model, num_ctx, temperature,
                 json_mode, num_predict, timeout):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "think": False,
        "options": {"temperature": temperature,
                    "num_ctx": num_ctx,
                    "num_predict": num_predict},
    }
    if json_mode:
        payload["format"] = "json"
    resp = _post(url, payload, {}, timeout)
    return (resp["message"]["content"],
            resp.get("prompt_eval_count", 0),
            resp.get("eval_count", 0))


def _post_openai(url, system, user, model, temperature,
                 json_mode, num_predict, timeout, api_key=None, scheme=None):
    api_key = api_key if api_key is not None else API_KEY
    scheme = (scheme or AUTH_SCHEME).lower()
    if not api_key:
        raise RuntimeError("LLM_BACKEND=openai 需要 API key "
                           "(环境变量 LLM_API_KEY 或 --key 传入; dots.ai 等 apikey 方案同样填入 LLM_API_KEY)")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": num_predict,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # 关闭深度思考 (dots.ai/GLM 等模型默认开启, 只会返回 reasoning_content, 白烧 token)。
    # 由 LLM_ENABLE_THINKING 控制: false(默认) 关, true 开。
    if not ENABLE_THINKING:
        # 双保险: 官方 thinking:disabled(智谱 GLM 用人参) + chat_template_kwargs(dots.ai)
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {}
    if scheme == "apikey":
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = _post(url, payload, headers, timeout)
    except Exception:
        # 部分云端模型不支持 response_format, 退回普通模式重试一次
        if json_mode:
            payload.pop("response_format", None)
            resp = _post(url, payload, headers, timeout)
        else:
            raise
    choices = resp.get("choices") or [{}]
    m = choices[0].get("message", {}) if choices else {}
    content = m.get("content") or m.get("reasoning_content") or ""
    usage = resp.get("usage", {}) or {}
    return (content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
