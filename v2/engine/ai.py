"""Optional generative-AI layer (Claude). Runs only if ANTHROPIC_API_KEY is set.

Two jobs:
  1. classify_texts  - label line texts the rules + ML could not label.
  2. draft_summaries - rewrite the account summary into a reviewer-ready comment.

Governance: only distinct text strings (no amounts, no names) are sent for
classification; prompts force a fixed category list and JSON output, and every
AI label is stored with source = "LLM" so reviewers can see what the AI decided.
Without a key the pipeline still runs end to end (rules + ML + templates).
"""
from __future__ import annotations

import json
import os
import socket

import pandas as pd

MODEL = os.environ.get("RECON_LLM_MODEL", "claude-sonnet-5-5")


def available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _client():
    import anthropic
    return anthropic.Anthropic()


def classify_texts(df: pd.DataFrame, categories: list[str], batch: int = 80) -> pd.DataFrame:
    if not available():
        return df
    todo = df.loc[df["category"].isin(["Unclassified"]), "full_text"].dropna().unique().tolist()
    if not todo:
        return df
    client, labels = _client(), {}
    cats = categories + ["Other"]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        prompt = (
            "You classify SAP general-ledger line texts from a balance-sheet reconciliation "
            f"(working group: {df['work_group'].iloc[0]}). Texts may be abbreviated or in German, French, "
            "Hungarian etc. For each numbered text pick exactly one category from this list: "
            f"{json.dumps(cats)}. Answer ONLY with JSON: {{\"1\": \"category\", ...}}.\n\n"
            + "\n".join(f"{n + 1}. {t}" for n, t in enumerate(chunk)))
        msg = client.messages.create(model=MODEL, max_tokens=4000,
                                     messages=[{"role": "user", "content": prompt}])
        try:
            raw = msg.content[0].text
            ans = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception:
            continue
        for k, v in ans.items():
            if v in cats and v != "Other":
                labels[chunk[int(k) - 1]] = v
    m = df["category"].eq("Unclassified") & df["full_text"].isin(labels.keys())
    df.loc[m, "category"] = df.loc[m, "full_text"].map(labels)
    df.loc[m, "category_source"] = "LLM"
    return df


def draft_summaries(summary: pd.DataFrame, top_n: int = 40) -> pd.DataFrame:
    if not available() or summary.empty:
        return summary
    client = _client()
    idx = summary.sort_values("rai_eur", ascending=False).head(top_n).index
    for i in idx:
        facts = summary.loc[i, "reviewer_summary"]
        prompt = ("Rewrite these reconciliation facts as a concise, professional comment a Finance & "
                  "Accounting reviewer would accept (max 70 words). State what makes up the balance, what is "
                  "wrong, who must act and by when. Do not invent numbers.\n\nFacts: " + facts)
        msg = client.messages.create(model=MODEL, max_tokens=300, messages=[{"role": "user", "content": prompt}])
        summary.loc[i, "reviewer_summary"] = msg.content[0].text.strip()
        summary.loc[i, "summary_source"] = "LLM"
    return summary


# ---------------------------------------------------------------------------
# Comment writer used by the dashboard ("Write comment with AI")
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a senior balance sheet reconciliation accountant at a large telecom group (SAP, SOX "
    "environment). You write reconciliation comments that a Finance & Accounting reviewer will accept: "
    "factual, specific, short, in English. SAP texts may be abbreviated or in German, French, "
    "Hungarian, Portuguese etc. - interpret them. Never invent numbers, dates or document numbers: "
    "use only the facts given. If something is a guess, label it 'Likely cause:'. Plain text only, "
    "no markdown, no greeting.")


# ---------------------------------------------------------------------------
# Provider layer: Claude (Anthropic), Gemini (Google, free tier) or a local
# model through Ollama. Only this part knows about providers - the prompts and
# the dashboard are the same for all of them.
# ---------------------------------------------------------------------------
PROVIDERS = {
    "groq": "Groq (free, fast)",
    "gemini": "Gemini (Google, free tier)",
    "openrouter": "OpenRouter (free models)",
    "mistral": "Mistral (free tier)",
    "claude": "Claude (Anthropic, paid)",
    "ollama": "Local model (Ollama, free)",
}
# Providers that speak the OpenAI "chat/completions" format: base URL per provider
COMPAT = {
    "groq": os.environ.get("RECON_GROQ_URL", "https://api.groq.com/openai/v1"),
    "openrouter": os.environ.get("RECON_OPENROUTER_URL", "https://openrouter.ai/api/v1"),
    "mistral": os.environ.get("RECON_MISTRAL_URL", "https://api.mistral.ai/v1"),
}
# Preferred models (shown first when the provider offers them)
PREFERRED = {
    "groq": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"],
    "openrouter": [],
    "mistral": ["mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"],
}
GEMINI_URL = os.environ.get("RECON_GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
OLLAMA_URL = os.environ.get("RECON_OLLAMA_URL", "http://localhost:11434")
FALLBACK_MODELS = {
    "claude": ["claude-sonnet-5-5", "claude-haiku-5-5"],
    "gemini": ["gemini-flash-latest", "gemini-flash-lite-latest"],
    "ollama": ["llama3.1", "qwen2.5"],
    "groq": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"],
    "openrouter": ["openai/gpt-oss-120b:free", "meta-llama/llama-3.3-70b-instruct:free"],
    "mistral": ["mistral-small-latest", "mistral-medium-latest"],
}


class AIError(Exception):
    """Readable error for the dashboard."""


class BusyError(AIError):
    """Provider overloaded or rate-limited: worth retrying or switching model."""


LAST_MODEL = {"name": None}   # which model actually answered (after automatic fallback)


_TIMEOUT_MSG = ("The AI service took too long to answer. Free tiers are often busy - try again, "
                "or pick a 'flash-lite' model under AI settings.")


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 60):
    import json as _json
    import urllib.error
    import urllib.request
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "ReconRadar/1.0",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        if e.code in (429, 500, 502, 503, 504):
            raise BusyError(_friendly(e.code, detail)) from None
        raise AIError(_friendly(e.code, detail)) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise AIError(_TIMEOUT_MSG) from None
        if url.startswith(OLLAMA_URL):
            raise AIError("Ollama is not running. Open the Ollama app (or run 'ollama serve') and try again.") from None
        raise AIError(f"Could not reach the AI service ({e.reason}). Check your internet connection.") from None
    except (TimeoutError, socket.timeout):
        raise AIError(_TIMEOUT_MSG) from None


def _friendly(code: int, detail: str) -> str:
    d = detail.lower()
    if code in (401, 403) or "api key" in d or "api_key" in d:
        return "The API key is not valid. Check it under AI settings."
    if code in (500, 502, 503, 504) or "high demand" in d or "overloaded" in d:
        return ("The model is overloaded right now (high demand on the provider's side - not your setup). "
                "Try again in a minute or pick another model.")
    if code == 429:
        return "Rate limit reached (free tiers allow only a few requests per minute/day). Wait a moment and retry."
    if code == 404 or "not found" in d:
        return "This model is not available. Pick another model under AI settings."
    return f"The AI service returned an error ({code}): {detail[:160]}"


def _gemini_rank(name: str):
    """Stable 'flash' models first (fast, free-tier friendly); previews, experiments and
    code-named models last, because they can be slow, rate-limited or switched off."""
    import re
    ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    v = -float(ver.group(1)) if ver else 0.0
    unstable = any(t in name for t in ["preview", "exp", "experimental"]) or not re.search(r"(flash|pro)", name)
    if re.fullmatch(r"gemini-[\d.]+-flash", name):
        tier = 0
    elif name in ("gemini-flash-latest",):
        tier = 1
    elif re.fullmatch(r"gemini-[\d.]+-flash-lite", name) or name == "gemini-flash-lite-latest":
        tier = 2
    elif "flash" in name and not unstable:
        tier = 3
    elif "pro" in name and not unstable:
        tier = 4
    else:
        tier = 5
    return (tier + (3 if unstable else 0), v, name)


def _gemini(prompt: str, key: str, model: str, max_tokens: int) -> str:
    """Gemini via streaming (SSE): text arrives in pieces, so a slow answer does not hit
    the read timeout. Thinking is set to 'low' for speed; if the model does not accept
    that setting, the request is retried without it."""
    import urllib.error
    import urllib.request
    base = {"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max(max_tokens * 6, 2048), "temperature": 0.3}}
    variants = [{"thinkingLevel": "low"}, {"thinkingBudget": 0}, None]
    last_err = None
    for think in variants:
        body = json.loads(json.dumps(base))
        if think:
            body["generationConfig"]["thinkingConfig"] = think
        req = urllib.request.Request(
            f"{GEMINI_URL}/models/{model}:streamGenerateContent?alt=sse", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            parts, reason = [], None
            with urllib.request.urlopen(req, timeout=120) as r:
                for raw in r:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    for c in chunk.get("candidates") or []:
                        reason = c.get("finishReason") or reason
                        for prt in c.get("content", {}).get("parts", []):
                            if prt.get("text") and not prt.get("thought"):
                                parts.append(prt["text"])
                    if not chunk.get("candidates") and chunk.get("promptFeedback"):
                        reason = chunk["promptFeedback"].get("blockReason")
            text = "".join(parts).strip()
            if not text:
                raise AIError(f"Gemini returned no text (reason: {reason}). Try again or pick another model.")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:400]
            # unsupported thinking setting -> try the next variant
            if e.code == 400 and think and "think" in detail.lower():
                last_err = detail
                continue
            if e.code in (429, 500, 502, 503, 504):
                raise BusyError(_friendly(e.code, detail)) from None
            raise AIError(_friendly(e.code, detail)) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise AIError(_TIMEOUT_MSG) from None
            raise AIError(f"Could not reach Gemini ({e.reason}). Check your internet connection.") from None
        except (TimeoutError, socket.timeout):
            raise AIError(_TIMEOUT_MSG) from None
    raise AIError(f"Gemini rejected the request: {last_err}")


def _gemini_with_fallback(prompt: str, key: str, model: str, max_tokens: int) -> str:
    """Free-tier Gemini is often 'busy' (503/429). Retry the chosen model once after a short
    pause, then automatically try the next stable flash models from the user's list."""
    import time
    tried, last = [], None
    candidates = [model] + [m for m in list_models("gemini", key) if m != model and "flash" in m][:3]
    for i, m in enumerate(candidates):
        for attempt in range(2 if i == 0 else 1):
            try:
                text = _gemini(prompt, key, m, max_tokens)
                LAST_MODEL["name"] = m
                return text
            except BusyError as e:
                last = e
                time.sleep(3 if attempt == 0 else 0)
        tried.append(m)
    raise AIError(f"All tried Gemini models are busy right now ({', '.join(tried)}). "
                  "This is on Google's side - wait a minute and try again, or use another provider.") from last


def _compat(provider: str, prompt: str, key: str, model: str, max_tokens: int) -> str:
    headers = {"Authorization": f"Bearer {key}"}
    if provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/reconradar", "X-Title": "ReconRadar"})
    body = {"model": model, "temperature": 0.3, "max_tokens": max(max_tokens * 4, 1024),
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
    data = _http("POST", f"{COMPAT[provider]}/chat/completions", body, headers=headers, timeout=120)
    choices = data.get("choices") or []
    text = (choices[0].get("message", {}).get("content") or "") if choices else ""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    if not text.strip():
        raise AIError(f"{PROVIDERS[provider].split(' (')[0]} returned no text. Try again or pick another model.")
    return text.strip()


def _with_fallback(provider: str, prompt: str, key: str, model: str, max_tokens: int) -> str:
    """Retry once if the model is busy / rate-limited, then try up to 3 other models."""
    import time
    tried, last = [], None
    others = [m for m in list_models(provider, key) if m != model][:3]
    for i, m in enumerate([model] + others):
        for attempt in range(2 if i == 0 else 1):
            try:
                text = _compat(provider, prompt, key, m, max_tokens)
                LAST_MODEL["name"] = m
                return text
            except BusyError as e:
                last = e
                time.sleep(3 if attempt == 0 else 0)
        tried.append(m)
    raise AIError(f"All tried models are busy or rate-limited right now ({', '.join(tried)}). "
                  "Wait a minute and try again, or switch provider.") from last


def list_models(provider: str, api_key: str | None = None) -> list[str]:
    """Models the user can pick; falls back to a short default list."""
    try:
        if provider == "claude":
            import anthropic
            ids = [m.id for m in anthropic.Anthropic(api_key=api_key).models.list(limit=50)]
            return ids or FALLBACK_MODELS["claude"]
        if provider == "gemini":
            data = _http("GET", f"{GEMINI_URL}/models?pageSize=200", headers={"x-goog-api-key": api_key or ""}, timeout=15)
            ids = [m["name"].split("/", 1)[-1] for m in data.get("models", [])
                   if "generateContent" in m.get("supportedGenerationMethods", [])
                   and "gemini" in m["name"] and not any(x in m["name"] for x in ["image", "tts", "audio", "embedding", "live"])]
            return sorted(ids, key=_gemini_rank) or FALLBACK_MODELS["gemini"]
        if provider == "ollama":
            data = _http("GET", f"{OLLAMA_URL}/api/tags", timeout=5)
            return [m["name"] for m in data.get("models", [])]
        if provider in COMPAT:
            data = _http("GET", f"{COMPAT[provider]}/models", headers={"Authorization": f"Bearer {api_key}"},
                         timeout=15)
            skip = ["whisper", "tts", "guard", "embed", "ocr", "moderation", "playai", "orpheus",
                    "transcri", "vision", "image", "audio", "compound", "codestral", "pixtral", "voxtral"]
            ids = [m["id"] for m in data.get("data", []) if not any(x in m["id"].lower() for x in skip)]
            if provider == "openrouter":            # only the free models
                ids = [i for i in ids if i.endswith(":free")]
            pref = [m for m in PREFERRED.get(provider, []) if m in ids]
            return (pref + sorted(i for i in ids if i not in pref)) or FALLBACK_MODELS[provider]
    except Exception:
        pass
    return FALLBACK_MODELS.get(provider, [])


def _call(prompt: str, api_key: str | None = None, model: str | None = None, max_tokens: int = 500,
          provider: str = "claude") -> str:
    if provider == "claude":
        try:
            import anthropic
        except ImportError:
            raise AIError("The 'anthropic' package is missing. Run: python3 -m pip install -r requirements.txt") from None
        try:
            client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
            msg = client.messages.create(model=model or MODEL, max_tokens=max_tokens, system=SYSTEM_PROMPT,
                                         messages=[{"role": "user", "content": prompt}])
        except anthropic.APIStatusError as e:
            raise AIError(_friendly(e.status_code, str(e))) from None
        except anthropic.APIConnectionError:
            raise AIError("Could not reach Claude. Check your internet connection.") from None
        LAST_MODEL["name"] = model or MODEL
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    if provider in COMPAT:
        key = api_key or os.environ.get(f"{provider.upper()}_API_KEY", "")
        if not key:
            raise AIError("Add your API key under AI settings.")
        return _with_fallback(provider, prompt, key, model, max_tokens)

    if provider == "gemini":
        return _gemini_with_fallback(prompt, api_key or os.environ.get("GEMINI_API_KEY", ""), model, max_tokens)

    if provider == "ollama":
        body = {"model": model, "stream": False, "options": {"temperature": 0.3, "num_predict": max_tokens * 2},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
        data = _http("POST", f"{OLLAMA_URL}/api/chat", body, timeout=300)
        text = data.get("message", {}).get("content", "")
        # some local models wrap their reasoning in <think> tags - keep only the answer
        if "</think>" in text:
            text = text.split("</think>", 1)[1]
        LAST_MODEL["name"] = model
        return text.strip()

    raise AIError(f"Unknown AI provider: {provider}")


def _s(v) -> str:
    return "" if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v)


def _item_line(r) -> str:
    return (f"doc {r['doc_no']} | posted {pd.to_datetime(r['posting_date']):%d.%m.%Y} | {int(r['age_days'])} days | "
            f"{r['amount_lc']:,.2f} {_s(r['curr_lc'])} | user {_s(r['user'])} | text: {_s(r['text'])} | "
            f"header: {_s(r['header_text'])} | ref: {_s(r['reference'])} | category: {_s(r['category'])} | "
            f"flags: {_s(r['reason_text']) or 'none'}")


def write_group_comment(items: pd.DataFrame, problem: str, owner: str, action: str, due,
                        api_key: str | None = None, model: str | None = None, provider: str = "claude") -> str:
    """One reconciliation comment for a to-do group (entity + account + problem)."""
    r0 = items.iloc[0]
    by_cur = items.groupby("curr_lc")["amount_lc"].sum()
    texts = items["text"].fillna("(blank)").value_counts().head(8)
    sample = items.sort_values("risk_score", ascending=False).head(15)
    prompt = (
        f"Write the reconciliation comment for this group of open items (max 130 words).\n"
        f"Structure, one short paragraph each: 1) What the balance is. 2) What is wrong. "
        f"3) Likely cause (only if the facts support it). 4) Action: who, what, by when.\n\n"
        f"Working group: {r0['work_group']} | ERP: {r0['erp']} | Entity: {r0['cocd']}\n"
        f"Account: {r0['account']} {r0['account_desc']} ({r0['account_mgmt']})\n"
        f"Problem found by the system: {problem}\n"
        f"Open items in group: {len(items)} | Net amount: "
        + ", ".join(f"{v:,.2f} {k}" for k, v in by_cur.items())
        + f" | Posting dates {pd.to_datetime(items['posting_date']).min():%d.%m.%Y} to "
          f"{pd.to_datetime(items['posting_date']).max():%d.%m.%Y} | Oldest {int(items['age_days'].max())} days\n"
        f"Most common SAP texts: " + "; ".join(f"'{t}' x{n}" for t, n in texts.items()) + "\n"
        f"Responsible: {owner} | Required action: {action} | Due: {due}\n\n"
        f"Highest-risk items:\n" + "\n".join(_item_line(r) for _, r in sample.iterrows()))
    return _call(prompt, api_key, model, 600, provider)


def write_item_comment(item: pd.Series, api_key: str | None = None, model: str | None = None,
                       provider: str = "claude") -> str:
    """A reviewer-ready comment for one line item."""
    prompt = (
        "Write the reconciliation comment for this single open item (max 60 words): what it is, "
        "whether it needs action, and the next step with owner and due date.\n\n"
        f"Working group: {item['work_group']} | Entity: {item['cocd']} | Account: {item['account']} "
        f"{item['account_desc']}\nItem: {_item_line(item)}\n"
        f"System classification: {item['recon_class']} | Problem: {item['issue_type']}\n"
        f"Responsible: {item['owner_team']} | Action: {item['action']} | Due: {item['due_date']}")
    return _call(prompt, api_key, model, 300, provider)
