#!/usr/bin/env python3
"""
generate.py
GitHub Actionsで毎朝7時(JST)に自動実行。
RSS取得 -> Haikuスコアリング -> scored_news.json に保存
"""
import json, os, re, sys
import xml.etree.ElementTree as ET
import concurrent.futures
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import urlopen, Request

JST = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCORING_MODEL = "claude-haiku-4-5-20251001"
RSS_MAX_ITEMS = 20
FRESHNESS_HOURS = 24
OUTPUT_FILE = "scored_news.json"

RSS_FEEDS = [
    {"url":"https://news.google.com/rss/search?q=%E6%97%A5%E9%8A%80+%E9%87%91%E8%9E%8D%E6%94%BF%E7%AD%96+%E7%82%BA%E6%9B%BF+%E5%86%86%E5%AE%89&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (BOJ)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=%E6%97%A5%E6%9C%AC%E7%B5%8C%E6%B8%88+%E9%87%91%E5%88%A9+%E3%83%9E%E3%83%BC%E3%82%B1%E3%83%83%E3%83%88&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (Market)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=%E7%82%BA%E6%9B%BF%E4%BB%8B%E5%85%A5+%E5%86%86%E9%AB%98+%E5%86%86%E5%AE%89&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (FX)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=Bank+of+Japan+monetary+policy+interest+rate&hl=en&gl=US&ceid=US:en","label":"Google EN (BOJ)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Federal+Reserve+FOMC+interest+rate+decision&hl=en&gl=US&ceid=US:en","label":"Google EN (Fed)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Japan+yen+dollar+forex+currency+market&hl=en&gl=US&ceid=US:en","label":"Google EN (FX)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Bloomberg+Reuters+Japan+economy+BOJ+yen&hl=en&gl=US&ceid=US:en","label":"Google EN (Bloomberg/Reuters)","lang":"en"},
]

SCORE_PROMPT = """ニュース記事を5軸で各10点満点（計50点）でスコアリングしてください。英語記事でも日本語で評価してください。
A. 注目度 B. 先進性 C. 意外性 D. 著者適合性（経済・金融・為替・日銀） E. 読者価値（忙しい日本人ビジネスマン）
最適なマガジンも選択：boj/fx/market/global/basic
JSONのみ回答：{"A":X,"B":X,"C":X,"D":X,"E":X,"total":X,"reason":"30字以内","magazine":"id"}"""

def call_claude(system_prompt, user_msg, model, max_tokens=200):
    payload = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role":"user","content":user_msg}]
    }).encode("utf-8")
    req = Request("https://api.anthropic.com/v1/messages", data=payload, headers={
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }, method="POST")
    try:
        with urlopen(req, timeout=60) as res:
            return json.loads(res.read())["content"][0]["text"]
    except Exception as e:
        return f"[ERROR] {e}"

def parse_pubdate(s):
    if not s: return None
    try: return parsedate_to_datetime(s)
    except Exception: pass
    try: return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception: return None

def fetch_rss(feed):
    url, label, lang = feed["url"], feed["label"], feed.get("lang","ja")
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    try:
        req = Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urlopen(req, timeout=15) as res:
            data = res.read()
        root = ET.fromstring(data)
        ns = {"atom":"http://www.w3.org/2005/Atom"}
        channel = root.find("channel")
        if channel is None:
            for e in root.findall("atom:entry", ns)[:RSS_MAX_ITEMS]:
                title = e.findtext("atom:title", namespaces=ns) or ""
                lel = e.find("atom:link", ns)
                link = lel.get("href","") if lel is not None else ""
                pub_str = e.findtext("atom:published", namespaces=ns) or ""
                pub_dt = parse_pubdate(pub_str)
                if pub_dt and pub_dt < cutoff: continue
                items.append({"title":title.strip(),"link":link,"pubDate":pub_str[:16],"desc":"","lang":lang,"source":label})
        else:
            for item in channel.findall("item")[:RSS_MAX_ITEMS]:
                title = re.sub(r"<[^>]+>","",item.findtext("title") or "").strip()
                link  = item.findtext("link") or ""
                desc  = re.sub(r"<[^>]+>","",item.findtext("description") or "")[:300]
                pub_str = (item.findtext("pubDate") or "")[:16]
                pub_dt  = parse_pubdate(pub_str)
                if pub_dt and pub_dt < cutoff: continue
                if title and link:
                    items.append({"title":title,"link":link,"pubDate":pub_str,"desc":desc,"lang":lang,"source":label})
    except Exception as e:
        print(f"RSS error ({label}): {e}", file=sys.stderr)
    return items

def fetch_all():
    all_items, seen = [], set()
    for feed in RSS_FEEDS:
        for item in fetch_rss(feed):
            key = item["title"][:60]
            if key not in seen and item["link"]:
                seen.add(key)
                all_items.append(item)
    all_items.sort(key=lambda x: x.get("pubDate",""), reverse=True)
    return all_items

def score_single(item):
    prefix = "[EN] " if item.get("lang") == "en" else ""
    user_msg = f"{prefix}Title: {item['title']}\nSummary: {item.get('desc','')}"
    raw = call_claude(SCORE_PROMPT, user_msg, model=SCORING_MODEL)
    try:
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group())
            if "total" not in d:
                d["total"] = sum(d.get(k,0) for k in ["A","B","C","D","E"])
            return {**item,"scoreData":d,"total":int(d["total"]),"reason":d.get("reason",""),"magazine_tag":d.get("magazine","market")}
    except Exception:
        pass
    return {**item,"scoreData":{},"total":0,"reason":"error","magazine_tag":"market"}

def score_batch(items):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(score_single, item): item for item in items}
        for f in concurrent.futures.as_completed(futs):
            try: results.append(f.result())
            except Exception: results.append({**futs[f],"scoreData":{},"total":0,"reason":"","magazine_tag":"market"})
    results.sort(key=lambda x: x.get("total",0), reverse=True)
    return results

def main():
    now_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M')
    print(f"[{now_str} JST] Starting generate.py")
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    print("Fetching RSS feeds...")
    items = fetch_all()
    print(f"Fetched {len(items)} items")
    print("Scoring with Haiku...")
    scored = score_batch(items)
    print(f"Scored. Top score: {scored[0]['total'] if scored else 0}/50")
    output = {
        "generated_at": now_str,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "item_count": len(scored),
        "items": scored
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(scored)} items to {OUTPUT_FILE}")
    for i, item in enumerate(scored[:3], 1):
        print(f"  {i}. [{item['total']}/50] {item['title'][:60]}")

if __name__ == "__main__":
    main()
