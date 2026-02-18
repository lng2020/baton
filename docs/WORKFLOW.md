# Workflow Documentation

## Daily Usage Flow

```
1. Generate an idea
   |
2. Open Web Manager on phone (or SSH)
   |
3. Voice/text input the task description
   |
4. Task enters tasks/pending/
   |
5. Task Dispatcher auto-assigns to available worktree
   |
6. Claude Code executes (Plan -> Code -> Test -> Commit)
   |
7. Auto-merge to main branch
   |
8. Review results on GitHub
```

## Plan Mode Flow

```
1. Task enters pending
   |
2. Dispatcher launches Claude Code in Plan Mode
   |
3. Plan results shown in Web Manager
   |
4. Human review (Approve / Modify / Reject)
   |
5. Approved plans execute automatically
```

## Task File Format

Each task is a Markdown file in `tasks/pending/`. Example:

```markdown
# Implement User Authentication

## Requirements
- JWT-based authentication
- Login and registration endpoints
- Password hashing with bcrypt
- Token refresh mechanism

## Acceptance Criteria
- All endpoints return proper HTTP status codes
- Tests cover happy path and error cases
- API documentation updated
```

## Git Worktree Parallelization

Multiple Claude Code instances run in parallel using Git worktrees:

1. **Create**: `bash scripts/worktree_manager.sh create <task-id>`
2. **Work**: Claude Code develops in the isolated worktree
3. **Merge**: `bash scripts/worktree_manager.sh merge <task-id>`
4. **Cleanup**: `bash scripts/worktree_manager.sh cleanup <task-id>`

## Error Handling

When a task fails:
1. The task file is moved to `tasks/failed/`
2. An error log is created alongside it (`<task-id>.error.log`)
3. The worktree is cleaned up
4. You can retry the task via the Web Manager or manually move it back to `tasks/pending/`

## Experience Accumulation

After each task, Claude Code updates `PROGRESS.md` with:
- Lessons learned during the task
- Known issues discovered
- Architecture decisions made

This file serves as long-term memory across sessions, preventing repeated mistakes.
