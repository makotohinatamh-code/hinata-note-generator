#!/usr/bin/env python3
"""
generate.py  —  GitHub Actionsで実行
モード1: schedule  毎朝7時JST。RSS取得→スコアリング→scored_news.json保存
モード2: article   スマホからトリガー。選択ニュースで記事生成→article_result.json保存
"""
import json, os, re, sys, concurrent.futures
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import urlopen, Request

JST               = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCORING_MODEL     = "claude-haiku-4-5-20251001"
GENERATION_MODEL  = "claude-sonnet-4-20250514"
FRESHNESS_HOURS   = 24
SCORED_FILE       = "scored_news.json"
RESULT_FILE       = "article_result.json"
MODE              = os.environ.get("GENERATE_MODE", "schedule")
SELECTED_INDICES  = os.environ.get("SELECTED_INDICES", "")
ARTICLE_MODE      = os.environ.get("ARTICLE_MODE", "free")

RSS_FEEDS = [
    {"url":"https://news.google.com/rss/search?q=%E6%97%A5%E9%8A%80+%E9%87%91%E8%9E%8D%E6%94%BF%E7%AD%96+%E7%82%BA%E6%9B%BF+%E5%86%86%E5%AE%89&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (BOJ)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=%E6%97%A5%E6%9C%AC%E7%B5%8C%E6%B8%88+%E9%87%91%E5%88%A9+%E3%83%9E%E3%83%BC%E3%82%B1%E3%83%83%E3%83%88&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (Market)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=%E7%82%BA%E6%9B%BF%E4%BB%8B%E5%85%A5+%E5%86%86%E9%AB%98+%E5%86%86%E5%AE%89&hl=ja&gl=JP&ceid=JP:ja","label":"Google News JP (FX)","lang":"ja"},
    {"url":"https://news.google.com/rss/search?q=Bank+of+Japan+monetary+policy+interest+rate&hl=en&gl=US&ceid=US:en","label":"Google EN (BOJ)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Federal+Reserve+FOMC+interest+rate+decision&hl=en&gl=US&ceid=US:en","label":"Google EN (Fed)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Japan+yen+dollar+forex+currency+market&hl=en&gl=US&ceid=US:en","label":"Google EN (FX)","lang":"en"},
    {"url":"https://news.google.com/rss/search?q=Bloomberg+Reuters+Japan+economy+BOJ+yen&hl=en&gl=US&ceid=US:en","label":"Google EN (Bloomberg/Reuters)","lang":"en"},
]

def call_claude(system_prompt, user_msg, model, max_tokens=4000, retries=2):
    payload = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role":"user","content":user_msg}]
    }).encode("utf-8")
    for attempt in range(retries + 1):
        req = Request("https://api.anthropic.com/v1/messages", data=payload, headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        }, method="POST")
        try:
            with urlopen(req, timeout=90) as res:
                return json.loads(res.read())["content"][0]["text"]
        except Exception as e:
            print(f"Claude APIエラー (attempt {attempt+1}/{retries+1}): {e}", file=sys.stderr)
            if attempt == retries:
                return f"[ERROR] {e}"
            import time as _time
            _time.sleep(3)
    return "[ERROR] max retries exceeded"

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
            for e in root.findall("atom:entry", ns)[:20]:
                title   = e.findtext("atom:title", namespaces=ns) or ""
                lel     = e.find("atom:link", ns)
                link    = lel.get("href","") if lel is not None else ""
                pub_raw = e.findtext("atom:published", namespaces=ns) or ""
                pub_dt  = parse_pubdate(pub_raw)
                if pub_dt and pub_dt < cutoff: continue
                display = pub_dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if pub_dt else pub_raw[:16]
                pub_iso = pub_dt.isoformat() if pub_dt else ""
                items.append({"title":title.strip(),"link":link,"pubDate":display,
                               "pub_dt":pub_iso,"desc":"","lang":lang,"source":label})
        else:
            for item in channel.findall("item")[:20]:
                title   = re.sub(r"<[^>]+>","",item.findtext("title") or "").strip()
                link    = item.findtext("link") or ""
                desc    = re.sub(r"<[^>]+>","",item.findtext("description") or "")[:300]
                pub_raw = item.findtext("pubDate") or ""
                pub_dt  = parse_pubdate(pub_raw)
                if pub_dt and pub_dt < cutoff: continue
                if title and link:
                    display = pub_dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if pub_dt else pub_raw[:16]
                    pub_iso = pub_dt.isoformat() if pub_dt else ""
                    items.append({"title":title,"link":link,"pubDate":display,
                                  "pub_dt":pub_iso,"desc":desc,"lang":lang,"source":label})
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
    all_items.sort(key=lambda x: x.get("pub_dt",""), reverse=True)
    return all_items

SCORE_PROMPT = """ニュース記事を5軸で各10点満点（計50点）でスコアリングしてください。英語記事でも日本語で評価してください。
A.注目度 B.先進性 C.意外性 D.著者適合性（経済・金融・為替・日銀） E.読者価値
最適なマガジンも選択：boj/fx/market/global/basic
JSONのみ回答：{"A":X,"B":X,"C":X,"D":X,"E":X,"total":X,"reason":"30字以内","magazine":"id"}"""

def score_single(item):
    prefix = "[EN] " if item.get("lang") == "en" else ""
    raw = call_claude(SCORE_PROMPT,
                      f"{prefix}Title: {item['title']}\nSummary: {item.get('desc','')}",
                      model=SCORING_MODEL, max_tokens=150)
    try:
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group())
            if "total" not in d:
                d["total"] = sum(d.get(k,0) for k in ["A","B","C","D","E"])
            return {**item,"scoreData":d,"total":int(d["total"]),
                    "reason":d.get("reason",""),"magazine_tag":d.get("magazine","market")}
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

PERSONA_BASE = """あなたは「日向真（ひなた まこと）」として記事を書きます。
34歳・男性・横浜在住。元大手証券会社 株式ディーリング部（デスクトレーダー6年）。育休中（第一子1歳6ヶ月）。

【文体・トーン】
一人称「私」。です・ます調。口語的・テンポ速い。短文多用。
読者と同じ目線で話す語り口を徹底する。上から目線・自慢・説教はしない。
絵文字は📊 📌 💡 🏛️ 🌐 📉 📈 🔍 ⚡ 🗓️ のみ。

【呼びかけ表現】
- 呼びかけは「みなさん」のみ。「忙しいみなさん」は禁止。
- 冒頭で呼びかけなくても構わない。

【ディーラー経験の活かし方】
- 「私がディーラーをやっていた頃」「ディーラー時代は〜」は使わない。
- 代わりに：「現場にいた頃」「証券会社にいたとき」「ディーリングデスクにいたとき」
- エピソードは「驚き・失敗・気づき」を中心に書く。

【禁止ワード・表現】
断定的相場予測・特定銘柄推奨・アスタリスク区切り・論文調・週次表現
「おはようございます」「こんにちは」「こんばんは」などの時刻挨拶
「忙しいみなさん」「私がディーラーをやっていた頃」「ディーラー時代は〜」

【時制】渡される【ニュース日時情報】を確認し、今日配信なら「今日」、昨日なら「昨日」、2日以上前なら「先日」「今週」など正確な表現を使う。"""

PERSONA_FREE = PERSONA_BASE + """
【タイトル（必須・30〜40字）】
- 「誰も言わない〜の本当の理由」「〜の裏側」「〜だと思ってた。〜までは。」などの形式
- ニュースタイトルをそのまま使わない。必ず日向真の視点で考案すること。
- 時刻挨拶禁止

【構成】# タイトル（H1）→冒頭フック(100字)→参照URL→## 📌まず事実確認(600字)→## 🔍現場目線の裏読み(700字・エピソード必須)→## 🗓️今日注目しておくべきこと(350字)→締め
文字数1,900〜2,100字。Markdown形式。ハッシュタグは末尾に入れない。

【質の基準（必須）】
- 事実と推測を明確に区別する。推測には「〜と思われます」「おそらく〜」を使う。断定は裏付けのある事実のみ。
- 論文調・専門用語の羅列は禁止。難解な用語は必ず1文で平易に言い換える。例：「自然利率（経済を過熱も冷却もさせない中立的な金利）」
- 「現場にいたから気づけたこと」を1つ以上入れる。一般報道との差別化ポイントを明確にする。
- 読後に「なるほど」「明日誰かに話したい」と思えるような具体的な視点を1つ必ず入れる。
- 数字・固有名詞を使う場合は出典またはニュース元を明記する。根拠のない数字は使わない。
- 冒頭フックは「なぜ今これを読むべきか」が3秒で伝わること。抽象的な書き出しは禁止。"""

PERSONA_PAID = PERSONA_BASE + """
【有料記事】無料パート(2,000字)+有料パート(1,500字)。
無料: 冒頭→参照URL→## 📌表の話→## 🔍ここが気になった→## ⚡ディーラー時代の実体験→有料誘導
有料: ## 💡なぜ私はそう読んだのか→## 📈私が注目した数字→## 🗓️今後3日間のシナリオ→締め
有料ラインは「---（ここから有料）---」で明示。"""

PERSONA_SCENARIO = PERSONA_BASE + """
【シナリオ分析】無料(1,500字)+有料(2,000字)。
無料: 冒頭→参照URL→## 📊今回の構図→## 🔍サプライズが起きるとしたら→有料誘導
有料: ## ⚡シナリオA:メイン→## ⚡シナリオB:上振れ→## ⚡シナリオC:下振れ→## 🗓️確認する指標→締め
有料ラインは「---（ここから有料）---」で明示。"""

TWEET_PROMPT = """「日向真（ひなた まこと）」としてXに投稿します。元大手証券会社ディーラー・育休パパ。
禁止：アスタリスク区切り・断定的予測。絵文字は📊📌💡🏛️🌐📉📈🔍⚡🗓️のみ。
4本セットで出力：
【①告知】公開と同時。数字・フック冒頭。末尾「👉 【noteのURLをここに貼る】」
【②共感】当日21:00。核心を独立した知識として。URLなし。
【③保存】翌朝7:00。リスト形式。末尾「👉 【noteのURLをここに貼る】」
【④エンゲ】翌夜21:00。本音・質問でエンゲージメントを引き出す。URLなし。"""

AI_REV_PROMPT = """日本語文章のAI文体パターンをチェックしてください。
①アスタリスク区切り ②論文調表現 ③機械的列挙 ④接続詞の連続 ⑤「〜ではないでしょうか」多用 ⑥週次表現 ⑦過度な前置き
{"ai_score":0-100,"issues":[{"type":"問題タイプ","location":"該当箇所20字以内","suggestion":"修正案"}],"overall":"クリア/軽微な問題あり/要修正"} JSONのみ。"""

MAG_PROMPT = """以下の記事の最適なnoteマガジンを推薦: boj=日銀・金融政策 fx=為替・円安 market=マーケット global=海外発 basic=経済基礎
{"primary_magazine":"id","secondary_magazine":"idまたはnull","reason":"30字以内","paid_recommendation":true/false,"paid_reason":"理由"} JSONのみ。"""

def build_news_dt_context(selected_items):
    now_jst  = datetime.now(JST)
    now_date = now_jst.date()
    lines = []
    for i, item in enumerate(selected_items, 1):
        pub_iso = item.get("pub_dt","")
        pub_dt  = None
        if pub_iso:
            try: pub_dt = datetime.fromisoformat(pub_iso.replace("Z","+00:00"))
            except Exception: pass
        if pub_dt:
            pub_jst = pub_dt.astimezone(JST)
            delta   = (now_date - pub_jst.date()).days
            if delta == 0:   rel = f"今日({pub_jst.strftime('%H:%M')}配信)"
            elif delta == 1: rel = f"昨日({pub_jst.strftime('%m/%d %H:%M')}配信)"
            elif delta <= 3: rel = f"{delta}日前({pub_jst.strftime('%m/%d')}配信)"
            else:            rel = f"{delta}日前({pub_jst.strftime('%m/%d')}配信) ※古い記事"
        else:
            rel = "配信日時不明"
        lines.append(f"【{i}】{item['title'][:50]} → {rel}")
    return "\n".join(lines)

def parse_json_safe(raw):
    try:
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m: return json.loads(m.group())
    except Exception: pass
    return {}

def generate_article(selected_items, mode="free"):
    persona_map = {"free":PERSONA_FREE,"paid":PERSONA_PAID,"scenario":PERSONA_SCENARIO}
    persona     = persona_map.get(mode, PERSONA_FREE)
    news_text   = "\n\n".join([f"【{i+1}】{n['title']}\nURL: {n.get('link','')}" for i,n in enumerate(selected_items)])
    dt_context  = build_news_dt_context(selected_items)
    now_jst     = datetime.now(JST)

    user_msg = f"""現在日時: {now_jst.strftime('%Y年%m月%d日 %H:%M')} JST

【ニュース日時情報 — 時制決定に必ず使うこと】
{dt_context}

以下のニュースを元に記事を1本書いてください。

{news_text}

【必須事項】
1. 時制は【ニュース日時情報】を厳守
2. 「今日」「本日」は今日配信のニュースの場合のみ使用
3. AIらしい文体禁止（アスタリスク区切り禁止）
4. Markdown形式で出力
5. ディーラー時代のエピソードを必ず1回以上入れる"""

    print("  note記事生成中...")
    note = call_claude(persona, user_msg, model=GENERATION_MODEL, max_tokens=5000)

    print("  X投稿生成中...")
    tweet = call_claude(TWEET_PROMPT,
                        f"以下のnote記事を元にX投稿4本セットを生成してください。\n\n{note[:1500]}",
                        model=GENERATION_MODEL, max_tokens=800)

    print("  AI文体チェック中...")
    ai_rev = parse_json_safe(
        call_claude(AI_REV_PROMPT, note[:1200], model=GENERATION_MODEL, max_tokens=600)
    )

    title_m = re.search(r"^#\s+(.+)", note, re.MULTILINE)
    title   = title_m.group(1).strip() if title_m else selected_items[0]["title"][:40]

    print("  マガジン推薦中...")
    mag = parse_json_safe(
        call_claude(MAG_PROMPT, f"タイトル：{title}\n冒頭：{note[:400]}", model=SCORING_MODEL, max_tokens=200)
    )

    agents = []
    if mode in ("paid","scenario"):
        print("  エージェントレビュー中（3名）...")
        agent_defs = [
            ("📈 経済アナリスト",  "元日銀出身の独立系経済アナリスト(15年)として有料note記事をレビュー。{\"score\":0-100,\"issues\":[],\"strengths\":[],\"rewrite_suggestions\":[],\"verdict\":\"合格/要修正/不合格\"} JSONのみ。"),
            ("🎯 戦略コンサルタント","外資系戦略コンサルとして有料note記事をレビュー。{\"score\":0-100,\"issues\":[],\"strengths\":[],\"rewrite_suggestions\":[],\"verdict\":\"合格/要修正/不合格\"} JSONのみ。"),
            ("🏛️ 政策アドバイザー", "元経済産業省・金融庁出身の政策アドバイザーとして有料note記事をレビュー。{\"score\":0-100,\"issues\":[],\"strengths\":[],\"rewrite_suggestions\":[],\"verdict\":\"合格/要修正/不合格\"} JSONのみ。"),
        ]
        def run_agent(name, prompt):
            raw = call_claude(prompt, f"以下の記事をレビューしてください：\n\n{note}", model=GENERATION_MODEL, max_tokens=600)
            d = parse_json_safe(raw)
            d["agent"] = name
            return d
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(run_agent, name, prompt) for name, prompt in agent_defs]
            agents = [f.result() for f in futs]

    tweet_parts = re.split(r'【[①②③④][^\】]*】', tweet)
    labels = ["① 告知投稿","② 共感・深掘り投稿","③ 保存系投稿","④ エンゲージメント投稿"]
    tweet_list = [{"label":labels[i] if i < len(labels) else f"投稿{i+1}", "text":p.strip()}
                  for i, p in enumerate([x.strip() for x in tweet_parts if x.strip()][:4])]

    return {
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M"),
        "title": title,
        "mode": mode,
        "note": note,
        "tweet": tweet,
        "tweets": tweet_list,
        "ai_review": ai_rev,
        "magazine": mag,
        "agents": agents,
        "source_news": [n["title"] for n in selected_items],
        "source_urls":  [n.get("link","") for n in selected_items],
    }

def main():
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    if MODE == "schedule":
        print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}] schedule mode")
        items = fetch_all()
        print(f"Fetched {len(items)} items. Scoring...")
        scored = score_batch(items)
        output = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                  "item_count": len(scored), "items": scored}
        with open(SCORED_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(scored)} items to {SCORED_FILE}")

    elif MODE == "article":
        print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}] article mode ({ARTICLE_MODE})")
        if not SELECTED_INDICES:
            print("ERROR: SELECTED_INDICES not set", file=sys.stderr); sys.exit(1)
        with open(SCORED_FILE, encoding="utf-8") as f:
            scored_data = json.load(f)
        all_items = scored_data.get("items", [])
        indices  = [int(i.strip()) for i in SELECTED_INDICES.split(",") if i.strip().isdigit()]
        selected = [all_items[i] for i in indices if i < len(all_items)]
        if not selected:
            print("ERROR: No valid items selected", file=sys.stderr); sys.exit(1)
        print(f"Generating for {len(selected)} items...")
        result = generate_article(selected, mode=ARTICLE_MODE)
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Done. Title: {result['title']}")

if __name__ == "__main__":
    main()
