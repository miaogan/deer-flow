"""OpenSandbox implementation for DeerFlow.

This module adapts OpenSandbox SDK to DeerFlow's Sandbox interface.
"""

import asyncio
import base64
import logging
import os
import shutil
from typing import Optional

from src.sandbox.sandbox import Sandbox

logger = logging.getLogger(__name__)

# Lazy imports to avoid dependency errors when not using this provider
_sandbox_module = None
_interpreter_module = None


def _get_opensandbox_modules():
    """Lazy import OpenSandbox modules."""
    global _sandbox_module, _interpreter_module
    if _sandbox_module is None:
        try:
            from opensandbox import Sandbox as OpenSandboxClient
            from code_interpreter import CodeInterpreter, SupportedLanguage
            _sandbox_module = OpenSandboxClient
            _interpreter_module = (CodeInterpreter, SupportedLanguage)
        except ImportError as e:
            raise ImportError(
                "OpenSandbox SDK not installed. "
                "Install with: pip install opensandbox code-interpreter"
            ) from e
    return _sandbox_module, _interpreter_module


class OpenSandbox(Sandbox):
    """Sandbox implementation using OpenSandbox SDK.

    This adapter bridges OpenSandbox's async API to DeerFlow's sync interface.
    """

    def __init__(
        self,
        id: str,
        sandbox,
        interpreter,
        workspace_path: str = "/workspace",
    ):
        """Initialize OpenSandbox adapter.

        Args:
            id: Unique sandbox identifier.
            sandbox: OpenSandbox Sandbox instance.
            interpreter: CodeInterpreter instance.
            workspace_path: Path to persistent workspace inside container.
        """
        super().__init__(id)
        self._sandbox = sandbox
        self._interpreter = interpreter
        self._workspace_path = workspace_path
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Get module references
        _, self._modules = _get_opensandbox_modules()
        self._CodeInterpreter, self._SupportedLanguage = self._modules

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop for async operations."""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def _run_async(self, coro):
        """Run async coroutine in sync context."""
        loop = self._get_loop()
        if loop.is_running():
            # If loop is already running (e.g., in async context),
            # we need to use run_coroutine_threadsafe or nest_asyncio
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
        return loop.run_until_complete(coro)

    async def _execute_code_async(
        self,
        code: str,
        language,
    ) -> str:
        """Execute code asynchronously."""
        result = await self._interpreter.codes.run(
            code=code,
            language=language,
        )

        parts = []
        if result.logs.stdout:
            stdout = "\n".join(l.text.strip() for l in result.logs.stdout if l.text.strip())
            if stdout:
                parts.append(stdout)

        if result.logs.stderr:
            stderr = "\n".join(l.text.strip() for l in result.logs.stderr if l.text.strip())
            if stderr:
                parts.append(f"[stderr]\n{stderr}")

        output = "\n".join(parts) or "(no output)"
        return output

    def execute_command(self, command: str) -> str:
        """Execute a bash command in the sandbox.

        Args:
            command: The command to execute.

        Returns:
            The output of the command.
        """
        try:
            return self._run_async(
                self._execute_code_async(command, self._SupportedLanguage.SHELL)
            )
        except Exception as e:
            logger.error(f"Failed to execute command: {e}")
            return f"Error: {e}"

    def read_file(self, path: str) -> str:
        """Read the content of a file.

        Args:
            path: The absolute path of the file to read.

        Returns:
            The content of the file.
        """
        # Use cat command to read file content
        command = f"cat {path} 2>/dev/null || echo 'Error: File not found: {path}'"
        result = self.execute_command(command)
        if result.startswith("Error:"):
            return result
        return result

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """Write content to a file.

        Args:
            path: The absolute path of the file to write to.
            content: The text content to write to the file.
            append: Whether to append the content to the file.
        """
        # Ensure directory exists
        dir_path = os.path.dirname(path)
        if dir_path:
            self.execute_command(f"mkdir -p {dir_path}")

        # Write content using heredoc to handle special characters
        # Escape single quotes in content
        escaped_content = content.replace("'", "'\"'\"'")

        if append:
            command = f"echo '{escaped_content}' >> {path}"
        else:
            command = f"cat > {path} << 'EOFMARKER'\n{content}\nEOFMARKER"

        result = self.execute_command(command)
        if result.startswith("Error:"):
            raise IOError(f"Failed to write file: {result}")

    def update_file(self, path: str, content: bytes) -> None:
        """Update a file with binary content.

        Args:
            path: The absolute path of the file to update.
            content: The binary content to write to the file.
        """
        # Convert binary to base64 and decode in shell
        b64_content = base64.b64encode(content).decode("utf-8")
        dir_path = os.path.dirname(path)
        if dir_path:
            self.execute_command(f"mkdir -p {dir_path}")

        command = f"echo '{b64_content}' | base64 -d > {path}"
        result = self.execute_command(command)
        if result.startswith("Error:"):
            raise IOError(f"Failed to update file: {result}")

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """List the contents of a directory.

        Args:
            path: The absolute path of the directory to list.
            max_depth: The maximum depth to traverse. Default is 2.

        Returns:
            The contents of the directory as a list of paths.
        """
        command = f"find {path} -maxdepth {max_depth} 2>/dev/null | head -500"
        result = self.execute_command(command)

        if result.startswith("Error:"):
            return []

        lines = [line.strip() for line in result.strip().split("\n") if line.strip()]
        return lines

    async def cleanup(self) -> None:
        """Clean up sandbox resources."""
        try:
            await self._sandbox.kill()
            logger.info(f"Cleaned up OpenSandbox {self._id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup OpenSandbox {self._id}: {e}")
