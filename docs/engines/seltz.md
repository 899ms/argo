# 引擎：`seltz`

- 准入时间: 2026-09-14T16:32:34+08:00
- 状态: admitted
- cost_tier: api
- type: seltz
- quality_score: 1.0
- avg_latency_ms: 1919.2
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| `SELTZ_API_KEY` | 是 |

## 最近验证

- health: pass · latency=1560.2ms · count=2
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine seltz
python3 scripts/engine_validate.py --engine seltz --stage health
```
