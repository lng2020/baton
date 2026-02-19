"""baton-init: interactively create a project from a GitHub repo URL."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parent.parent / "config" / "projects.yaml"
AGENT_YAML_TEMPLATE = Path(__file__).parent.parent / "agent.yaml"
DEFAULT_COLOR = "#0f3460"
BASE_PORT = 9100


def parse_repo(raw: str) -> tuple[str, str, str]:
    """Return (url, owner, repo_name) from a GitHub URL or shorthand."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$", raw)
    if m:
        return raw.rstrip("/"), m.group(1), m.group(2)
    m = re.match(r"^([^/]+)/([^/]+)$", raw)
    if m:
        url = f"https://github.com/{m.group(1)}/{m.group(2)}"
        return url, m.group(1), m.group(2)
    print(f"Error: cannot parse repo URL: {raw}")
    sys.exit(1)


def load_projects_yaml() -> dict:
    """Load projects.yaml, creating it if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        return {"projects": []}
    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    if data is None:
        return {"projects": []}
    if "projects" not in data:
        data["projects"] = []
    return data


def next_available_port(projects: list[dict]) -> int:
    """Find the next available agent port."""
    used_ports: set[int] = set()
    for p in projects:
        url = p.get("agent_url", "")
        m = re.search(r":(\d+)$", url)
        if m:
            used_ports.add(int(m.group(1)))
    port = BASE_PORT
    while port in used_ports:
        port += 1
    return port


def prompt_confirm(message: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(message + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_value(label: str, default: str) -> str:
    display = f" [{default}]" if default else ""
    answer = input(f"{label}{display}: ").strip()
    return answer if answer else default


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="baton-init",
        description="Create a new Baton project from a GitHub repo",
    )
    parser.add_argument("repo", help="GitHub repo URL or user/repo shorthand")
    parser.add_argument("--path", help="Local clone destination")
    parser.add_argument("--name", help="Display name for the project")
    parser.add_argument("--id", dest="project_id", help="Project ID")
    parser.add_argument("--color", default=DEFAULT_COLOR, help="Hex color (default: %(default)s)")
    parser.add_argument("--agent-port", type=int, help="Agent port (default: auto-assign)")
    parser.add_argument("--no-clone", action="store_true", help="Skip cloning if repo already exists locally")

    args = parser.parse_args()

    # 1. Parse repo URL
    repo_url, owner, repo_name = parse_repo(args.repo)
    print(f"Repository: {repo_url} ({owner}/{repo_name})")

    # 2. Determine local path
    local_path = Path(args.path).expanduser() if args.path else Path.home() / "projects" / repo_name

    # 3. Clone the repo
    if args.no_clone:
        print(f"Skipping clone (--no-clone). Using path: {local_path}")
    elif local_path.exists():
        print(f"Warning: {local_path} already exists.")
        if not prompt_confirm("Use existing directory?"):
            print("Aborted.")
            sys.exit(0)
    else:
        if not prompt_confirm(f"Clone to {local_path}?"):
            print("Aborted.")
            sys.exit(0)
        print(f"Cloning {repo_url} ...")
        result = subprocess.run(
            ["git", "clone", repo_url, str(local_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error: git clone failed:\n{result.stderr.strip()}")
            sys.exit(1)
        print("Clone complete.")

    # 4. Determine project ID, name, description
    default_id = args.project_id or repo_name.lower().replace("-", "_")
    default_name = args.name or repo_name.replace("-", " ").replace("_", " ").title()

    project_id = prompt_value("Project ID", default_id)
    project_name = prompt_value("Project name", default_name)
    description = prompt_value("Description", "")

    # 5. Load config and check for duplicates
    data = load_projects_yaml()
    existing_ids = {p["id"] for p in data["projects"]}
    if project_id in existing_ids:
        print(f"Error: project ID '{project_id}' already exists in {CONFIG_PATH}")
        sys.exit(1)

    # 6. Auto-assign agent port
    if args.agent_port:
        port = args.agent_port
    else:
        port = next_available_port(data["projects"])
    agent_url = f"http://localhost:{port}"
    print(f"Agent URL: {agent_url}")

    # 7. Create tasks directory structure
    tasks_dir = local_path / "tasks"
    for status in ("pending", "in_progress", "completed", "failed"):
        (tasks_dir / status).mkdir(parents=True, exist_ok=True)
    print(f"Created tasks directories in {tasks_dir}")

    # 8. Create agent.yaml in project root if it doesn't exist
    project_agent_yaml = local_path / "agent.yaml"
    if not project_agent_yaml.exists() and AGENT_YAML_TEMPLATE.exists():
        shutil.copy2(AGENT_YAML_TEMPLATE, project_agent_yaml)
        print(f"Created {project_agent_yaml} from template")

    # 9. Update config/projects.yaml
    new_entry = {
        "id": project_id,
        "name": project_name,
        "path": str(local_path),
        "repo": repo_url,
        "description": description,
        "color": args.color,
        "tasks_dir": "tasks",
        "agent_url": agent_url,
    }
    data["projects"].append(new_entry)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    print(f"Updated {CONFIG_PATH}")

    # 10. Print summary
    print(f"\nProject '{project_name}' created!")
    print(f"  - Path: {local_path}")
    print(f"  - Agent URL: {agent_url}")
    print(f"\nNext steps:")
    print(f"  cd {local_path} && baton-agent --port {port}")


if __name__ == "__main__":
    main()
