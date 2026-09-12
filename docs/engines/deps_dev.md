# 引擎：`deps_dev`

- 准入时间: 2026-09-12T16:37:56+08:00
- 状态: admitted
- cost_tier: free
- type: deps_dev
- quality_score: 1.0
- avg_latency_ms: 216.0
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=275.7ms · count=9
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine deps_dev
python3 scripts/engine_validate.py --engine deps_dev --stage health
```
