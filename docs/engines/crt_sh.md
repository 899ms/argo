# 引擎：`crt_sh`

- 准入时间: 2026-09-12T21:26:15+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 0.5
- avg_latency_ms: 6603.5
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=3281.2ms · count=10
- quality: pass · score=0.5 · empty_rate=0.5

## 调用

```bash
python3 scripts/search.py "查询词" --engine crt_sh
python3 scripts/engine_validate.py --engine crt_sh --stage health
```
