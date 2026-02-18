// Kanban board rendering
(function () {
    const projectId = window.BATON_PROJECT_ID;
    const statuses = ["pending", "in_progress", "completed", "failed"];

    async function loadTasks() {
        try {
            const res = await fetch(`/api/projects/${projectId}/tasks`);
            if (!res.ok) throw new Error(res.statusText);
            const tasks = await res.json();
            renderTasks(tasks);
        } catch (err) {
            console.error("Failed to load tasks:", err);
        }
    }

    function renderTasks(tasks) {
        for (const status of statuses) {
            const list = document.querySelector(`[data-list="${status}"]`);
            const count = document.querySelector(`[data-count="${status}"]`);
            const items = tasks[status] || [];
            count.textContent = items.length;
            if (!items.length) {
                list.innerHTML = '<div class="empty-state">No tasks</div>';
                continue;
            }
            list.innerHTML = items.map(t => {
                const modified = new Date(t.modified).toLocaleDateString();
                const errorBadge = t.has_error_log ? '<span class="error-badge">error log</span>' : '';
                return `
                    <div class="task-card" onclick="openTaskDetail('${status}', '${escAttr(t.filename)}')">
                        <h4>${escHtml(t.title)}</h4>
                        <div class="task-meta">${escHtml(t.id)} &middot; ${modified}</div>
                        ${errorBadge}
                    </div>
                `;
            }).join("");
        }
    }

    async function loadWorktrees() {
        try {
            const res = await fetch(`/api/projects/${projectId}/worktrees`);
            if (!res.ok) return;
            const worktrees = await res.json();
            const el = document.getElementById("worktrees-content");
            if (!worktrees.length) {
                el.innerHTML = '<div class="empty-state">No worktrees</div>';
                return;
            }
            el.innerHTML = worktrees.map(w => `
                <div class="info-item">
                    <strong>${escHtml(w.branch || "(detached)")}</strong><br>
                    <code>${escHtml(w.commit ? w.commit.substring(0, 8) : "")}</code>
                    &middot; ${escHtml(w.path)}
                    ${w.is_bare ? " (bare)" : ""}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load worktrees:", err);
        }
    }

    async function loadCommits() {
        try {
            const res = await fetch(`/api/projects/${projectId}/commits`);
            if (!res.ok) return;
            const commits = await res.json();
            const el = document.getElementById("commits-content");
            if (!commits.length) {
                el.innerHTML = '<div class="empty-state">No commits</div>';
                return;
            }
            el.innerHTML = commits.map(c => `
                <div class="info-item">
                    <code>${escHtml(c.sha.substring(0, 8))}</code>
                    <strong>${escHtml(c.message)}</strong><br>
                    ${escHtml(c.author)} &middot; ${escHtml(c.date)}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load commits:", err);
        }
    }

    function escHtml(s) {
        const d = document.createElement("div");
        d.textContent = s || "";
        return d.innerHTML;
    }

    function escAttr(s) {
        return s.replace(/'/g, "\\'").replace(/"/g, "&quot;");
    }

    loadTasks();
    loadWorktrees();
    loadCommits();
    setInterval(loadTasks, 15000);
})();

function togglePanel(header) {
    header.classList.toggle("open");
    const content = header.nextElementSibling;
    content.classList.toggle("open");
}
