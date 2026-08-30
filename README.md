# StoryScienceLab · 小说拆书工程

> **把一本小说拆成可验证的结构化数据**：场景抽取 → 多智能体挖掘（暗线/设定/人物/文风）→ 自包含可视化工作台。
> Local-first pipeline that turns a novel into auditable structured data — scenes, hidden plotlines,
> worldbuilding, character arcs and style analysis — rendered into a single self-contained HTML workspace.

---

## ✨ 特性

- **多类型模型匹配**：`models.json` 预设注册表（本地 Ollama / 智谱 GLM / dots.ai / DeepSeek / Kimi / 通义…），OpenAI 兼容端点任加
- **角色级模型路由**：抽取、挖掘、评述等阶段可分别指定不同模型（便宜模型跑高频，强模型做评述）
- **单文件成品**：整本书的拆书结果是一个自包含 `index.html`（零外部依赖，双击即开）
- **四维拆解**：剧情推理（证据→簇→结论，伏笔-回收链）/ 设定图谱 / 人物简历 / 文风分析（篇→段→句→词）
- **创作解析标注**：关键场景自动打「导演注释」（plot_function），拉片画布逐镜回看
- **编辑评述 agent**：每个板块给出专业编辑视角的 habits / strengths / weaknesses / summary
- **防幻觉**：设定词条全局锚定、公版体系图 RAG 触发式加载、证据链可溯源到原文
- **零重依赖**：管线仅用 Python 标准库（sqlite3 / urllib / re / concurrent.futures），jieba 可选
- **跑批日志**：logbook 结构化双通道日志 + `cli analyze` 健康度报告（H1-H5）

## 🖥️ 成品 Demo（唯一演示）

用《诡秘之主》前 300 章跑出的完整拆书工作台（剧情/人物/设定/文风 + 编辑评述）：

| | |
|---|---|
| **单文件** | [`demo/index.html`](demo/index.html) — 6.8MB 自包含，浏览器直接打开 |
| 概览 | ![概览](demo/screenshots/01_概览.png) |
| 人物 | ![人物](demo/screenshots/02_人物.png) |
| 剧情拉片画布 | ![剧情拉片画布](demo/screenshots/03_剧情拉片画布.png) |
| 文风 | ![文风](demo/screenshots/04_文风.png) |

> ⚠️ **版权声明**：demo 数据来自公开连载小说《诡秘之主》的**评论性可视化**（关系图谱/数据统计/短引用），
> 仅用于展示引擎能力。小说原文 **不会** 出现在仓库中，`resource/` 下的用户文本一律不入库（见 .gitignore）。
> 请仅对你有权处理的作品运行本管线。

---

## 🚀 快速开始（改配置 → 放小说 → 跑 → 看产物）

```bash
# 1. 把你的小说放好（txt 或 epub 转 txt）
#    resource/小说.txt   ← 默认路径（.gitignore 已排除，不会入库）
#    或改 settings.json 的 novel.path / novel.name / novel.chapters

# 2. 选后端跑全流程（本地 Ollama 零成本，需先装 Ollama + qwen3:8b）
python src/cli.py extract  --backend qwen3-8b
python src/cli.py collect  --backend qwen3-8b
python src/cli.py mine     --backend qwen3-8b
python src/cli.py viz      --backend qwen3-8b   # 生成 outputs/<书名>_<时间>/index.html

# 云端（效果更强，需 API key）
set LLM_API_KEY=你的key
python src/cli.py extract --backend dots3-note
python src/cli.py collect --backend dots3-note
python src/cli.py mine    --backend dots3-note
python src/cli.py viz     --backend dots3-note
```

也可以 `python src/cli.py` 进入交互菜单（选后端 + 选任务）。

## 🧠 多类型模型匹配（models.json）

所有模型集中在一个注册表 [`models.json`](models.json)，`cli.py --backend <预设名>` 选择主模型：

```bash
python src/cli.py --list-backends    # 查看全部预设与能力字段（🆓免费 / 💰付费标注）
```

内置 **16 个预设 / 4 种后端协议**（数据来源：2026 年 8 月各厂商最新文档）：

| 预设名 | 厂商 / 后端协议 | 模型（2026 最新） | 上下文 | 成本 |
|---|---|---|---|---|
| `dots3-note` | 小红书 dots.ai（openai） | dots3-note-prev | 512K | 💰 |
| `gpt-5` | OpenAI（openai） | **gpt-5** | 400K | 💰 |
| `gpt-5-mini` | OpenAI 性价比（openai） | **gpt-5-mini** | 400K | 💰 |
| `claude-opus-4-8` | Anthropic 旗舰（**anthropic 原生**） | **claude-opus-4-8** | **1M** | 💰 |
| `claude-sonnet-4-6` | Anthropic 主力（**anthropic 原生**） | **claude-sonnet-4-6** | **1M** | 💰 |
| `claude-haiku-4-5` | Anthropic 便宜快（**anthropic 原生**） | claude-haiku-4-5 | 200K | 💰 |
| `gemini-3-flash` | Google 免费（**google 原生**） | **gemini-3-flash-preview** | **1M** | 🆓 |
| `gemini-3-pro` | Google 旗舰（**google 原生**） | **gemini-3-pro-preview** | **2M** | 💰 |
| `deepseek-v3-2` | DeepSeek V3.2 稀疏注意力（openai） | **deepseek-v3.2** | 128K | 💰 |
| `deepseek-r1` | DeepSeek R1 推理（openai） | deepseek-r1-0528 | 128K | 💰 |
| `kimi-k2-5` | Moonshot K2.5（openai） | **kimi-k2.5** | 262K | 💰 |
| `qwen3-max` | 阿里通义 Qwen3-Max（openai） | **qwen3-max** | 262K | 💰 |
| `glm-4.7-flash` | 智谱 GLM-4.7-Flash（openai） | glm-4.7-flash | 200K | 🆓 |
| `glm-5-2` | 智谱 GLM-5.2 **1M MIT 开源**（openai） | **glm-5.2** | **1M** | 💰 |
| `groq-llama` | Groq 高速（openai） | llama-3.3-70b-versatile | 128K | 🆓 |
| `qwen3-8b` | 本地 Ollama（ollama） | qwen3:8b | 32K | 🆓🏠 |

- **四种后端协议**：`ollama`（本地）/ `openai`（OpenAI 兼容，绝大多数厂商）/ `anthropic`（Claude 原生 /v1/messages）/ `google`（Gemini 原生 generateContent）——`llm_client.py` 已内置适配，**新增厂商只需往 `presets` 加一组**
- **key 不落盘**：从环境变量 `LLM_API_KEY` 或 `--key` 传入；所有云端后端共用这一个 key 入口
- **模型 ID 以厂商最新文档为准**——本表是 2026-08 查证值，厂商发新版后改 `model` 字段即可

### 角色路由（--role）

管线按角色分阶段，每个角色可以指定不同模型：

| 角色 | 阶段 | 建议 |
|---|---|---|
| `extract` | 场景抽取（高频短输出） | 便宜/快 |
| `collect` | 四维收集（高频） | 便宜/快 |
| `mine` | 暗线推理/设定强化/人物简历（中频长输出） | 强模型 |
| `review` | 编辑评述/创作解析（低频高质量） | 最强模型 |
| `summary` | 章纲/总结 | 中 |
| `embed` | 向量化（仅 ollama） | bge-m3 |

```bash
# 主模型用本地 qwen3，评述/创作解析用 Claude Sonnet 4.6，挖掘用 DeepSeek V3.2
python src/cli.py collect --backend qwen3-8b
python src/cli.py mine    --backend qwen3-8b --role mine=deepseek-v3-2
python src/cli.py review  --backend qwen3-8b --role review=claude-sonnet-4-6
python src/cli.py annotate --backend qwen3-8b --role review=claude-sonnet-4-6
```

> **运行时清晰提示**：每条命令启动都会打印当前后端摘要
> `▶ 后端 [xxx] 名称 🆓/💰 | model / 上下文 / 并发 / JSON模式`；
> 每个子任务结束打印耗时 `⏱ ... 耗时 Xs`；
> LLM 请求失败会给出中文定位提示（`401`=key 无效、`403`=无权限、`404`=模型名/端点错、`429`=限流请降并发）。

## 📖 命令行总览（`src/cli.py` 统一入口）

```
python src/cli.py <任务> [--backend 预设名] [--chapters N] [--model M] [--key K]
                     [--role role=预设] [--genre 类别] [--parallel N]
```

| 任务 | 做什么 | 产物 |
|---|---|---|
| `extract` | 分场景抽取（TXT→SQLite） | `data/stage1_v2_<N>_<书>.db` |
| `collect` | Stage1 四维收集（设定/暗线/人物/文风采样） | `outputs/<书>_<日期>/stage1/*.json` |
| `mine` | Stage2 四维挖掘（剧情推理/设定强化/人物简历/文风） | 同上（clue_graph 等） |
| `annotate` | 创作解析标注（关键场景 plot_function/note） | `scenes_annotations.json` |
| `review` | 全板块编辑评述（专业编辑视角） | `editor_reviews.json` |
| `build` | Stage2 直出（章纲/人物/设定/总结） | `outputs/<书>_<日期>/stage2/` |
| `personality` | 人物性格六维向量（雷达图） | `personality.json` |
| `graph` | 知识图谱（角色+设定，章节筛选） | `knowledge_graph.html` |
| `detail` | 拆书详情页（四 Tab 全景） | `book_detail.html` |
| `report` | 本地 vs 云端对比报告 | `final_report.html` |
| `rag "问题"` | RAG 问答 | 控制台答案 |
| `viz` | **一键**：详情页+图谱+报告 → 单页工作台 | `outputs/<书>_<日期>/index.html` |
| `analyze` | 跑批日志健康度报告 | 控制台 H1-H5 |

其他参数：

- `--genre 类别`（宫斗/修仙/诡秘/系统/灵异/都市…）：**按类别**加载公版体系图作参考（补档/分类/候选），默认不加载，与原文冲突以原文为准
- `--doubt-index 0-1`：质疑指数，控制暗线推理的簇触发阈值与思考深度
- `--parallel N`：collect/mine 的场景级并发数（默认 4，注意模型 concurrency 限制）
- `--src DIR`：可视化任务指定读哪个 stage1 产物目录

## 🗂️ 目录结构

```
novel_pipeline/
├─ settings.json              # ★ 配置：小说路径/章数/主模型
├─ models.json                # ★ 模型预设注册表 + 角色路由
├─ demo/                      # ★ 唯一成品 demo（index.html + 截图）
├─ resource/                  #   放你的小说 txt（.gitignore 排除，不入库）
├─ run.bat                    #   一键入口（默认跑可视化层）
├─ README.md / docs/          #   使用说明 + 技术文档 + 算法设计 + 日志分析
├─ src/                       # ★ 全部代码（.py + .html 模板）
│  ├─ cli.py                  #   统一入口（任务编排 + 后端切换 + 可视化归档）
│  ├─ llm_client.py           #   统一 LLM 调用层（预设解析 + 角色路由 + 重试）
│  ├─ config.py               #   配置中心（单一真源 settings.json）
│  ├─ extract.py / main.py / db.py / segment.py / prompts.py   # Stage1 抽取
│  ├─ stage1_collect.py       #   Stage1 四维收集（设定/暗线/人物/文风采样）
│  ├─ stage2_mine.py          #   Stage2 四维挖掘（推理/设定强化/简历/文风）
│  ├─ clue_agent.py / setting_agent.py / character_agent.py / main_agent.py   # 拆书 agent
│  ├─ scene_annotator.py      #   创作解析标注（拉片画布 🎬）
│  ├─ editor_review.py        #   全板块编辑评述
│  ├─ style_sampler.py / style_baseline.py   # 文风四维分析 + 跨书基准
│  ├─ webnovel_lexicon.py / knowledge_router.py / knowledge.py   # 公版体系图 RAG
│  ├─ logbook.py / analyze_log.py            # 跑批日志 + 健康度分析
│  ├─ export_graph.py / build_detail.py / final_report.py / gen_personality.py  # 可视化
│  └─ index_template.html / detail_template.html / graph_template.html           # 模板
├─ data/                      # 中间产物（.gitignore 排除，可跨次复用）
└─ outputs/                   # 每次运行的产物目录（.gitignore 排除）
```

## 🔧 底层管线（技术速览）

```
小说.txt ─► 分场景抽取 actinfo v2 ─► SQLite
                  │
                  ▼
      Stage1 四维收集(设定/暗线/人物/文风采样) ─► JSON
                  │
                  ▼
      Stage2 四维挖掘(推理/设定强化/简历/文风分析) ─► JSON
                  │
                  ▼
      annotate / review ─► 创作解析 + 编辑评述
                  │
                  ▼
      viz: 单文件自包含 index.html（零外部依赖）
```

- **明线/暗线分离**：Stage1 逐场景轻量抽取（0 推理，token 硬上限）；暗线证据聚类（bge-m3 向量，阈值 0.62）后批量推理
- **质疑指数** `doubt_index`：控制证据簇触发阈值与思考深度，防过度推理（见 [docs/doubt_index_design.md](docs/doubt_index_design.md)）
- **⚠️ 云端深度思考必须关**：dots.ai/GLM 默认开 thinking 时只返回 `reasoning_content`（token 虚高 6 倍）。`llm_client.py` 已内置双保险关闭（`thinking.disabled` + `chat_template_kwargs.enable_thinking:false`）
- **输入省 42% token**：抽取层产出 actinfo JSON 流比 TXT 原文直出更省
- **防幻觉三板斧**：设定词条全局锚定（跨场景共享注册表）/ 公版体系图触发式加载（`--genre` 才启用）/ 证据链可溯源到原文

详见 [docs/技术文档.md](docs/技术文档.md)（架构/schema/性能/踩坑）与 [docs/使用说明.md](docs/使用说明.md)（操作/FAQ）。

## ⚙️ 参数配置与运行效率

**完整速查**：[docs/参数配置与运行效率.md](docs/参数配置与运行效率.md)（settings/models/CLI/环境变量全字段表 + 实测数据）

**统一配置注册表**：全部行为开关收敛于 `src/config_schema.py`（分层来源：**运行时覆盖 > 环境变量 > settings.json > 内置默认**），`python src/cli.py --list-config` 打印全表；完整清单见 [docs/配置总览.md](docs/配置总览.md)（自动生成，防文档漂移）。新增开关 = 一行 `register()`，零改中心代码。常用行为开关：

| 开关 | 配置项 | 说明 |
|---|---|---|
| 省本地 CPU | `EMBED_OFF=1` 或 `embed.off` | 跳过 bge-m3 向量索引/设定向量化（FTS 检索不受影响；暗线聚类所需证据向量不跳过） |
| 全量重抽设定 | `--fresh` 或 `collect.fresh` | 删除旧 settings_graph 增量，设定全量重抽（全书重跑必须加） |
| 联网搜索 | `EXTERNAL_SEARCH=1` 或 `rag.external_search` | 启用 knowledge_router L3 联网钩子（需自行实现搜索接口） |
| 类别加载 | `--genre 宫斗` | 按类别加载公版体系域（默认 global 不开放） |

### 运行效率参考（300 章实测，dots.ai 主后端）

| 阶段 | 50 章 | 300 章 | 说明 |
|---|---|---|---|
| extract 场景抽取 | ~10-20 分钟 | ~1 小时+ | 每场景 1 次 LLM |
| collect 四维收集 | ~15-30 分钟 | ~1.5-2 小时 | **最大耗时项**（每场景 2-3 次 LLM） |
| mine 四维挖掘 | ~5-10 分钟 | ~30-60 分钟 | 簇级批量推理 |
| annotate 创作解析 | **0.8 分钟** | **4.3 分钟** | 实测 0.9s/场景 |
| review 编辑评述 | ~15s | ~15s | 5 板块 5 次调用，与规模无关 |
| viz 可视化 | ~30s | ~1 分钟 | 纯本地渲染，零 LLM |

**全流程**（extract→collect→mine→viz）：50 章 30-60 分钟；300 章预计 **1.5~3 小时**（dots.ai 并发 1 限流 RPM=60）。

### 关键参数速查

```bash
# 快模型跑高频 + 强模型做评述（成本/速度最优组合）
python src/cli.py collect --backend groq-llama          # 🆓 数百 tok/s
python src/cli.py mine    --backend groq-llama --role mine=deepseek-chat
python src/cli.py review  --backend dots3-note --role review=claude-sonnet

# 推理深度 / 并发 / 类别库
--doubt-index 0.5        # 0.3 保守 → 0.7 激进（簇触发阈值）
--parallel 4             # 不要超过预设 concurrency（dots.ai=1, 强模型=2-4）
--genre 诡秘             # 按类别加载公版体系图作参考（默认不加载）
```

- **增量复用**：`extract` 有缓存秒过；固定 `run.date_suffix` 可让设定图谱跨次累积；只改可视化用 `viz --src <已有stage1目录>` 纯本地渲染
- **全字段表**：settings.json（novel/llm/run.archive）、models.json（18 预设字段）、CLI 参数、环境变量（`LLM_API_KEY`/`LLM_ROLE_MODELS`/`NOVEL_NAME` 等）→ 见上方文档

## ⚙️ 环境准备

```bash
# Python ≥ 3.10（仅标准库即可跑通；jieba 用于词频，可选）
pip install jieba

# 本地模式：Ollama + 模型（零成本/离线）
ollama pull qwen3:8b      # 抽取/挖掘
ollama pull bge-m3        # RAG 向量（可选，未拉则自动降级规则切分 + FTS5）

# 云端模式：只需 API key
set LLM_API_KEY=你的key   # dots.ai / 智谱 / DeepSeek / Kimi 通用
```

## ⚖️ 版权与合规

- `resource/` 下的小说原文**不入库**（.gitignore 硬性排除），请仅处理你有权使用的作品
- demo 为公开作品的评论性可视化（短引用 + 统计），用途为技术展示
- 模型 key 严禁写入任何入库文件

## 已知取舍

1. 本地小模型偶发吐坏 JSON；换 8b 或云端全成功
2. 云端 dots.ai 限流，全量跑需耐心（300 章约 2-4 小时）
3. 排比/修辞为规则近似检测，数值仅作风格参考
4. 人物重名依赖别名图确定性归并，个别同人可能仍拆分
5. 向量相似度阈值 0.62、`doubt_index` 0.5 为《诡秘之主》实测校准，换书可调

## 📄 License

MIT — 详见 [LICENSE](LICENSE)。
