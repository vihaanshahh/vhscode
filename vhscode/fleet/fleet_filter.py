"""Optional on-device classifier for preselected transient progress events."""

from collections import deque
from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


MODEL_ID = "Xenova/mobilebert-uncased-mnli"
MODEL_REVISION = "8b0ea66ab7b190bba77418ba03b67d69cfc9a1ee"
MODEL_FILES = {
    "config.json": "93b1698798a8ff921b49c40d3afa7584342b94b06fde7851d342134eadaf61c7",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "onnx/model_quantized.onnx": "1dc36bc1f41b1d1da9e8f28f69f8dc6fa316410b2cde057756ddfb7083a48021",
}
RUNTIME_PACKAGES = (
    "numpy==2.2.6; python_version == '3.10'",
    "numpy==2.3.5; python_version >= '3.11'",
    "onnxruntime==1.23.2",
    "tokenizers==0.22.2",
)
RUNTIME_VERSION = "\n".join(RUNTIME_PACKAGES)
HYPOTHESES = (
    "This is a status update about work in progress.",
    "This is a report of a concrete result.",
)
MAX_PENDING = 8


def cache_dir():
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "opencode" / "fleet-filter" / MODEL_REVISION


class ProgressFilter:
    """A True decision suppresses a caller-selected transient progress event."""

    def __init__(
        self, enabled: bool = False, *, idle_seconds: float = 60.0, deadline_seconds: float = 1.0
    ):
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.deadline_seconds = deadline_seconds
        self._condition = threading.Condition()
        self._pending = deque()
        self._thread = None
        self._closed = False

    def submit(self, text: str) -> Future[bool]:
        """Queue one transient event and return immediately, failing open on overload."""
        decision = Future()
        if self.enabled and 0 < len(text) <= 512 and "\n" not in text and "\r" not in text:
            with self._condition:
                if not self._closed and len(self._pending) < MAX_PENDING:
                    if self._thread is None:
                        self._thread = threading.Thread(target=self._run, daemon=True)
                        try:
                            self._thread.start()
                        except RuntimeError:
                            self._thread = None
                    if self._thread is not None:
                        self._pending.append((time.monotonic() + self.deadline_seconds, text, decision))
                        self._condition.notify()
                        return decision
        decision.set_result(False)
        return decision

    def close(self):
        """Stop accepting work and release queued events for display."""
        with self._condition:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
            self._condition.notify()
        for _, _, decision in pending:
            if decision.set_running_or_notify_cancel():
                decision.set_result(False)

    def _run(self):
        worker = None
        idle_at = retry_at = 0.0
        try:
            while True:
                with self._condition:
                    if not self._closed and not self._pending:
                        until_idle = None if worker is None else max(0.0, idle_at - time.monotonic())
                        self._condition.wait(until_idle)
                    if self._closed:
                        return
                    request = self._pending.popleft() if self._pending else None
                if worker is not None and time.monotonic() >= idle_at:
                    worker.close()
                    worker = None
                if request is None:
                    continue
                expires, text, decision = request
                if not decision.set_running_or_notify_cancel():
                    continue
                suppress = False
                try:
                    now = time.monotonic()
                    if retry_at <= now < expires:
                        if worker is None:
                            worker = _Worker()
                        suppress = worker.classify(text, expires)
                        idle_at = time.monotonic() + self.idle_seconds
                except (OSError, ValueError):
                    if worker is not None:
                        worker.close()
                        worker = None
                    retry_at = time.monotonic() + min(5.0, self.idle_seconds)
                decision.set_result(suppress and not self._closed and time.monotonic() < expires)
        finally:
            if worker is not None:
                worker.close()
            self.close()


class _Worker:
    """Own the subprocess and its private byte stream from the supervisor thread."""

    def __init__(self):
        cache = cache_dir()
        if (cache / "ready").read_text(encoding="utf-8") != RUNTIME_VERSION:
            raise ValueError("classifier setup is required")
        parent, child = socket.socketpair()
        try:
            with child:
                self.process = subprocess.Popen(
                    [str(cache / "venv" / "bin" / "python"), str(Path(__file__).resolve()), "worker"],
                    stdin=child,
                    stdout=child,
                    stderr=subprocess.DEVNULL,
                )
        except OSError:
            parent.close()
            raise
        self.connection = parent
        self.ready = False
        self.startup_expires = time.monotonic() + 10.0

    def classify(self, text, expires):
        if not self.ready:
            try:
                ready = self._read(min(expires, self.startup_expires))
            except TimeoutError:
                if time.monotonic() < self.startup_expires:
                    return False
                raise
            if ready != b"R":
                raise ValueError("classifier startup failed")
            self.ready = True
        self.connection.settimeout(max(0.0, expires - time.monotonic()))
        self.connection.sendall(json.dumps(text).encode("ascii") + b"\n")
        result = self._read(expires)
        if result not in (b"0", b"1"):
            raise ValueError("invalid classifier response")
        return result == b"1"

    def _read(self, expires):
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.connection.settimeout(remaining)
        return self.connection.recv(1)

    def close(self):
        self.connection.close()
        self.process.kill()
        self.process.wait()


def _load_model():
    import numpy
    import onnxruntime
    from tokenizers import Tokenizer

    model = cache_dir() / "model"
    config = json.loads((model / "config.json").read_text(encoding="utf-8"))
    entailment = config["label2id"]["ENTAILMENT"]
    contradiction = config["label2id"]["CONTRADICTION"]
    tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(
        str(model / "onnx" / "model_quantized.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
    )
    input_names = {item.name for item in session.get_inputs()}

    def classify(text):
        def probability(hypothesis):
            tokens = tokenizer.encode(text, hypothesis)
            if len(tokens.ids) > 128:
                return None
            inputs = {
                "input_ids": [tokens.ids],
                "attention_mask": [tokens.attention_mask],
                "token_type_ids": [tokens.type_ids],
            }
            feed = {name: numpy.asarray(inputs[name], dtype=numpy.int64) for name in input_names}
            logits = session.run(None, feed)[0][0][[entailment, contradiction]]
            weights = numpy.exp(logits - logits.max())
            return float(weights[0] / weights.sum())

        progress = probability(HYPOTHESES[0])
        if progress is None or progress < 0.80:
            return False
        signal = probability(HYPOTHESES[1])
        return signal is not None and signal <= 0.35

    return classify


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _setup():
    import fcntl
    from urllib.request import urlopen

    if not (3, 10) <= sys.version_info[:2] <= (3, 13):
        for minor in (13, 12, 11, 10):
            interpreter = shutil.which(f"python3.{minor}")
            if interpreter is not None:
                os.execv(interpreter, [interpreter, str(Path(__file__).resolve()), "setup"])
        raise SystemExit("Classifier setup requires Python 3.10–3.13 on PATH.")

    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, expected_sha in MODEL_FILES.items():
            destination = cache / "model" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and _sha256(destination) == expected_sha:
                continue
            url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}"
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as downloaded:
                temporary = Path(downloaded.name)
            try:
                with urlopen(url, timeout=60) as source, temporary.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                if _sha256(temporary) != expected_sha:
                    raise ValueError(f"checksum mismatch for {name}")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        ready = cache / "ready"
        python = cache / "venv" / "bin" / "python"
        if not (ready.is_file() and ready.read_text(encoding="utf-8") == RUNTIME_VERSION and python.is_file()):
            ready.unlink(missing_ok=True)
            if not python.is_file():
                subprocess.run([sys.executable, "-m", "venv", str(cache / "venv")], check=True)
            subprocess.run(
                [str(python), "-m", "pip", "install", "--only-binary=:all:", *RUNTIME_PACKAGES],
                check=True,
            )
            temporary = cache / "ready.tmp"
            temporary.write_text(RUNTIME_VERSION, encoding="utf-8")
            temporary.replace(ready)
    print(f"Installed classifier in {cache}")


def _worker():
    classify = _load_model()
    output = sys.stdout.buffer
    output.write(b"R")
    output.flush()
    for request in sys.stdin.buffer:
        suppress = classify(json.loads(request))
        output.write(b"1" if suppress else b"0")
        output.flush()


def _smoke():
    worker = _Worker()
    try:
        examples = {
            "Running tests...": True,
            "I'm looking into it.": True,
            "The migration failed with a permissions error.": False,
            "Waiting for your approval to continue.": False,
            "Running tests but checkout is failing.": False,
            "The migration passed and 20 records were updated.": False,
        }
        for text, expected in examples.items():
            if worker.classify(text, time.monotonic() + 8.0) != expected:
                raise SystemExit(f"Unexpected classifier decision for {text!r}")
        print("Local MobileBERT suppressed routine progress and preserved failures, approvals, and results.")
    finally:
        worker.close()


if __name__ == "__main__":
    commands = {"setup": _setup, "smoke": _smoke, "worker": _worker}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(f"Usage: {Path(__file__).name} setup|smoke")
    commands[sys.argv[1]]()
