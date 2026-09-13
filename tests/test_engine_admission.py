#!/usr/bin/env python3
"""engine_admission 契约测试：准入状态的 blocked/reason 必须自洽。

## 守的是什么缺陷

`record_validation` 曾写 `reason = reason or current.get("reason")`——把**历史**
失败原因粘滞下来。典型触发序列（批次九实测）：

  1. `engine_validate --stage health` 首次失败 → 留下 `reason=health_failed`
  2. 修好后再跑 `--stage all --admit`：health/quality 双 pass、blocked=False
  3. 但本次 reason 为空 → 回退成历史的 `health_failed`
  4. 准入记录于是自相矛盾：`blocked=false`（或旧记录里 blocked=true）
     配 `reason=health_failed`，而 `health.status=pass`

后果：24 个引擎（批次九全部 + realtime_index）在
`~/.cache/unified-search/admission/*.json` 里被标 `blocked=true` 且
`reason=health_failed`，`routable=False` — 引擎「装了没通电」：能单跑、
能被显式调用，但永远不参与自动路由。这是批次九最重要的回归。

## 判据

- 本次未 block 时，reason 必须为空（不得残留历史失败原因）
- 本次 block 时，reason 不得为空
- blocked=False 的记录，其 health.status 不得是 fail（否则状态层自相矛盾）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestAdmissionReasonConsistency(unittest.TestCase):
    """reason 必须反映本次判定，不粘滞历史。"""

    def setUp(self):
        # 隔离状态目录：准入记录写入 ~/.cache 下，测试不得污染真实状态。
        #
        # 注意：**不要** importlib.reload(engine_admission)。reload 会在
        # sys.modules 里换掉模块对象，而 engine_status / engines 等模块在
        # 导入期已持有旧对象的引用——同进程后续用例（如
        # test_engine_catalog::test_routable_only_flag_actually_filters）
        # 会看到「CLI 子进程 191 个 routable」与「本进程旧模块 192 个」
        # 不一致而失败（实测污染，已定位）。改为 monkeypatch 模块级
        # 状态目录解析函数即可，不换模块对象。
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("ARGO_STATE_DIR")
        os.environ["ARGO_STATE_DIR"] = self._tmp.name
        import engine_admission
        self.mod = engine_admission
        # 状态目录若被模块缓存过，显式失效（不 reload 模块）
        for attr in ("_STATE_DIR_CACHE", "_state_dir_cache", "_DIR_CACHE"):
            if hasattr(self.mod, attr):
                setattr(self.mod, attr, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ARGO_STATE_DIR", None)
        else:
            os.environ["ARGO_STATE_DIR"] = self._old
        # 清掉本测试写入的隔离记录，并让后续用例重新读到原目录
        for attr in ("_STATE_DIR_CACHE", "_state_dir_cache", "_DIR_CACHE"):
            if hasattr(self.mod, attr):
                setattr(self.mod, attr, None)
        self._tmp.cleanup()

    def test_stale_reason_is_cleared_on_success(self):
        """核心回归：先失败留 reason，再成功必须清掉。"""
        m = self.mod
        # 1) 首次 health 失败
        m.record_validation("eng_x", stages_passed=["health"],
                            blocked=True, reason="health_failed",
                            health={"ok": False, "status": "fail"})
        rec = m.load_admission("eng_x")
        self.assertTrue(rec["blocked"])
        self.assertEqual(rec["reason"], "health_failed")

        # 2) 修好后重跑：health+quality 全 pass
        m.record_validation("eng_x", stages_passed=["health", "quality"],
                            quality_score=1.0, admit=True,
                            health={"ok": True, "status": "pass"},
                            quality={"ok": True, "quality_score": 1.0})
        rec = m.load_admission("eng_x")
        self.assertFalse(rec["blocked"], "本次通过必须解 block")
        self.assertEqual(rec["reason"], "",
                         "不得残留历史 health_failed（旧实现的粘滞 bug）")
        self.assertTrue(m.is_admitted("eng_x"))

    def test_blocked_record_always_has_reason(self):
        """被 block 时 reason 不得为空（否则无法归因）。"""
        m = self.mod
        m.record_validation("eng_y", stages_passed=["health"],
                            blocked=True)
        rec = m.load_admission("eng_y")
        self.assertTrue(rec["blocked"])
        self.assertTrue(rec["reason"], "blocked 必须带原因")

    def test_explicit_reason_is_kept(self):
        """显式传入的 reason 优先，不被覆盖。"""
        m = self.mod
        rec = m.record_validation("eng_z", stages_passed=["health"],
                                  blocked=True, reason="quota_exhausted")
        self.assertEqual(rec["reason"], "quota_exhausted")

    def test_stages_passed_are_merged(self):
        """分阶段跑（先 health 后 quality）必须累积，不覆盖。"""
        m = self.mod
        m.record_validation("eng_m", stages_passed=["health"],
                            health={"ok": True, "status": "pass"})
        rec = m.record_validation("eng_m", stages_passed=["quality"],
                                  quality_score=0.9)
        self.assertIn("health", rec["stages_passed"])
        self.assertIn("quality", rec["stages_passed"])

    def test_health_fail_blocks(self):
        """health 明确失败 → 必须 block。"""
        m = self.mod
        rec = m.record_validation("eng_f", stages_passed=["health"],
                                  health={"ok": False, "status": "fail"})
        self.assertTrue(rec["blocked"])


class TestAdmissionStateNotSelfContradictory(unittest.TestCase):
    """状态自洽门禁：blocked 与 health.status 不得互相矛盾。

    这条同时看守真实状态目录——若存量记录里出现「blocked=true 但
    health.status=pass」的形态，说明粘滞 bug 回归或有人手改了状态文件。
    """

    def test_no_blocked_record_with_passing_health(self):
        """始终检查**真实**状态目录（不受隔离用例的 ARGO_STATE_DIR 影响）。

        此前的实现读 `os.environ.get("ARGO_STATE_DIR", ~/.cache/...)`，
        而同文件的前序用例会临时设置该变量并在 tearDown 里清掉——本用例
        于是指向临时目录、`is_dir()` 为假而被 skip，关键守卫实际从未运行
        （skip 比 fail 更危险：它看起来是「通过」）。改为直接问模块要
        真实状态目录，不经环境变量。
        """
        import engine_admission
        from pathlib import Path as _P

        try:
            # 模块的真源函数（不受测试环境变量污染）
            adm_dir = _P(str(engine_admission.admission_dir()))
        except Exception:
            adm_dir = _P(os.path.expanduser("~/.cache/unified-search")) / "admission"
        if not adm_dir.is_dir():
            self.skipTest("无准入状态目录（干净环境）")
        bad = []
        for p in adm_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            h = d.get("health") or {}
            eid = d.get("engine_id")
            reason = str(d.get("reason") or "")
            # 指纹 1（原）：blocked=true + reason=health_failed，但 health.status=pass
            # ——粘滞 bug 的形态，会让 routable=False。
            if d.get("blocked") and h.get("ok") is True and reason.startswith("health_failed"):
                bad.append(eid)
                continue
            # 指纹 2（新增）：blocked=false 却残留 reason。
            # 不变式是「blocked=False ⇒ reason 为空」（reason 语义 = 为什么不能用）。
            # 旧实现只在成功路径写 `validation_passed`，正是漏过指纹 1 的同类矛盾：
            # 测试只锁了 health_failed 一种值，换个值就溜过去了（实测线上 64 条）。
            # 这里锁**形状**而非某个字面量，避免下次再换一个词重演。
            if not d.get("blocked") and reason.strip():
                bad.append(f"{eid}(reason={reason!r})")
        self.assertFalse(
            bad,
            f"准入记录自相矛盾（会导致 routable/可读性判读错误）：{sorted(bad)}"
            f" —— blocked=true 不得配 health.status=pass；"
            f"blocked=false 不得残留任何 reason",
        )

    def test_validate_success_path_never_writes_reason(self):
        """engine_validate 成功路径不得写 reason（不变式的调用方一侧）。

        上面那条锁的是**状态文件**；这条锁**写入方**，两道一起才闭环：
        只锁状态文件的话，下次改回写 validation_passed 要等状态目录被
        重新生成才会暴露。
        """
        import inspect
        import engine_validate
        src = inspect.getsource(engine_validate)
        # 允许出现在注释里，不允许出现在赋值语句里
        offenders = [
            ln.strip() for ln in src.splitlines()
            if "reason" in ln and "=" in ln
            and not ln.strip().startswith("#")
            and "validation_passed" in ln
        ]
        self.assertEqual(
            offenders, [],
            f"engine_validate 在 reason 赋值处写 validation_passed，"
            f"违反 blocked=False ⇒ reason 为空：{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
