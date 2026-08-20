---
name: ros2-mcp
description: >-
  Inspect, debug, and interact with a live ROS2 system. Use when the user asks
  about running nodes, topics, services, or messages in their ROS2 environment.
  Supports listing nodes/topics/services, echoing live messages, calling
  services, validating node manifests against runtime state, diagnosing
  communication errors (QoS mismatch, wrong namespace, missing publisher), and
  generating manifest YAML for undocumented nodes. Works on localhost via shell
  commands. For remote machines the user must SSH and run commands manually.
---

# ROS2 MCP — Live System Inspector & Debugger

## Overview

This skill gives you direct visibility into a running ROS2 system. It uses the
`scripts/ros2_cli.py` helper to introspect the graph, capture real messages, call
services, diff manifest declarations against runtime state, and reason over the
results to diagnose problems.

> [!IMPORTANT]
> **ROS2 must be sourced before running any command.** Always prefix with
> `source /opt/ros/<distro>/setup.bash &&` or confirm the user has already done so.
> If `rclpy` cannot be imported, the script will print a clear error and exit.

> [!NOTE]
> **Manifest directory**: By default `./ros2_manifests/` relative to wherever you
> run the script. Override with `ROS2_MCP_MANIFEST_DIR=/path/to/dir`.

---

## Quick Start

```bash
# List all running nodes
python3 scripts/ros2_cli.py nodes --output /tmp/nodes.json

# List topics
python3 scripts/ros2_cli.py topics --output /tmp/topics.json

# Echo 3 messages from a topic
python3 scripts/ros2_cli.py echo /chatter std_msgs/msg/String --count 3 --output /tmp/echo.json

# Validate a node against its manifest
python3 scripts/ros2_cli.py validate talker --output /tmp/validate.json

# Auto-generate a manifest YAML for a node
python3 scripts/ros2_cli.py generate-manifest talker --output /tmp/talker_manifest.yaml
```

---

## Utility Script

All operations use **one script** with subcommands:

```bash
python3 scripts/ros2_cli.py <subcommand> [args] --output <file>
```

### 1. `nodes` — List running nodes

```bash
python3 scripts/ros2_cli.py nodes --output /tmp/nodes.json
```

Output: `[{"name": "talker", "namespace": "/", "full": "/talker"}, ...]`

---

### 2. `topics` — List active topics

```bash
python3 scripts/ros2_cli.py topics --output /tmp/topics.json
```

Output: `[{"topic": "/chatter", "types": ["std_msgs/msg/String"]}, ...]`

---

### 3. `topic-info` — Publisher/subscriber counts for a topic

```bash
python3 scripts/ros2_cli.py topic-info /cmd_vel --output /tmp/info.json
```

Output: `{"topic": "/cmd_vel", "publisher_count": 1, "subscriber_count": 1, "publisher_nodes": [...], "subscriber_nodes": [...]}`

---

### 4. `node-info` — Publishers, subscribers, services of a node

```bash
python3 scripts/ros2_cli.py node-info talker --output /tmp/node.json
# With namespace:
python3 scripts/ros2_cli.py node-info talker --namespace /my_ns --output /tmp/node.json
```

---

### 5. `services` — List active services

```bash
python3 scripts/ros2_cli.py services --output /tmp/services.json
```

---

### 6. `echo` — Capture live messages from a topic

```bash
python3 scripts/ros2_cli.py echo /chatter std_msgs/msg/String --count 5 --timeout 10 --output /tmp/echo.json
```

- `--count N`: number of messages to capture (required)
- `--timeout SEC`: max wait in seconds (default 5.0)

Output: `{"topic": "...", "captured_count": 5, "timed_out": false, "messages": [...]}`

> [!TIP]
> If `timed_out: true` and `captured_count: 0`, the topic is likely not publishing.
> Check `topic-info` for publisher count. If 0 publishers → node hasn't started or
> crashed. If publisher exists but no messages → check QoS mismatch (see Diagnosis).

---

### 7. `rosout` — Capture recent log messages

```bash
python3 scripts/ros2_cli.py rosout --count 20 --timeout 5 --output /tmp/logs.json
```

Output same structure as `echo`, messages are `rcl_interfaces/msg/Log`.

> [!NOTE]
> `/rosout` uses `TRANSIENT_LOCAL` durability in ROS2 Humble+. The script
> automatically matches this QoS so you receive buffered log history.

---

### 8. `call-service` — Call a service

```bash
python3 scripts/ros2_cli.py call-service /add_two_ints \
  example_interfaces/srv/AddTwoInts \
  --fields '{"a": 3, "b": 5}' \
  --timeout 5 \
  --output /tmp/response.json
```

- `--fields JSON`: request payload (default `{}`)
- `--timeout SEC`: how long to wait for the service

---

### 9. `list-manifests` — Show which nodes have manifest files

```bash
python3 scripts/ros2_cli.py list-manifests --output /tmp/manifests.json
```

Output: `{"manifest_dir": "...", "nodes": ["talker", "listener"]}`

---

### 10. `get-manifest` — Read a node's declared design intent

```bash
python3 scripts/ros2_cli.py get-manifest talker --output /tmp/manifest.json
```

---

### 11. `validate` — Diff manifest vs runtime state

```bash
python3 scripts/ros2_cli.py validate talker --output /tmp/validate.json
# With namespace:
python3 scripts/ros2_cli.py validate talker --namespace /my_ns --output /tmp/validate.json
```

Output:
```json
{
  "node": "talker",
  "publishes": {"missing_in_runtime": [], "undeclared_in_manifest": ["/extra_topic"]},
  "subscribes": {"missing_in_runtime": ["/cmd"], "undeclared_in_manifest": []},
  "services": {"missing_in_runtime": [], "undeclared_in_manifest": []}
}
```

> [!IMPORTANT]
> `missing_in_runtime` = declared but not running → likely bug or uninitialized publisher.
> `undeclared_in_manifest` = running but not documented → update the manifest.

---

### 12. `generate-manifest` — Auto-generate a manifest YAML from live state

```bash
python3 scripts/ros2_cli.py generate-manifest talker --output /tmp/talker_manifest.yaml
```

Generates a ready-to-commit YAML in `MANIFEST_SCHEMA.md` format, with all live
publishers/subscribers/services pre-filled. User should review and add
`description` fields before committing.

---

## Workflow

Follow this sequence for every debugging request:

### Step 1 — Confirm system is running
```bash
python3 scripts/ros2_cli.py nodes --output /tmp/nodes.json
```
Read the file. If empty → tell user no nodes found, do not proceed.

### Step 2 — Check manifest (if available)
```bash
python3 scripts/ros2_cli.py list-manifests --output /tmp/manifests.json
```
If the relevant node has a manifest → run `validate` immediately. Discrepancies
in `missing_in_runtime` are the most common root cause of bugs.

### Step 3 — Inspect live graph
```bash
python3 scripts/ros2_cli.py node-info <node> --output /tmp/node.json
python3 scripts/ros2_cli.py topic-info <topic> --output /tmp/topic.json
```

### Step 4 — Read actual messages
```bash
python3 scripts/ros2_cli.py echo <topic> <type> --count 3 --output /tmp/echo.json
```
This is the ground truth. Never conclude without reading real messages.

### Step 5 — Check logs
```bash
python3 scripts/ros2_cli.py rosout --count 30 --output /tmp/logs.json
```
Look for ERROR/WARN entries. Match timestamps to when the symptom appeared.

### Step 6 — Diagnose and advise
Based on collected data, apply the rules in **Common Failure Patterns** below.
For code/launch fixes: provide the exact change. For config fixes: show the diff.
Always re-run steps 3–5 after the user applies a fix to confirm resolution.

---

## Common Failure Patterns

| Symptom | Likely Cause | How to Confirm | Fix |
|---|---|---|---|
| `publisher_count: 0` on a topic | Node not started or crashed | `rosout` for ERROR | Check launch file, re-run node |
| `subscriber_count: 0` | Wrong topic name / namespace | Compare `node-info` pub vs sub names | Fix topic name in code |
| `captured_count: 0`, publisher exists | QoS mismatch | Check both sides' QoS in code | Match `reliability` + `durability` |
| `missing_in_runtime` in `validate` | Publisher/sub not initialized | `rosout` for init errors | Check constructor, conditional init |
| Echo shows stale timestamp | Publisher running but blocked | `rosout` for WARN | Check callback blocking, timer period |
| Service call times out | Service not running | `services` list | Ensure service node is running |

---

## Generating a Manifest for a New Node

When the user has a node with no manifest:

1. Run `generate-manifest <node>` to create a draft YAML.
2. Show the output to the user and ask them to fill in `description` fields.
3. Save the reviewed file to `ros2_manifests/<node>.yaml`.
4. Run `validate <node>` to confirm the manifest matches runtime.

---

## Common Mistakes

1. **Forgetting to source ROS2**: Always check if `rclpy` is importable. If the
   script fails with `ModuleNotFoundError: No module named 'rclpy'`, the user
   needs to `source /opt/ros/<distro>/setup.bash` first.

2. **Wrong namespace**: `/talker` and `talker` are different in ROS2. If
   `node-info` fails, try `--namespace /` explicitly or check `nodes` output.

3. **Concluding without data**: Never say "looks fine" without having read actual
   messages from `echo`. The topic existing does not mean data is flowing.
