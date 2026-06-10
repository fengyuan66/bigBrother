import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SOURCES_DIR = APP_DIR / "sources"
SUMMARIES_DIR = APP_DIR / "summaries"

DEFAULT_WEBCAM_PATH = SOURCES_DIR / "webcam" / "latest.txt"
DEFAULT_SCREENSHARE_PATH = SOURCES_DIR / "video" / "latest.txt"
DEFAULT_BROWSER_PATH = SOURCES_DIR / "browser" / "tabs.txt"


@dataclass
class StudyResources:
    webcam_text: str
    screenshare_text: str
    browser_text: str
    missing_sources: list[str]
    source_metadata: dict[str, dict]

    def iter_sources(self, include_stale=False):
        pairs = [
            ("webcam", self.webcam_text),
            ("screenshare", self.screenshare_text),
            ("browser", self.browser_text),
        ]
        visible_pairs = []
        for name, text in pairs:
            if not text:
                continue
            metadata = self.source_metadata.get(name, {})
            if metadata.get("stale") and not include_stale:
                continue
            visible_pairs.append((name, text))
        return visible_pairs

    def describe_source(self, source_name):
        metadata = self.source_metadata.get(source_name, {})
        text = {
            "webcam": self.webcam_text,
            "screenshare": self.screenshare_text,
            "browser": self.browser_text,
        }.get(source_name, "")

        if not text:
            return ""

        if metadata.get("stale"):
            age_seconds = metadata.get("age_seconds")
            if age_seconds is not None:
                age_label = f"{age_seconds:.1f}s old"
            else:
                age_label = "age unknown"
            return f"[STALE: {age_label}]\n{text}"

        return text

    def as_prompt_text(self):
        sections = []
        for source_name, text in self.iter_sources():
            sections.append(f"[{source_name}]\n{text.strip()}")

        stale_sources = [
            name
            for name, metadata in self.source_metadata.items()
            if metadata.get("stale")
        ]
        if stale_sources:
            sections.append(f"[stale_sources]\n{', '.join(stale_sources)}")

        if self.missing_sources:
            sections.append(f"[missing_sources]\n{', '.join(self.missing_sources)}")

        if sections:
            return "\n\n".join(sections)
        return "[resources]\nNo fresh resource text available."


class ResourceLoader:
    def __init__(self):
        self.webcam_candidates = self._build_candidates(
            env_var="BIG_BROTHER_WEBCAM_PATH",
            default_path=DEFAULT_WEBCAM_PATH,
            fallbacks=[
                SOURCES_DIR / "webcam" / "summary.txt",
                SOURCES_DIR / "webcam" / "latest.json",
                SUMMARIES_DIR / "webcam_summary.json",
            ],
        )
        self.screenshare_candidates = self._build_candidates(
            env_var="BIG_BROTHER_SCREENSHARE_PATH",
            default_path=DEFAULT_SCREENSHARE_PATH,
            fallbacks=[
                SOURCES_DIR / "screenshare" / "latest.txt",
                SOURCES_DIR / "video" / "latest.txt",
                SOURCES_DIR / "video" / "summary.txt",
                SOURCES_DIR / "video" / "latest.json",
                SUMMARIES_DIR / "screen_summary.json",
                SUMMARIES_DIR / "screenshare_summary.json",
            ],
        )
        self.browser_candidates = self._build_candidates(
            env_var="BIG_BROTHER_BROWSER_PATH",
            default_path=DEFAULT_BROWSER_PATH,
            fallbacks=[
                SOURCES_DIR / "browser" / "index.json",
                SOURCES_DIR / "browser" / "browser_live.txt",
                SUMMARIES_DIR / "browser_summary.json",
                APP_DIR / "browser_live.txt",
                APP_DIR / "tabs.txt",
            ],
        )

    def load(self, max_age_seconds=None):
        webcam_text, webcam_missing, webcam_metadata = self._read_optional(
            self.webcam_candidates,
            "webcam",
            max_age_seconds=max_age_seconds,
        )
        screenshare_text, screenshare_missing, screenshare_metadata = self._read_optional(
            self.screenshare_candidates,
            "screenshare",
            max_age_seconds=max_age_seconds,
        )
        browser_text, browser_missing, browser_metadata = self._read_optional(
            self.browser_candidates,
            "browser",
            max_age_seconds=max_age_seconds,
        )

        missing_sources = []
        for name in [webcam_missing, screenshare_missing, browser_missing]:
            if name:
                missing_sources.append(name)

        return StudyResources(
            webcam_text=webcam_text,
            screenshare_text=screenshare_text,
            browser_text=browser_text,
            missing_sources=missing_sources,
            source_metadata={
                "webcam": webcam_metadata,
                "screenshare": screenshare_metadata,
                "browser": browser_metadata,
            },
        )

    def describe_paths(self):
        return {
            "webcam": str(self._resolve_primary_path(self.webcam_candidates)),
            "screenshare": str(self._resolve_primary_path(self.screenshare_candidates)),
            "browser": str(self._resolve_primary_path(self.browser_candidates)),
        }

    def validate_paths(self):
        return {
            "webcam": self._path_status(self._resolve_primary_path(self.webcam_candidates)),
            "screenshare": self._path_status(self._resolve_primary_path(self.screenshare_candidates)),
            "browser": self._path_status(self._resolve_primary_path(self.browser_candidates)),
        }

    def _read_optional(self, candidates, source_name, max_age_seconds=None):
        path = self._resolve_primary_path(candidates)
        metadata = self._build_metadata(path, max_age_seconds=max_age_seconds)
        if not path.exists():
            return "", source_name, metadata

        try:
            text = self._read_resource_text(path)
        except OSError:
            return "", source_name, metadata

        if not text:
            return "", source_name, metadata
        return text, "", metadata

    def _build_candidates(self, env_var, default_path, fallbacks):
        configured = os.getenv(env_var)
        if configured:
            return [Path(configured)]

        unique_candidates = []
        seen = set()
        for candidate in [default_path, *fallbacks]:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)
        return unique_candidates

    def _resolve_primary_path(self, candidates):
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _read_resource_text(self, path):
        raw_text = path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return ""
        if path.suffix.lower() != ".json":
            return raw_text

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text

        if not isinstance(data, dict):
            return raw_text

        summary = str(data.get("summary", "")).strip()
        if summary:
            return summary

        tab_count = data.get("tab_count")
        browser = str(data.get("browser", "")).strip()
        top_domains = data.get("top_domains")
        if isinstance(top_domains, list):
            top_domains_text = ", ".join(str(item).strip() for item in top_domains if str(item).strip())
        else:
            top_domains_text = ""

        if tab_count is not None:
            browser_label = browser or "browser"
            domain_label = top_domains_text or "none"
            return f"{tab_count} open tab(s) in {browser_label}. Top domains: {domain_label}."

        return raw_text

    def _path_status(self, path):
        if not path.exists():
            return {"path": str(path), "exists": False, "bytes": 0}
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {"path": str(path), "exists": True, "bytes": size}

    def _build_metadata(self, path, max_age_seconds=None):
        metadata = {
            "path": str(path),
            "exists": path.exists(),
            "age_seconds": None,
            "stale": False,
        }
        if not path.exists():
            return metadata
        try:
            age_seconds = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return metadata
        metadata["age_seconds"] = age_seconds
        if max_age_seconds is not None and age_seconds > max_age_seconds:
            metadata["stale"] = True
        return metadata
