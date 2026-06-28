"""Google Gemini AI integration for market classification and signal analysis."""
import asyncio
import time
import re
from typing import Optional, Dict, Any, List
import logging

from .base import AIAnalysis, AnomalyReport, BaseAIClient, create_signal_prompt, create_classification_prompt, create_match_analysis_prompt
from .logger import get_ai_logger

logger = logging.getLogger(__name__)


class GeminiAnalyzer(BaseAIClient):
    """
    Gemini-powered analyzer for:
    - Fast market categorization (replaces Groq)
    - Signal analysis and reasoning (replaces Claude)
    - Market anomaly detection
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                from backend.config import settings
                self.api_key = settings.GEMINI_API_KEY

            if not self.api_key:
                raise ValueError("GEMINI_API_KEY not configured")

            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except ImportError:
                raise ImportError("google-genai not installed. Run: pip install google-genai")

        return self._client

    def _generate_sync(self, prompt: str, max_tokens: int, temperature: float) -> tuple[str, int]:
        from google.genai import types
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        tokens_used = response.usage_metadata.total_token_count if response.usage_metadata else 0
        return (response.text or "").strip(), tokens_used

    async def _generate(self, prompt: str, max_tokens: int = 200, temperature: float = 0.1) -> tuple[str, int]:
        # The google-genai SDK call is blocking; run it off the event loop
        # so it doesn't stall uvicorn/APScheduler during a scan.
        return await asyncio.wait_for(
            asyncio.to_thread(self._generate_sync, prompt, max_tokens, temperature),
            timeout=15.0,
        )

    def _log_to_db(self, provider: str, call_type: str, prompt: str, result: str,
                   latency_ms: float, tokens_used: int, related_market: str = None):
        try:
            ai_logger = get_ai_logger()
            record = ai_logger.log_call(
                provider=provider,
                model=self.model,
                prompt=prompt,
                response=result,
                latency_ms=latency_ms,
                tokens_used=tokens_used,
                call_type=call_type,
                related_market=related_market,
                success=True
            )
            from backend.models.database import SessionLocal, AILog
            from datetime import datetime
            db = SessionLocal()
            try:
                db_record = AILog(
                    timestamp=datetime.fromisoformat(record.timestamp),
                    provider=record.provider,
                    model=record.model,
                    call_type=record.call_type,
                    latency_ms=record.latency_ms,
                    tokens_used=record.tokens_used,
                    cost_usd=record.cost_usd,
                    success=True,
                    related_market=related_market
                )
                db.add(db_record)
                db.commit()
            finally:
                db.close()
        except Exception:
            pass

    async def classify_market(self, title: str, description: str = "") -> tuple[str, float]:
        start_time = time.time()
        try:
            prompt = create_classification_prompt(title, description)
            result, tokens_used = await self._generate(prompt, max_tokens=20, temperature=0.1)
            latency_ms = (time.time() - start_time) * 1000

            logger.debug(f"Gemini classification: '{title[:30]}' -> {result} ({latency_ms:.0f}ms)")
            self._log_to_db("gemini", "classification", prompt, result, latency_ms, tokens_used)

            parts = result.lower().split(",")
            category = parts[0].strip()
            confidence = 0.7
            if len(parts) > 1:
                try:
                    confidence = int(parts[1].strip()) / 100
                except ValueError:
                    pass

            valid_categories = ["weather", "crypto", "politics", "economics", "sports", "other"]
            if category not in valid_categories:
                for cat in valid_categories:
                    if cat in result.lower():
                        category = cat
                        break
                else:
                    category = "other"

            return (category, min(1.0, max(0.0, confidence)))

        except Exception as e:
            logger.error(f"Gemini classification failed: {e}")
            return ("other", 0.0)

    async def extract_market_details(self, title: str) -> Dict[str, Any]:
        start_time = time.time()
        try:
            prompt = f"""Extract details from this prediction market title:

"{title}"

Respond in this exact format (use N/A if not found):
threshold: <number or N/A>
direction: <above/below/N/A>
asset: <asset name or N/A>
timeframe: <date/period or N/A>"""

            result, _tokens_used = await self._generate(prompt, max_tokens=100, temperature=0.1)
            latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"Gemini extraction ({latency_ms:.0f}ms): {result[:50]}...")

            details: Dict[str, Any] = {"threshold": None, "direction": None, "asset": None, "timeframe": None}
            for line in result.split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if value.lower() != "n/a":
                        if key == "threshold":
                            num_match = re.search(r'[\d,\.]+', value)
                            if num_match:
                                try:
                                    details["threshold"] = float(num_match.group().replace(',', ''))
                                except ValueError:
                                    pass
                        elif key == "direction":
                            if "above" in value.lower():
                                details["direction"] = "above"
                            elif "below" in value.lower():
                                details["direction"] = "below"
                        elif key in details:
                            details[key] = value
            return details

        except Exception as e:
            logger.error(f"Gemini extraction failed: {e}")
            return {"threshold": None, "direction": None, "asset": None, "timeframe": None}

    async def analyze_signal(
        self,
        signal_data: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> AIAnalysis:
        start_time = time.time()
        try:
            prompt = create_signal_prompt(signal_data, context)
            result, tokens_used = await self._generate(prompt, max_tokens=200, temperature=0.3)
            latency_ms = (time.time() - start_time) * 1000

            self._log_to_db(
                "gemini", "analysis", prompt, result, latency_ms, tokens_used,
                related_market=signal_data.get("market_ticker")
            )

            confidence = 0.65
            if any(w in result.lower() for w in ("reliable", "strong", "confident", "clear")):
                confidence = 0.8
            elif any(w in result.lower() for w in ("uncertain", "risky", "weak", "caution")):
                confidence = 0.4

            return AIAnalysis(
                reasoning=result,
                confidence=confidence,
                raw_response=result,
                model_used=self.model,
                provider="gemini",
                latency_ms=latency_ms,
                tokens_used=tokens_used
            )

        except Exception as e:
            logger.error(f"Gemini analysis failed: {e}")
            return AIAnalysis(
                reasoning=f"Analysis unavailable: {e}",
                confidence=0.0,
                model_used=self.model,
                provider="gemini",
                latency_ms=(time.time() - start_time) * 1000
            )

    async def analyze_match(self, data: Dict[str, Any]) -> AIAnalysis:
        """Detailed periodic read on a live football match's Polymarket order book + state."""
        start_time = time.time()
        try:
            prompt = create_match_analysis_prompt(data)
            result, tokens_used = await self._generate(prompt, max_tokens=350, temperature=0.4)
            latency_ms = (time.time() - start_time) * 1000

            self._log_to_db(
                "gemini", "match_analysis", prompt, result, latency_ms, tokens_used,
                related_market=data.get("condition_id"),
            )

            return AIAnalysis(
                reasoning=result,
                confidence=0.6,
                raw_response=result,
                model_used=self.model,
                provider="gemini",
                latency_ms=latency_ms,
                tokens_used=tokens_used,
            )
        except Exception as e:
            logger.error(f"Gemini match analysis failed: {e}")
            return AIAnalysis(
                reasoning=f"Analysis unavailable: {e}",
                confidence=0.0,
                model_used=self.model,
                provider="gemini",
                latency_ms=(time.time() - start_time) * 1000,
            )

    async def detect_anomalies(self, markets: List[Dict[str, Any]]) -> List[AnomalyReport]:
        if not markets:
            return []
        start_time = time.time()
        try:
            market_summary = "\n".join(
                f"- {m.get('ticker', '?')}: price={m.get('price', '?')}, volume={m.get('volume', '?')}"
                for m in markets[:20]
            )
            prompt = f"""Analyze these prediction markets for anomalies (price spikes, unusual volume, odd spreads):

{market_summary}

List any anomalies as:
ticker: <ticker>
type: <price_spike|volume_anomaly|spread_unusual>
severity: <low|medium|high>
description: <one sentence>

If no anomalies, respond: none"""

            result, _tokens_used = await self._generate(prompt, max_tokens=300, temperature=0.2)
            latency_ms = (time.time() - start_time) * 1000
            logger.debug(f"Gemini anomaly detection ({latency_ms:.0f}ms)")

            if "none" in result.lower():
                return []

            anomalies = []
            current: Dict[str, str] = {}
            for line in result.split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    current[key.strip().lower()] = value.strip()
                    if len(current) == 4:
                        try:
                            anomalies.append(AnomalyReport(
                                market_ticker=current.get("ticker", "unknown"),
                                anomaly_type=current.get("type", "unknown"),
                                severity=current.get("severity", "low"),
                                description=current.get("description", ""),
                                ai_analysis=result
                            ))
                        except Exception:
                            pass
                        current = {}
            return anomalies

        except Exception as e:
            logger.error(f"Gemini anomaly detection failed: {e}")
            return []


_gemini_singleton: Optional[GeminiAnalyzer] = None
_gemini_checked = False


def get_gemini_client() -> Optional[GeminiAnalyzer]:
    """Return a shared GeminiAnalyzer if GEMINI_API_KEY is configured, else None."""
    global _gemini_singleton, _gemini_checked
    if not _gemini_checked:
        _gemini_checked = True
        from backend.config import settings
        if settings.GEMINI_API_KEY:
            _gemini_singleton = GeminiAnalyzer(model=settings.GEMINI_MODEL)
        else:
            logger.info("GEMINI_API_KEY not set — AI signal analysis disabled")
    return _gemini_singleton


async def classify_with_gemini_fallback(
    title: str,
    description: str = "",
    gemini_client: Optional[GeminiAnalyzer] = None
) -> tuple[str, float]:
    """Classify market using Gemini, falling back to keyword matching."""
    if gemini_client:
        try:
            return await gemini_client.classify_market(title, description)
        except Exception as e:
            logger.warning(f"Gemini failed, using keyword fallback: {e}")

    from backend.core.classifier import classify_market
    return classify_market(title, description)
