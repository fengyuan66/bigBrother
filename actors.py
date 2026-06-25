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
DEFAULT_AGENT_MODEL = (
    os.getenv("BIG_BROTHER_AGENT_MODEL")
    or os.getenv("BIG_BROTHER_MPA_MODEL")
    or os.getenv("BIG_BROTHER_WATCHER_MODEL")
    or "generic-light-llm"
)
DEFAULT_PERSONALITY_MODEL = os.getenv("BIG_BROTHER_PERSONALITY_MODEL", DEFAULT_AGENT_MODEL)
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
class PersonalityResult:
    triggered: bool
    should_speak: bool
    spoken_text: str
    delivery_notes: str
    actor_mode: str


@dataclass
class AgentPlan:
    sufficient: bool
    focus_state: str
    summary: str
    evidence: list[str]
    response_required: bool
    response_text: str
    requested_resources: list[dict]
    todo_writes: list[dict]
    notes: list[str]
    actor_mode: str

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

    def evaluate(self, session_goal, plan_result, evidence_items):
        if not plan_result.triggered or not plan_result.should_intervene:
            return PersonalityResult(
                triggered=False,
                should_speak=False,
                spoken_text="Response actor is waiting for an intervention plan.",
                delivery_notes="No spoken intervention is needed yet.",
                actor_mode="idle",
            )

        if not self.client:
            return self._fallback(session_goal, plan_result, evidence_items)

        evidence_lines = []
        for decision in evidence_items:
            evidence = decision.relevant_evidence or [decision.summary]
            evidence_lines.extend(
                cleaned
                for cleaned in (str(item).strip() for item in evidence)
                if cleaned
            )

        prompt = (
            "You are the Personality actor in a study-support system.\n"
            f"Voice brief:\n{self.personality_brief}\n\n"
            "You receive an agent plan and must turn it into the exact short line that should be "
            "spoken to the user.\n"
            "Voice: direct, clear, and human. Say exactly what you observe from the agenda, "
            "then tell the user exactly what to do next to return to focus. Use short, natural sentences.\n"
            "Stay grounded in the agenda and evidence. Do not invent facts. Do not mention internal "
            "actors, thresholds, or system architecture. Sound natural when read aloud.\n"
            "Return strict JSON with keys:\n"
            "should_speak: boolean\n"
            "spoken_text: string under 320 characters\n"
            "delivery_notes: string under 160 characters\n\n"
            f"Study intention:\n{session_goal}\n\n"
            f"Plan agenda:\n{plan_result.agenda}\n\n"
            f"Plan rationale:\n{plan_result.rationale}\n\n"
            "Supporting points:\n"
            f"{chr(10).join('- ' + item for item in plan_result.supporting_points) or '- None'}\n\n"
            "Evidence:\n"
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

    def _fallback(self, session_goal, plan_result, evidence_items):
        supporting_points = []
        for decision in evidence_items:
            for item in decision.relevant_evidence or [decision.summary]:
                cleaned = str(item).strip()
                if cleaned and cleaned not in supporting_points:
                    supporting_points.append(cleaned)

        spoken_text = (
            f"Pause for a second. You said you're working on {session_goal}. "
            f"{plan_result.agenda}"
        )
        return PersonalityResult(
            triggered=True,
            should_speak=True,
            spoken_text=spoken_text[:320],
            delivery_notes="Warm, firm, and conversational.",
            actor_mode="fallback",
        )


class AgentActor:
    def __init__(self, ledger=None):
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("BIG_BROTHER_BASE_URL", DEFAULT_BASE_URL)
        self.model = DEFAULT_AGENT_MODEL
        self.ledger = ledger
        self.client = (
            OpenAI(api_key=api_key, base_url=self.base_url) if api_key and OpenAI else None
        )

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(
        self,
        session_goal,
        resources,
        *,
        stimulus_type="",
        current_context=None,
        historic_context=None,
    ):
        current_context = current_context or {}
        historic_context = historic_context or []
        if not self.client:
            return self._fallback(
                session_goal,
                resources,
                stimulus_type=stimulus_type,
                current_context=current_context,
            )

        prompt = (
            "You are the main agent in a study-support system.\n"
            "A stimulus happened. Your job is to decide if the current evidence is sufficient, "
            "whether the user needs a response now, and what targeted resources should be fetched next.\n"
            "Prefer cheap evidence first. Request extra scans only when needed.\n"
            "Resource policy:\n"
            "- For tab_opened/tab_refreshed/tab_closed: analyze browser logs first. Treat browser title + URL as first-priority evidence. "
            "Do not request a VLM scan unless the browser read is still genuinely ambiguous.\n"
            "- For inactivity: do not assume distraction. First inspect browser/tab context; if still inactive, "
            "schedule a 30-second recheck and a webcam scan.\n"
            "- Only ask for a response when the evidence is already sufficient. If more evidence is needed, keep response_required false.\n"
            "- Never leave summary blank. Summarize what changed in one sentence.\n"
            "- Keep context grounded in the current evidence and short recent history.\n"
            "Return strict JSON with keys:\n"
            "sufficient: boolean\n"
            "focus_state: string (focused, distracted, inactive, uncertain)\n"
            "summary: short string under 220 characters\n"
            "evidence: array of short strings\n"
            "response_required: boolean\n"
            "response_text: short string under 260 characters\n"
            "requested_resources: array of objects with keys type, reason, source, due_in_seconds(optional), priority(optional)\n"
            "todo_writes: array of objects with keys note, due_in_seconds, kind\n"
            "notes: array of short strings\n\n"
            f"Study goal:\n{session_goal}\n\n"
            f"Stimulus:\n{stimulus_type or 'unknown'}\n\n"
            f"Current context:\n{truncate_for_prompt(json.dumps(current_context, ensure_ascii=False), 1200)}\n\n"
            f"Historic context:\n{truncate_for_prompt(json.dumps(historic_context[-5:], ensure_ascii=False), 1200)}\n\n"
            f"Available resources:\n{truncate_for_prompt(resources.as_prompt_text())}"
        )
        data = chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0,
            max_tokens=360,
            ledger=self.ledger,
            component="agent",
        )
        plan = AgentPlan(
            sufficient=bool(data.get("sufficient", True)),
            focus_state=str(data.get("focus_state", "uncertain")).strip() or "uncertain",
            summary=str(data.get("summary", "")).strip(),
            evidence=self._normalize_strings(data.get("evidence")),
            response_required=bool(data.get("response_required", False)),
            response_text=str(data.get("response_text", "")).strip(),
            requested_resources=self._normalize_actions(data.get("requested_resources")),
            todo_writes=self._normalize_actions(data.get("todo_writes")),
            notes=self._normalize_strings(data.get("notes")),
            actor_mode=f"llm:{self.model}",
        )
        return self._validate_plan(plan, resources, stimulus_type=stimulus_type)

    def _fallback(self, session_goal, resources, *, stimulus_type="", current_context=None):
        current_context = current_context or {}
        resource_text = resources.as_prompt_text().lower()
        evidence = []
        focus_state = "focused"
        response_required = False
        response_text = ""
        requested_resources = []
        todo_writes = []
        notes = []

        for term in DISTRACTION_TERMS:
            if term in resource_text and term not in session_goal.lower():
                evidence.append(f"Current resources mention {term}.")
                focus_state = "distracted"
                response_required = True
                response_text = f"Come back to {session_goal}. If {term} is not helping that task, close it and refocus."
                break

        if stimulus_type in {"stimulus:tab_opened", "stimulus:tab_refreshed", "stimulus:tab_closed"}:
            requested_resources.append(
                {"type": "browser_rag", "reason": "Refresh page context for the changed tab.", "source": "browser", "priority": "high"}
            )
            if not resources.browser_text:
                requested_resources.append(
                    {"type": "screen_scan", "reason": "Need the visible screen to disambiguate the browser change.", "source": "screen", "priority": "high"}
                )

        if stimulus_type == "stimulus:inactivity":
            focus_state = "inactive"
            notes.append("User appears inactive; browser context may still be relevant.")
            todo_writes.append({"note": "Re-check inactivity state.", "due_in_seconds": 30, "kind": "inactivity_recheck"})
            requested_resources.append(
                {"type": "webcam_scan", "reason": "Check whether the user is still physically present.", "source": "webcam", "due_in_seconds": 30, "priority": "medium"}
            )

        return AgentPlan(
            sufficient=not requested_resources,
            focus_state=focus_state,
            summary=(
                "Potential distraction found in the current resources."
                if response_required
                else "No immediate intervention. The agent may need more context."
            ),
            evidence=evidence,
            response_required=response_required,
            response_text=response_text,
            requested_resources=requested_resources,
            todo_writes=todo_writes,
            notes=notes,
            actor_mode="fallback",
        )

    def _normalize_strings(self, value):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _normalize_actions(self, value):
        if not isinstance(value, list):
            return []
        cleaned = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if not str(item.get("type", "")).strip() and not str(item.get("note", "")).strip():
                continue
            cleaned.append(dict(item))
        return cleaned

    def _validate_plan(self, plan: AgentPlan, resources, *, stimulus_type: str = ""):
        requested_resources = [dict(item) for item in plan.requested_resources]
        summary = plan.summary.strip()
        evidence = list(plan.evidence)
        focus_state = plan.focus_state if plan.focus_state in {"focused", "distracted", "inactive", "uncertain"} else "uncertain"

        if not summary:
            if evidence:
                summary = evidence[0]
            elif stimulus_type.startswith("stimulus:tab_") and resources.browser_text:
                first_line = next((line.strip() for line in resources.browser_text.splitlines() if line.strip().startswith("1.")), "")
                summary = f"Browser state changed. {first_line}" if first_line else "Browser state changed."
            else:
                summary = "Agent reviewed the current evidence."

        normalized_requests = []
        for item in requested_resources:
            request_type = str(item.get("type", "")).strip().lower()
            if request_type in {"screen", "screen_scan"}:
                item["type"] = "screen_scan"
            elif request_type in {"webcam", "webcam_scan"}:
                item["type"] = "webcam_scan"
            elif request_type in {"browser", "browser_scan", "browser_rag"}:
                item["type"] = "browser_rag"
            normalized_requests.append(item)

        if stimulus_type.startswith("stimulus:tab_") and resources.browser_text:
            normalized_requests = [item for item in normalized_requests if item.get("type") != "screen_scan" or not plan.sufficient]

        response_required = bool(plan.response_required)
        response_text = plan.response_text.strip()
        if not plan.sufficient:
            response_required = False
            response_text = ""

        return AgentPlan(
            sufficient=bool(plan.sufficient),
            focus_state=focus_state,
            summary=summary[:220],
            evidence=evidence,
            response_required=response_required,
            response_text=response_text[:260],
            requested_resources=normalized_requests,
            todo_writes=list(plan.todo_writes),
            notes=list(plan.notes),
            actor_mode=plan.actor_mode,
        )
