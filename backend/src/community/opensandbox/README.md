# OpenSandbox Provider Configuration Example

This example shows how to configure DeerFlow to use OpenSandbox as the sandbox backend.

## Basic Configuration

Add this to your `config.yaml`:

```yaml
sandbox:
  use: src.community.opensandbox:OpenSandboxProvider
  image: sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter
  entrypoint: ["/opt/opensandbox/code-interpreter.sh"]
  workspace_host_path: /path/to/sandboxes  # Host path for persistent workspace
  workspace_container_path: /workspace      # Container mount path (default: /workspace)
  python_version: "3.12"
  environment:
    CUSTOM_VAR: value
```

## Full Example

```yaml
# Model configuration (required)
models:
  - name: gpt-4
    display_name: GPT-4
    use: langchain_openai:ChatOpenAI
    model: gpt-4
    api_key: $OPENAI_API_KEY

# Sandbox configuration with OpenSandbox
sandbox:
  use: src.community.opensandbox:OpenSandboxProvider
  image: sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter
  workspace_host_path: ./sandboxes
  python_version: "3.12"

# Tools configuration
tools:
  - name: web_search
    group: web
    use: src.community.tavily.tools:web_search_tool

  - name: bash
    group: bash
    use: src.sandbox.tools:bash_tool

  - name: read_file
    group: file:read
    use: src.sandbox.tools:read_file_tool

  - name: write_file
    group: file:write
    use: src.sandbox.tools:write_file_tool

# Skills configuration
skills:
  container_path: /mnt/skills
```

## Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `use` | Provider class path | Required |
| `image` | Docker image for sandbox | `sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter` |
| `entrypoint` | Container entrypoint | `["/opt/opensandbox/code-interpreter.sh"]` |
| `workspace_host_path` | Host path for persistent storage | `./sandboxes` |
| `workspace_container_path` | Container mount path | `/workspace` |
| `python_version` | Python version in sandbox | `"3.12"` |
| `environment` | Extra environment variables | `{}` |
| `mounts` | Additional volume mounts | `[]` |

## Volume Mounts

You can mount additional directories:

```yaml
sandbox:
  use: src.community.opensandbox:OpenSandboxProvider
  mounts:
    - host_path: /path/on/host
      container_path: /path/in/container
      read_only: false
```

## Dependencies

Make sure to install the required packages:

```bash
pip install opensandbox code-interpreter
```

## Differences from AioSandbox

| Feature | OpenSandbox | AioSandbox |
|---------|-------------|------------|
| Communication | gRPC | HTTP API |
| File API | Via shell commands | Direct HTTP endpoints |
| SDK | `opensandbox` + `code_interpreter` | `agent-sandbox` |
| Image Registry | Aliyun CN | Volcengine |
