"""App settings: env vars (ASST_*) take precedence over config/settings.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.json"
DEFAULT_PATHS_DIR = REPO_ROOT / "paths"
DEFAULT_WORKSPACE = Path(os.getenv("ASST_WORKSPACE", "/tmp/assistant"))
REPO_MAP_PATH = REPO_ROOT / "config" / "repo_map.json"


@dataclass
class Settings:
    mock: bool = False
    admin_password: str = "change-me"
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_account_id: str = ""          # optional; else resolved from /myself
    jira_devinfo_field: str = ""
    github_repo: str = ""
    github_repos: list = field(default_factory=list)
    github_project_map: dict = field(default_factory=dict)
    repo_map: dict = field(default_factory=dict)
    github_token: str = ""
    github_host: str = "github.com"
    github_ssh_url: str = ""
    model_triage: str = ""
    model_action: str = ""
    jql_queries: list = field(default_factory=list)
    scan_jira: bool = True
    scan_github_issues: bool = False
    github_issue_repos: list = field(default_factory=list)
    schedule_enabled: bool = False
    schedule_hour: int = 2
    schedule_minute: int = 0
    max_tickets_per_run: int = 20
    workspace: Path = DEFAULT_WORKSPACE
    settings_path: Path = DEFAULT_SETTINGS_PATH
    paths_dir: Path = DEFAULT_PATHS_DIR
    db_path: Path = REPO_ROOT / "assistant.db"

    @classmethod
    def load(cls, settings_path: Path | None = None) -> "Settings":
        path = Path(settings_path or os.getenv("ASST_SETTINGS", DEFAULT_SETTINGS_PATH))
        s = cls()
        if path.exists():
            data = json.loads(path.read_text())
            s.jql_queries = data.get("jql_queries", [])
            sources = data.get("sources", {}) or {}
            s.scan_jira = bool((sources.get("jira", {}) or {}).get("enabled", s.scan_jira))
            github_issues = sources.get("github_issues", {}) or {}
            s.scan_github_issues = bool(github_issues.get("enabled", s.scan_github_issues))
            s.github_issue_repos = list(github_issues.get("repos", s.github_issue_repos) or [])
            sch = data.get("schedule", {})
            s.schedule_enabled = bool(sch.get("enabled", False))
            s.schedule_hour = int(sch.get("hour", 2))
            s.schedule_minute = int(sch.get("minute", 0))
            s.jira_base_url = data.get("jira", {}).get("base_url", s.jira_base_url)
            s.jira_email = data.get("jira", {}).get("email", s.jira_email)
            s.jira_account_id = data.get("jira", {}).get("account_id", s.jira_account_id)
            s.jira_devinfo_field = data.get("jira", {}).get("devinfo_field", s.jira_devinfo_field)
            s.github_repo = data.get("github", {}).get("repo", s.github_repo)
            repos = list(data.get("github", {}).get("repos", [])) or None
            if not repos and s.github_repo:
                repos = [s.github_repo]
            s.github_repos = repos or []
            s.github_project_map = dict(data.get("github", {}).get("project_repo_map", {}) or {})
            s.repo_map = dict(data.get("github", {}).get("repo_map", {}) or {})
            s.github_host = data.get("github", {}).get("host", s.github_host)
            s.github_ssh_url = data.get("github", {}).get("ssh_url", s.github_ssh_url)
            s.model_triage = data.get("models", {}).get("triage", s.model_triage)
            s.model_action = data.get("models", {}).get("action", s.model_action)
            s.max_tickets_per_run = int(data.get("run", {}).get("max_tickets_per_run", s.max_tickets_per_run))
        s.settings_path = path
        s.db_path = Path(os.getenv("ASST_DB_PATH", s.db_path))
        s.workspace = Path(os.getenv("ASST_WORKSPACE", s.workspace))
        s.workspace.mkdir(parents=True, exist_ok=True)
        s.mock = _to_bool(os.getenv("ASST_MOCK", "1" if s.mock else "0"))
        s.admin_password = os.getenv("ASST_ADMIN_PASSWORD", "mock-assistant" if s.mock else s.admin_password)
        s.jira_base_url = os.getenv("ASST_JIRA_BASE_URL", s.jira_base_url).rstrip("/")
        s.jira_email = os.getenv("ASST_JIRA_EMAIL", s.jira_email)
        s.jira_api_token = os.getenv("ASST_JIRA_API_TOKEN", "")
        s.jira_account_id = os.getenv("ASST_JIRA_ACCOUNT_ID", s.jira_account_id)
        s.jira_devinfo_field = os.getenv("ASST_JIRA_DEVINFO_FIELD", s.jira_devinfo_field)
        s.github_repo = os.getenv("ASST_GITHUB_REPO", s.github_repo)
        s.github_token = os.getenv("ASST_GITHUB_TOKEN", "")
        s.github_host = os.getenv("ASST_GITHUB_HOST", s.github_host)
        s.github_ssh_url = os.getenv("ASST_GITHUB_SSH_URL", s.github_ssh_url)
        env_repos = [r.strip() for r in os.getenv("ASST_GITHUB_REPOS", "").split(",") if r.strip()]
        if env_repos:
            s.github_repos = env_repos
        env_issue_repos = [r.strip() for r in os.getenv("ASST_GITHUB_ISSUE_REPOS", "").split(",") if r.strip()]
        if env_issue_repos:
            s.github_issue_repos = env_issue_repos
            s.scan_github_issues = True
        if "ASST_SCAN_JIRA" in os.environ:
            s.scan_jira = _to_bool(os.getenv("ASST_SCAN_JIRA"))
        if "ASST_SCAN_GITHUB_ISSUES" in os.environ:
            s.scan_github_issues = _to_bool(os.getenv("ASST_SCAN_GITHUB_ISSUES"))
        env_map = {}
        for pair in os.getenv("ASST_GITHUB_PROJECT_MAP", "").split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() and v.strip():
                    env_map[k.strip()] = v.strip()
        if env_map:
            s.github_project_map = env_map
        if not s.github_repos and s.github_repo:
            s.github_repos = [s.github_repo]
        if not s.github_repo and s.github_repos:
            s.github_repo = s.github_repos[0]
        if s.mock:
            s.github_issue_repos = ["demo/mock-repo"]
        if REPO_MAP_PATH.exists():
            try:
                s.repo_map = dict(json.loads(REPO_MAP_PATH.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        s.model_triage = os.getenv("ASST_OP_MODEL_TRIAGE", s.model_triage)
        s.model_action = os.getenv("ASST_OP_MODEL_ACTION", s.model_action)
        return s

    def token_hint(self) -> str:
        """Which important secrets are still missing (for console warnings)."""
        missing = []
        if not self.mock:
            if not (self.jira_base_url and self.jira_api_token):
                missing.append("Jira base URL / API token")
            if not self.github_token:
                missing.append("GitHub token")
        return ", ".join(missing) if missing else ""


def _to_bool(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


settings = Settings.load()
