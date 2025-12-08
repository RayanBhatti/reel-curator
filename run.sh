#!/bin/bash

# Reel Curator - Run Script
# This starts the Flask backend which also serves the frontend

cd "$(dirname "$0")"

echo "🎬 Starting Reel Curator..."
echo "   Open http://localhost:5000 in your browser"
echo ""

python backend/app.py
