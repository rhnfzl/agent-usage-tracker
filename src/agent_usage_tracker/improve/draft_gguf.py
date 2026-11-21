"""Generate drafts with a private, owned llama.cpp server."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .draft import _FIELDS


class _LocalHTTP(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def generate(adapter: dict[str, Any], prompt: str, directory: Path) -> str:
    endpoint = directory.resolve() / "gguf.sock"
    with subprocess.Popen(
        [adapter["exe"], *adapter.get("prefix", []), "--offline", "--no-webui",
         "--model", adapter["model_path"], "--host", str(endpoint),
         "--ctx-size", "16384", "--parallel", "1",
         "--gpu-layers", str(adapter["gpu_layers"])],
        cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as server:
        try:
            while server.poll() is None:
                try:
                    with closing(_LocalHTTP(endpoint, timeout=2)) as client:
                        client.request("GET", "/health")
                        if client.getresponse().status == 200:
                            break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError("local llama-server failed to start")
            properties: dict[str, Any] = {name: {"type": "string"} for name in sorted(_FIELDS)}
            properties["rubric_ids"] = {"type": "array", "items": {"type": "string"}}
            schema = {"type": "object", "properties": properties, "required": sorted(_FIELDS),
                      "additionalProperties": False}
            payload = {"messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object", "schema": schema},
                       "max_tokens": 1200, "temperature": 0, "stream": False}
            with closing(_LocalHTTP(endpoint, timeout=180)) as client:
                client.request("POST", "/v1/chat/completions", json.dumps(payload).encode(),
                               {"Content-Type": "application/json"})
                response = client.getresponse()
                if response.status != 200:
                    raise RuntimeError("local llama-server rejected the draft request")
                raw = response.read(262145)
            if len(raw) > 262144:
                raise RuntimeError("local GGUF response exceeds the output limit")
            choice = json.loads(raw)["choices"][0]
            message = choice["message"]
            if choice["finish_reason"] != "stop" or message.get("tool_calls"):
                raise RuntimeError("local GGUF response did not finish with text")
            text = message["content"]
            if not isinstance(text, str) or not text:
                raise RuntimeError("local GGUF response is empty")
            return text
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(2)
                except subprocess.TimeoutExpired:
                    server.kill()
