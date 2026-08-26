"""Layer 3 — LLM scoring with Gemini 2.0 Flash (free tier).

Sends batches of ~10 listings per API call, asks for structured JSON scores,
computes a weighted composite, then applies the post-scoring hard rules:
  * international + visa==NO      -> drop
  * india + estimated CTC < floor -> drop
  * composite < minimum          -> drop

If ``GEMINI_API_KEY`` is absent or the API keeps failing, a deterministic
rule-based fallback keeps the pipeline alive (Risk Register: "Gemini free tier
gets reduced/removed"). The fallback is coarse but never blocks the run.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from common import config
from processing.models import (Pipeline, RawJob, ScoredJob, Verdict, VisaStatus)
from processing.sponsor_register import SponsorRegister

log = logging.getLogger("jobradar")

_PROMPT_HEADER = """You are a STRICT job relevance scorer for a specific candidate:

CANDIDATE PROFILE — Divyansh, ~1 year experience, AI Software Engineer at Amdocs.
Builds PRODUCTION LLM and agentic systems: custom multi-agent orchestration, \
retrieval-grounded generation (RAG), Azure OpenAI (GPT-4.1), LangChain/LangGraph, MCP, \
NL->SQL pipelines, LoRA/PEFT fine-tuning, agent memory, and evaluation-gated deployment \
(CI-blocking eval suites). Strong Python/FastAPI/Docker, MLOps, drift monitoring. \
Published researcher (Wiley). The BEST-FIT roles are: AI Engineer, GenAI / Generative AI \
Engineer, Applied AI Engineer, Agentic AI Engineer, LLM Engineer, Forward Deployed \
Engineer (at AI/LLM product companies), and ML Engineer. These should score HIGHEST on \
role_fit. Generic BI/reporting/analytics or plain SWE roles score LOW.

Target geographies: UAE/Dubai (TOP priority), UK, India, Germany, Netherlands, Ireland, \
Europe, and remote. ONLY roles that accept 0-2 years of experience are acceptable — this is \
a hard requirement, not a preference.

For EACH job below return one JSON object with these fields:
- index (int): copy the job's index exactly.
- years_required (int): the MINIMUM years of professional experience the job requires. Use 0 \
if it is explicitly entry-level/graduate/junior or states no experience requirement. If it \
says e.g. "3+ years", "5-7 years", "minimum 4 years", return the lower bound (3, 5, 4). A title \
containing Senior, Sr, Lead, Staff, Principal, or Architect implies at least 3 years — return \
3 or more for those UNLESS the description explicitly says it accepts 0-2 years / entry level.
- experience_fit (0-100): 90+ if clearly 0-2 YoE, 50 if ambiguous, 0 if it needs 3+ years.
- role_fit (0-100): Is this a real AI/ML/GenAI/DS role matching the candidate profile? 0 for \
pure BI/reporting/analytics or generic SWE, 50 for hybrid, 90+ for core AI/GenAI/agentic/LLM \
or a Forward Deployed Engineer role at an AI/LLM product company.
- tech_stack_match (0-100): overlap with Python, LLMs, RAG, agents, Azure/AWS, MLOps, NLP, \
transformers, PyTorch.
- phd_required (bool): true ONLY if a PhD/doctorate is MANDATORY to apply. Return false if a \
PhD is merely preferred, "a plus", "nice to have", "or equivalent experience", or listed as \
"PhD/Master's/Bachelor's" — those are acceptable. When unstated, return false.
- visa_status: "YES" if sponsorship is offered/implied, "NO" if right-to-work/no-sponsorship \
is required, "UNKNOWN" if genuinely unclear. Return "N/A" for India jobs. IMPORTANT: for UAE \
and Gulf roles employer work-visa sponsorship is standard practice, so treat UNSTATED \
sponsorship there as "YES" unless the ad says otherwise; for UK/Europe, unstated stays \
"UNKNOWN".
- salary_estimate: for INDIA jobs with no disclosed salary, estimate a likely CTC range in \
LPA (e.g. "18-25 LPA") from role/company/location; else null.
- red_flags: one short string ONLY for a genuinely concerning issue. Do NOT flag a notice \
period of 30 days or less, and do NOT flag UAE / Dubai / UK / India / Europe / remote \
locations — those are all in scope. Otherwise null.

Return ONLY a JSON array of these objects. No prose, no markdown fences.

JOBS:
"""


class Scorer:
    def __init__(self):
        sc = config.scoring()
        self.gcfg = sc["gemini"]
        self.weights = sc["weights"]
        self.thresholds = sc["thresholds"]
        self.visa_map = sc["visa_score_map"]
        self.tech_keywords = [k.lower() for k in sc["tech_stack_keywords"]]
        # Resume-alignment priority boost (Dubai/UAE + AI/GenAI/FDE/Applied-AI).
        pb = sc.get("priority_boost") or {}
        self.pb_max = int(pb.get("max_bonus", 0))
        self.pb_geo = [(str(r["match"]).lower(), int(r["bonus"]))
                       for r in pb.get("geo", [])]
        self.pb_title = [(str(r["match"]).lower(), int(r["bonus"]))
                         for r in pb.get("title", [])]
        # Deterministic visa verification (UK/NL official sponsor registers).
        self.sponsors = SponsorRegister()
        self._sponsor_upgrades = 0
        self.api_key = config.env(self.gcfg["api_key_env"])
        # Optional second provider: Groq (OpenAI-compatible, generous free tier).
        # Any ladder entry prefixed "groq:" is routed here.
        self.groq_key = config.env(self.gcfg.get("groq_api_key_env", "GROQ_API_KEY"))
        self.groq_endpoint = self.gcfg.get(
            "groq_endpoint", "https://api.groq.com/openai/v1/chat/completions")
        self.batch_size = int(self.gcfg.get("batch_size", 10))
        self.max_desc = int(self.gcfg.get("max_description_chars", 500))
        self.min_interval = float(self.gcfg.get("min_request_interval_secs", 0))
        self._last_call = 0.0
        # Model ladder: try the best model first; when it exhausts its quota
        # (daily limit) or stays saturated, cascade to the next. Falls back to
        # a single "model" entry for backward compatibility.
        self.models: list[str] = [m for m in
                                  (self.gcfg.get("models")
                                   or [self.gcfg.get("model")]) if m]
        self.model_idx = 0

    # ---- public entry point ----

    def score(self, jobs: list[RawJob]) -> list[ScoredJob]:
        if not jobs:
            return []
        use_llm = bool(self.api_key or self.groq_key)
        if not use_llm:
            log.warning("[scorer] no LLM key (GEMINI/GROQ) -> rule-based fallback")

        if use_llm:
            log.info("[scorer] model ladder: %s", " -> ".join(self.models))

        scored: list[ScoredJob] = []
        for start in range(0, len(jobs), self.batch_size):
            batch = jobs[start:start + self.batch_size]
            # Once every model in the ladder is exhausted, model_idx runs past
            # the end and _score_batch_llm returns immediately with no network
            # call — the rest of the run finishes on the rule-based fallback.
            results = (self._score_batch_llm(batch) if use_llm
                       else [None] * len(batch))
            for job, raw_score in zip(batch, results):
                scored.append(self._assemble(job, raw_score))
            log.info("[scorer] scored %d/%d%s", min(start + self.batch_size,
                     len(jobs)), len(jobs),
                     f"  [{self.models[self.model_idx]}]"
                     if self.model_idx < len(self.models) else "  [fallback]")

        if self._sponsor_upgrades:
            log.info("[scorer] visa upgraded to YES via sponsor register: %d",
                     self._sponsor_upgrades)
        return self._apply_hard_rules(scored)

    # ---- Gemini call ----

    def _score_batch_llm(self, batch: list[RawJob]) -> list[dict[str, Any] | None]:
        """Score one batch, walking the model ladder as models get exhausted."""
        prompt = self._build_prompt(batch)
        while self.model_idx < len(self.models):
            model = self.models[self.model_idx]
            outcome, data = self._call_model(model, prompt, batch)
            if outcome == "ok":
                return data
            if outcome == "account-exhausted":
                # Account-wide billing/credit block — every model draws from the
                # same balance, so cascading is pointless. Abort the whole ladder.
                self.model_idx = len(self.models)
                log.error("[scorer] Gemini account credits/quota exhausted -> "
                          "rule-based fallback for the rest of this run "
                          "(check Gemini API billing)")
                break
            # This model is exhausted/unusable for now — advance the ladder.
            remaining = self.models[self.model_idx + 1:]
            log.warning("[scorer] model '%s' exhausted (%s)%s", model, outcome,
                        f" -> switching to '{remaining[0]}'" if remaining
                        else " -> rule-based fallback for the rest of this run")
            self.model_idx += 1
        return [None] * len(batch)

    def _call_model(self, model: str, prompt: str, batch: list[RawJob]
                    ) -> tuple[str, list[dict[str, Any] | None] | None]:
        """Dispatch to the right provider. Returns (outcome, data):
          ("ok", aligned_scores)     — success
          ("daily-quota", None)      — RPD exhausted; advance immediately
          ("account-exhausted", None)— billing/credit block; abort ladder
          ("unavailable", None)      — RPM/5xx/no-key; advance
        """
        if model.startswith("groq:"):
            return self._call_groq(model[len("groq:"):], prompt, batch)
        if not self.api_key:
            return "unavailable", None      # Gemini model but no Gemini key
        return self._call_gemini(model, prompt, batch)

    def _call_gemini(self, model: str, prompt: str, batch: list[RawJob]
                     ) -> tuple[str, list[dict[str, Any] | None] | None]:
        endpoint = self.gcfg["endpoint"].format(model=model)
        gen_config: dict[str, Any] = {
            "temperature": float(self.gcfg.get("temperature", 0.1)),
        }
        # Gemma models don't support Gemini's JSON response-mode; the prompt
        # still asks for a JSON array and _parse_scores is tolerant of prose.
        if not model.startswith("gemma"):
            gen_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        retries = int(self.gcfg.get("max_retries", 3))
        backoff = int(self.gcfg.get("retry_backoff_secs", 5))

        for attempt in range(1, retries + 1):
            try:
                self._throttle()
                resp = requests.post(endpoint, params={"key": self.api_key},
                                     json=payload, timeout=90)
                if resp.status_code == 429:
                    body = resp.text.lower()
                    # Account-wide billing/credit depletion blocks every model.
                    if any(k in body for k in ("prepay", "credit", "billing")):
                        log.error("[scorer] %s: %s", model, resp.text[:200])
                        return "account-exhausted", None
                    # Daily-quota exhaustion won't clear today — advance now.
                    if "per day" in body or "perday" in body:
                        log.warning("[scorer] %s daily quota reached", model)
                        return "daily-quota", None
                    # Per-minute rate limit — transient, wait and retry.
                    wait = backoff * attempt
                    log.warning("[scorer] %s rate-limited (RPM); backoff %ss",
                                model, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code in (500, 503):
                    wait = backoff * attempt
                    log.warning("[scorer] %s HTTP %s (transient); backoff %ss",
                                model, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    # e.g. 404 retired model / 403 bad key — surface and advance.
                    log.error("[scorer] %s HTTP %s: %s", model,
                              resp.status_code, resp.text[:200])
                    return "unavailable", None
                text = self._extract_text(resp.json())
                parsed = self._parse_scores(text)
                return "ok", self._align(parsed, batch)
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("[scorer] %s attempt %d/%d failed: %s",
                            model, attempt, retries, exc)
                time.sleep(backoff)
        return "unavailable", None

    def _call_groq(self, model: str, prompt: str, batch: list[RawJob]
                   ) -> tuple[str, list[dict[str, Any] | None] | None]:
        """Call Groq's OpenAI-compatible chat endpoint. Same outcome contract."""
        if not self.groq_key:
            return "unavailable", None      # Groq model but no GROQ_API_KEY
        headers = {"Authorization": f"Bearer {self.groq_key}",
                   "Content-Type": "application/json"}
        # Groq (OpenAI-compatible) JSON mode returns an OBJECT, not a bare array,
        # so ask for {"results": [...]} — _parse_scores already unwraps that.
        groq_prompt = (prompt + '\n\nReturn a single JSON object of the form '
                       '{"results": [ one object per job, as specified above ]}.')
        payload = {
            "model": model,
            "temperature": float(self.gcfg.get("temperature", 0.1)),
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": groq_prompt}],
        }
        retries = int(self.gcfg.get("max_retries", 3))
        backoff = int(self.gcfg.get("retry_backoff_secs", 5))
        tag = f"groq:{model}"

        for attempt in range(1, retries + 1):
            try:
                self._throttle()
                resp = requests.post(self.groq_endpoint, headers=headers,
                                     json=payload, timeout=90)
                if resp.status_code == 429:
                    body = resp.text.lower()
                    # Groq signals daily exhaustion via "per day" / TPD / RPD.
                    if "per day" in body or "rpd" in body or "tpd" in body:
                        log.warning("[scorer] %s daily quota reached", tag)
                        return "daily-quota", None
                    wait = backoff * attempt
                    log.warning("[scorer] %s rate-limited; backoff %ss", tag, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code in (500, 502, 503):
                    wait = backoff * attempt
                    log.warning("[scorer] %s HTTP %s (transient); backoff %ss",
                                tag, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    log.error("[scorer] %s HTTP %s: %s", tag,
                              resp.status_code, resp.text[:200])
                    return "unavailable", None
                content = resp.json()["choices"][0]["message"]["content"]
                parsed = self._parse_scores(content)
                return "ok", self._align(parsed, batch)
            except (requests.RequestException, ValueError, KeyError,
                    IndexError) as exc:
                log.warning("[scorer] %s attempt %d/%d failed: %s",
                            tag, attempt, retries, exc)
                time.sleep(backoff)
        return "unavailable", None

    def _throttle(self) -> None:
        """Space out API calls to respect requests-per-minute limits."""
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _build_prompt(self, batch: list[RawJob]) -> str:
        lines = []
        for i, j in enumerate(batch):
            snippet = j.description[:self.max_desc].replace("\n", " ")
            pl = j.pipeline.value if j.pipeline else "?"
            lines.append(
                f"[{i}] ({pl}) {j.title} at {j.company or 'Unknown'}, "
                f"{j.location or 'n/a'} — {snippet}")
        return _PROMPT_HEADER + "\n".join(lines)

    @staticmethod
    def _extract_text(response: dict) -> str:
        candidates = response.get("candidates", [])
        if not candidates:
            raise ValueError("no candidates in Gemini response")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    @staticmethod
    def _parse_scores(text: str) -> list[dict[str, Any]]:
        text = text.strip()
        # Strip accidental ```json fences.
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Last resort: grab the first [...] block.
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                raise ValueError("no JSON array in model output")
            data = json.loads(m.group(0))
        if isinstance(data, dict):
            data = data.get("jobs") or data.get("results") or [data]
        return data

    @staticmethod
    def _align(parsed: list[dict], batch: list[RawJob]) -> list[dict | None]:
        """Match parsed objects back to batch positions by their ``index``."""
        by_index: dict[int, dict] = {}
        for obj in parsed:
            try:
                by_index[int(obj.get("index"))] = obj
            except (TypeError, ValueError):
                continue
        # Fall back to positional order if indices are missing/garbled.
        if len(by_index) < len(parsed):
            return [parsed[i] if i < len(parsed) else None
                    for i in range(len(batch))]
        return [by_index.get(i) for i in range(len(batch))]

    # ---- assembling a ScoredJob ----

    def _assemble(self, job: RawJob, raw: dict[str, Any] | None) -> ScoredJob:
        if raw is None:
            return self._fallback_score(job)

        visa = self._maybe_upgrade_visa(job, self._coerce_visa(
            job, raw.get("visa_status")))
        sj = ScoredJob(
            raw=job,
            experience_fit=raw.get("experience_fit", 0),
            years_required=raw.get("years_required", 0),
            role_fit=raw.get("role_fit", 0),
            tech_stack_match=raw.get("tech_stack_match", 0),
            visa_status=visa,
            salary_estimate=raw.get("salary_estimate"),
            phd_required=bool(raw.get("phd_required", False)),
            red_flags=raw.get("red_flags"),
        )
        sj.composite_score = self._composite(sj)
        sj.priority_bonus = self._priority_bonus(job)
        sj.composite_score = min(100, sj.composite_score + sj.priority_bonus)
        sj.verdict = self._verdict(sj.composite_score)
        return sj

    def _priority_bonus(self, job: RawJob) -> int:
        """Resume-alignment boost: best geo match + best title match, capped.

        Rewards the top-priority geography (Dubai/UAE) and the roles that fit
        Divyansh's profile (AI/GenAI/Applied-AI/Agentic/LLM/Forward-Deployed).
        """
        if not (self.pb_geo or self.pb_title):
            return 0
        geo_hay = f"{job.location} {job.description[:400]}".lower()
        title_hay = job.title.lower()
        geo = max((b for m, b in self.pb_geo if m in geo_hay), default=0)
        title = max((b for m, b in self.pb_title if m in title_hay), default=0)
        return min(self.pb_max, geo + title)

    def _coerce_visa(self, job: RawJob, value: Any) -> VisaStatus:
        if job.pipeline == Pipeline.INDIA:
            return VisaStatus.NA
        v = str(value or "UNKNOWN").upper().strip()
        return {"YES": VisaStatus.YES, "NO": VisaStatus.NO,
                "UNKNOWN": VisaStatus.UNKNOWN}.get(v, VisaStatus.UNKNOWN)

    def _maybe_upgrade_visa(self, job: RawJob, visa: VisaStatus) -> VisaStatus:
        """Override UNKNOWN/lower to YES when the employer is on the UK/NL
        official sponsor register — authoritative beats the LLM's guess. An
        explicit NO from the ad is left untouched (registers list eligibility,
        not this specific role's stance)."""
        if (job.pipeline == Pipeline.INTERNATIONAL and visa == VisaStatus.UNKNOWN
                and self.sponsors.is_sponsor(job.company, job.location)):
            self._sponsor_upgrades += 1
            return VisaStatus.YES
        return visa

    def _composite(self, sj: ScoredJob) -> int:
        w = self.weights
        if sj.raw.pipeline == Pipeline.INTERNATIONAL:
            visa_sub = self.visa_map.get(sj.visa_status.value, 0)
            total = (sj.experience_fit * w["experience_fit"]
                     + sj.role_fit * w["role_fit"]
                     + sj.tech_stack_match * w["tech_stack_match"]
                     + visa_sub * w["visa_status"])
            return int(round(total))
        # India: redistribute the visa weight across the remaining three.
        base = w["experience_fit"] + w["role_fit"] + w["tech_stack_match"]
        total = (sj.experience_fit * w["experience_fit"]
                 + sj.role_fit * w["role_fit"]
                 + sj.tech_stack_match * w["tech_stack_match"]) / base
        return int(round(total))

    def _verdict(self, score: int) -> Verdict:
        if score >= self.thresholds["strong_match"]:
            return Verdict.STRONG
        if score >= self.thresholds["minimum_composite"]:
            return Verdict.REVIEW
        return Verdict.DROP

    # ---- rule-based fallback ----

    def _fallback_score(self, job: RawJob) -> ScoredJob:
        text = f"{job.title} {job.description}".lower()
        hits = sum(1 for k in self.tech_keywords if k in text)
        tech = min(100, hits * 15)
        role = 80 if any(k in text for k in
                         ("machine learning", " ml ", "ai engineer", "genai",
                          "nlp", "deep learning", "llm")) else 45
        exp = 40 if any(k in job.title.lower() for k in
                        ("senior", "lead", "staff", "principal")) else 65
        visa = (VisaStatus.NA if job.pipeline == Pipeline.INDIA
                else self._maybe_upgrade_visa(job, VisaStatus.UNKNOWN))
        sj = ScoredJob(raw=job, experience_fit=exp, role_fit=role,
                       tech_stack_match=tech, visa_status=visa,
                       red_flags="scored by fallback (no LLM)")
        sj.composite_score = self._composite(sj)
        sj.priority_bonus = self._priority_bonus(job)
        sj.composite_score = min(100, sj.composite_score + sj.priority_bonus)
        sj.verdict = self._verdict(sj.composite_score)
        return sj

    # ---- post-scoring hard rules ----

    def _apply_hard_rules(self, scored: list[ScoredJob]) -> list[ScoredJob]:
        min_score = self.thresholds["minimum_composite"]
        max_years = int(self.thresholds.get("max_years_experience", 2))
        min_role = int(self.thresholds.get("min_role_fit", 0))
        min_exp = int(self.thresholds.get("min_experience_fit", 0))
        floor_lpa = float(config.pipelines()["pipelines"]["india"]["min_ctc_lpa"])
        kept = []
        drop_years = drop_role = drop_visa = drop_salary = drop_thresh = 0
        drop_phd = 0

        for sj in scored:
            # PhD backstop — the pre-filter regex catches explicit "PhD required"
            # phrasings; this catches the ones the LLM judged mandatory that the
            # regex missed.
            if sj.phd_required:
                drop_phd += 1
                continue
            # Hard experience gate — the whole point of the search. A role that
            # requires more than max_years is dropped no matter how good its
            # other scores are. (years_required=0 = entry/unstated, kept.)
            if sj.years_required > max_years:
                drop_years += 1
                continue
            if min_exp and sj.experience_fit < min_exp:
                drop_years += 1
                continue
            # Not actually an AI/ML role -> drop, don't just annotate.
            if min_role and sj.role_fit < min_role:
                drop_role += 1
                continue
            if (sj.raw.pipeline == Pipeline.INTERNATIONAL
                    and sj.visa_status == VisaStatus.NO):
                drop_visa += 1
                continue
            if sj.raw.pipeline == Pipeline.INDIA:
                est = _estimate_top_lpa(sj.salary_estimate)
                if est is not None and est < floor_lpa:
                    drop_salary += 1
                    continue
            if sj.composite_score < min_score:
                drop_thresh += 1
                continue
            kept.append(sj)

        kept.sort(key=lambda s: s.composite_score, reverse=True)
        log.info("[scorer] hard rules: %d -> %d (PhD req: %d, >%dyr exp: %d, "
                 "low role-fit: %d, visa=NO: %d, low CTC: %d, below %d: %d)",
                 len(scored), len(kept), drop_phd, max_years, drop_years,
                 drop_role, drop_visa, drop_salary, min_score, drop_thresh)
        return kept


def _estimate_top_lpa(estimate: str | None) -> float | None:
    """Pull the highest LPA number out of a string like '18-25 LPA'."""
    if not estimate:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", estimate)
    if not nums:
        return None
    return max(float(n) for n in nums)
