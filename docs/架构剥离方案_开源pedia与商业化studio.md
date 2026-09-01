# 架构剥离方案：开源 pedia × 商业化 studio

> 日期：2026-09-01
> 目标：把「开源拆书引擎」与「在线优质化服务」拆成两个物理隔离的工程，开源仓只保留 pedia + 数据源。
> 命名约定：开源仓本地目录与统称均为 **pedia**（GitHub 远程仓库原名 `skkbsgzf/NovelPedia`，可后续在 GitHub 侧改名）；商业化仓为 **studio**（目录 `D:\StoryScienceLab\studio`）。

---

## 一、背景与决策

开源侧（GitHub: `skkbsgzf/NovelPedia`，MIT）要交付的是 **pedia —— 一个 wiki 式百科**：采集 + 推理 agent → 输出可编辑的 pedia HTML + 数据源。
商业化侧是**在线优质化服务**：人物究极小传、深度人物分析、专家团会诊、演员表演人设卡、剧组工作台。

用户已拍板三个决策：

| 决策项 | 结论 |
|---|---|
| 商业化工程位置 | `D:\StoryScienceLab\studio` |
| viz 产物精简 | **只留 pedia** — 删独立详情页/图谱页，保留数据生成 |
| studio 首批内容 | 骨架 + 设计文档 + 角色卡 demo（Web 服务雏形暂不搬） |

---

## 二、诊断：产物混乱的根因

`cli.py viz` 原先**一次生成 4 套互相重叠的产物**：

| 产物 | 体积 | 生成者 | 判定 |
|---|---|---|---|
| `index.html` | 28 MB | `index_template.html` | ✅ **pedia**，唯一该要的成品 |
| `book_detail.html` + `detail_data.json` | 2.7 MB | `build_detail.py` + `detail_template.html` | ⚠️ 旧式详情页，与 pedia 重叠 |
| `knowledge_graph.html` + `graph_data.json` | 21 MB | `export_graph.py` + `graph_template.html` | ⚠️ 独立图谱页，力导图**已内联进 pedia** |
| `personality.json` | — | `gen_personality.py` | 数据供给，保留 |

**关键约束**（查 pedia 模板占位符确认）：`index_template.html` 消费 21 个数据占位符，包含
`__DETAIL_DATA__` / `__GRAPH_DATA__` / `__PERSONALITY_DATA__` / `__EDITOR_REVIEWS__` / `__SCENE_ANNOTATIONS__`。

> 所以 `build_detail.py`、`export_graph.py`、`gen_personality.py`、`editor_review.py`、`scene_annotator.py`
> **一律不能删** —— 它们是 pedia 的数据供给方。要清的是**渲染副产物**，不是数据源模块。

---

## 三、边界定义（铁律）

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  pedia (开源, MIT)          │        │  studio (闭源, 商业化)        │
│  ─────────────────────────  │  单向  │  ──────────────────────────  │
│  采集 + 推理 agent           │ ─────► │  深度人物分析 / 专家团会诊    │
│  产出 pedia (wiki 式 HTML)   │  只读  │  演员表演人设卡 / 剧组工作台  │
│  产出数据源 (db + JSON)      │        │  积分 / 会员 / 在线跑批       │
└─────────────────────────────┘        └──────────────────────────────┘
```

1. **开源仓不反向依赖 studio** —— 不允许出现 studio 的 import / 路径 / 商业化概念。
2. **只读消费** —— studio 读开源仓产物，不改开源仓任何文件。
3. **GitHub 上不保留任何优质化服务代码** —— 商业化逻辑一律进 `studio/`。

---

## 四、执行清单（已完成）

### 4.1 studio 工程骨架

```
studio/
├── README.md          定位 / 边界 / 数据源契约
├── docs/              5 份商业化设计文档（编号排序）
├── demos/             角色卡样例_克莱恩.html
├── src/               服务层代码（待实现）
├── web/               前端（待实现）
└── data/              运行时数据
```

### 4.2 文档与 demo 归位

| 原位置 | 新位置 |
|---|---|
| `StoryScienceLab_商业化与落地指南.md` | `studio/docs/01_商业化与落地指南.md` |
| `StoryScienceLab_人物小传与剧组工作台_产品设计.md` | `studio/docs/02_...` |
| `StoryScienceLab_深度人物分析_采集与推理方案.md` | `studio/docs/03_...` |
| `StoryScienceLab_产品蓝图2.0.md` | `studio/docs/04_...` |
| `StoryScienceLab_用户系统设计方案.md` | `studio/docs/05_...` |
| `产品设计_角色卡样例_克莱恩.html` | `studio/demos/角色卡样例_克莱恩.html` |

根目录保留的开源技术文档：`Pipeline倒退式优化方案` / `Pipeline架构v3` / `stage2双读与CLI面板` / `提取优化_汇总` / `调研_*` / `四大样板间_惊艳目标定义`。

### 4.3 死代码清理（归档，非删除 → `archive/novel_pipeline_死代码_20260901/`）

| 文件 | 判定依据 |
|---|---|
| `src/check_viz.py` | 全仓零引用 |
| `src/final_report.py` | 仅被 `cli.py` 的 `report` 命令引用（一次性「本地 vs 云端」评测） |
| `_verify_cdp.py` | 一次性 Edge CDP 渲染校验，仅文档提及 |
| `_verify_shots/` | 上述校验的截图产物 |
| `_legacy/` | 已 gitignore 的旧代码 |
| `_run_*.log` | 跑批日志 |

同步移除 `cli.py` 的 `cmd_report` 函数、`report` 命令注册、帮助文本（3 处）。

> **`_web_demo.py` / `_demo_run.py` 保留** —— 它们是在线服务雏形，按决策暂不搬迁。

### 4.4 viz 产物精简

| 改动 | 文件 |
|---|---|
| 删除 `book_detail.html` 渲染段 | `src/build_detail.py` |
| 删除 `knowledge_graph.html` 渲染段 + 14 处 `_p()` 调试计时 + `TEMPLATE`/`OUT_HTML` 死变量 | `src/export_graph.py` |
| 归档 `detail_template.html`(36KB) / `graph_template.html`(21KB) | → `archive/novel_pipeline_废弃模板_20260901/` |
| 更新 viz docstring 与帮助文本 | `src/cli.py` |
| 同步更新产物说明表与目录树 | `README.md`、`docs/技术文档.md` |

**viz 现在的产物**（全部为数据源 + 唯一页面）：

```
outputs/<小说名>_<日期>/
├── index.html          ★ pedia 主页面（自包含，内嵌全部数据）
├── detail_data.json    人物/设定/场景/文风结构化数据
├── graph_data.json     关系图谱 nodes/edges
└── personality.json    人物性格六维
```

### 4.5 顺带修复：`build_detail.py` 分词性能坑

排查冒烟超时时发现（**既有问题，非本次引入**）：

- 环境**未安装 jieba**，而 `requirements.txt` 承诺「仅标准库、零第三方包」→ 必然走兜底分词路径。
- 兜底路径两处灾难：
  1. 对全书 300 万字逐字符做 2–4 字滑窗 + `re.search`（≈900 万次正则）；
  2. 去重用 `any(w in k for k in kept)` —— O(唯一词 × 保留词) 平方级，1308 章直接卡死数十分钟。

**修复**（保持输出语义完全等价，不引入第三方依赖）：

1. 先按标点切分文本，只对干净片段滑窗（跨标点窗口原逻辑本就丢弃）；
2. 去重改用「已保留词的全部子串集合」做 O(1) 命中。

验证：小样本新旧实现**输出完全一致**，提速 3.3x；全书级别加速远大于此（消除平方项）。

---

## 五、数据源契约（studio ← pedia）

| 数据 | 路径（相对 `outputs/<书名>/`） | 说明 |
|---|---|---|
| pedia 页面 | `index.html` | Wikipedia 式成品，可直接二次加工 |
| 结构化数据 | `detail_data.json` | 人物/设定/场景/文风主数据 |
| 关系图谱 | `graph_data.json` | 实测 1607 节点 / 8682 边（诡秘之主 1308 章） |
| 人格六维 | `personality.json` | 目前为占位默认值，待 stage3 深挖 |
| 场景批注 | `scene_annotations.json` | 编辑视角批注 |
| 原始事实库 | `data/stage1_v2_<N>_<书名>.db` | SQLite，`scenes.who_json` 等 |

---

## 六、遗留问题（本次未修，需单独排期）

### 6.1 ⚠️ `STAGE2_DIR` 不存在 —— 人物/设定数据为 0

**现象**：`config.STAGE2_DIR = outputs/诡秘之主_20260831_full/stage2` **目录不存在**。
新架构 `mine`（`stage2_mine.py`）产出 `characters_resume.json` / `settings_system.json`，但**没写进 stage2/，产物散落在 stage1/**。

**影响**：`build_detail.py` 从 stage2 读人物/设定 → 全空（`人物: 0 | 图谱: 0 节点`）。
`export_graph.py` 已加 stage1 fallback 所以能出数据，但 `build_detail.py` 还没有。

**修法**（二选一）：
- A. 改 `mine` 把产物正确写入 `STAGE2_DIR`（治本，但需重跑 mine）；
- B. 给 `build_detail.py` 也加 stage1 fallback，对齐 `export_graph.py` 的做法（治标，快）。

### 6.2 `gen_personality.py` 产出占位默认值

输入仅 name + 身份(截 120 字) + 弧光(截 150 字)，且 `except: v=50` 静默兜底 → 六维全是 `{50,50,50,50,50,50}`。
`character_agent.collect_from_actinfo()` 已产出 `sayings/doings/co_occurrences` 但未被统计（白捡的证据源）。

### 6.3 studio 代码尚未动工

`src/adapter.py` / `deep_profile.py` / `actor_card.py` / `web/` 均为空，待 pipeline 稳定后再写。

---

## 七、后续待办

- [ ] 修 STAGE2 缺口（推荐方案 B 先行，保证 pedia 人物数据不全空）
- [ ] 修复 `gen_personality.py` 输入与静默默认值
- [ ] `studio/src/adapter.py` — 数据源适配层
- [ ] `studio/src/deep_profile.py` — 深度人物分析四模块
- [ ] `studio/src/actor_card.py` — 演员表演人设卡生成器
- [ ] 开源前全仓合规扫描（确认无商业化代码残留、无密钥）
