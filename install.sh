#!/bin/bash
# Install script for ros2mcp CLI on Ubuntu

# Get directory of the script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Make files executable
chmod +x "$DIR/cli.py"
chmod +x "$DIR/server.py"

echo "Installing ros2mcp CLI to /usr/local/bin/ros2mcp..."
sudo ln -sf "$DIR/cli.py" /usr/local/bin/ros2mcp

if [ $? -eq 0 ]; then
    echo "✓ Installation successful! You can now run 'ros2mcp' from any terminal."
    echo "  Try running: ros2mcp --help"
else
    echo "✗ Installation failed. Please ensure you have sudo privileges."
    exit 1
fi
