import json
import os
from dataclasses import dataclass

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


DEFAULT_BASE_URL = os.getenv("BIG_BROTHER_BASE_URL", "https://api.openai.com/v1")
DEFAULT_WATCHER_MODEL = os.getenv("BIG_BROTHER_WATCHER_MODEL", "generic-light-llm")
DEFAULT_MPA_MODEL = os.getenv("BIG_BROTHER_MPA_MODEL", DEFAULT_WATCHER_MODEL)


@dataclass
class WatcherDecision:
    off_task: bool
    confidence: float
    summary: str
    relevant_evidence: list[str]
    actor_mode: str


@dataclass
class MPAResult:
    triggered: bool
    should_intervene: bool
    agenda: str
    rationale: str
    supporting_points: list[str]
    actor_mode: str


class WatcherActor:
    def __init__(self):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("BIG_BROTHER_WATCHER_MODEL", DEFAULT_WATCHER_MODEL)
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, session_goal, resources):
        if not self.client:
            return self._fallback(session_goal, resources)

        prompt = (
            "You are the Watcher actor in a study-support agent system.\n"
            "Your job is to review evidence from available resources and decide whether there is "
            "relevant information suggesting the user is off-task.\n"
            "Only include evidence that matters to the study intention. Ignore decorative or "
            "identity details unless they are directly relevant.\n"
            "Be matter-of-fact, concise, and non-judgmental.\n"
            "Return strict JSON with keys:\n"
            "off_task: boolean\n"
            "confidence: number from 0 to 1\n"
            "summary: string under 180 characters\n"
            "relevant_evidence: array of strings, each under 180 characters\n\n"
            f"Study intention:\n{session_goal}\n\n"
            f"Available resources:\n{resources.as_prompt_text()}"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )

        data = json.loads(response.choices[0].message.content or "{}")
        return WatcherDecision(
            off_task=bool(data.get("off_task")),
            confidence=float(data.get("confidence", 0.5)),
            summary=str(data.get("summary", "No summary provided.")),
            relevant_evidence=self._normalize_evidence(data.get("relevant_evidence")),
            actor_mode=f"llm:{self.model}",
        )

    def _fallback(self, session_goal, resources):
        evidence = []
        suspicious_terms = [
            "phone",
            "selfie",
            "scrolling",
            "youtube",
            "netflix",
            "tiktok",
            "instagram",
            "discord",
            "game",
            "gaming",
            "shopping",
            "social media",
        ]

        goal_lower = session_goal.lower()
        for source_name, text in resources.iter_sources():
            lowered = text.lower()
            for term in suspicious_terms:
                if term in lowered and term not in goal_lower:
                    evidence.append(f"{source_name}: {self._extract_sentence(text, term)}")
                    break

        off_task = bool(evidence)
        summary = (
            "Relevant off-task evidence found in current resources."
            if off_task
            else "No relevant off-task evidence found in current resources."
        )
        return WatcherDecision(
            off_task=off_task,
            confidence=0.35 if off_task else 0.2,
            summary=summary,
            relevant_evidence=evidence[:4],
            actor_mode="fallback",
        )

    def _normalize_evidence(self, value):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _extract_sentence(self, text, term):
        for chunk in text.replace("\n", " ").split("."):
            if term in chunk.lower():
                return chunk.strip()[:180]
        return text.strip()[:180]


class MainProcessingActor:
    def __init__(self):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("BIG_BROTHER_MPA_MODEL", DEFAULT_MPA_MODEL)
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, session_goal, watcher_decisions):
        positive_decisions = [decision for decision in watcher_decisions if decision.off_task]
        if not positive_decisions:
            return MPAResult(
                triggered=False,
                should_intervene=False,
                agenda="Waiting for enough consecutive watcher positives.",
                rationale="The watcher has not produced enough consecutive off-task booleans yet.",
                supporting_points=[],
                actor_mode="idle",
            )

        if not self.client:
            return self._fallback(session_goal, positive_decisions)

        evidence_lines = []
        for index, decision in enumerate(positive_decisions, start=1):
            evidence = decision.relevant_evidence or [decision.summary]
            for item in evidence:
                cleaned = str(item).strip()
                if cleaned:
                    evidence_lines.append(f"{index}. {cleaned}")

        prompt = (
            "You are the Main Processing Agent (MPA) in a study-support system.\n"
            "You receive only watcher-approved off-task signals after the watcher has already met "
            "the consecutive-threshold requirement.\n"
            "Your job is to convert those observations into an intervention agenda for a downstream "
            "personality/speaking agent.\n"
            "Be concise, practical, and evidence-grounded. Do not invent facts.\n"
            "Return strict JSON with keys:\n"
            "should_intervene: boolean\n"
            "agenda: string under 220 characters\n"
            "rationale: string under 220 characters\n"
            "supporting_points: array of short strings\n\n"
            f"Study intention:\n{session_goal}\n\n"
            "Watcher-approved evidence:\n"
            f"{chr(10).join(evidence_lines) if evidence_lines else 'None'}"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )

        data = json.loads(response.choices[0].message.content or "{}")
        return MPAResult(
            triggered=True,
            should_intervene=bool(data.get("should_intervene", True)),
            agenda=str(data.get("agenda", "Review the watcher evidence and redirect the user.")),
            rationale=str(data.get("rationale", "Multiple watcher turns indicate likely off-task behavior.")),
            supporting_points=self._normalize_points(data.get("supporting_points")),
            actor_mode=f"llm:{self.model}",
        )

    def _fallback(self, session_goal, positive_decisions):
        supporting_points = []
        for decision in positive_decisions:
            evidence = decision.relevant_evidence or [decision.summary]
            for item in evidence:
                cleaned = str(item).strip()
                if cleaned and cleaned not in supporting_points:
                    supporting_points.append(cleaned)

        agenda = (
            f"Redirect the user back to: {session_goal}. Ask whether the flagged behavior is actually helping that goal."
        )[:220]
        return MPAResult(
            triggered=True,
            should_intervene=True,
            agenda=agenda,
            rationale="The watcher produced consecutive off-task booleans, so escalation is now warranted.",
            supporting_points=supporting_points[:4],
            actor_mode="fallback",
        )

    def _normalize_points(self, value):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []
