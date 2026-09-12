# 引擎：`biorxiv`

- 准入时间: 2026-09-12T16:38:04+08:00
- 状态: admitted
- cost_tier: free
- type: biorxiv
- quality_score: 1.0
- avg_latency_ms: 1784.0
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1240.8ms · count=2
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine biorxiv
python3 scripts/engine_validate.py --engine biorxiv --stage health
```
