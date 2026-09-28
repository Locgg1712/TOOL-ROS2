from setuptools import setup

package_name = 'ros2_mcp_tools'

setup(
    name=package_name,
    version='0.1.0',
    py_modules=['server', 'cli', 'multirobot_lint'],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools', 'pyyaml'],
    zip_safe=True,
    maintainer='Loc',
    maintainer_email='you@example.com',
    description=(
        'ROS2 graph introspection tools (MCP server + CLI) plus '
        'multirobot_lint, a static scanner for known ROS2 multi-robot '
        'sim-to-real pitfalls, runnable standalone or via colcon test.'
    ),
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            # `ros2mcp <subcommand>` — interactive CLI, needs a sourced ROS2
            # environment for every subcommand except lint-multirobot.
            'ros2mcp = cli:main',
            # `ros2_mcp_server` — MCP stdio server for Claude Desktop/Code.
            'ros2_mcp_server = server:main',
            # `ros2_multirobot_lint <path> [--fix]` — pure static analysis,
            # no ROS2 environment required. Also used by test/ under colcon test.
            'ros2_multirobot_lint = multirobot_lint:main',
        ],
    },
)
