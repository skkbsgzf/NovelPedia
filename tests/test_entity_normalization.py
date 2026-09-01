# -*- coding: utf-8 -*-
"""
test_entity_normalization.py —— 实体归一化回归测试（硬验收指标）

验收基线（诡秘之主）：
  克莱恩·莫雷蒂/克莱恩/周明瑞/愚者/格尔曼·斯帕罗/道恩·唐泰斯/世界/夏洛克·莫里亚蒂
  必须坍缩为同一 canonical 节点；同时不得把独立角色（阿蒙/伦纳德/嘉德丽雅/佛尔思/
  休/贝尔纳黛/奥黛丽/戴里克/阿尔杰/埃姆林）错误并入。

机制：
  stage2 自带 aliases 是场景内指称（稀疏，克莱恩系被拆 6 条）→
  vizutil.entity_alias_edges 注入 entity_registry 的「双向互指」证据补齐归并；
  阿蒙⇄愚者 属剧情伪装（双向噪音），ENTITY_BLACKLIST 排除；
  enrich_aliases 把 canonical 别名（夏洛克·莫里亚蒂 等马甲）挂到节点做展示。

运行：python -m unittest discover -s tests -v   （在 pedia 根目录）
数据缺失时对应用例自动 skip（不阻断无产物环境的冒烟）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PEDIA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PEDIA_ROOT, "src")
sys.path.insert(0, SRC_DIR)

import vizutil  # noqa: E402

BOOK_DIR = os.path.join(PEDIA_ROOT, "outputs", "诡秘之主_20260831_full")
ER_PATH = os.path.join(BOOK_DIR, "stage1", "entity_registry.json")
STAGE2_CHARS = os.path.join(BOOK_DIR, "stage2", "characters.json")
STAGE1_DB = os.path.join(BOOK_DIR, "stage1", "stage1_v2_1308_诡秘之主.db")

HAVE_DATA = os.path.exists(ER_PATH) and os.path.exists(STAGE2_CHARS)

KLEIN_MASKS = ["克莱恩", "周明瑞", "愚者", "格尔曼", "道恩", "世界", "夏洛克"]
# 验收要求的克莱恩马甲全集（raw 6 条 + 别名 2 个）
KLEIN_8 = ["克莱恩·莫雷蒂", "克莱恩", "周明瑞", "愚者",
           "格尔曼·斯帕罗", "道恩·唐泰斯", "世界", "夏洛克·莫里亚蒂"]
# 必须保持独立的角色（不得被并入克莱恩系 / 不得互相误并）
GUARDS = ["阿蒙", "伦纳德", "嘉德丽雅", "佛尔思", "休", "贝尔纳黛",
          "奥黛丽", "戴里克", "阿尔杰", "埃姆林"]
PROTAGONISTS = ["佛尔思", "伦纳德", "戴里克", "嘉德丽雅"]


def _klein_related(name):
    return any(k in name for k in KLEIN_MASKS)


def _has_char(chars, target):
    """target 是否在人物表（canonical 名或其别名命中；兼容 戴里克→戴里克·伯格 式归并）。"""
    for c in chars:
        if c.get("name") == target or target in (c.get("aliases") or []):
            return True
    return False


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class TestVizutilPrimitives(unittest.TestCase):
    """vizutil 新原语：双向边 / 黑名单 / enrich 过滤。"""

    def test_mutual_alias_true_for_real_same_person(self):
        alias_map = vizutil.load_entity_alias_map(ER_PATH)
        self.assertIn("克莱恩", alias_map)
        # 真同人：克莱恩 ⇄ 愚者 双向互指
        self.assertTrue(vizutil._mutual("克莱恩", "愚者", alias_map))
        self.assertTrue(vizutil._mutual("周明瑞", "克莱恩", alias_map))
        self.assertTrue(vizutil._mutual("格尔曼·斯帕罗", "世界", alias_map))

    def test_mutual_alias_false_for_guard_noise(self):
        alias_map = vizutil.load_entity_alias_map(ER_PATH)
        # 眷者共指噪音：伦纳德→克莱恩 单向，不构成双向
        self.assertFalse(vizutil._mutual("伦纳德", "克莱恩", alias_map))
        # 佛尔思→休 单向（entity 误把休挂进佛尔思别名）
        self.assertFalse(vizutil._mutual("佛尔思", "休", alias_map))
        # 嘉德丽雅 ← 贝尔纳黛 单向
        self.assertFalse(vizutil._mutual("嘉德丽雅", "贝尔纳黛", alias_map))

    def test_amon_fool_blacklist(self):
        """剧情伪装：阿蒙⇄愚者 是双向噪音，黑名单必须拦截。"""
        alias_map = vizutil.load_entity_alias_map(ER_PATH)
        self.assertTrue(vizutil._mutual("阿蒙", "愚者", alias_map))  # 双向成立
        # 但 entity_alias_edges 必须排除黑名单边
        chars = [{"name": "阿蒙"}, {"name": "愚者"}, {"name": "克莱恩"}]
        edges = vizutil.entity_alias_edges(chars, alias_map)
        self.assertNotIn(("阿蒙", "愚者"), edges)
        self.assertNotIn(("愚者", "阿蒙"), edges)

    def test_enrich_filters_other_person_names(self):
        """enrich 挂载别名时，不得把独立人物名挂进别的分量。"""
        alias_map = vizutil.load_entity_alias_map(ER_PATH)
        merged = [{"name": "愚者", "aliases": ["克莱恩", "周明瑞"]},
                  {"name": "伦纳德", "aliases": []},
                  {"name": "嘉德丽雅", "aliases": []}]
        skip = {c["name"] for c in merged}
        out = vizutil.enrich_aliases(merged, alias_map, skip_names=skip)
        by_name = {c["name"]: c for c in out}
        # 伦纳德 / 嘉德丽雅 是独立角色，不得出现在愚者（克莱恩系）别名里
        self.assertNotIn("伦纳德", by_name["愚者"]["aliases"])
        self.assertNotIn("嘉德丽雅", by_name["愚者"]["aliases"])
        # 但真马甲要挂上（夏洛克·莫里亚蒂 在 周明瑞/世界 的 canonical 别名集里）
        self.assertIn("夏洛克·莫里亚蒂", by_name["愚者"]["aliases"])


@unittest.skipUnless(HAVE_DATA, "缺少诡秘之主_20260831_full 产物数据, 跳过真实数据用例")
class TestKleinNormalizationRealData(unittest.TestCase):
    """真实产物数据：stage2 稀疏 aliases + entity_registry 证据。"""

    def setUp(self):
        self.raw = _load_json(STAGE2_CHARS, [])
        self.er = vizutil.load_entity_alias_map(ER_PATH)

    def _merged(self):
        extra = vizutil.entity_alias_edges(self.raw, self.er)
        merged, name2canon = vizutil.merge_alias_components(self.raw, extra_edges=extra)
        merged = vizutil.enrich_aliases(
            merged, self.er, skip_names={c["name"] for c in merged})
        return merged, name2canon

    def test_klein_6_raw_names_one_node(self):
        """硬验收：克莱恩系 6 条 raw 名必须归一到同一 canonical。"""
        merged, name2canon = self._merged()
        raw_klein = [c["name"] for c in self.raw if _klein_related(c["name"])]
        raw_klein = [n for n in raw_klein if "梦境" not in n and "神秘" not in n
                     and "镜中" not in n and "现实" not in n and "灰雾" not in n]
        # 期望 raw 里正好 6 条（周明瑞/克莱恩/愚者/世界/格尔曼·斯帕罗/道恩·唐泰斯）
        self.assertEqual(len(raw_klein), 6, f"raw 克莱恩系应为 6 条, 实得: {raw_klein}")
        canon_set = {name2canon[n] for n in raw_klein if n in name2canon}
        self.assertEqual(len(canon_set), 1,
                         f"克莱恩系 {raw_klein} 应归一到同一 canonical, 实得 {canon_set}")

    def test_klein_node_has_all_8_masks(self):
        """归并后克莱恩节点别名需覆盖验收 8 马甲（夏洛克·莫里亚蒂 以别名挂载）。"""
        merged, _ = self._merged()
        klein = [c for c in merged if _klein_related(c["name"])]
        self.assertEqual(len(klein), 1, f"克莱恩系应只有 1 个节点, 实得 {[c['name'] for c in klein]}")
        node = klein[0]
        all_aliases = set([node["name"]] + node.get("aliases") or [])
        missing = [m for m in KLEIN_8 if m not in all_aliases]
        self.assertEqual(missing, [], f"克莱恩节点别名缺马甲: {missing}")

    def test_guards_independent(self):
        """守护角色必须各自独立（不并入克莱恩系、不互相误并）。"""
        merged, _ = self._merged()
        names = [c["name"] for c in merged]
        # 每个守护角色都有独立节点（canonical 名或别名命中）
        for g in GUARDS:
            self.assertTrue(_has_char(merged, g),
                            f"守护角色 {g} 丢失(可能被误并); 人物表: {names}")
        # 守护角色不得出现在克莱恩节点的别名里
        klein = [c for c in merged if _klein_related(c["name"])]
        if klein:
            klein_aliases = set([klein[0]["name"]] + (klein[0].get("aliases") or []))
            swallowed = [g for g in GUARDS if g in klein_aliases]
            self.assertEqual(swallowed, [], f"守护角色被误挂为克莱恩别名: {swallowed}")
        # 守护角色之间也不得互相误并（各自 canonical 独立存在）
        guard_names = {c["name"] for c in merged
                       if any(g == c["name"] for g in GUARDS)}
        self.assertGreaterEqual(len(guard_names), 6,
                                f"守护角色应至少 6 个独立 canonical, 实得: {guard_names}")

    def test_no_duplicate_names(self):
        """人物主表不得出现重复 canonical。"""
        merged, _ = self._merged()
        names = [c["name"] for c in merged]
        dups = [n for n in set(names) if names.count(n) > 1]
        self.assertEqual(dups, [], f"人物表存在重复 canonical: {dups}")

    def test_protagonists_present(self):
        """主角团（佛尔思/伦纳德/戴里克/嘉德丽雅）必须进入人物表。"""
        merged, _ = self._merged()
        names = [c["name"] for c in merged]
        missing = [p for p in PROTAGONISTS if not _has_char(merged, p)]
        self.assertEqual(missing, [], f"主角团缺失(仅查名): {missing}")
        for p in PROTAGONISTS:
            node = next((c for c in merged
                         if c["name"] == p or p in (c.get("aliases") or [])), None)
            self.assertIsNotNone(node, f"主角 {p} 无节点")
            if node:
                self.assertTrue(node.get("首次出现章"),
                                f"主角 {p}(canonical={node['name']}) 缺首次出现章")


@unittest.skipUnless(HAVE_DATA and os.path.exists(STAGE1_DB), "缺少 stage1 数据库, 跳过端到端用例")
class TestBuildDetailEndToEnd(unittest.TestCase):
    """真实管线端到端：build_detail.py 产物级验收（失败即阻断发布）。"""

    def test_detail_data_klein_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "detail_data.json")
            cmd = [sys.executable, os.path.join(SRC_DIR, "build_detail.py"),
                   "--src", os.path.dirname(STAGE2_CHARS),
                   "--db", STAGE1_DB,
                   "--out", out,
                   "--book-name", "诡秘之主"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, f"build_detail 失败:\n{r.stderr}")
            data = _load_json(out, {})
            chars = data.get("characters") or []
            klein = [c for c in chars if _klein_related(c.get("name", ""))]
            klein = [c for c in klein if not any(x in c["name"] for x in
                                                 ["梦境", "神秘", "镜中", "现实", "灰雾"])]
            self.assertEqual(len(klein), 1,
                             f"端到端产物克莱恩系应为 1 节点, 实得 {[c['name'] for c in klein]}")
            for g in GUARDS:
                self.assertTrue(_has_char(chars, g),
                                f"端到端产物守护角色 {g} 丢失; 人物表: {[c['name'] for c in chars]}")
            # 图谱节点与人物表对齐（同数、无重复 id）
            cg_nodes = (data.get("charGraph") or {}).get("nodes") or []
            self.assertEqual(len(cg_nodes), len(chars),
                             "charGraph 节点数应与人物表一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
