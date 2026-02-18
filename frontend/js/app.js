// Home page — project grid
(function () {
    const grid = document.getElementById("project-grid");
    const statsBar = document.getElementById("stats-bar");

    async function loadProjects() {
        try {
            const res = await fetch("/api/projects");
            if (!res.ok) throw new Error(res.statusText);
            const projects = await res.json();
            renderStats(projects);
            renderGrid(projects);
        } catch (err) {
            grid.innerHTML = '<div class="empty-state">Failed to load projects.</div>';
            console.error(err);
        }
    }

    function renderStats(projects) {
        let total = 0, pending = 0, inProgress = 0, completed = 0, failed = 0;
        for (const p of projects) {
            const c = p.task_counts;
            pending += c.pending || 0;
            inProgress += c.in_progress || 0;
            completed += c.completed || 0;
            failed += c.failed || 0;
        }
        total = pending + inProgress + completed + failed;
        statsBar.innerHTML = `
            <div class="stat-item"><strong>${projects.length}</strong> projects</div>
            <div class="stat-item"><strong>${total}</strong> total tasks</div>
            <div class="stat-item" style="color:var(--pending)"><strong>${pending}</strong> pending</div>
            <div class="stat-item" style="color:var(--in-progress)"><strong>${inProgress}</strong> in progress</div>
            <div class="stat-item" style="color:var(--completed)"><strong>${completed}</strong> completed</div>
            <div class="stat-item" style="color:var(--failed)"><strong>${failed}</strong> failed</div>
        `;
    }

    function renderGrid(projects) {
        if (!projects.length) {
            grid.innerHTML = '<div class="empty-state">No projects configured.</div>';
            return;
        }
        grid.innerHTML = projects.map(p => {
            const c = p.task_counts;
            const counts = ["pending", "in_progress", "completed", "failed"]
                .filter(s => (c[s] || 0) > 0)
                .map(s => `<span class="task-count ${s}">${c[s]} ${s.replace("_", " ")}</span>`)
                .join("");
            const healthClass = p.healthy ? "healthy" : "unhealthy";
            return `
                <div class="project-card" style="border-left-color:${p.color}" onclick="location.href='/project/${p.id}'">
                    <div class="project-card-header">
                        <h3>${escHtml(p.name)}</h3>
                        <div class="project-card-badges">
                            <span class="health-dot ${healthClass}" title="${healthClass}"></span>
                        </div>
                    </div>
                    <p>${escHtml(p.description)}</p>
                    <div class="task-counts">${counts || '<span style="color:var(--text-muted);font-size:0.8rem">No tasks</span>'}</div>
                </div>
            `;
        }).join("");
    }

    function escHtml(s) {
        const d = document.createElement("div");
        d.textContent = s;
        return d.innerHTML;
    }

    loadProjects();
    setInterval(loadProjects, 30000);
})();
