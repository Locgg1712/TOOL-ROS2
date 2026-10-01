import glob

from setuptools import setup

package_name = 'ros2_mcp_tools'

setup(
    name=package_name,
    version='0.2.0',
    py_modules=['server', 'cli', 'multirobot_lint', 'ros2_infra'],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        # manifests must ship with the install, otherwise server.py (which looks next
        # to its own file) cannot find them after `colcon build`
        ('share/' + package_name + '/ros2_manifests', glob.glob('ros2_manifests/*.yaml')),
    ],
    install_requires=['setuptools', 'pyyaml'],
    zip_safe=True,
    maintainer='Loc',
    maintainer_email='you@example.com',
    description=(
        'ROS2 graph introspection tools (MCP server + CLI) plus multirobot_lint, '
        'a static scanner for known ROS2 multi-robot sim-to-real pitfalls.'
    ),
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'ros2mcp = cli:main',
            'ros2_mcp_server = server:main',
            'ros2_multirobot_lint = multirobot_lint:main',
        ],
    },
)
