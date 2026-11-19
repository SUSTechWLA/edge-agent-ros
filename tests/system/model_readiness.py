"""Bound the cold-start probe inside the private acceptance stack's Ollama container."""
import subprocess
import time


def wait_for_cached_model(stack, project, env, model, seconds=40):
    deadline = time.monotonic() + seconds
    command = ["docker", "compose", "-p", project, "-f", str(stack), "exec", "-T", "model", "ollama", "list"]
    observed = "no listing received"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = subprocess.run(command, cwd=stack.parent, env=env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"Ollama readiness timed out for {model}: {observed}") from error
        if not result.returncode:
            names = [row.split()[0] for row in result.stdout.splitlines()[1:] if row.strip()]
            if model in names:
                return
            observed = f"cached models: {names}"
        else:
            diagnostic = (result.stderr or result.stdout).strip()
            observed = f"ollama list exited {result.returncode}: {diagnostic[-400:]}"
            if "could not connect to ollama server" not in diagnostic.lower() and "connection refused" not in diagnostic.lower():
                raise RuntimeError(observed)
        time.sleep(min(0.15, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"Ollama readiness timed out for {model}: {observed}")
