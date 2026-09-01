# 规格划分 v2：pedia（开源）× studio（闭源）

> 日期：2026-09-01（基于 `架构剥离方案_开源pedia与商业化studio.md` 修正）
> 修正点：开源仓**不做深度人格分析**；深度人格分析整体划归 studio；实体归一化列为 pedia 硬验收指标。
> 本文档对旧方案冲突处具有**覆盖效力**。

---

## 〇、三条铁律（不变）

1. **开源仓不反向依赖 studio。** `pedia` 内不得 import / 引用 / 出现任何 studio 概念（含 `gen_personality`、`deep_profile`、`actor_card`）。
2. **studio 只读消费 pedia 产物**，不改开源仓任何文件。
3. **GitHub 不保留任何商业化/深度分析代码。**

---

## 一、pedia（开源，MIT）范围

### 1.1 做（核心交付）
| 模块 | 说明 |
|---|---|
| 故事提取 | stage1 采集 + stage2 挖掘（人物/设定/场景/文风） |
| RAG | 本地知识库检索（db + JSON 索引），供自托管问答 |
| **Pedia 归一化** | 把抽取结果统一成 `pedia/v1` schema，跨书可对齐、可裁剪、可回流 |

### 1.2 不做（本次明确划出）
- ❌ **深度人格分析 / 人物"揉碎"**。包括但不限于：六维性格向量、心理剖面、多维人格、专家团会诊、演员表演人设卡。
- ❌ 任何需要"主观推断人物内在"的 LLM 打分（见 §三 对 `gen_personality.py` 的处置）。

### 1.3 ⚠️ 实体归一化硬指标（验收门，必须满足）

Pedia 归一化的最低正确性门槛：**同一人物必须坍缩为一个 canonical 实体**，不得因别名/马甲分裂成多节点。

- **验收样例（诡秘之主）**：`克莱恩·莫雷蒂 / 克莱恩 / 周明瑞 / 愚者 / 格尔曼·斯帕罗 / 道恩·唐泰斯 / 世界 / 夏洛克·莫里亚蒂` 必须在最终 pedia / 图谱里归为**同一个人物节点**。
- **现有能力**（已具备，需保护 + 加回归测试）：
  - `entity_registry.json` 每条角色带 `canonical` + `aliases`，原始数据已把 `周明瑞` 挂在 `克莱恩`/`克莱恩·莫雷蒂` 的别名链上；
  - `build_detail.merge_alias_components` 用别名连通分量（union-find）归并同人；
  - `export_graph.py` 的 `alias` 映射把 `who_json` 对齐到规范名。
- **必须补的验收件**：
  - 加 **实体归一化回归测试**：断言上述名字集合在 `detail_data.json` / `graph_data.json` 中映射到同一 `name`；CI 或冒烟跑批后自动校验，失败即阻断。
  - 测试数据固化在 `tests/fixtures/entity_resolution_诡秘之主.json`，作为永不退化的基线。

> 说明：机制在，但数据碎片化严重（一个角色裂成 8+ canonical）。"分清"= 用回归测试锁死合并结果，防止后续挖掘/抽取改动把合并打散。

---

## 二、studio（闭源商业化）范围

### 2.1 做（核心交付）
| 模块 | 说明 | 代码落点（待实现） |
|---|---|---|
| 人物"揉碎"——深度人格分析 | 六维性格 + 心理剖面 + 动机/恐惧/价值排序 + 关系深层结构 | `studio/src/deep_profile.py` |
| 人物究极小传 | 基于 pedia 数据源的深度扩写 | `studio/src/adapter.py` 读取后扩写 |
| 专家团会诊 | 多视角交叉分析 | 后续 |
| 演员表演人设卡 | 给选角/表演指导用，非纯视觉卡 | `studio/src/actor_card.py` |
| 在线门户 / 积分会员 | Web 服务 + 算力计费 | `studio/web/` |

### 2.2 数据来源（只读 pedia 契约）
- `index.html`（wiki 成品，二次加工用）
- `detail_data.json`（人物/设定/场景主数据）
- `graph_data.json`（关系图谱）
- `data/stage1_v2_<N>_<书名>.db`（原始事实库，含 `who_json`）
- ⚠️ **不再依赖 `personality.json`**——该文件从 pedia 产出中移除，改由 studio 自己生成（见 §三）。

---

## 三、`gen_personality.py` 的处置（修正旧方案冲突）

旧 `架构剥离方案` 把 `personality.json` 列为 pedia 产物（"数据供给，保留"），并写"在 pedia 修 gen_personality"。**按本 v2，二者均撤销：**

1. **从 pedia 移除深度人格生成**：
   - `pedia/src/gen_personality.py` → **迁移到 `studio/src/deep_profile.py`**（作为深度分析的种子实现，可大幅增强，仅留姓名+身份+弧光太浅，应接入 sayings/doings/co_occurrences 等已抽取证据）。
   - `pedia/outputs/.../personality.json` 不再由开源仓产出；pedia 模板 `index_template.html` 的 `__PERSONALITY_DATA__` 占位若仅用于"深度雷达"，应从开源 pedia 移除（保留轻量"存在感/活跃度"指标可由 story extraction 直接统计，不属深度人格）。
2. **studio 接手**：`deep_profile.py` 读 pedia 数据源 → 输出深度人格产物（六维及更细粒度），供小传/人设卡消费。
3. **可接受的非深度替代**：若开源 pedia 想展示一点人物"活跃度/戏份"，用 stage1 已统计的 `appearances` 频次即可，零 LLM 推断、零主观打分——这不违反"不做深度人格分析"。

---

## 四、数据源契约修订（pedia → studio）

| 数据 | 归属 | 说明 |
|---|---|---|
| `index.html` | pedia | wiki 成品 |
| `detail_data.json` | pedia | 人物/设定/场景（**已做实体归一化**） |
| `graph_data.json` | pedia | 关系图谱（**同人已合并**） |
| `scene_annotations.json` | pedia | 编辑批注 |
| `*.db` | pedia | 原始事实库 |
| **`personality.json`（深度六维）** | **studio** | **不再由开源仓产出** |
| 深度人格 / 小传 / 人设卡 | **studio** | 商业化增值，闭源 |

---

## 五、执行清单（待排期）

- [ ] **实体归一化回归测试**：固化 `克莱恩=周明瑞=愚者=…` 基线，冒烟自动校验（pedia）
- [ ] 迁移 `gen_personality.py` → `studio/src/deep_profile.py`（studio）
- [ ] 从 pedia 模板移除深度人格占位（或改为"活跃度"统计）（pedia）
- [ ] `studio/src/adapter.py` 读取 pedia 数据源（studio）
- [ ] `studio/src/actor_card.py` 人设卡生成（studio）
- [ ] 开源前合规扫描：确认 pedia 无 studio 代码、无密钥（pedia）

---

## 六、一句话总结

> **pedia 只做"把故事拆干净 + 归一化 + 可检索"（克莱恩和周明瑞必须是同一个人）；人物"揉碎"的深度活儿，全部进 studio。**
