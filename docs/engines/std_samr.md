# 引擎：`std_samr`

- 准入时间: 2026-09-12T21:19:24+08:00
- 状态: admitted
- cost_tier: free
- type: std_samr
- quality_score: 1.0
- avg_latency_ms: 382.0
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=518.0ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine std_samr
python3 scripts/engine_validate.py --engine std_samr --stage health
```
