# 引擎：`endoflife`

- 准入时间: 2026-09-12T16:37:58+08:00
- 状态: admitted
- cost_tier: free
- type: endoflife
- quality_score: 1.0
- avg_latency_ms: 305.3
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=235.1ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine endoflife
python3 scripts/engine_validate.py --engine endoflife --stage health
```
