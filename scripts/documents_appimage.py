"""Small Linux/Windows setup window over the existing Documents installers."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter.font import families, nametofont
from typing import TypedDict, cast
from urllib.parse import urlsplit

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = {
    "preflight": "Checking this computer…",
    "download": "Downloading Documents…",
    "verification": "Verifying the download…",
    "unpacking": "Preparing the runtime…",
    "environment": "Preparing the runtime…",
    "initialization": "Initializing Documents…",
    "activation": "Activating Documents…",
    "readiness": "Checking Documents…",
    "recovery": "Finishing recovery…",
    "complete": "Documents is installed.",
}
_STATES = {"running", "succeeded", "failed", "cancelled"}
_EVENT_FIELDS = {
    "format",
    "schema_version",
    "operation_id",
    "sequence",
    "operation",
    "application_profile",
    "phase",
    "state",
    "bytes_completed",
    "bytes_total",
    "error_code",
    "retryable",
    "cancellation_safe",
}


class LauncherError(RuntimeError):
    """A fixed, user-readable diagnostic without native output or credentials."""


class _ProcessOptions(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


def _is_windows() -> bool:
    return sys.platform == "win32"


def _process_options(*, detached: bool = False) -> _ProcessOptions:
    if _is_windows():
        # These constants are fixed by the Windows process API. Keeping them
        # literal also permits contract tests on the Linux development host.
        return {"creationflags": 0x08000000 | (0x00000200 if detached else 0)}
    return {"start_new_session": True} if detached else {}


def _powershell_command(script: str) -> list[str]:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    executable = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    preamble = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$OutputEncoding=[Text.UTF8Encoding]::new($false); "
    )
    encoded = base64.b64encode((preamble + script).encode("utf-16le")).decode("ascii")
    return [
        str(executable),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def _script_command(
    path: Path, arguments: list[str], *, named_parameters: bool = False
) -> list[str]:
    if not _is_windows():
        return [str(path), *arguments]
    quoted = ["'" + str(path).replace("'", "''") + "'"]
    quoted.extend(
        value
        if named_parameters and re.fullmatch(r"-[A-Za-z][A-Za-z0-9]*", value)
        else "'" + value.replace("'", "''") + "'"
        for value in arguments
    )
    return _powershell_command("& " + " ".join(quoted) + "; exit $LASTEXITCODE")


def _restore_child_dll_search() -> None:
    if _is_windows() and getattr(sys, "frozen", False):
        # PyInstaller's DLL directory is inherited by subprocesses. All bootstrap
        # extension modules above are already imported; installed commands must
        # use their own libraries after the one-file bootstrap exits.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        reset = kernel.SetDllDirectoryW
        reset.argtypes = [ctypes.c_wchar_p]
        reset.restype = ctypes.c_int
        if not reset(None):
            raise LauncherError("Documents could not prepare its Windows launcher.")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LauncherError("Documents returned an invalid status. Please try again.")
    return cast(dict[str, object], value)


def load_configuration(appdir: Path) -> dict[str, object]:
    payload = (appdir / "launcher.json").read_bytes()
    if len(payload) > 16_384:
        raise LauncherError("This Documents launcher is incomplete. Download it again.")
    record = _object(json.loads(payload))
    if (
        type(record.get("schema_version")) is not int
        or record.get("schema_version") != 1
        or record.get("product") != "carlina-documents"
        or not isinstance(record.get("version"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_revision", "")))
    ):
        raise LauncherError("This Documents launcher is incomplete. Download it again.")
    expected = record.get("installer_sha256")
    expected_platform = "win-64" if _is_windows() else "linux-64"
    if record.get("platform", "linux-64") != expected_platform:
        raise LauncherError("This Documents launcher is for a different platform.")
    installer = appdir / ("installer.ps1" if _is_windows() else "installer.sh")
    if (
        not isinstance(expected, str)
        or not _HASH.fullmatch(expected)
        or installer.is_symlink()
        or hashlib.sha256(installer.read_bytes()).hexdigest() != expected
    ):
        raise LauncherError("The installer has changed. Download Documents again.")
    return record


def validate_event(
    payload: bytes, operation_id: str, sequence: int
) -> dict[str, object]:
    if len(payload) > 8192 or not payload.endswith(b"\n"):
        raise LauncherError("The installer returned an invalid progress record.")
    record = _object(json.loads(payload))
    if (
        set(record) != _EVENT_FIELDS
        or record["format"] != "carlina.installation-event"
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or record["operation_id"] != operation_id
        or type(record["sequence"]) is not int
        or record["sequence"] != sequence
        or record["operation"] != "install"
        or record["application_profile"] != "documents"
        or record["phase"] not in _PHASES
        or record["state"] not in _STATES
        or type(record["retryable"]) is not bool
        or type(record["cancellation_safe"]) is not bool
    ):
        raise LauncherError("The installer returned an invalid progress record.")
    for key in ("bytes_completed", "bytes_total"):
        value = record[key]
        if value is not None and (type(value) is not int or value < 0):
            raise LauncherError("The installer returned an invalid progress record.")
    completed, total = record["bytes_completed"], record["bytes_total"]
    if isinstance(completed, int) and isinstance(total, int) and completed > total:
        raise LauncherError("The installer returned an invalid progress record.")
    return record


class DocumentsSetup:
    """The UI owns interaction; installed commands retain runtime/data authority."""

    def __init__(self, root: tk.Tk, appdir: Path, program_root: Path) -> None:
        self.root = root
        self.appdir = appdir
        self.configuration = load_configuration(appdir)
        self.program_root = program_root
        self.events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.can_cancel = False
        self.operation = ""
        self.email = tk.StringVar()
        self.password = tk.StringVar()
        self.confirmation = tk.StringVar()
        self.location = tk.StringVar(value=str(program_root))
        self.heading = tk.StringVar(value="Carlina Documents")
        self.message = tk.StringVar(value="Checking your installation…")
        self.detail = tk.StringVar(value="")
        self._build_window()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.after(80, self._drain)
        self._background("probe", self._probe)

    @property
    def launcher(self) -> Path:
        return self.program_root / (
            "carlina-documents.ps1" if _is_windows() else "carlina-documents"
        )

    def _build_window(self) -> None:
        self.root.title("Carlina Documents")
        self.root.geometry("660x620")
        self.root.minsize(620, 580)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        available = {name.casefold(): name for name in families(self.root)}
        family = next(
            (
                available[name]
                for name in ("segoe ui", "dejavu sans", "liberation sans", "helvetica")
                if name in available
            ),
            "sans",
        )
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            nametofont(font_name, root=self.root).configure(family=family, size=11)
        style.configure("TFrame", background="#f5f3ee")
        style.configure(
            "TLabel",
            background="#f5f3ee",
            foreground="#25231f",
            font=(family, 11),
        )
        style.configure("Heading.TLabel", font=(family, 23, "bold"))
        style.configure("TButton", padding=(14, 9))
        style.configure("Primary.TButton", background="#8d5638", foreground="#ffffff")
        style.map(
            "Primary.TButton",
            background=[("active", "#75452c"), ("disabled", "#d8d4ca")],
            foreground=[("disabled", "#69645c")],
        )
        self.root.configure(background="#f5f3ee")
        frame = ttk.Frame(self.root, padding=32)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        ttk.Label(frame, textvariable=self.heading, style="Heading.TLabel").grid(
            sticky="w", pady=(0, 18)
        )
        ttk.Label(frame, textvariable=self.message, wraplength=550).grid(sticky="w")
        self.content = ttk.Frame(frame)
        self.content.grid(row=2, sticky="nsew", pady=18)
        self.content.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=3, sticky="ew", pady=(8, 12))
        ttk.Label(frame, textvariable=self.detail, wraplength=550).grid(
            row=4, sticky="w", pady=(0, 14)
        )
        actions = ttk.Frame(frame)
        actions.grid(row=5, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.secondary = ttk.Button(actions, text="Close", command=self.close)
        self.secondary.grid(row=0, column=0, sticky="w")
        self.primary = ttk.Button(
            actions, text="Please wait", state="disabled", style="Primary.TButton"
        )
        self.primary.grid(row=0, column=1, sticky="e")

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        self.content.columnconfigure(0, weight=1)
        self.content.columnconfigure(1, weight=0)

    def _environment(self) -> dict[str, str]:
        _restore_child_dll_search()
        environment = {
            **os.environ,
            "CARLINA_PROFILE": "documents",
            "CARLINA_PROGRAM_ROOT": str(self.program_root),
            "PYTHONNOUSERSITE": "1",
        }
        for key in tuple(environment):
            if key in {
                "PYTHONHOME",
                "PYTHONPATH",
                "TCL_LIBRARY",
                "TK_LIBRARY",
            } or key.startswith("_PYI_"):
                environment.pop(key)
        bundle = getattr(sys, "_MEIPASS", None)
        if _is_windows() and isinstance(bundle, str):
            root = Path(bundle).resolve()
            environment["PATH"] = os.pathsep.join(
                entry
                for entry in environment.get("PATH", "").split(os.pathsep)
                if entry and not Path(entry).resolve().is_relative_to(root)
            )
        return environment

    def _background(self, operation: str, action: Callable[[], object]) -> None:
        self.busy = True
        self.operation = operation
        self.can_cancel = operation == "install"
        self.cancel.clear()
        self.primary.configure(state="disabled")
        self.secondary.configure(
            text="Cancel" if self.can_cancel else "Please wait",
            state="normal" if self.can_cancel else "disabled",
        )
        self.progress.configure(mode="indeterminate")
        self.progress.grid()
        self.progress.start(12)

        def perform() -> None:
            try:
                result = action()
            except LauncherError as exc:
                self.events.put(("error", str(exc)))
            except Exception:
                self.events.put(
                    (
                        "error",
                        "Documents could not finish this step. Check available disk space and your internet connection, then try again.",
                    )
                )
            else:
                self.events.put((operation, result))

        self.worker = threading.Thread(
            target=perform, name="documents-setup", daemon=False
        )
        self.worker.start()

    def _json_command(self, command: str) -> dict[str, object]:
        result = subprocess.run(
            _script_command(self.launcher, [command, "--json"]),
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
            **_process_options(),
        )
        if len(result.stdout) > 1_048_576:
            raise LauncherError(
                "Documents returned an invalid status. Please try again."
            )
        return _object(json.loads(result.stdout))

    def _probe(self) -> str:
        if not self.launcher.exists():
            if any(
                (self.program_root / name).exists()
                for name in ("current", "carlina", "carlina.ps1", "carlina.cmd")
            ):
                raise LauncherError(
                    "This location contains a managed installation that needs its own launcher or recovery tools. Choose a separate Documents location."
                )
            # Fresh preparation can leave state/versions without an active
            # pointer. The native CLI owns frozen-input retry admission.
            return "install"
        status = self.launcher.lstat()
        if not stat.S_ISREG(status.st_mode) or (
            not _is_windows()
            and (status.st_uid != os.getuid() or status.st_mode & 0o022)
        ):
            raise LauncherError(
                "The installed Documents launcher has unexpected ownership or permissions."
            )
        if _is_windows():
            self._check_windows_launcher()
        report = self._json_command("status")
        managed = _object(report.get("managed_install"))
        if report.get("application_profile") is None:
            raise LauncherError(
                "This launcher does not report its application profile. Use the existing installation's launcher or recovery tools."
            )
        if (
            report.get("application_profile") != "documents"
            or Path(str(managed.get("program_root"))).resolve()
            != self.program_root.resolve()
        ):
            raise LauncherError("The selected location belongs to another application.")
        doctor = self._json_command("doctor")
        checks = doctor.get("checks")
        if isinstance(checks, list):
            for item in cast(list[object], checks):
                check = _object(item)
                if check.get("id") == "accounts.admin" and check.get("status") == "ok":
                    return "ready"
        return "account"

    def _check_windows_launcher(self) -> None:
        path = "'" + str(self.launcher).replace("'", "''") + "'"
        script = (
            "$acl=Get-Acl -LiteralPath " + path + "; "
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
            "$trusted=@($sid,'S-1-5-18','S-1-5-32-544'); $safe=$owner -in $trusted; "
            "foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) { "
            "if($rule.AccessControlType -eq 'Allow' -and "
            "(($rule.FileSystemRights -band 0xD0156) -ne 0) -and "
            "$rule.IdentityReference.Value -notin $trusted) {$safe=$false} }; "
            "@{safe=[bool]$safe} | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            _powershell_command(script),
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            **_process_options(),
        )
        if (
            result.returncode
            or len(result.stdout) > 4096
            or _object(json.loads(result.stdout)).get("safe") is not True
        ):
            raise LauncherError(
                "The installed Documents launcher can be changed by another account. Choose a private program location."
            )

    def _show_install(self) -> None:
        self.heading.set("Install Documents")
        self.message.set(
            "Extract, review and translate documents on your computer. Setup downloads the application. After it opens, follow the guide to activate its tools and prepare sentence splitting."
        )
        self._clear_content()
        ttk.Label(self.content, text="Program location").grid(sticky="w", pady=(0, 6))
        self.location_entry = ttk.Entry(self.content, textvariable=self.location)
        self.location_entry.grid(row=1, column=0, sticky="ew")
        ttk.Button(self.content, text="Choose…", command=self._choose_location).grid(
            row=1, column=1, padx=(8, 0)
        )
        self.detail.set(
            "An internet connection is needed. Your documents are stored separately from this setup program."
        )
        self.primary.configure(
            text="Install Documents", state="normal", command=self.install
        )
        self.location_entry.focus_set()

    def _choose_location(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="Choose a Documents program location"
        )
        if selected:
            self.location.set(str(Path(selected)))

    def install(self) -> None:
        selected = Path(self.location.get()).expanduser()
        if not selected.is_absolute():
            self.detail.set("Choose an absolute program location.")
            return
        self.program_root = selected.absolute()
        self._clear_content()
        self.message.set("Installing Documents…")
        self.detail.set(
            "You can cancel. Setup will finish its current safe step before stopping."
        )
        self._background("install", self._install)

    def _install(self) -> str:
        selected_state = self._probe()
        if selected_state != "install":
            return selected_state
        identifier = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(
            prefix="carlina-documents-setup-"
        ) as temporary:
            cancel_path = Path(temporary) / "cancel"
            command = [
                "/bin/sh",
                str(self.appdir / "installer.sh"),
                "--program-root",
                str(self.program_root),
                "--skip-init",
                "--no-path-link",
                "--supervised-preparation",
                "--events-jsonl",
                "--operation-id",
                identifier,
                "--cancel-file",
                str(cancel_path),
            ]
            if _is_windows():
                command = _script_command(
                    self.appdir / "installer.ps1",
                    [
                        "-ProgramRoot",
                        str(self.program_root),
                        "-SkipInit",
                        "-NoPathUpdate",
                        "-SupervisedPreparation",
                        "-EventsJsonl",
                        "-OperationId",
                        identifier,
                        "-CancelFile",
                        str(cancel_path),
                    ],
                    named_parameters=True,
                )
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    command,
                    env=self._environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    **_process_options(),
                )
                finished = threading.Event()

                def forward_cancellation() -> None:
                    # stdout can be quiet while a connection or atomic native
                    # step is pending. Cancellation must not wait for a record.
                    while not finished.wait(0.1):
                        if self.cancel.is_set():
                            cancel_path.touch(mode=0o600, exist_ok=True)
                            return

                watcher = threading.Thread(
                    target=forward_cancellation, name="documents-cancel", daemon=False
                )
                watcher.start()
                sequence = 0
                terminal: dict[str, object] | None = None
                invalid = False
                assert process.stdout is not None
                try:
                    while True:
                        if self.cancel.is_set() and not cancel_path.exists():
                            cancel_path.touch(mode=0o600, exist_ok=False)
                        line = process.stdout.readline(8193)
                        if not line:
                            break
                        if invalid:
                            continue
                        try:
                            if terminal is not None:
                                raise LauncherError(
                                    "Unexpected progress after completion."
                                )
                            record = validate_event(line, identifier, sequence + 1)
                        except ValueError, LauncherError, TypeError:
                            invalid = True
                            self.cancel.set()
                        else:
                            sequence += 1
                            self.events.put(("progress", record))
                            if record["state"] != "running":
                                terminal = record
                except BaseException:
                    self.cancel.set()
                    raise
                finally:
                    if process.poll() is None and invalid:
                        cancel_path.touch(mode=0o600, exist_ok=True)
                    # Always drain and wait before dropping the controller. Native
                    # custody, recovery and safe-boundary cancellation stay in CLI.
                    while process.stdout.read(65_536):
                        pass
                    process.stdout.close()
                    code = process.wait()
                    finished.set()
                    watcher.join()
                if invalid or terminal is None:
                    raise LauncherError(
                        "Setup did not return a complete result. Retry with the same location."
                    )
                if terminal["state"] == "cancelled" and code == 130:
                    return "cancelled"
                if code != 0 or terminal["state"] != "succeeded":
                    raise LauncherError(
                        "Installation did not complete. Check the setup requirements, network connection and free disk space, then retry the same location."
                    )
        return "account"

    def _show_account(self) -> None:
        self.heading.set("Your local account")
        self.message.set(
            "Create the first administrator account for this Documents installation. Credentials stay on this computer."
        )
        self._clear_content()
        for row, (label, variable, hidden) in enumerate(
            (
                ("Email", self.email, False),
                ("Password", self.password, True),
                ("Confirm password", self.confirmation, True),
            )
        ):
            ttk.Label(self.content, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 14), pady=5
            )
            entry = ttk.Entry(
                self.content, textvariable=variable, show="•" if hidden else ""
            )
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            if row == 0:
                entry.focus_set()
        self.content.columnconfigure(1, weight=1)
        self.content.columnconfigure(0, weight=0)
        self.detail.set(
            "Use at least 8 characters. No model API account is required for passive MCP work."
        )
        self.primary.configure(
            text="Create account", state="normal", command=self.create_account
        )

    def create_account(self) -> None:
        email, password = self.email.get().strip(), self.password.get()
        if (
            not email
            or len(email) > 100
            or email.count("@") != 1
            or email.startswith("@")
            or email.endswith("@")
            or any(char.isspace() for char in email)
        ):
            self.detail.set("Enter a valid email address.")
            return
        if len(password) < 8 or password != self.confirmation.get():
            self.detail.set(
                "Use at least 8 characters and enter the same password twice."
            )
            return
        try:
            if len(password.encode("utf-8")) > 4096:
                self.detail.set("Choose a password shorter than 4096 bytes.")
                return
            payload = json.dumps(
                {"email": email, "password": password}, ensure_ascii=False
            ).encode()
        except UnicodeError:
            self.detail.set(
                "Enter an email and password with valid Unicode characters."
            )
            return
        self.password.set("")
        self.confirmation.set("")
        self._initialize(payload)

    def _initialize(self, payload: bytes) -> None:
        def initialize() -> str:
            result = subprocess.run(
                _script_command(self.launcher, ["init", "--admin-stdin"]),
                input=payload,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                **_process_options(),
            )
            if result.returncode:
                raise LauncherError(
                    "Account setup did not finish. An older installation may need its normal setup command. Retry or use an existing account."
                )
            if self._probe() != "ready":
                raise LauncherError(
                    "No administrator was created. An installation with existing users needs its account recovery tools; existing accounts have not been replaced."
                )
            return "ready"

        self.message.set("Preparing your account…")
        self.detail.set(
            "Finishing database setup. This step must complete before closing."
        )
        self._clear_content()
        self._background("account", initialize)

    def _start(self) -> str:
        report = self._json_command("status")
        if report.get("running") is True and report.get("managed_process") is not True:
            raise LauncherError(
                "Another runtime is using the Documents address. Stop it or choose a separate port before opening this installation."
            )
        if report.get("running") is not True:
            child = subprocess.Popen(
                _script_command(self.launcher, ["start", "--host", "127.0.0.1"]),
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_process_options(detached=True),
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                time.sleep(1)
                report = self._json_command("status")
                if (
                    report.get("running") is True
                    and report.get("managed_process") is True
                ):
                    break
                if child.poll() is not None:
                    raise LauncherError(
                        "Documents could not start. Check its logs and whether another app is using its port."
                    )
            else:
                raise LauncherError(
                    "Documents is taking longer than expected to start. Its managed runtime has been left intact; try opening it again."
                )
        url = report.get("url")
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme != "http"
            or urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise LauncherError("Documents returned an unexpected browser address.")
        return url

    def _drain(self) -> None:
        while not self.events.empty():
            kind, value = self.events.get()
            if kind == "progress":
                record = _object(value)
                self.message.set(_PHASES[str(record["phase"])])
                completed, total = record["bytes_completed"], record["bytes_total"]
                if isinstance(completed, int) and isinstance(total, int) and total > 0:
                    self.progress.stop()
                    self.progress.configure(
                        mode="determinate", maximum=total, value=completed
                    )
                    self.detail.set(
                        f"{completed / 1_048_576:.1f} of {total / 1_048_576:.1f} MiB"
                    )
                continue
            self.busy = False
            self.can_cancel = False
            self.progress.stop()
            self.progress.grid_remove()
            self.secondary.configure(text="Close", state="normal")
            if kind == "error":
                self.heading.set("Documents needs attention")
                self.message.set(str(value))
                self.detail.set(
                    "Check the message above and retry using the same location."
                )
                self.primary.configure(
                    text="Try again",
                    state="normal",
                    command=lambda: self._background("probe", self._probe),
                )
            elif kind == "start":
                if webbrowser.open(str(value)):
                    self.heading.set("Documents is running")
                    self.message.set(
                        "The app is open in your browser. Closing this window leaves Documents running."
                    )
                else:
                    self.message.set("Open this address in your browser:")
                self.detail.set(str(value))
                self.primary.configure(
                    text="Open Documents",
                    state="normal",
                    command=lambda url=str(value): webbrowser.open(url),
                )
            elif value in {"install", "cancelled"}:
                self._show_install()
                if value == "cancelled":
                    self.detail.set(
                        "Setup was cancelled. You can resume using the same location."
                    )
            elif value == "account":
                self._show_account()
            elif value == "ready":
                self.heading.set("Opening Documents")
                self.message.set("Starting or reconnecting to your local application…")
                self.detail.set(
                    "Activate Document Processor, Translator and Sentence Segmenter in the browser's Settings when you need them."
                )
                self._background("start", self._start)
        self.root.after(80, self._drain)

    def close(self) -> None:
        if self.busy:
            if self.can_cancel:
                self.cancel.set()
                self.message.set("Cancelling setup…")
                self.detail.set(
                    "Waiting for the installer to finish its current safe step."
                )
            return
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carlina Documents setup")
    parser.add_argument("--program-root", type=Path)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--diagnostics", type=Path)
    arguments = parser.parse_args(argv)
    appdir = Path(__file__).resolve().parent
    try:
        configuration = load_configuration(appdir)
        if arguments.diagnostics:
            with arguments.diagnostics.open("x", encoding="utf-8") as output:
                json.dump(
                    {
                        "product": "carlina-documents",
                        "version": configuration["version"],
                        "source_revision": configuration["source_revision"],
                        "platform": "win-64" if _is_windows() else "linux-64",
                        "frozen": bool(getattr(sys, "frozen", False)),
                        "python": sys.version.split()[0],
                        "tcl": tk.TclVersion,
                        "tk": tk.TkVersion,
                        "tcl_patchlevel": str(tk.Tcl().eval("info patchlevel")),
                        "bundled_installer_verified": True,
                    },
                    output,
                    indent=2,
                )
            return 0
        if arguments.version:
            if sys.stdout is not None:
                print(f"Carlina Documents {configuration['version']}")
            return 0
        root = tk.Tk(className="CarlinaDocuments")
        if _is_windows():
            iconbitmap = cast(Callable[..., str], root.iconbitmap)
            iconbitmap(default=str(appdir / "carlina-documents.ico"))
        program_root = arguments.program_root or Path(
            os.environ.get(
                "CARLINA_PROGRAM_ROOT",
                str(
                    Path(
                        os.environ.get(
                            "LOCALAPPDATA", str(Path.home() / "AppData/Local")
                        )
                    )
                    / "Programs/CarlinaDocuments"
                    if _is_windows()
                    else Path.home() / ".local/lib/carlina-documents"
                ),
            )
        )
        setup = DocumentsSetup(root, appdir, program_root.expanduser().absolute())
        try:
            root.mainloop()
        finally:
            setup.cancel.set()
            if setup.worker is not None:
                setup.worker.join()
        return 0
    except LauncherError, OSError, ValueError, tk.TclError:
        message = "Documents could not open its setup window. Download the installer again or use the setup guide."
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif _is_windows() and not arguments.version and not arguments.diagnostics:
            ctypes.windll.user32.MessageBoxW(None, message, "Carlina Documents", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
