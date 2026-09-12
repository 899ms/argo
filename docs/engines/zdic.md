# 引擎：`zdic`

- 准入时间: 2026-09-12T21:19:37+08:00
- 状态: admitted
- cost_tier: free
- type: zdic
- quality_score: 1.0
- avg_latency_ms: 127.6
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=125.4ms · count=1
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine zdic
python3 scripts/engine_validate.py --engine zdic --stage health
```
