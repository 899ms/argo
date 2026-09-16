#!/usr/bin/env python3
"""serp_spec.py — SERP 提取选择器/搜索 URL 唯一来源（ego-search 内部共用）。

背景：ego_search.py 与 webbridge_adapter.py 曾各自维护一份**逐字节相同**的
SERP 提取 IIFE 和 SEARCH_URLS（2026-09-13 审查重复清单 #1/#2）——一份修了
选择器另一份不会跟着变，是「重复直接制造 bug」的典型温床。选择器、日期
抽取、搜索入口 URL 的改动只改本文件。

占位符约定（两运行时统一）：
  %%ENGINE_JSON%%  引擎名 JSON 字符串（"baidu"）
  %%N%%            结果条数（整数）
"""

from __future__ import annotations

import json as _json

# 搜索入口 URL（q 为 URL 编码后的查询词）
SEARCH_URLS = {
    "bing": "https://www.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "google": "https://www.google.com/search?q={q}",
}

# 页面内 SERP 提取 IIFE 体（返回 JSON 字符串）。页面内选择器写死在此；
# Node 层只注入纯安全占位符。
SERP_EXTRACT_TEMPLATE = r"""(() => {
  const configs = {
    bing: { item: 'li.b_algo', link: 'h2 a', snippet: '.b_caption p, p' },
    baidu: { item: "div#content_left div[class*='c-container'], div#content_left div.result", link: 'h3 a', snippet: ".c-abstract, [class*='content-right']" },
    google: { item: 'div.g, div[data-sncf]', link: 'a h3', snippet: 'div.VwiC3b, div[data-sncf]' }
  };
  const engine = %%ENGINE_JSON%%;
  const n = %%N%%;
  const cfg = configs[engine] || configs.bing;
  const items = [];
  const extractDate = (t) => {
    const m = t.match(/(20\d{2})[年\/\-\.](\d{1,2})[月\/\-\.](\d{1,2})/);
    if (m) return m[1] + '-' + String(m[2]).padStart(2,'0') + '-' + String(m[3]).padStart(2,'0');
    const m2 = t.match(/(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})/i);
    if (m2) { const mo={jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}; return m2[3] + '-' + mo[m2[2].toLowerCase().slice(0,3)] + '-' + String(m2[1]).padStart(2,'0'); }
    const m3 = t.match(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2}),? (\d{4})/i);
    if (m3) { const mo={jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}; return m3[3] + '-' + mo[m3[1].toLowerCase().slice(0,3)] + '-' + String(m3[2]).padStart(2,'0'); }
    return '';
  };
  document.querySelectorAll(cfg.item).forEach(el => {
    const a = el.querySelector(cfg.link);
    if (!a) return;
    const p = el.querySelector(cfg.snippet);
    const text = (el.innerText || '') + ' ' + (a.href || '');
    items.push({
      title: (a.innerText || '').trim(),
      url: a.href || '',
      snippet: (p ? p.innerText : '').trim().slice(0, 300),
      published_at: extractDate(text),
    });
  });
  if (!items.length) {
    document.querySelectorAll('h2 a, h3 a').forEach(a => {
      if (items.length >= n) return;
      items.push({ title: (a.innerText || '').trim(), url: a.href || '', snippet: '', published_at: extractDate((a.innerText || '') + ' ' + (a.href || '')) });
    });
  }
  return JSON.stringify(items.slice(0, n));
})()"""

# 各引擎「SERP 容器已渲染」的探针选择器（仅文档/诊断用；就绪等待由
# ego_search 的空结果退避重抽实现，两运行时通用）
READY_SELECTORS = {
    "bing": "li.b_algo",
    "baidu": "#content_left",
    "google": "div.g",
}


def build_serp_js(engine: str, n: int) -> str:
    """按引擎与条数生成 SERP 提取 IIFE（占位符注入的唯一入口）。"""
    return (SERP_EXTRACT_TEMPLATE
            .replace("%%ENGINE_JSON%%", _json.dumps(str(engine)))
            .replace("%%N%%", str(int(n))))
