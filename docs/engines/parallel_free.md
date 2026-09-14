# 引擎：`parallel_free`

- 准入时间: 2026-09-14T14:42:32+08:00
- 状态: admitted
- cost_tier: free
- type: parallel_free
- quality_score: 0.996
- avg_latency_ms: 1583.6
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1400.1ms · count=10
- quality: pass · score=0.996 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine parallel_free
python3 scripts/engine_validate.py --engine parallel_free --stage health
```
