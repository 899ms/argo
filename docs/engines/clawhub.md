# 引擎：`clawhub`

- 准入时间: 2026-09-15T17:03:23+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 6646.3
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=5094.3ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine clawhub
python3 scripts/engine_validate.py --engine clawhub --stage health
```
