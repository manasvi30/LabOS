#!/bin/bash
# LabOS — Quick Start
# Usage: bash start.sh

echo "🔬 Starting LabOS..."

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required. Please install Python 3.10+"
    exit 1
fi

# Install dependencies
echo "📦 Installing dependencies..."
pip install fastapi uvicorn paramiko httpx -q

# Start server
echo "🚀 LabOS is starting on http://localhost:8000"
cd src && python3 api_server.py
