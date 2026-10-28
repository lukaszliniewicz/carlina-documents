"""Native Framework compilation must work independently of the temp code page."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from carlina.core.native_preparation_windows import (
    render_powershell_preparation_helpers,
)

pytestmark = [
    pytest.mark.fast,
    pytest.mark.contract,
    pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell"),
]


def _ps_string(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"


def _compiler_probe_script(helper: str, work: str, *, invalid: bool) -> str:
    source = (
        "public static class CarlinaCompilerContract { invalid syntax }"
        if invalid
        else "public static class CarlinaCompilerContract { "
        "public static int Value() { return 17; } }"
    )
    return f"""
$ErrorActionPreference = 'Stop'
. ({_ps_string(helper)})
$work = {_ps_string(work)}
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetOwner($owner)
$acl.SetAccessRuleProtection($true,$false)
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
foreach ($sid in @($owner, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {{
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $sid,'FullControl',$inherit,'None','Allow'))
}}
[IO.Directory]::SetAccessControl($work,$acl)
$invalid = {"$true" if invalid else "$false"}
$compiled = $false
try {{
    $bytes = Get-CarlinaPreparationNativeAssembly -NativeSource '{source}' -Work $work
    if ($invalid) {{ throw 'Invalid source was accepted.' }}
    if ($bytes.GetType().FullName -cne 'System.Byte[]') {{ throw 'Assembly bytes were enumerated.' }}
    $assembly = [Reflection.Assembly]::Load($bytes)
    $type = $assembly.GetType('CarlinaCompilerContract',$true,$false)
    if ($type.GetMethod('Value').Invoke($null,@()) -ne 17) {{ throw 'Wrong compiled method.' }}
    $compiled = $true
}} catch {{
    if (-not $invalid -or $_.Exception.Message -cne 'Native preparation supervision is unavailable.') {{ throw }}
}}
$responses = @(Get-ChildItem -LiteralPath $work -Filter '*.rsp' -File)
if ($responses.Count -ne 1) {{ throw 'Response evidence missing.' }}
$responseBytes = [IO.File]::ReadAllBytes($responses[0].FullName)
if ($responseBytes.Length -lt 3 -or $responseBytes[0] -ne 239 -or
    $responseBytes[1] -ne 187 -or $responseBytes[2] -ne 191) {{ throw 'Response is not UTF8 BOM.' }}
if ($invalid) {{
    $logs = @(Get-ChildItem -LiteralPath $work -Filter '*.log' -File)
    if ($logs.Count -ne 2 -or ($logs | Measure-Object -Property Length -Sum).Sum -le 0) {{
        throw 'Compiler diagnostics were not retained.'
    }}
}}
[Console]::WriteLine((@{{compiled=$compiled;invalid=$invalid}} | ConvertTo-Json -Compress))
"""


@pytest.mark.parametrize("unicode_temp", [False, True])
@pytest.mark.parametrize("invalid", [False, True])
def test_native_compiler_preserves_private_evidence_with_unicode_temp(
    tmp_path: Path, *, unicode_temp: bool, invalid: bool
) -> None:
    helper = tmp_path / "helpers.ps1"
    helper.write_text(render_powershell_preparation_helpers(), encoding="utf-8")
    work = tmp_path / "Work Ω"
    work.mkdir()
    witness = work / "foreign-marker.bin"
    witness.write_bytes(b"preserve unrelated evidence\n")
    temp = tmp_path / ("Temp Ω" if unicode_temp else "Temp A")
    temp.mkdir()
    environment = dict(os.environ, TEMP=str(temp), TMP=str(temp))
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = _compiler_probe_script(str(helper), str(work), invalid=invalid)
    result = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"compiled": not invalid, "invalid": invalid}
    assert witness.read_bytes() == b"preserve unrelated evidence\n"
