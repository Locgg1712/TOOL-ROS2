#!/bin/bash
# Install script for ros2mcp CLI on Ubuntu

set -e

# Get directory of the script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Make files executable
chmod +x "$DIR/cli.py"
chmod +x "$DIR/server.py"

# Install Python dependencies (mcp[cli], pyyaml).
# rclpy itself comes from your sourced ROS2 environment, not pip.
echo "Installing Python dependencies from requirements.txt ..."
if command -v pip3 &>/dev/null; then
    pip3 install -r "$DIR/requirements.txt"
elif command -v pip &>/dev/null; then
    pip install -r "$DIR/requirements.txt"
else
    echo "⚠ pip not found — skipping Python dependency installation."
    echo "  Run 'pip install -r requirements.txt' manually before using this tool."
fi

echo "Installing ros2mcp CLI to /usr/local/bin/ros2mcp..."
sudo ln -sf "$DIR/cli.py" /usr/local/bin/ros2mcp

if [ $? -eq 0 ]; then
    echo "✓ Installation successful! You can now run 'ros2mcp' from any terminal."
    echo "  Try running: ros2mcp --help"
else
    echo "✗ Installation failed. Please ensure you have sudo privileges."
    exit 1
fi
