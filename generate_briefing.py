"""
毎朝の医療情報ブリーフィング生成スクリプト
- PubMed最新論文（内科・精神科・漢方・栄養学・徒手療法）
- 中医学・漢方関連情報
- Claude APIで日本語3分間サマリーを生成
- Alexa Flash Briefing形式のJSONを出力
"""

import json
import os
import uuid
import time
import datetime
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.parse import quote
from urllib.error import URLError

# ========== 設定 ==========
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# PubMed検索クエリ（先生の専門・興味に合わせてカスタマイズ）
PUBMED_QUERIES = [
    {
        "label": "内科・総合診療",
        "query": '(internal medicine[MeSH] OR general practice[MeSH]) AND ("last 2 days"[PDat])',
        "max": 3,
    },
    {
        "label": "漢方・中医学",
        "query": '(traditional chinese medicine[MeSH] OR kampo[Title/Abstract] OR herbal medicine[MeSH]) AND ("last 7 days"[PDat])',
        "max": 3,
    },
    {
        "label": "分子栄養学・栄養療法",
        "query": '(nutritional therapy[MeSH] OR orthomolecular[Title/Abstract] OR micronutrient[Title/Abstract]) AND ("last 7 days"[PDat])',
        "max": 2,
    },
    {
        "label": "精神科・心療内科",
        "query": '(psychiatry[MeSH] OR psychosomatic medicine[MeSH]) AND clinical[Title/Abstract] AND ("last 7 days"[PDat])',
        "max": 2,
    },
    {
        "label": "徒手療法・動作分析",
        "query": '(manual therapy[MeSH] OR musculoskeletal manipulations[MeSH] OR movement analysis[Title/Abstract]) AND ("last 14 days"[PDat])',
        "max": 2,
    },
]

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EMAIL = os.environ.get("NCBI_EMAIL", "user@example.com")  # PubMed推奨


# ========== PubMed取得関数 ==========
def fetch_pubmed_articles(query: str, max_results: int = 3) -> list[dict]:
    """PubMed E-utilities APIから論文情報を取得する"""
    articles = []
    try:
        # esearch: PMIDリストを取得
        search_url = (
            f"{PUBMED_BASE}esearch.fcgi"
            f"?db=pubmed&term={quote(query)}&retmax={max_results}"
            f"&sort=date&email={EMAIL}&tool=morning-briefing&retmode=json"
        )
        req = Request(search_url, headers={"User-Agent": "MorningBriefing/1.0"})
        with urlopen(req, timeout=15) as resp:
            search_result = json.loads(resp.read().decode())

        pmids = search_result.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []

        time.sleep(0.5)  # NCBI APIレート制限に配慮

        # efetch: 論文詳細（タイトル・アブスト）をXMLで取得
        ids_str = ",".join(pmids)
        fetch_url = (
            f"{PUBMED_BASE}efetch.fcgi"
            f"?db=pubmed&id={ids_str}&rettype=abstract&retmode=xml"
            f"&email={EMAIL}&tool=morning-briefing"
        )
        req2 = Request(fetch_url, headers={"User-Agent": "MorningBriefing/1.0"})
        with urlopen(req2, timeout=20) as resp2:
            xml_data = resp2.read().decode()

        root = ET.fromstring(xml_data)
        for article in root.findall(".//PubmedArticle"):
            title_el = article.find(".//ArticleTitle")
            abstract_el = article.find(".//AbstractText")
            journal_el = article.find(".//Title")
            pub_date = article.find(".//PubDate/Year")

            title = title_el.text if title_el is not None else "No title"
            abstract = abstract_el.text if abstract_el is not None else "No abstract"
            journal = journal_el.text if journal_el is not None else "Unknown journal"
            year = pub_date.text if pub_date is not None else "2026"

            # アブストラクトは長いので300文字に切る
            if abstract and len(abstract) > 300:
                abstract = abstract[:300] + "..."

            articles.append({
                "title": title,
                "abstract": abstract,
                "journal": journal,
                "year": year,
            })

        time.sleep(0.5)

    except URLError as e:
        print(f"[WARN] PubMed fetch error: {e}")
    except Exception as e:
        print(f"[WARN] Unexpected error in fetch_pubmed_articles: {e}")

    return articles


# ========== Claude APIでサマリー生成 ==========
def generate_summary_with_claude(articles_by_category: dict) -> str:
    """
    取得した論文情報をClaude APIに渡して
    日本語3分間ブリーフィング（約600-700文字）を生成する
    """
    if not ANTHROPIC_API_KEY:
        return _generate_fallback_summary(articles_by_category)

    # プロンプト用に論文情報を整形
    articles_text = ""
    for category, articles in articles_by_category.items():
        if articles:
            articles_text += f"\n【{category}】\n"
            for a in articles:
                articles_text += f"  ・{a['title']} ({a['journal']}, {a['year']})\n"
                if a.get("abstract"):
                    articles_text += f"    要約: {a['abstract'][:200]}\n"

    today = datetime.date.today().strftime("%Y年%m月%d日")

    system_prompt = """あなたは内科医向けの医療情報ナレーターです。
毎朝Alexaで読み上げる3分間（600〜700文字）の医療ブリーフィングを作成します。

ルール:
1. 「おはようございます。{日付}の医療情報ブリーフィングです。」で始める
2. PubMed論文と中医学・漢方情報から、臨床的に有用なトピックを3〜4個ピックアップ
3. 各トピックは1〜2文で、臨床パール（明日から使えるヒント）として述べる
4. 専門用語は日本語で、英語論文タイトルは簡潔に和訳する
5. 「今日も充実した診療をお過ごしください」で締める
6. Alexaが読み上げやすいよう、句読点を適切に使い、記号（括弧・英数字）は最小限に
7. 合計600〜700文字程度（3分間で読み切れる長さ）"""

    user_message = f"""本日（{today}）の論文情報をもとに、ブリーフィングを作成してください。

{articles_text}

上記から臨床的に最も価値あるトピックを選び、3分間ブリーフィングにまとめてください。"""

    # Anthropic Messages API（直接HTTP）
    import json as _json
    from urllib.request import urlopen as _urlopen, Request as _Request

    payload = _json.dumps({
        "model": "claude-opus-4-6",
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}]
    }).encode()

    req = _Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST"
    )

    try:
        with _urlopen(req, timeout=30) as resp:
            result = _json.loads(resp.read().decode())
        return result["content"][0]["text"]
    except Exception as e:
        print(f"[WARN] Claude API error: {e}")
        return _generate_fallback_summary(articles_by_category)


def _generate_fallback_summary(articles_by_category: dict) -> str:
    """Claude APIが使えない場合のシンプルなフォールバック"""
    today = datetime.date.today().strftime("%Y年%m月%d日")
    text = f"おはようございます。{today}の医療情報ブリーフィングです。"

    count = 0
    for category, articles in articles_by_category.items():
        for a in articles:
            if count >= 4:
                break
            text += f"{category}の分野では、{a['title'][:60]}という研究が注目されています。"
            count += 1

    text += "今日も充実した診療をお過ごしください。"
    return text


# ========== Flash Briefing JSON生成 ==========
def build_flash_briefing_json(summary_text: str) -> list[dict]:
    """Alexa Flash Briefing形式のJSONを生成する"""
    today = datetime.datetime.utcnow()

    # Flash Briefingの最大文字数は4500文字だが、3分間は約650文字が目安
    if len(summary_text) > 4000:
        summary_text = summary_text[:4000] + "。以上、本日のブリーフィングでした。"

    return [
        {
            "uid": str(uuid.uuid5(uuid.NAMESPACE_DNS, today.strftime("%Y-%m-%d") + "-oda-hospital")),
            "updateDate": today.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
            "titleText": f"医療情報ブリーフィング {datetime.date.today().strftime('%Y年%m月%d日')}",
            "mainText": summary_text,
            "redirectionUrl": "https://pubmed.ncbi.nlm.nih.gov/"
        }
    ]


# ========== メイン処理 ==========
def main():
    print("=== 医療情報ブリーフィング生成開始 ===")
    print(f"実行日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. PubMed論文を各カテゴリで取得
    articles_by_category = {}
    for query_config in PUBMED_QUERIES:
        label = query_config["label"]
        print(f"  PubMed取得中: {label}...")
        articles = fetch_pubmed_articles(query_config["query"], query_config["max"])
        if articles:
            articles_by_category[label] = articles
            print(f"    → {len(articles)}件取得")
        else:
            print(f"    → 該当なし（クエリ: {query_config['query'][:50]}...）")

    total = sum(len(v) for v in articles_by_category.values())
    print(f"\n合計 {total} 件の論文を取得しました。")

    # 2. Claude APIでサマリー生成
    print("\nClaude APIでブリーフィング生成中...")
    summary = generate_summary_with_claude(articles_by_category)
    print(f"生成完了（{len(summary)}文字）")
    print("\n--- 生成されたブリーフィング ---")
    print(summary)
    print("--------------------------------\n")

    # 3. Flash Briefing JSON生成
    briefing_json = build_flash_briefing_json(summary)

    # 4. docs/feed.json に保存（GitHub Pages用）
    os.makedirs("docs", exist_ok=True)
    output_path = "docs/feed.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(briefing_json, f, ensure_ascii=False, indent=2)

    print(f"Flash Briefing JSON を保存しました: {output_path}")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
