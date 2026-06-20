import json
import os
import time
from dataclasses import dataclass

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from agent_core import estimate_text_tokens


DEFAULT_BASE_URL = os.getenv("BIG_BROTHER_BASE_URL", "https://api.openai.com/v1")
MAX_RESOURCE_PROMPT_CHARS = int(os.getenv("BIG_BROTHER_MAX_RESOURCE_CHARS", "4000"))
ACTOR_TIMEOUT_SECONDS = int(os.getenv("BIG_BROTHER_ACTOR_TIMEOUT_SECONDS", "60"))
ACTOR_RETRIES = int(os.getenv("BIG_BROTHER_ACTOR_RETRIES", "2"))


def truncate_for_prompt(text: str, limit: int = MAX_RESOURCE_PROMPT_CHARS) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 22)] + "\n...[truncated]..."


def chat_json(client, model, prompt, *, temperature, max_tokens, ledger=None, component=""):
    """Single JSON-mode chat call with retries, timeout, and ledger accounting."""
    last_error = None
    for attempt in range(max(1, ACTOR_RETRIES + 1)):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=ACTOR_TIMEOUT_SECONDS,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            if ledger is not None:
                usage = getattr(response, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or estimate_text_tokens(prompt)
                completion_tokens = getattr(usage, "completion_tokens", 0) or estimate_text_tokens(content)
                ledger.record_call(component or model, prompt_tokens, completion_tokens)
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
        except Exception as exc:
            last_error = exc
            time.sleep(0.5 * (2**attempt))
    raise last_error
DEFAULT_WATCHER_MODEL = os.getenv("BIG_BROTHER_WATCHER_MODEL", "generic-light-llm")
DEFAULT_MPA_MODEL = os.getenv("BIG_BROTHER_MPA_MODEL", DEFAULT_WATCHER_MODEL)
DEFAULT_PERSONALITY_MODEL = os.getenv("BIG_BROTHER_PERSONALITY_MODEL", DEFAULT_MPA_MODEL)
DISTRACTION_TERMS = [
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
    "roblox",
    "x.com",
    "twitter",
    "twitch",
]


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


@dataclass
class PersonalityResult:
    triggered: bool
    should_speak: bool
    spoken_text: str
    delivery_notes: str
    actor_mode: str


class WatcherActor:
    def __init__(self, ledger=None):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("BIG_BROTHER_WATCHER_MODEL", DEFAULT_WATCHER_MODEL)
        self.ledger = ledger
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, session_goal, resources):
        if not resources.iter_sources():
            return WatcherDecision(
                off_task=False,
                confidence=0.0,
                summary="No fresh resource text available for the watcher.",
                relevant_evidence=[],
                actor_mode="no-fresh-resources",
            )

        if not self.client:
            return self._fallback(session_goal, resources)

        prompt = (
            "You are the Watcher actor in a study-support agent system.\n"
            "Your job is to review evidence from available resources and decide whether there is "
            "relevant information suggesting the user is off-task.\n"
            "Treat the supplied resources as the complete current world state. "
            "Do not carry facts over from earlier turns. "
            "If a distraction is not present in the current resources, do not mention it.\n"
            "Only include evidence that matters to the study intention. Ignore decorative or "
            "identity details unless they are directly relevant.\n"
            "Be matter-of-fact, concise, and non-judgmental.\n"
            "Return strict JSON with keys:\n"
            "off_task: boolean\n"
            "confidence: number from 0 to 1\n"
            "summary: string under 180 characters\n"
            "relevant_evidence: array of strings, each under 180 characters\n\n"
            f"Study intention:\n{session_goal}\n\n"
            f"Available resources:\n{truncate_for_prompt(resources.as_prompt_text())}"
        )

        data = chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0,
            max_tokens=220,
            ledger=self.ledger,
            component="watcher",
        )
        decision = WatcherDecision(
            off_task=bool(data.get("off_task")),
            confidence=float(data.get("confidence", 0.5)),
            summary=str(data.get("summary", "No summary provided.")),
            relevant_evidence=self._normalize_evidence(data.get("relevant_evidence")),
            actor_mode=f"llm:{self.model}",
        )
        return self._validate_grounding(decision, resources)

    def _fallback(self, session_goal, resources):
        evidence = []
        goal_lower = session_goal.lower()
        for source_name, text in resources.iter_sources():
            lowered = text.lower()
            for term in DISTRACTION_TERMS:
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

    def _validate_grounding(self, decision, resources):
        if not decision.off_task:
            return decision

        resource_text = "\n".join(text for _, text in resources.iter_sources()).lower()
        if not resource_text.strip():
            return WatcherDecision(
                off_task=False,
                confidence=0.0,
                summary="No fresh resource text available for the watcher.",
                relevant_evidence=[],
                actor_mode=f"{decision.actor_mode}:ungrounded",
            )

        claimed_text = " ".join([decision.summary, *decision.relevant_evidence]).lower()
        claimed_terms = [term for term in DISTRACTION_TERMS if term in claimed_text]

        if claimed_terms and not any(term in resource_text for term in claimed_terms):
            return WatcherDecision(
                off_task=False,
                confidence=0.0,
                summary="Watcher rejected an ungrounded off-task claim from the current resources.",
                relevant_evidence=[],
                actor_mode=f"{decision.actor_mode}:ungrounded",
            )

        return decision


class MainProcessingActor:
    def __init__(self, ledger=None):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("BIG_BROTHER_MPA_MODEL", DEFAULT_MPA_MODEL)
        self.ledger = ledger
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

        data = chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0,
            max_tokens=280,
            ledger=self.ledger,
            component="mpa",
        )
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


class PersonalityActor:
    def __init__(self, ledger=None):
        self.ledger = ledger
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = os.getenv("BIG_BROTHER_PERSONALITY_MODEL", DEFAULT_PERSONALITY_MODEL)
        self.personality_brief = os.getenv(
            "BIG_BROTHER_PERSONALITY_BRIEF",
            (
                "You are Big Brother's final speaking voice. Sound direct, warm, slightly assertive, "
                "and human. Keep the user focused without being cruel, preachy, or robotic. Prefer "
                "one short spoken message that feels natural out loud."
            ),
        )
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, session_goal, mpa_result, watcher_decisions):
        if not mpa_result.triggered or not mpa_result.should_intervene:
            return PersonalityResult(
                triggered=False,
                should_speak=False,
                spoken_text="Personality actor is waiting for an intervention agenda.",
                delivery_notes="No spoken intervention is needed yet.",
                actor_mode="idle",
            )

        if not self.client:
            return self._fallback(session_goal, mpa_result, watcher_decisions)

        evidence_lines = []
        for decision in watcher_decisions:
            evidence = decision.relevant_evidence or [decision.summary]
            evidence_lines.extend(
                cleaned
                for cleaned in (str(item).strip() for item in evidence)
                if cleaned
            )

        prompt = (
            "You are the Personality actor in a study-support system.\n"
            f"Voice brief:\n{self.personality_brief}\n\n"
            "You receive an MPA agenda and must turn it into the exact short line that should be "
            "spoken to the user.\n"
            "Voice: blunt, alien, and zero-bullshit. Say exactly what you observe from the agenda, "
            "then command the user exactly what to do next to return to focus. Use short, direct sentences. "
            "Every spoken line must include a playful alien threat to shoot Earth with laser beams if the user "
            "does not refocus. Keep it absurd.\n"
            "Stay grounded in the agenda and evidence. Do not invent facts. Do not mention internal "
            "actors, thresholds, or system architecture. Sound natural when read aloud.\n"
            "Return strict JSON with keys:\n"
            "should_speak: boolean\n"
            "spoken_text: string under 320 characters\n"
            "delivery_notes: string under 160 characters\n\n"
            f"Study intention:\n{session_goal}\n\n"
            f"MPA agenda:\n{mpa_result.agenda}\n\n"
            f"MPA rationale:\n{mpa_result.rationale}\n\n"
            "Supporting points:\n"
            f"{chr(10).join('- ' + item for item in mpa_result.supporting_points) or '- None'}\n\n"
            "Watcher evidence:\n"
            f"{chr(10).join('- ' + item for item in evidence_lines) or '- None'}"
        )

        data = chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0.6,
            max_tokens=240,
            ledger=self.ledger,
            component="personality",
        )
        return PersonalityResult(
            triggered=True,
            should_speak=bool(data.get("should_speak", True)),
            spoken_text=str(
                data.get(
                    "spoken_text",
                    "Come back to the study task and tell me if the current distraction is actually helping.",
                )
            ),
            delivery_notes=str(
                data.get(
                    "delivery_notes",
                    "Short, calm, and firm.",
                )
            ),
            actor_mode=f"llm:{self.model}",
        )

    def _fallback(self, session_goal, mpa_result, watcher_decisions):
        supporting_points = []
        for decision in watcher_decisions:
            for item in decision.relevant_evidence or [decision.summary]:
                cleaned = str(item).strip()
                if cleaned and cleaned not in supporting_points:
                    supporting_points.append(cleaned)

        spoken_text = (
            f"Pause for a second. You said you're working on {session_goal}. "
            f"{mpa_result.agenda}"
        )
        return PersonalityResult(
            triggered=True,
            should_speak=True,
            spoken_text=spoken_text[:320],
            delivery_notes="Warm, firm, and conversational.",
            actor_mode="fallback",
        )
