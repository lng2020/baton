from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DispatcherConfig:
    enabled: bool = False
    command: str = ""


@dataclass
class ProjectConfig:
    id: str
    name: str
    path: str
    repo: str = ""
    description: str = ""
    color: str = "#0f3460"
    tasks_dir: str = "tasks"
    agent_url: str = ""
    dispatcher: DispatcherConfig | None = None

    @property
    def project_path(self) -> Path:
        return Path(self.path)

    @property
    def tasks_path(self) -> Path:
        return self.project_path / self.tasks_dir


@dataclass
class BatonConfig:
    projects: list[ProjectConfig] = field(default_factory=list)


_config: BatonConfig | None = None
_config_path: Path | None = None
_config_mtime: float = 0.0


def load_config(path: str | Path | None = None) -> BatonConfig:
    global _config, _config_path, _config_mtime
    if path is None:
        path = Path(__file__).parent.parent / "config" / "projects.yaml"
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)
    projects = []
    for p in raw.get("projects", []):
        dispatcher_raw = p.pop("dispatcher", None)
        cfg = ProjectConfig(**p)
        if dispatcher_raw and isinstance(dispatcher_raw, dict):
            cfg.dispatcher = DispatcherConfig(**dispatcher_raw)
        projects.append(cfg)
    _config = BatonConfig(projects=projects)
    _config_path = path
    _config_mtime = path.stat().st_mtime
    return _config


def get_config() -> BatonConfig:
    if _config is None:
        return load_config()
    if _config_path is not None:
        try:
            current_mtime = _config_path.stat().st_mtime
            if current_mtime != _config_mtime:
                return load_config(_config_path)
        except OSError:
            pass
    return _config


def get_project_by_id(project_id: str) -> ProjectConfig | None:
    for p in get_config().projects:
        if p.id == project_id:
            return p
    return None
