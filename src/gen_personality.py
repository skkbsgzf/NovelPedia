"""
gen_personality.py ——批量生成人物性格六维向量 (雷达图数据
输入: characters.json (name/身份/弧光)
输出: personality.json [{name, dims: {混沌守序, 善良邪恶, 理智感性 内向外向, 果断犹豫, 隐忍张扬}}]
维度值0-100, 50 为中性。分批调用(每批1人)+重试; 已有缓存则复用, --force 强制全量重跑。
"""
import os, sys, json, re, argparse, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import llm_client

ap = argparse.ArgumentParser()
ap.add_argument("--src", default=C.RUN_SRC_DIR, help="Stage2 产物目录（local|cloud|cloud_fixed）")
ap.add_argument("--out", default="", help="personality.json 输出路径; 留空用默认")
ap.add_argument("--model", default="glm-4.7-flash",
                help="生成用模型 preset 名 (默认 glm-4.7-flash; 旧 dots3-note 需 key)")
ap.add_argument("--force", action="store_true", help="强制全量重跑(忽略已有缓存)")
A = ap.parse_args()
# 新结构: stage2 产物在 output/pedia_<书>_<日期>/stage2/; --src 传具体目录则用之
BASE = A.src if (A.src and A.src != C.RUN_SRC_DIR and os.path.isdir(A.src)) else C.STAGE2_DIR


def _stage1_fallback(SRC):
    """新架构兼容(对齐 build_detail._stage1_fallback 人物口径):
    mine 不再产出旧式 stage2/characters.json, 缺失时从 stage1 补全
      - 骨架: character_facts.json 出场频次 top N (DETAIL_TOP_CHARS, 默认80)
      - 填充: characters_resume.json 的 identity->身份, trajectory->弧光
    """
    import os as _os
    s1 = _os.path.join(_os.path.dirname(SRC), "stage1")

    def load(p, default=None):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return default if default is not None else {}

    cf = load(_os.path.join(s1, "character_facts.json"), {})
    freq = {}
    for name, v in (cf.get("characters") or {}).items():
        n = len((v or {}).get("appearances") or [])
        if n:
            freq[name] = n
    top_n = int(_os.environ.get("DETAIL_TOP_CHARS", "80"))
    names = [n for n, _f in sorted(freq.items(), key=lambda kv: -kv[1])[:top_n]]
    if not names:
        return []
    resume = load(_os.path.join(s1, "characters_resume.json"), {})
    by_name = {c.get("name"): c for c in (resume.get("characters") or [])
               if isinstance(c, dict)}
    return [{"name": n,
             "身份": (by_name.get(n) or {}).get("identity") or "",
             "弧光": (by_name.get(n) or {}).get("trajectory") or ""}
            for n in names]


def _valid(x):
    d = x.get("dims") or {}
    return bool(d) and any(v != 50 for v in d.values())


def _clamp(v):
    try:
        return max(0, min(100, int(v)))
    except Exception:
        return 50


def main():
    try:
        CHARS = json.load(open(os.path.join(BASE, "characters.json"), encoding="utf-8"))
    except FileNotFoundError:
        CHARS = _stage1_fallback(BASE)
        print(f"[fallback] stage2/characters.json 缺失, 已从 stage1 补全: 人物 {len(CHARS)}")
        if not CHARS:
            print("stage1 也无人物数据, 无法生成")
            sys.exit(1)
    OUT = os.path.join(C.OUTPUT_DIR, "personality.json") if not A.out else A.out

    DIMS = ["混沌守序", "善良邪恶", "理智感性", "内向外向", "果断犹豫", "隐忍张扬"]

    try:
        cached = json.load(open(OUT, encoding="utf-8"))
        need_names = {c.get("name") for c in CHARS}
        have_names = {x.get("name") for x in cached if _valid(x)}
        missing = need_names - have_names
    except Exception:
        cached, missing = None, {c.get("name") for c in CHARS}

    if cached and not missing and not A.force:
        print(f"✅personality.json 已缓存({len(cached)} 人 全部有效), 跳过 LLM 调用 (--force 强制重跑)")
        sys.exit(0)
    if cached and missing:
        print(f"♻️ 缓存缺{len(missing)} 人({sorted(missing)[:5]}...), 增量补全")

    sys_prompt = (
        "你是小说人物性格分析器。对每个人物，在以下 6 个维度上打分，每个维度必须是 0 到 100 之间的整数（50 为中性），"
        "严禁输出负数或 -1/0/1 这类三元值：\n"
        "混沌守序、善良邪恶、理智感性、内向外向、果断犹豫、隐忍张扬\n"
        "只输出合法 JSON 数组，格式："
        '[{"name": "人物名", "dims": {"混沌守序": 数值, "善良邪恶": 数值, "理智感性": 数值, "内向外向": 数值, "果断犹豫": 数值, "隐忍张扬": 数值}}]'
        "示例：{\"name\":\"示例人物\",\"dims\":{\"混沌守序\":70,\"善良邪恶\":40,\"理智感性\":65,\"内向外向\":30,\"果断犹豫\":55,\"隐忍张扬\":80}}\n"
        "根据身份与弧光证据推断，不要臆造。"
    )

    print(f"输入人物数 {len(CHARS)} | 本次需生成: {len(CHARS)}")

    # 注意: dots3-note 等 openai 后端在 response_format=json_object 下强制单对象输出
    # (多人大批只返回第一个人且 ~50tok 提前停), BATCH 必须为 1 逐人调用。
    BATCH = 1
    need_chars = [c for c in CHARS if c.get("name") in missing] if (missing and cached and not A.force) else CHARS
    batches = [need_chars[i:i + BATCH] for i in range(0, len(need_chars), BATCH)]
    all_data = []
    for bi, batch in enumerate(batches, 1):
        lines = []
        for i, c in enumerate(batch, 1):
            nm = c.get("name", "")
            ident = (c.get("身份") or "").replace("\n", " ")[:120]
            arc = (c.get("弧光") or "").replace("\n", " ")[:150]
            lines.append(f"{i}. {nm} | 身份: {ident} | 弧光: {arc}")
        prompt = "\n".join(lines)

        def extract_list(obj):
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                if "name" in obj and "dims" in obj:
                    return [obj]
                for v in obj.values():
                    if isinstance(v, list) and v and all(isinstance(d, dict) for d in v):
                        return v
            return None

        # 重试至多 3 次 拿到该批所有人物为止
        got = []
        for attempt in range(3):
            out, pin, pout = llm_client.chat(sys_prompt, prompt, json_mode=True,
                                             num_predict=400, temperature=0.2 + 0.1 * attempt,
                                             model=A.model)
            data = None
            try:
                data = extract_list(json.loads(out))
            except Exception:
                pass
            if not data:
                for wrapped in ('[' + out + ']', '[%s]' % out.strip()):
                    try:
                        cand = extract_list(json.loads(wrapped))
                        if cand:
                            data = cand
                            break
                    except Exception:
                        continue
            if not data:
                m = re.search(r"\[[\s\S]*\]", out)
                data = extract_list(json.loads(m.group(0))) if m else None
            # 计算本批成功数
            if data:
                for d in data:
                    if d.get("name") and d.get("dims"):
                        got.append(d)
            names_got = {d.get("name") for d in got if d.get("name")}
            names_need = {c.get("name") for c in batch}
            missing_now = names_need - names_got
            print(f"  批{bi}/{len(batches)} 尝试{attempt+1}: {pin}/{pout}tok | 拿到{len(names_got)}/{len(names_need)}"
                  + (f" | 缺{list(missing_now)[:3]}" if missing_now else " | 完整"))
            if not missing_now:
                break
        all_data.extend(got)
        time.sleep(3)  # 降频, 规避智谱免费版 QPM 限流 (配合 _post 退避)

        # 增量落盘: 每批结束即写, 崩溃可续跑(重跑时靠缓存合并补齐)
        _by = {d.get("name"): d.get("dims") for d in all_data if d.get("name") and d.get("dims")}
        if cached:
            for x in cached:
                if _valid(x) and x.get("name") not in _by:
                    _by[x["name"]] = x["dims"]
        _res = [{"name": c.get("name", ""), "dims": {k: _clamp(_by.get(c.get("name", ""), {}).get(k)) for k in DIMS}}
                for c in CHARS]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(_res, f, ensure_ascii=False, indent=1)

    if not all_data:
        print("全部批次解析失败")
        sys.exit(1)

    # 合并: 新生成 + 缓存有效值增量补全时保底
    by_name = {d.get("name"): d.get("dims") for d in all_data if d.get("name") and d.get("dims")}
    if cached:
        for x in cached:
            if _valid(x) and x.get("name") not in by_name:
                by_name[x["name"]] = x["dims"]
    result = []
    for c in CHARS:
        nm = c.get("name", "")
        dims = by_name.get(nm) or {}
        clean = {}
        for k in DIMS:
            v = dims.get(k)
            try:
                v = max(0, min(100, int(v)))
            except Exception:
                v = 50
            clean[k] = v
        result.append({"name": nm, "dims": clean})

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"已写 {OUT}: {len(result)} 人")
    if result:
        print("样本:", result[0]["name"], "->", json.dumps(result[0]["dims"], ensure_ascii=False))


if __name__ == "__main__":
    main()
