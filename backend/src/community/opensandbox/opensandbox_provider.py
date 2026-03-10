"""OpenSandbox Provider for DeerFlow.

This module provides a SandboxProvider implementation using OpenSandbox SDK.
"""

import asyncio
import hashlib
import logging
import threading
from typing import Optional

from src.config import get_app_config
from src.sandbox.sandbox import Sandbox
from src.sandbox.sandbox_provider import SandboxProvider

from .opensandbox_sandbox import OpenSandbox

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_IMAGE = "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter"
DEFAULT_ENTRYPOINT = ["/opt/opensandbox/code-interpreter.sh"]
DEFAULT_WORKSPACE_PATH = "/workspace"


class OpenSandboxProvider(SandboxProvider):
    """Sandbox provider using OpenSandbox SDK.

    This provider creates isolated containers for code execution using
    the OpenSandbox platform.

    Configuration options in config.yaml under sandbox:
        use: src.community.opensandbox:OpenSandboxProvider
        image: sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter
        workspace_host_path: /path/on/host  # Host path for persistent workspace
        workspace_container_path: /workspace  # Container mount path
        python_version: "3.12"
        environment:
          CUSTOM_VAR: value
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sandboxes: dict[str, OpenSandbox] = {}  # sandbox_id -> OpenSandbox
        self._thread_sandboxes: dict[str, str] = {}  # thread_id -> sandbox_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._config = self._load_config()

        # Lazy import OpenSandbox modules
        self._OpenSandboxClient = None
        self._CodeInterpreter = None
        self._SupportedLanguage = None

    def _get_modules(self):
        """Lazy import OpenSandbox modules."""
        if self._OpenSandboxClient is None:
            from opensandbox import Sandbox as OpenSandboxClient
            from code_interpreter import CodeInterpreter, SupportedLanguage
            from opensandbox.api.lifecycle.models.volume import Volume
            from opensandbox.models import Host
            self._OpenSandboxClient = OpenSandboxClient
            self._CodeInterpreter = CodeInterpreter
            self._SupportedLanguage = SupportedLanguage
            self._Volume = Volume
            self._Host = Host
        return self._OpenSandboxClient, self._CodeInterpreter, self._SupportedLanguage

    def _load_config(self) -> dict:
        """Load sandbox configuration from app config."""
        config = get_app_config()
        sandbox_config = config.sandbox

        # Get workspace host path from config
        workspace_host_path = getattr(sandbox_config, "workspace_host_path", None)
        if not workspace_host_path:
            # Default to a subdirectory in the project
            import os
            workspace_host_path = os.path.join(os.getcwd(), "sandboxes")

        # Get entrypoint (can be string or list)
        entrypoint = getattr(sandbox_config, "entrypoint", None) or DEFAULT_ENTRYPOINT
        if isinstance(entrypoint, str):
            entrypoint = [entrypoint]

        return {
            "image": getattr(sandbox_config, "image", None) or DEFAULT_IMAGE,
            "entrypoint": entrypoint,
            "workspace_host_path": workspace_host_path,
            "workspace_container_path": getattr(sandbox_config, "workspace_container_path", None) or DEFAULT_WORKSPACE_PATH,
            "python_version": getattr(sandbox_config, "python_version", "3.12"),
            "environment": getattr(sandbox_config, "environment", {}) or {},
            "mounts": getattr(sandbox_config, "mounts", []) or [],
        }

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def _run_async(self, coro):
        """Run async coroutine in sync context."""
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str) -> str:
        """Generate a deterministic sandbox ID from thread ID."""
        return hashlib.sha256(thread_id.encode()).hexdigest()[:8]

    async def _create_sandbox_async(self, sandbox_id: str, thread_id: str) -> OpenSandbox:
        """Create a new OpenSandbox instance asynchronously."""
        OpenSandboxClient, CodeInterpreter, _ = self._get_modules()

        config = self._config

        # Prepare volumes for persistent workspace
        volumes = []
        
        # 1. Thread-specific workspace
        if config["workspace_host_path"]:
            import os
            host_path = os.path.join(config["workspace_host_path"], f"thread-{thread_id}")
            os.makedirs(host_path, exist_ok=True)

            volumes.append(
                self._Volume(
                    name="workspace",
                    host=self._Host(path=host_path),
                    mount_path=config["workspace_container_path"],
                )
            )

        # 2. Skills mount (required by DeerFlow)
        try:
            app_config = get_app_config()
            skills_path = app_config.skills.get_skills_path()
            container_path = app_config.skills.container_path

            if skills_path.exists():
                volumes.append(
                    self._Volume(
                        name="skills",
                        host=self._Host(path=str(skills_path)),
                        mount_path=container_path,
                    )
                )
                logger.info(f"Mounting skills: {skills_path} -> {container_path}")
        except Exception as e:
            logger.warning(f"Failed to setup skills mount: {e}")

        # 3. Extra mounts from config
        for mount_config in config.get("mounts", []):
            volumes.append(
                self._Volume(
                    name=mount_config.container_path.replace("/", "_").strip("_"),
                    host=self._Host(path=mount_config.host_path),
                    mount_path=mount_config.container_path,
                )
            )

        # Prepare environment
        env = {"PYTHON_VERSION": config["python_version"]}
        env.update(config["environment"])

        # Create sandbox
        logger.info(f"Creating OpenSandbox with image: {config['image']}")
        sandbox = await OpenSandboxClient.create(
            image=config["image"],
            entrypoint=config["entrypoint"],
            env=env,
            volumes=volumes if volumes else None,
        )

        # Create code interpreter
        interpreter = await CodeInterpreter.create(sandbox)

        # Create adapter
        opensandbox = OpenSandbox(
            id=sandbox_id,
            sandbox=sandbox,
            interpreter=interpreter,
            workspace_path=config["workspace_container_path"],
        )

        return opensandbox

    def acquire(self, thread_id: str | None = None) -> str:
        """Acquire a sandbox for the given thread.

        Args:
            thread_id: Thread identifier. If None, generates a random ID.

        Returns:
            The sandbox ID.
        """
        if thread_id is None:
            import uuid
            thread_id = str(uuid.uuid4())

        # Check if sandbox already exists for this thread
        with self._lock:
            if thread_id in self._thread_sandboxes:
                sandbox_id = self._thread_sandboxes[thread_id]
                if sandbox_id in self._sandboxes:
                    logger.debug(f"Reusing existing sandbox {sandbox_id} for thread {thread_id}")
                    return sandbox_id

            sandbox_id = self._deterministic_sandbox_id(thread_id)

        # Create new sandbox
        logger.info(f"Creating OpenSandbox {sandbox_id} for thread {thread_id}")

        try:
            opensandbox = self._run_async(
                self._create_sandbox_async(sandbox_id, thread_id)
            )

            with self._lock:
                self._sandboxes[sandbox_id] = opensandbox
                self._thread_sandboxes[thread_id] = sandbox_id

            logger.info(f"OpenSandbox {sandbox_id} created successfully")
            return sandbox_id

        except Exception as e:
            logger.exception(f"Failed to create OpenSandbox for thread {thread_id}")
            raise RuntimeError(f"Failed to create sandbox: {e}") from e

    def get(self, sandbox_id: str) -> Sandbox | None:
        """Get a sandbox by ID.

        Args:
            sandbox_id: The sandbox identifier.

        Returns:
            The sandbox instance, or None if not found.
        """
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        """Release a sandbox.

        Args:
            sandbox_id: The sandbox identifier.
        """
        with self._lock:
            opensandbox = self._sandboxes.pop(sandbox_id, None)
            if opensandbox is None:
                return

            # Remove from thread mapping
            thread_id = None
            for tid, sid in list(self._thread_sandboxes.items()):
                if sid == sandbox_id:
                    thread_id = tid
                    del self._thread_sandboxes[tid]
                    break

        # Cleanup sandbox
        if opensandbox:
            try:
                self._run_async(opensandbox.cleanup())
                logger.info(f"Released OpenSandbox {sandbox_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup OpenSandbox {sandbox_id}: {e}")

    def shutdown(self) -> None:
        """Shutdown all sandboxes."""
        logger.info("Shutting down OpenSandboxProvider")

        with self._lock:
            sandbox_ids = list(self._sandboxes.keys())

        for sandbox_id in sandbox_ids:
            try:
                self.release(sandbox_id)
            except Exception as e:
                logger.warning(f"Failed to release sandbox {sandbox_id}: {e}")

        # Close event loop
        if self._loop and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

        logger.info("OpenSandboxProvider shutdown complete")
