# -*- coding: utf-8 -*-
"""
管线配置中心（单一真源）→ 统一注册表 config_schema.py

所有行为开关经 config_schema 注册表读取(分层: 运行时覆盖 > 环境变量 > settings.json > 默认)。
本文件保留 C.XXX 常量名供全工程引用(兼容零改动), 值全部来自注册表。
目录约定（详见 README）：
  - data/     中间产物：Stage1 数据库、向量缓存、LLM 直出 JSON、调试缓存（可跨次运行复用）
  - outputs/  每次运行的可视化产物：outputs/<小说名>_<日期>/  下全是自包含 HTML + 数据 JSON
"""
import os
import re
import json
import datetime

import config_schema as _CS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, "settings.json")

# ===== 小说（注册表: novel.*）=====
NOVEL_NAME = _CS.get("novel.name") or "小说"
_BOOK_PATH = _CS.get("novel.path") or ""
# 相对路径一律按项目根解析(开源友好: settings.json 里写 resource/小说.txt 即可)
if _BOOK_PATH and os.path.isabs(_BOOK_PATH):
    BOOK_PATH = _BOOK_PATH
elif _BOOK_PATH and os.path.splitext(_BOOK_PATH)[1].lower() in (".txt", ".epub"):
    # 相对路径已含文件名(如 resource/小说.txt): 直接挂项目根, 不再拼默认名
    BOOK_PATH = os.path.join(PROJECT_ROOT, _BOOK_PATH)
else:
    # 只给了目录(或留空): 用默认文件名 小说.txt
    BOOK_PATH = os.path.join(PROJECT_ROOT, _BOOK_PATH or "resource", "小说.txt")
CHAPTERS = int(_CS.get("novel.chapters") or 50)

# ===== LLM（注册表: llm.*）=====
LLM_BACKEND = _CS.get("llm.backend") or "local"
OLLAMA_BASE = _CS.get("llm.base_url") or "http://localhost:11434"
EXTRACT_MODEL = _CS.get("llm.model") or "qwen3:8b"
EMBED_MODEL = _CS.get("llm.embed_model") or "bge-m3"
LLM_API_KEY = _CS.get("llm.api_key") or ""
LLM_AUTH_SCHEME = _CS.get("llm.auth_scheme") or ("none" if LLM_BACKEND == "local" else "apikey")

# ===== 目录 =====
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
# 默认后缀带时间戳(YYYYMMDD_HHMMSS), 同一天多次运行互不覆盖;
# 若 settings.json run.date_suffix 手动指定(如 "20260827_incr"), 则用固定后缀实现增量累积。
_DATE_SUFFIX = _CS.get("run.date_suffix") or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", f"{NOVEL_NAME}_{_DATE_SUFFIX}")
# 产物子目录: stage1 过程数据 / stage2 过程数据 / RAG
STAGE1_DIR = os.path.join(OUTPUT_DIR, "stage1")
STAGE2_DIR = os.path.join(OUTPUT_DIR, "stage2")
RAG_DIR = os.path.join(OUTPUT_DIR, "rag")
RUN_SRC_DIR = _CS.get("run.src_dir") or "cloud_fixed"

# ===== 知识库归档参数(长文本边际爆炸, 见 docs/doubt_index_design.md) =====
# 从 settings.json run 段读取, 可调; 对应 clue_agent 的活跃层过滤/代际压缩
W_RECENT = int(_CS.get("run.archive.w_recent") or 30)       # 最近 N 章出现过 → 活跃(长程依赖保护)
K_FREQ = int(_CS.get("run.archive.k_freq") or 3)            # 在 ≥N 章出现 → 活跃(非噪声)
K_CONFIRM = int(_CS.get("run.archive.k_confirm") or 2)      # 被后文证实 N 次 → 永久活跃(长程伏笔保护)
CONSOLIDATE_EVERY = int(_CS.get("run.archive.every") or 20) # 每 N 章做一次代际压缩(mine 前)

# 数据库 / 报告落在 data/（可复用，不随每次运行翻新）
# 按小说名隔离: 不同小说建不同 db/缓存, 避免跨书污染(场景ID/段落ID 会冲突)
_NOVEL_SLUG = re.sub(r"[^\w\u4e00-\u9fff-]", "_", NOVEL_NAME) if NOVEL_NAME else "novel"
DB_PATH = os.path.join(DATA_DIR, f"stage1_v2_{CHAPTERS}_{_NOVEL_SLUG}.db")
REPORT_PATH = os.path.join(DATA_DIR, f"stage1_v2_{CHAPTERS}_{_NOVEL_SLUG}_report.json")
# 每次运行的 db 副本 + Excel 放 outputs/<book>_<date>/
RUN_DB_COPY = os.path.join(STAGE1_DIR, f"stage1_v2_{CHAPTERS}_{_NOVEL_SLUG}.db")
EXCEL_PATH = os.path.join(OUTPUT_DIR, f"{NOVEL_NAME}_数据.xlsx")
# 卷积式知识库: 独立 JSON 文件(非 db 表), 随 stage1 逐场景增量累积
KB_PATH = os.path.join(OUTPUT_DIR, f"{NOVEL_NAME}_知识库.json")

# ===== 模型(Ollama) 参数 =====
OLLAMA_NUM_PARALLEL = 4      # 抽取并发数(不要超过显存/内存承受)
TEMPERATURE = 0.2            # 抽取任务低随机性，禁"发挥"
NUM_CTX = 4096               # 单场景上下文窗口
NUM_PREDICT = 900            # 单次输出 token 上限(防跑飞;两轮 JSON 均远小于此)
REQUEST_TIMEOUT = 300        # 单次请求超时(秒);并发下首次加载模型较慢，留足余量
THINK = False                # qwen3 思维链开关:抽取任务必须关闭(否则慢且污染 JSON)

# ===== 分块(场景)参数 =====
SEG_MIN_PARA = 8             # 场景块最小段落数(<8 并入邻块)
SEG_MAX_PARA = 15            # 场景块最大段落数(>15 强制断)
SEG_OVERLAP = 1             # 相邻块重叠段落数(仅用于抽取上下文,不写库坐标)
SEG_MODE = _CS.get("extract.seg_mode") or "rule"  # 默认切分模式: "rule"(纯规则) | "vector"(规则+向量突变兜底)

# 向量突变阈值:实测校准值,不要凭直觉拍
#   《诡秘之主》第1章(69段)相邻段余弦相似度分布:
#   min=0.351 p10=0.414 p25=0.483 p50=0.554 p75=0.592 max=0.972
#   阈值 0.40 触发 6% | 0.45 触发 13% | 0.55 触发 49%(碎成渣,会覆盖规则抓到的真实边界)
#   0.42 对应每章 4-9 个隐性边界,量级合理
VECTOR_DROP_THRESHOLD = 0.42

# ===== 向量化(bge-m3)批量参数 =====
# 实测:逐条 /api/embeddings 2181ms/条; 批量 /api/embed 51ms/条(快 42 倍)
EMBED_BATCH_SIZE = 64        # 单次批量条数
EMBED_TIMEOUT = 300          # 批量请求超时(秒)

# ===== 文学层限量(程序侧硬约束,防模型过度摘录) =====
MAX_RHETORIC = 3             # 修辞最多保留条数
MAX_KEY_SENTENCES = 3        # 关键句最多保留条数
MIN_KEY_SENTENCE_LEN = 8     # 关键句最短字数(滤掉"痛!""好痛!"这类碎句)
MAX_KEYWORDS = 8             # 检索关键词最多条数

# ===== 样例规模 =====
SAMPLE_CHAPTERS = CHAPTERS   # 与 settings.json 的 chapters 一致

# 时间状语(场景开头信号)
TIME_SIGNALS = [
    "翌日", "次日", "第二天", "第三日", "数日后", "几天后", "片刻后", "许久后",
    "入夜", "半夜", "清晨", "黎明", "傍晚", "黄昏", "正午", "上午", "下午", "晚上",
    "转瞬", "刹那", "一年", "数月", "此时", "那一刻", "次日清晨",
]
# 地点状语(场景开头信号)
PLACE_SIGNALS = [
    "回到", "来到", "出了", "走出", "走进", "进入", "登上", "离开", "抵达",
    "去了", "前往", "穿过", "回到", "退入", "转入", "步入",
]
# 场景切换信号(叙事镜头切换, 开启另一条线; 命中的块作为强边界, 合并时优先保留)
# 典型: "与此同时"把当前场景切走, 跳去并行的另一头
SCENE_SWITCH_SIGNALS = [
    "与此同时", "同一时刻", "同一时间", "而在这时", "就在这时",
    "而此刻", "而在另一边", "而另一边", "与此同时，",
]

PUNCT_DIALOG = ("“", "”", "「", "」", "『", "』", '"', "'")
