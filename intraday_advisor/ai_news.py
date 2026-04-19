import os
import yfinance as yf

def fetch_and_analyze_news(symbol: str) -> dict:
    """
    Fetches the latest ~10 news items for the ticker using yfinance.
    Then performs sentiment analysis using Google Gemini GenAI (if API key available)
    or falls back to local VADER NLP heuristics.
    """
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    
    ticker_obj = yf.Ticker(symbol)
    try:
        news_data = ticker_obj.news
    except Exception as e:
        return {"error": f"Failed to fetch news for {symbol}: {e}", "bias": "NEUTRAL", "articles": []}

    if not news_data:
        return {"error": f"No recent news found for {symbol}.", "bias": "NEUTRAL", "articles": []}

    articles_list = []
    text_corpus = ""
    
    for item in news_data[:10]:
        title = item.get("title", "")
        publisher = item.get("publisher", "Unknown")
        link = item.get("link", "")
        if title:
            articles_list.append({"title": title, "publisher": publisher, "link": link})
            text_corpus += f"- {title}\n"

    # Analyze Using Routing Engine
    api_key = os.getenv("GEMINI_API_KEY", "")
    bias = "NEUTRAL"
    analysis_reasoning = ""
    analyzer_engine = ""

    if api_key:
        analyzer_engine = "Google Gemini LLM"
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-pro')
            prompt = (
                f"Act as an expert Wall Street quantitative analyst. "
                f"Read the following recent news headlines for the stock {symbol}:\n\n{text_corpus}\n\n"
                f"First, declare the exact one-word bias (BULLISH, BEARISH, or NEUTRAL) taking into account "
                f"if this news will cause a crash or rally. Then, provide a 2 sentence explanation."
            )
            response = model.generate_content(prompt)
            output = response.text
            
            if "BULLISH" in output.upper(): bias = "BULLISH"
            elif "BEARISH" in output.upper(): bias = "BEARISH"
            
            analysis_reasoning = output
        except Exception as e:
            analyzer_engine = f"Gemini Error ({e}) -> Routed to Local NLP"
            bias, analysis_reasoning = _vader_fallback(text_corpus, symbol)
    else:
        analyzer_engine = "VADER Local NLP Heuristics (Zero-Latency Offline Model)"
        bias, analysis_reasoning = _vader_fallback(text_corpus, symbol)

    return {
        "symbol": symbol,
        "articles": articles_list,
        "bias": bias,
        "reasoning": analysis_reasoning,
        "engine": analyzer_engine,
        "error": None
    }


def _vader_fallback(text_corpus: str, symbol: str) -> tuple[str, str]:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        scores = analyzer.polarity_scores(text_corpus)
        compound = scores.get('compound', 0)
        
        if compound >= 0.15:
            bias = "BULLISH"
        elif compound <= -0.15:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"
            
        reasoning = f"The mathematical sentiment compound score is {compound:.2f}. "
        if bias == "BULLISH":
            reasoning += f"The linguistic frequency explicitly points to positive media coverage, indicating a rally potential for {symbol}."
        elif bias == "BEARISH":
            reasoning += f"Negative terminology outweighed positive words, warning of a potential localized crash or downside pressure on {symbol}."
        else:
            reasoning += "News appears largely factual or flat without aggressive emotional extremes."
            
        return bias, reasoning
    except ImportError:
        return "NEUTRAL", "NLP analyzer package `vaderSentiment` not found. Showing neutral bias."
