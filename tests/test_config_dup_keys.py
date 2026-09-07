#!/usr/bin/env python3
"""test_config_dup_keys — config.yaml 重复键回归门。

2026-09-07 实锤：OSM 块内 `timeout: 6`（前）与 `timeout: 10`（尾）同名键
重复，YAML 后者静默覆盖前者——声明的 6s 硬超时从未生效（geo 案 12.4s 拖尾
主因之一）。PyYAML 默认静默覆盖不报警，本门用自定义 Loader 显式拒绝
config 任何层级的重复键。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class _NoDupLoader(yaml.SafeLoader):
    """同 SafeLoader，但 mapping 出现重复键即抛错。"""


def _construct_no_dup(loader, node, deep=False):
    seen: dict = {}
    for k_node, v_node in node.value:
        k = loader.construct_object(k_node, deep=deep)
        if k in seen:
            raise ValueError(
                f"config 重复键 {k!r} @ line {k_node.start_mark.line + 1} "
                f"(YAML 同名键后者静默覆盖前者，属漂移)")
        seen[k] = v_node
    return {k: loader.construct_object(v, deep=deep) for k, v in seen.items()}


_NoDupLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_no_dup)


class TestConfigNoDuplicateKeys(unittest.TestCase):
    def test_config_yaml_no_duplicate_keys(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDupLoader)
        self.assertIsNotNone(data)
        self.assertIn("engines", data)

    def test_external_specs_no_duplicate_keys(self):
        specs_dir = Path(__file__).resolve().parent.parent / "engines" / "specs"
        for f in sorted(specs_dir.glob("*.yaml")):
            try:
                yaml.load(f.read_text(encoding="utf-8"), Loader=_NoDupLoader)
            except ValueError as e:
                self.fail(f"{f.name}: {e}")


if __name__ == "__main__":
    unittest.main()
