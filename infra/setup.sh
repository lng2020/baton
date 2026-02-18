#!/bin/bash
set -e

echo "Initializing Agentic Coding environment..."

# 1. Check dependencies
command -v git >/dev/null || { echo "Error: git is required"; exit 1; }
command -v claude >/dev/null || { echo "Error: claude-code CLI is required"; exit 1; }

# 2. Initialize Git (if not already a repo)
if [ ! -d ".git" ]; then
    git init
    git checkout -b main
fi

# 3. Create task directories
mkdir -p tasks/{pending,in_progress,completed,failed}
mkdir -p worktrees

# 4. Initialize PROGRESS.md if it doesn't exist
if [ ! -f "PROGRESS.md" ]; then
    cat > PROGRESS.md << 'EOF'
# PROGRESS.md — Experience Log

## Lessons Learned

## Known Issues

## Architecture Decision Records
EOF
fi

# 5. Set up cron backup (if database backup is enabled)
if [ -n "$DB_BACKUP_ENABLED" ]; then
    (crontab -l 2>/dev/null; echo "0 * * * * /workspace/infra/backup/db_backup_cron.sh") | crontab -
    echo "Database hourly backup enabled"
fi

# 6. Initial commit (if no commits exist)
if ! git rev-parse HEAD >/dev/null 2>&1; then
    git add -A
    git commit -m "chore: scaffold repo initialized"
fi

echo "Initialization complete!"
echo "  - Edit CLAUDE.md to add project information"
echo "  - Add task files to tasks/pending/"
echo "  - Run: python3 manager/task_dispatcher.py"
