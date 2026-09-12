# 引擎：`people_daily`

- 准入时间: 2026-09-12T21:19:39+08:00
- 状态: admitted
- cost_tier: free
- type: people_daily
- quality_score: 1.0
- avg_latency_ms: 446.8
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=542.8ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine people_daily
python3 scripts/engine_validate.py --engine people_daily --stage health
```
