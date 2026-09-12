# 引擎：`zhihu_hot_app`

- 准入时间: 2026-09-12T21:18:44+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 990.2
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=262.6ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine zhihu_hot_app
python3 scripts/engine_validate.py --engine zhihu_hot_app --stage health
```
