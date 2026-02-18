FROM ubuntu:24.04

# Base tools
RUN apt-get update && apt-get install -y \
    git curl wget tmux vim \
    python3 python3-pip python3-venv \
    nodejs npm \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Working directory
WORKDIR /workspace

# Copy project files
COPY . /workspace/

# Install Python dependencies
RUN pip3 install --break-system-packages pyyaml fastapi uvicorn jinja2 aiofiles

# Entry point: start manager
CMD ["python3", "manager/task_dispatcher.py"]
