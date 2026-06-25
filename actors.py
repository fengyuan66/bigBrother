import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from agent_core import estimate_text_tokens


ACTOR_TIMEOUT_SECONDS = int(os.getenv("BIG_BROTHER_ACTOR_TIMEOUT_SECONDS", "45"))
ACTOR_RETRIES = int(os.getenv("BIG_BROTHER_ACTOR_RETRIES", "1"))


def _default_base_url() -> str:
    return os.getenv("BIG_BROTHER_BASE_URL", "https://api.openai.com/v1").strip()


def _default_agent_model() -> str:
    return (
        os.getenv("BIG_BROTHER_MPA_MODEL", "").strip()
        or os.getenv("BIG_BROTHER_AGENT_MODEL", "").strip()
        or os.getenv("BIG_BROTHER_MODEL", "").strip()
    )


def _default_personality_model() -> str:
    return os.getenv("BIG_BROTHER_PERSONALITY_MODEL", "").strip()

BROWSER_DISTRACTION_TERMS = {
    "youtube",
    "netflix",
    "tiktok",
    "instagram",
    "discord",
    "twitter",
    "x.com",
    "twitch",
    "reddit",
    "shopping",
    "roblox",
}
VISUAL_DISTRACTION_TERMS = {
    "phone",
    "scrolling",
    "selfie",
    "shopping",
    "tiktok",
    "instagram",
    "discord",
}
NEUTRAL_VIDEO_TERMS = {"video", "lecture", "tutorial", "lesson", "course", "watch", "short", "shorts"}
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "your",
    "have",
    "about",
    "study",
    "studying",
    "work",
    "working",
    "need",
}
STIMULUS_POLICIES = {
    "tab_opened": {
        "procedural_resources": ["browser_rag"],
        "instruction": "A browser tab changed. Refresh browser data first and treat browser evidence as highest priority.",
    },
    "tab_refreshed": {
        "procedural_resources": ["browser_rag"],
        "instruction": "A browser tab changed. Refresh browser data first and treat browser evidence as highest priority.",
    },
    "tab_closed": {
        "procedural_resources": ["browser_rag"],
        "instruction": "A browser tab changed. Refresh browser data first and treat browser evidence as highest priority.",
    },
    "inactivity": {
        "procedural_resources": ["browser_rag", "webcam_scan"],
        "instruction": "Inactivity requires browser context first, then a presence check, then a scheduled recheck.",
    },
    "capture_updated": {
        "procedural_resources": [],
        "instruction": "A requested capture arrived. Re-evaluate it together with the current browser context.",
    },
    "todo_due": {
        "procedural_resources": [],
        "instruction": "A scheduled follow-up is due. Honor the todo kind first, then decide whether more evidence is needed.",
    },
    "manual": {
        "procedural_resources": [],
        "instruction": "Manual turn. Judge the current evidence and fetch more only if it is insufficient.",
    },
    "heartbeat": {
        "procedural_resources": [],
        "instruction": "Heartbeat. Only intervene if the evidence is already sufficient and materially changed.",
    },
}


def _chat_json(client, model, prompt, *, temperature, max_tokens, ledger=None, component=""):
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
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (2**attempt))
    raise last_error


def _keywords(text: str) -> list[str]:
    terms = []
    for part in re.findall(r"[a-z0-9]{3,}", str(text or "").lower()):
        if part in STOPWORDS:
            continue
        if part not in terms:
            terms.append(part)
    return terms


def _parse_browser_export(text: str) -> list[dict]:
    tabs = []
    current = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if match:
            if current:
                tabs.append(current)
            current = {"index": int(match.group(1)), "title": match.group(2).strip(), "url": "", "domain": ""}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("URL:"):
            current["url"] = stripped.split(":", 1)[1].strip()
            if current["url"]:
                current["domain"] = urlparse(current["url"]).netloc.lower()
            continue
        if stripped.startswith("Domain:"):
            current["domain"] = stripped.split(":", 1)[1].strip().lower()
    if current:
        tabs.append(current)

    unique = []
    seen = set()
    for tab in tabs:
        key = (tab.get("title", "").strip().lower(), tab.get("url", "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(tab)
    return unique


def _browser_assessment(session_goal: str, browser_text: str) -> dict:
    goal_text = str(session_goal or "").lower()
    goal_terms = _keywords(goal_text)
    allow_video = any(term in goal_text for term in NEUTRAL_VIDEO_TERMS | {"youtube"})
    tabs = _parse_browser_export(browser_text)

    evidence = []
    has_study_match = False
    distraction_tabs = []
    ambiguous_tabs = []

    for tab in tabs:
        title = str(tab.get("title", "")).strip()
        url = str(tab.get("url", "")).strip()
        domain = str(tab.get("domain", "")).strip().lower()
        haystack = f"{title} {url} {domain}".lower()
        if title or url:
            evidence.append(f"{title or '(untitled)'} - {url or '(no url)'}")

        goal_overlap = any(term in haystack for term in goal_terms)
        if goal_overlap:
            has_study_match = True

        distraction_hit = next((term for term in BROWSER_DISTRACTION_TERMS if term in haystack), "")
        educational_video = distraction_hit == "youtube" and goal_overlap
        if distraction_hit and not (distraction_hit == "youtube" and (allow_video or educational_video)):
            distraction_tabs.append({"tab": tab, "term": distraction_hit})

        generic_title = title.lower() in {"youtube", "new tab", ""}
        if generic_title and url:
            ambiguous_tabs.append(tab)

    if not tabs:
        return {
            "tabs": [],
            "focus_state": "uncertain",
            "summary": "No browser tabs are available yet.",
            "evidence": [],
            "needs_screen": False,
            "distraction_tabs": [],
        }

    if distraction_tabs:
        tab = distraction_tabs[0]["tab"]
        return {
            "tabs": tabs,
            "focus_state": "distracted",
            "summary": f"Browser-first read found a likely distraction tab: {tab.get('title') or tab.get('domain')}.",
            "evidence": evidence[:4],
            "needs_screen": False,
            "distraction_tabs": distraction_tabs,
        }

    if has_study_match:
        return {
            "tabs": tabs,
            "focus_state": "focused",
            "summary": "Browser tabs align with the study goal.",
            "evidence": evidence[:4],
            "needs_screen": False,
            "distraction_tabs": [],
        }

    if ambiguous_tabs:
        first = ambiguous_tabs[0]
        return {
            "tabs": tabs,
            "focus_state": "uncertain",
            "summary": f"Browser tab changed, but {first.get('title') or first.get('domain')} is too generic to judge from the title alone.",
            "evidence": evidence[:4],
            "needs_screen": True,
            "distraction_tabs": [],
        }

    return {
        "tabs": tabs,
        "focus_state": "uncertain",
        "summary": "Browser tabs changed, but the intent is still unclear from titles and URLs alone.",
        "evidence": evidence[:4],
        "needs_screen": False,
        "distraction_tabs": [],
    }


def _visual_assessment(text: str, session_goal: str) -> dict:
    haystack = str(text or "").lower()
    goal_text = str(session_goal or "").lower()
    for term in VISUAL_DISTRACTION_TERMS:
        if term in haystack and term not in goal_text:
            return {
                "focus_state": "distracted",
                "summary": f"Visual evidence mentions {term}.",
                "evidence": [f"Visual summary mentioned {term}."],
            }
    return {
        "focus_state": "uncertain" if haystack else "focused",
        "summary": "No strong distraction signal was found in the fresh visual summaries." if haystack else "No visual summaries are available.",
        "evidence": [],
    }


def _goal_focus_label(session_goal: str) -> str:
    goal = str(session_goal or "").strip()
    lowered = goal.lower()
    prefixes = [
        "i am studying ",
        "i'm studying ",
        "i am working on ",
        "i'm working on ",
        "studying ",
        "working on ",
    ]
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return goal[len(prefix) :].strip() or goal
    return goal


@dataclass
class AgentDecision:
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

    def response_signature(self) -> str:
        basis = "|".join([self.focus_state, self.summary, self.response_text, "|".join(self.evidence[:3])])
        return re.sub(r"\s+", " ", basis.strip().lower())


@dataclass
class PersonalityResult:
    triggered: bool
    should_speak: bool
    spoken_text: str
    delivery_notes: str
    actor_mode: str


class AgentActor:
    def __init__(self, ledger=None):
        self.ledger = ledger
        self.model = _default_agent_model()
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.client = None
        if api_key and self.model and OpenAI:
            self.client = OpenAI(api_key=api_key, base_url=_default_base_url())

    @property
    def enabled(self):
        return True

    def stimulus_policy(self, stimulus_type: str) -> dict:
        stimulus = str(stimulus_type or "").replace("stimulus:", "").strip()
        return dict(STIMULUS_POLICIES.get(stimulus, {}))

    def procedural_resource_requests(self, stimulus_type: str, payload: dict | None = None) -> list[dict]:
        payload = dict(payload or {})
        stimulus = str(stimulus_type or "").replace("stimulus:", "").strip()
        requests = []
        for resource_type in self.stimulus_policy(stimulus).get("procedural_resources", []):
            if resource_type == "browser_rag":
                requests.append(
                    {
                        "type": "browser_rag",
                        "reason": f"Procedural browser refresh for {stimulus}.",
                        "source": "browser",
                        "priority": "high",
                    }
                )
            elif resource_type == "webcam_scan":
                requests.append(
                    {
                        "type": "webcam_scan",
                        "reason": f"Procedural webcam check for {stimulus}.",
                        "source": "webcam",
                        "priority": "medium",
                    }
                )
            elif resource_type == "screen_scan":
                requests.append(
                    {
                        "type": "screen_scan",
                        "reason": f"Procedural screen check for {stimulus}.",
                        "source": "screen",
                        "priority": "high",
                    }
                )
        if stimulus == "todo_due":
            todo = payload.get("todo") or {}
            if str(todo.get("kind", "")).strip().lower() == "inactivity_recheck":
                requests.append(
                    {
                        "type": "browser_rag",
                        "reason": "Scheduled inactivity recheck: refresh browser context.",
                        "source": "browser",
                        "priority": "high",
                    }
                )
                requests.append(
                    {
                        "type": "webcam_scan",
                        "reason": "Scheduled inactivity recheck: confirm presence again.",
                        "source": "webcam",
                        "priority": "medium",
                    }
                )
        return requests

    def _heuristic_decision(
        self,
        session_goal,
        resources,
        *,
        stimulus_type="",
        stimulus_payload=None,
        current_context=None,
        historic_context=None,
    ):
        stimulus = str(stimulus_type or "").replace("stimulus:", "").strip()
        stimulus_payload = dict(stimulus_payload or {})
        current_context = current_context or {}
        historic_context = historic_context or []
        requested_resources = []
        todo_writes = []
        policy = self.stimulus_policy(stimulus)
        notes = [policy["instruction"]] if policy.get("instruction") else []

        browser = _browser_assessment(session_goal, resources.browser_text)
        fresh_visual_text = "\n".join(
            text for name, text in resources.iter_sources(include_stale=False) if name in {"webcam", "screenshare"}
        )
        visual = _visual_assessment(fresh_visual_text, session_goal)

        if stimulus in {"tab_opened", "tab_refreshed", "tab_closed"}:
            focus_label = _goal_focus_label(session_goal)
            if not resources.browser_text:
                requested_resources.extend(self.procedural_resource_requests(stimulus_type, stimulus_payload))
                return AgentDecision(
                    sufficient=False,
                    focus_state="uncertain",
                    summary="A browser tab changed, but no fresh browser export is available yet.",
                    evidence=[],
                    response_required=False,
                    response_text="",
                    requested_resources=requested_resources,
                    todo_writes=[],
                    notes=notes + ["Browser-first judgement is blocked until the tab export refreshes."],
                    actor_mode="heuristic",
                )

            if browser["focus_state"] == "distracted":
                top_title = browser["tabs"][0].get("title", "") if browser.get("tabs") else ""
                return AgentDecision(
                    sufficient=True,
                    focus_state="distracted",
                    summary=browser["summary"],
                    evidence=browser["evidence"],
                    response_required=True,
                    response_text=f"I can see {top_title or 'that tab'}, and it does not look helpful for {focus_label}. Close it or explain why it matters.",
                    requested_resources=[],
                    todo_writes=[],
                    notes=notes + ["Browser title and URL were used as the first-priority source."],
                    actor_mode="browser_first",
                )

            if browser["needs_screen"]:
                requested_resources.append(
                    {
                        "type": "screen_scan",
                        "reason": "Browser title is too generic; verify the visible page before judging.",
                        "source": "screen",
                        "priority": "high",
                    }
                )
                return AgentDecision(
                    sufficient=False,
                    focus_state="uncertain",
                    summary=browser["summary"],
                    evidence=browser["evidence"],
                    response_required=False,
                    response_text="",
                    requested_resources=requested_resources,
                    todo_writes=[],
                    notes=notes + ["Browser-first read was ambiguous, so the next escalation is a screen scan."],
                    actor_mode="browser_first",
                )

            return AgentDecision(
                sufficient=True,
                focus_state=browser["focus_state"],
                summary=browser["summary"],
                evidence=browser["evidence"],
                response_required=False,
                response_text="",
                requested_resources=[],
                todo_writes=[],
                notes=notes + ["Browser-first judgement completed without using VLM."],
                actor_mode="browser_first",
            )

        if stimulus == "inactivity":
            todo_writes.append({"note": "Recheck inactivity state.", "due_in_seconds": 30, "kind": "inactivity_recheck"})
            requested_resources.extend(self.procedural_resource_requests(stimulus_type, stimulus_payload))
            if resources.browser_text and browser["focus_state"] == "focused":
                notes.append("Browser context still looks on-task despite inactivity.")
            return AgentDecision(
                sufficient=False,
                focus_state="inactive",
                summary=(browser["summary"] if resources.browser_text else "Inactivity detected. Browser context needs to be checked first."),
                evidence=browser["evidence"],
                response_required=False,
                response_text="",
                requested_resources=requested_resources,
                todo_writes=todo_writes,
                notes=notes + ["Inactivity does not trigger an intervention by itself."],
                actor_mode="heuristic",
            )

        if stimulus == "todo_due":
            requested_resources.extend(self.procedural_resource_requests(stimulus_type, stimulus_payload or current_context))
            return AgentDecision(
                sufficient=False,
                focus_state="inactive",
                summary="A scheduled follow-up is due.",
                evidence=[],
                response_required=False,
                response_text="",
                requested_resources=requested_resources,
                todo_writes=[],
                notes=notes + ["This is a scheduled recheck ticket."],
                actor_mode="heuristic",
            )

        if browser["focus_state"] == "distracted":
            top_title = browser["tabs"][0].get("title", "") if browser.get("tabs") else "that current tab"
            return AgentDecision(
                sufficient=True,
                focus_state="distracted",
                summary=browser["summary"],
                evidence=browser["evidence"],
                response_required=True,
                response_text=f"Come back to {session_goal}. {top_title} looks unrelated right now.",
                requested_resources=[],
                todo_writes=[],
                notes=notes + ["Browser evidence remained the strongest signal."],
                actor_mode="heuristic",
            )

        if visual["focus_state"] == "distracted":
            return AgentDecision(
                sufficient=True,
                focus_state="distracted",
                summary=visual["summary"],
                evidence=visual["evidence"],
                response_required=True,
                response_text=f"Pause the distraction and return to {session_goal}.",
                requested_resources=[],
                todo_writes=[],
                notes=notes + ["Fresh visual evidence triggered the response."],
                actor_mode="heuristic",
            )

        if browser["focus_state"] == "uncertain" and not resources.browser_text:
            requested_resources.append(
                {
                    "type": "browser_rag",
                    "reason": "Refresh browser tab info before judging.",
                    "source": "browser",
                    "priority": "high",
                }
            )

        return AgentDecision(
            sufficient=not requested_resources,
            focus_state=browser["focus_state"],
            summary=browser["summary"],
            evidence=browser["evidence"],
            response_required=False,
            response_text="",
            requested_resources=requested_resources,
            todo_writes=[],
            notes=notes + ["No intervention is needed from the current evidence."],
            actor_mode="heuristic",
        )

    def _normalize_focus_state(self, value: str) -> str:
        state = str(value or "").strip().lower()
        if state in {"focused", "distracted", "uncertain", "inactive"}:
            return state
        return "uncertain"

    def _normalize_requested_resources(self, items) -> list[dict]:
        allowed_types = {"browser_rag", "screen_scan", "webcam_scan"}
        normalized = []
        seen = set()
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            request_type = str(raw.get("type", "")).strip().lower()
            if request_type in {"browser", "browser_scan"}:
                request_type = "browser_rag"
            elif request_type in {"screen"}:
                request_type = "screen_scan"
            elif request_type in {"webcam"}:
                request_type = "webcam_scan"
            if request_type not in allowed_types:
                continue
            reason = str(raw.get("reason", "")).strip()
            source = str(raw.get("source", "")).strip().lower()
            if not source:
                source = request_type.replace("_scan", "").replace("_rag", "")
            priority = str(raw.get("priority", "")).strip().lower() or ("high" if request_type != "webcam_scan" else "medium")
            dedupe = (request_type, source, reason)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            normalized.append(
                {
                    "type": request_type,
                    "reason": reason,
                    "source": source,
                    "priority": priority,
                }
            )
        return normalized

    def _normalize_todo_writes(self, items) -> list[dict]:
        normalized = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            note = str(raw.get("note", "")).strip()
            if not note:
                continue
            due_in_seconds = raw.get("due_in_seconds", 0)
            try:
                due_in_seconds = max(0.0, float(due_in_seconds or 0))
            except (TypeError, ValueError):
                due_in_seconds = 0.0
            kind = str(raw.get("kind", "scheduled")).strip() or "scheduled"
            normalized.append({"note": note, "due_in_seconds": due_in_seconds, "kind": kind})
        return normalized

    def _build_llm_prompt(
        self,
        session_goal,
        resources,
        *,
        stimulus,
        stimulus_payload,
        current_context,
        historic_context,
    ) -> str:
        policy = self.stimulus_policy(stimulus)
        browser = _browser_assessment(session_goal, resources.browser_text)
        fresh_visual_text = "\n".join(
            text for name, text in resources.iter_sources(include_stale=False) if name in {"webcam", "screenshare"}
        )
        visual = _visual_assessment(fresh_visual_text, session_goal)
        payload_text = json.dumps(stimulus_payload or {}, ensure_ascii=True, indent=2)
        current_text = json.dumps(current_context or {}, ensure_ascii=True, indent=2)
        history_text = json.dumps(historic_context or [], ensure_ascii=True, indent=2)
        policy_text = json.dumps(policy or {}, ensure_ascii=True, indent=2)
        browser_hint = json.dumps(browser, ensure_ascii=True, indent=2)
        visual_hint = json.dumps(visual, ensure_ascii=True, indent=2)
        return (
            "You are the MPA AI LLM ACTOR inside Big Brother.\n"
            "Your job is to judge whether the current evidence is sufficient before deciding anything else.\n"
            "If evidence is insufficient, request the minimum next resource(s) needed.\n"
            "Stimulus instructions are higher priority than your own interpretation.\n"
            "For browser tab changes, browser reading is the first-priority source and VLM should not be requested unless browser evidence is ambiguous.\n"
            "If evidence is sufficient, make a judgement. That judgement may include a spoken response, context notes, todo writes, or no intervention.\n"
            "Be careful with generic browser titles like 'New tab' or 'YouTube'. Do not overreact unless the evidence truly supports it.\n"
            "Write natural response_text. Do not mechanically repeat the full study-goal sentence if a shorter natural phrase is better.\n"
            "Return strict JSON with keys:\n"
            "sufficient, focus_state, summary, evidence, response_required, response_text, requested_resources, todo_writes, notes.\n"
            "Allowed focus_state values: focused, distracted, uncertain, inactive.\n"
            "Allowed requested resource types: browser_rag, screen_scan, webcam_scan.\n"
            "requested_resources must be empty when sufficient is true.\n"
            "If response_required is false, response_text should be empty.\n\n"
            f"Study goal:\n{session_goal}\n\n"
            f"Stimulus type:\n{stimulus}\n\n"
            f"Stimulus payload:\n{payload_text}\n\n"
            f"Stimulus policy:\n{policy_text}\n\n"
            f"Current context:\n{current_text}\n\n"
            f"Historic context:\n{history_text}\n\n"
            f"Fresh resources:\n{resources.as_prompt_text()}\n\n"
            f"Browser-side structured hint:\n{browser_hint}\n\n"
            f"Visual-side structured hint:\n{visual_hint}\n"
        )

    def evaluate(
        self,
        session_goal,
        resources,
        *,
        stimulus_type="",
        stimulus_payload=None,
        current_context=None,
        historic_context=None,
    ):
        stimulus = str(stimulus_type or "").replace("stimulus:", "").strip()
        stimulus_payload = dict(stimulus_payload or {})
        current_context = current_context or {}
        historic_context = historic_context or []

        if not self.client or not self.model:
            return self._heuristic_decision(
                session_goal,
                resources,
                stimulus_type=stimulus_type,
                stimulus_payload=stimulus_payload,
                current_context=current_context,
                historic_context=historic_context,
            )

        if stimulus in {"tab_opened", "tab_refreshed", "tab_closed"}:
            return self._heuristic_decision(
                session_goal,
                resources,
                stimulus_type=stimulus_type,
                stimulus_payload=stimulus_payload,
                current_context=current_context,
                historic_context=historic_context,
            )

        policy = self.stimulus_policy(stimulus)
        notes = [policy["instruction"]] if policy.get("instruction") else []
        prompt = self._build_llm_prompt(
            session_goal,
            resources,
            stimulus=stimulus,
            stimulus_payload=stimulus_payload,
            current_context=current_context,
            historic_context=historic_context,
        )
        data = _chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0.2,
            max_tokens=600,
            ledger=self.ledger,
            component="mpa",
        )

        requested_resources = self._normalize_requested_resources(data.get("requested_resources"))
        procedural_requests = self._normalize_requested_resources(
            self.procedural_resource_requests(stimulus_type, stimulus_payload or current_context)
        )
        todo_writes = self._normalize_todo_writes(data.get("todo_writes"))

        sufficient = bool(data.get("sufficient", False))
        if procedural_requests and not sufficient:
            merged = []
            seen = set()
            for item in [*procedural_requests, *requested_resources]:
                dedupe = (item.get("type"), item.get("source"), item.get("reason"))
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                merged.append(item)
            requested_resources = merged

        if stimulus == "inactivity":
            if not any(str(item.get("kind", "")).strip().lower() == "inactivity_recheck" for item in todo_writes):
                todo_writes.append({"note": "Recheck inactivity state.", "due_in_seconds": 30.0, "kind": "inactivity_recheck"})

        if sufficient:
            requested_resources = []

        response_required = bool(data.get("response_required", False))
        response_text = str(data.get("response_text", "")).strip()
        if not response_required:
            response_text = ""

        summary = str(data.get("summary", "")).strip() or "The actor evaluated the latest evidence."
        evidence = [str(item).strip() for item in (data.get("evidence") or []) if str(item).strip()][:8]
        extra_notes = [str(item).strip() for item in (data.get("notes") or []) if str(item).strip()]
        focus_state = self._normalize_focus_state(data.get("focus_state", "uncertain"))

        return AgentDecision(
            sufficient=sufficient,
            focus_state=focus_state,
            summary=summary,
            evidence=evidence,
            response_required=response_required,
            response_text=response_text,
            requested_resources=requested_resources,
            todo_writes=todo_writes,
            notes=notes + extra_notes,
            actor_mode=f"llm:{self.model}",
        )


class PersonalityActor:
    def __init__(self, ledger=None):
        self.ledger = ledger
        self.model = _default_personality_model()
        api_key = os.getenv("BIG_BROTHER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.client = None
        if api_key and self.model and OpenAI:
            self.client = OpenAI(api_key=api_key, base_url=_default_base_url())

    @property
    def enabled(self):
        return self.client is not None

    def evaluate(self, session_goal: str, decision: AgentDecision):
        if not decision.response_required:
            return PersonalityResult(
                triggered=False,
                should_speak=False,
                spoken_text="No spoken response is needed.",
                delivery_notes="Idle.",
                actor_mode="idle",
            )

        if not self.client:
            return PersonalityResult(
                triggered=True,
                should_speak=True,
                spoken_text=decision.response_text[:320],
                delivery_notes="Direct, short, and calm.",
                actor_mode="fallback",
            )

        prompt = (
            "Rewrite the intervention into one short spoken line.\n"
            "Be direct, warm, and concise. Stay grounded in the evidence.\n"
            "Return strict JSON with keys should_speak, spoken_text, delivery_notes.\n\n"
            f"Study goal:\n{session_goal}\n\n"
            f"Decision summary:\n{decision.summary}\n\n"
            f"Evidence:\n{chr(10).join('- ' + item for item in decision.evidence) or '- None'}\n\n"
            f"Draft response:\n{decision.response_text}"
        )
        data = _chat_json(
            self.client,
            self.model,
            prompt,
            temperature=0.5,
            max_tokens=180,
            ledger=self.ledger,
            component="personality",
        )
        return PersonalityResult(
            triggered=True,
            should_speak=bool(data.get("should_speak", True)),
            spoken_text=str(data.get("spoken_text", decision.response_text)).strip()[:320],
            delivery_notes=str(data.get("delivery_notes", "Direct and calm.")).strip()[:160],
            actor_mode=f"llm:{self.model}",
        )
