# 引擎：`openreview`

- 准入时间: 2026-09-12T16:37:55+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 0.96
- avg_latency_ms: 775.3
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=501.2ms · count=10
- quality: pass · score=0.96 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine openreview
python3 scripts/engine_validate.py --engine openreview --stage health
```
