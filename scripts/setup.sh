#!/bin/bash
set -e

cd "$(dirname "$0")/.."

echo "=== Creating virtual environment ==="
python3 -m venv .venv

source .venv/bin/activate

echo "=== Installing dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Copying .env if not exists ==="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example - please edit it with your actual credentials"
fi

echo ""
echo "Setup complete!"
echo "1. Edit .env with your Feishu app credentials"
echo "2. source .venv/bin/activate"
echo "3. uvicorn app.main:app --reload --port 8080"
