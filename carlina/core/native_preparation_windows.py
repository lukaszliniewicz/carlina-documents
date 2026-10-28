"""Windows PowerShell helpers for supervised native preparation."""

from __future__ import annotations

from .native_preparation import (
    MAX_PREPARATION_BYTES,
    MAX_PROGRAM_ROOT_BYTES,
    PREPARATION_HEADER,
    PREPARATION_NAME,
    PREPARATION_RETIREMENT_DIRNAME,
)


def render_powershell_preparation_helpers() -> str:
    """Render helper functions for Windows native preparation callers."""

    helpers = r"""
function Test-CarlinaPreparationPathChain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.IndexOf([char]0) -ge 0) {
        throw 'Native preparation context is invalid.'
    }
    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($fullPath)
        if ([string]::IsNullOrEmpty($root)) {
            throw 'Native preparation context is invalid.'
        }
        $current = $root
        $attributes = [IO.File]::GetAttributes($current)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Native preparation context is invalid.'
        }
        $relative = $fullPath.Substring($root.Length)
        $parts = $relative.Split([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar), [StringSplitOptions]::RemoveEmptyEntries)
        foreach ($part in $parts) {
            $current = [IO.Path]::Combine($current, $part)
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Native preparation context is invalid.'
            }
        }
        $isDirectory = (($attributes -band [IO.FileAttributes]::Directory) -ne 0)
        if ($Directory -and -not $isDirectory) {
            throw 'Native preparation context is invalid.'
        }
        if (-not $Directory -and $isDirectory) {
            throw 'Native preparation context is invalid.'
        }
        return $fullPath
    }
    catch {
        throw 'Native preparation context is invalid.'
    }
}

function Read-CarlinaPreparationSafeFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$MaximumBytes
    )

    try {
        $fullPath = Test-CarlinaPreparationPathChain -Path $Path
        $stream = [IO.FileStream]::new(
            $fullPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        try {
            if ($stream.Length -gt $MaximumBytes) {
                throw 'Native preparation completion is invalid.'
            }
            [byte[]]$buffer = [Array]::CreateInstance([byte], [int]$stream.Length)
            $offset = 0
            while ($offset -lt $buffer.Length) {
                $read = $stream.Read($buffer, $offset, $buffer.Length - $offset)
                if ($read -le 0) {
                    throw 'Native preparation completion is invalid.'
                }
                $offset += $read
            }
            if ($stream.ReadByte() -ne -1) {
                throw 'Native preparation completion is invalid.'
            }
            return ,$buffer
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        throw 'Native preparation completion is invalid.'
    }
}

function ConvertTo-CarlinaPreparationCanonicalPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    while ($fullPath.Length -gt $root.Length -and
        (($fullPath[$fullPath.Length - 1] -eq [IO.Path]::DirectorySeparatorChar) -or
         ($fullPath[$fullPath.Length - 1] -eq [IO.Path]::AltDirectorySeparatorChar))) {
        $fullPath = $fullPath.Substring(0, $fullPath.Length - 1)
    }
    return $fullPath
}

function Get-CarlinaPreparationNativeAssembly {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$NativeSource,
        [Parameter(Mandatory = $true)][string]$Work
    )

    $process = $null
    $processStarted = $false
    $stdoutTask = $null
    $stderrTask = $null

    try {
        try {
            if ([string]::IsNullOrEmpty($NativeSource)) {
                throw 'Native preparation supervision is unavailable.'
            }
            $workPath = Test-CarlinaPreparationPathChain -Path $Work -Directory
            $compilerPath = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
            $compilerPath = Test-CarlinaPreparationPathChain -Path $compilerPath

            $token = [Guid]::NewGuid().ToString('N')
            $sourcePath = Join-Path $workPath ('c-{0}.cs' -f $token)
            $responsePath = Join-Path $workPath ('c-{0}.rsp' -f $token)
            $assemblyPath = Join-Path $workPath ('c-{0}.dll' -f $token)
            $stdoutPath = Join-Path $workPath ('c-{0}.out.log' -f $token)
            $stderrPath = Join-Path $workPath ('c-{0}.err.log' -f $token)
            $targets = @($sourcePath, $responsePath, $assemblyPath, $stdoutPath, $stderrPath)

            foreach ($targetPath in $targets) {
                $targetName = [IO.Path]::GetFileName($targetPath)
                if ([IO.Directory]::GetFileSystemEntries($workPath, $targetName).Length -gt 0) {
                    throw 'Native preparation supervision is unavailable.'
                }
                try {
                    $null = [IO.File]::GetAttributes($targetPath)
                    throw 'Native preparation supervision is unavailable.'
                }
                catch [IO.FileNotFoundException] {
                }
                catch [IO.DirectoryNotFoundException] {
                }
            }

            $sourceStream = $null
            try {
                $sourceStream = [IO.FileStream]::new(
                    $sourcePath,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                $sourceBytes = [Text.UTF8Encoding]::new($false).GetBytes($NativeSource)
                $sourceStream.Write($sourceBytes, 0, $sourceBytes.Length)
                $sourceStream.Flush()
            }
            finally {
                if ($null -ne $sourceStream) {
                    $sourceStream.Dispose()
                }
            }

            $responseText = '/nologo /target:library /optimize+ /debug- /out:"{0}" "{1}"' -f $assemblyPath, $sourcePath
            $responseEncoding = [Text.UTF8Encoding]::new($true)
            $responsePreamble = $responseEncoding.GetPreamble()
            $responseBytes = $responseEncoding.GetBytes($responseText)
            $responseStream = $null
            try {
                $responseStream = [IO.FileStream]::new(
                    $responsePath,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                $responseStream.Write($responsePreamble, 0, $responsePreamble.Length)
                $responseStream.Write($responseBytes, 0, $responseBytes.Length)
                $responseStream.Flush()
            }
            finally {
                if ($null -ne $responseStream) {
                    $responseStream.Dispose()
                }
            }

            $startInfo = [Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $compilerPath
            $startInfo.Arguments = '@"' + $responsePath + '"'
            $startInfo.WorkingDirectory = $workPath
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $process = [Diagnostics.Process]::new()
            $process.StartInfo = $startInfo
            $processStarted = $process.Start()
            if (-not $processStarted) {
                throw 'Native preparation supervision is unavailable.'
            }

            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            $process.WaitForExit()
            $stdout = $stdoutTask.GetAwaiter().GetResult()
            $stderr = $stderrTask.GetAwaiter().GetResult()

            $stdoutEncoding = [Text.UTF8Encoding]::new($false)
            $stdoutBytes = $stdoutEncoding.GetBytes($stdout)
            $stdoutStream = $null
            try {
                $stdoutStream = [IO.FileStream]::new(
                    $stdoutPath,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                $stdoutStream.Write($stdoutBytes, 0, $stdoutBytes.Length)
                $stdoutStream.Flush()
            }
            finally {
                if ($null -ne $stdoutStream) {
                    $stdoutStream.Dispose()
                }
            }

            $stderrBytes = $stdoutEncoding.GetBytes($stderr)
            $stderrStream = $null
            try {
                $stderrStream = [IO.FileStream]::new(
                    $stderrPath,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                $stderrStream.Write($stderrBytes, 0, $stderrBytes.Length)
                $stderrStream.Flush()
            }
            finally {
                if ($null -ne $stderrStream) {
                    $stderrStream.Dispose()
                }
            }

            if ($process.ExitCode -ne 0) {
                throw 'Native preparation supervision is unavailable.'
            }
            [byte[]]$assemblyBytes = Read-CarlinaPreparationSafeFile -Path $assemblyPath -MaximumBytes 1048576
            if ($assemblyBytes.Length -le 0) {
                throw 'Native preparation supervision is unavailable.'
            }
            return ,$assemblyBytes
        }
        catch {
            throw 'Native preparation supervision is unavailable.'
        }
        finally {
            if ($processStarted -and $null -ne $process) {
                try {
                    $process.WaitForExit()
                }
                catch {
                }
                if ($null -ne $stdoutTask) {
                    try {
                        $null = $stdoutTask.GetAwaiter().GetResult()
                    }
                    catch {
                    }
                }
                if ($null -ne $stderrTask) {
                    try {
                        $null = $stderrTask.GetAwaiter().GetResult()
                    }
                    catch {
                    }
                }
            }
            if ($null -ne $process) {
                try {
                    $process.Dispose()
                }
                catch {
                }
            }
        }
    }
    catch {
        throw 'Native preparation supervision is unavailable.'
    }
}

function Start-PreparationCustodian {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][IO.FileStream]$Lease,
        [Parameter(Mandatory = $true)][string]$Work,
        [Parameter(Mandatory = $true)][string]$ProgramRoot,
        [Parameter(Mandatory = $true)][string]$VersionDir,
        [Parameter(Mandatory = $true)][string]$ClaimId,
        [Parameter(Mandatory = $true)][string]$IdentitySha256,
        [Parameter(Mandatory = $true)][string]$Unpacker,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$ExtractTemp,
        [switch]$PublisherProgress,
        [string]$CancelFile = ''
    )

    if (($PSVersionTable.PSVersion -lt [version]'5.1') -or
        ($PSVersionTable.PSEdition -ne 'Desktop') -or
        (-not [Environment]::Is64BitProcess) -or
        ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT)) {
        throw 'Native preparation supervision is unavailable.'
    }
    if ($null -eq $Lease -or $Lease.SafeFileHandle.IsClosed -or $Lease.SafeFileHandle.IsInvalid) {
        throw 'Native preparation context is invalid.'
    }

    try {
        $root = ConvertTo-CarlinaPreparationCanonicalPath -Path $ProgramRoot
        $work = ConvertTo-CarlinaPreparationCanonicalPath -Path $Work
        $version = ConvertTo-CarlinaPreparationCanonicalPath -Path $VersionDir
        $unpacker = ConvertTo-CarlinaPreparationCanonicalPath -Path $Unpacker
        $archive = ConvertTo-CarlinaPreparationCanonicalPath -Path $Archive
        $extract = ConvertTo-CarlinaPreparationCanonicalPath -Path $ExtractTemp
        $cancel = ''
        if (-not [string]::IsNullOrEmpty($CancelFile)) {
            if (-not [IO.Path]::IsPathRooted($CancelFile) -or
                $CancelFile.IndexOf([char]0) -ge 0) {
                throw 'Native preparation context is invalid.'
            }
            $cancel = ConvertTo-CarlinaPreparationCanonicalPath -Path $CancelFile
            if (-not [string]::Equals($cancel, $CancelFile, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Native preparation context is invalid.'
            }
        }
        if (-not [string]::Equals($root, $ProgramRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($work, $Work, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($version, $VersionDir, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($unpacker, $Unpacker, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($archive, $Archive, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($extract, $ExtractTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Native preparation context is invalid.'
        }
        $workName = [IO.Path]::GetFileName($work)
        $versionName = [IO.Path]::GetFileName($version)
        $expectedVersion = [IO.Path]::Combine([IO.Path]::Combine($root, 'versions'), $versionName)
        $workPrefix = $work + [IO.Path]::DirectorySeparatorChar
        $publisher = [bool]$PublisherProgress
        $publisherPathsValid = $false
        if ($publisher) {
            $expectedPublisher = ConvertTo-CarlinaPreparationCanonicalPath -Path (Join-Path $PSHOME 'powershell.exe')
            $publisherPathsValid =
                [string]::Equals($unpacker, $expectedPublisher, [StringComparison]::OrdinalIgnoreCase) -and
                -not $unpacker.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase) -and
                [IO.Path]::GetExtension($archive) -ieq '.ps1'
        }
        else {
            $publisherPathsValid = $cancel -eq ''
        }
        if (-not $workName.StartsWith('.carlina-install-', [StringComparison]::Ordinal) -or
            $workName.Length -le '.carlina-install-'.Length -or
            -not [string]::Equals([IO.Path]::GetDirectoryName($work), $root, [StringComparison]::OrdinalIgnoreCase) -or
            $versionName -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}$' -or
            $versionName.EndsWith('.') -or
            -not [string]::Equals($version, $expectedVersion, [StringComparison]::OrdinalIgnoreCase) -or
            (-not $publisher -and -not $unpacker.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
            -not $archive.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not $extract.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not $publisherPathsValid -or
            $ClaimId -cnotmatch '^[0-9a-f]{32}$' -or
            $IdentitySha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Native preparation context is invalid.'
        }
        $null = Test-CarlinaPreparationPathChain -Path $root -Directory
        $null = Test-CarlinaPreparationPathChain -Path $work -Directory
        $null = Test-CarlinaPreparationPathChain -Path $version -Directory
        $null = Test-CarlinaPreparationPathChain -Path $unpacker
        $null = Test-CarlinaPreparationPathChain -Path $archive
        $null = Test-CarlinaPreparationPathChain -Path $extract -Directory
    }
    catch {
        throw 'Native preparation context is invalid.'
    }

    # The detached process reads executable source and context from this owned
    # scratch tree. Do not inherit write access granted to other local users.
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $privateAcl = [Security.AccessControl.DirectorySecurity]::new()
    $privateAcl.SetOwner($owner)
    $privateAcl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    foreach ($principal in @($owner,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $principal, [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance, [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $privateAcl.AddAccessRule($rule)
    }
    [IO.Directory]::SetAccessControl($work, $privateAcl)

    $nativeSource = @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace CarlinaPreparation
{
    public sealed class CustodianProcess : IDisposable
    {
        private IntPtr process;
        private bool disposed;
        private bool waited;

        internal CustodianProcess(IntPtr processHandle)
        {
            if (processHandle == IntPtr.Zero || processHandle == new IntPtr(-1))
                throw new InvalidOperationException("Native preparation supervision is unavailable.");
            process = processHandle;
        }

        public bool WaitForExit(int milliseconds)
        {
            if (disposed || process == IntPtr.Zero)
                throw new InvalidOperationException("Native preparation supervision is unavailable.");
            if (milliseconds < -1)
                throw new InvalidOperationException("Native preparation supervision is unavailable.");
            uint timeout = milliseconds == -1 ? 0xffffffffU : (uint)milliseconds;
            uint result = Native.WaitForSingleObject(process, timeout);
            if (result == Native.WaitObject0Value)
            {
                waited = true;
                return true;
            }
            if (result == Native.WaitTimeoutValue)
                return false;
            throw new InvalidOperationException("Native preparation supervision is unavailable.");
        }

        public UInt32 ExitCode
        {
            get
            {
                if (disposed || process == IntPtr.Zero)
                    throw new InvalidOperationException("Native preparation supervision is unavailable.");
                if (!waited)
                    throw new InvalidOperationException("Native preparation did not complete.");
                uint exitCode;
                if (!Native.GetExitCodeProcess(process, out exitCode))
                    throw new InvalidOperationException("Native preparation supervision is unavailable.");
                return exitCode;
            }
        }

        public void Dispose()
        {
            if (!disposed)
            {
                disposed = true;
                IntPtr handle = process;
                process = IntPtr.Zero;
                if (handle != IntPtr.Zero)
                    Native.CloseHandle(handle);
            }
            GC.SuppressFinalize(this);
        }
    }

    public static class Native
    {
        private const uint CreateNoWindow = 0x08000000;
        private const uint CreateNewProcessGroup = 0x00000200;
        private const uint ExtendedStartupInfoPresent = 0x00080000;
        private const uint ProcThreadAttributeHandleList = 0x00020002;
        private const uint ProcThreadAttributeJobList = 0x0002000d;
        private const uint DuplicateSameAccess = 0x00000002;
        private const uint GenericRead = 0x80000000;
        private const uint GenericWrite = 0x40000000;
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint OpenExisting = 3;
        private const uint CreateNew = 1;
        private const uint FileAttributeNormal = 0x00000080;
        private const uint FileFlagOpenReparsePoint = 0x00200000;
        private const uint FileFlagSequentialScan = 0x08000000;
        private const uint FileAttributeDirectory = 0x00000010;
        private const uint FileAttributeReparsePoint = 0x00000400;
        private const uint StartfUseStdHandles = 0x00000100;
        private const int JobObjectBasicAccountingInformation = 1;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const uint WaitObject0 = 0x00000000;
        private const uint WaitTimeout = 0x00000102;
        private const uint Infinite = 0xffffffff;
        private const int ErrorInsufficientBuffer = 122;
        private const int MaxContextBytes = 65536;
        private const int MaxProgramRootBytes = @@MAX_PROGRAM_ROOT_BYTES@@;
        private const string ClaimName = ".carlina-bootstrap-claim";
        private const string ClaimHeader = "carlina.bootstrap-claim/1";

        internal static readonly uint WaitObject0Value = WaitObject0;
        internal static readonly uint WaitTimeoutValue = WaitTimeout;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfoEx
        {
            public StartupInfo StartupInfo;
            public IntPtr lpAttributeList;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int nLength;
            public IntPtr lpSecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            public bool bInheritHandle;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation
        {
            public uint dwFileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftCreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftLastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftLastWriteTime;
            public uint dwVolumeSerialNumber;
            public uint nFileSizeHigh;
            public uint nFileSizeLow;
            public uint nNumberOfLinks;
            public uint nFileIndexHigh;
            public uint nFileIndexLow;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobBasicAccountingInformation
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobBasicLimitInformation
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobExtendedLimitInformation
        {
            public JobBasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct RtlOsVersionInfo
        {
            public uint dwOSVersionInfoSize;
            public uint dwMajorVersion;
            public uint dwMinorVersion;
            public uint dwBuildNumber;
            public uint dwPlatformId;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string szCSDVersion;
        }

        private sealed class PreparationContext
        {
            public string LeaseHandle;
            public string Work;
            public string ProgramRoot;
            public string VersionDir;
            public string ClaimId;
            public string IdentitySha256;
            public string Unpacker;
            public string Archive;
            public string ExtractTemp;
            public string PreparationKind = "bundle";
            public string CancelFile = "";
        }

        private sealed class NativeChild
        {
            public IntPtr Process;
        }

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DuplicateHandle(
            IntPtr hSourceProcessHandle,
            IntPtr hSourceHandle,
            IntPtr hTargetProcessHandle,
            out IntPtr lpTargetHandle,
            uint dwDesiredAccess,
            [MarshalAs(UnmanagedType.Bool)] bool bInheritHandle,
            uint dwOptions);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateFileW")]
        private static extern SafeFileHandle CreateFileReadW(
            string lpFileName,
            uint dwDesiredAccess,
            uint dwShareMode,
            IntPtr lpSecurityAttributes,
            uint dwCreationDisposition,
            uint dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateFileW")]
        private static extern SafeFileHandle CreateFileInheritedW(
            string lpFileName,
            uint dwDesiredAccess,
            uint dwShareMode,
            ref SecurityAttributes lpSecurityAttributes,
            uint dwCreationDisposition,
            uint dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle hFile,
            out ByHandleFileInformation lpFileInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileSizeEx(SafeFileHandle hFile, out long lpFileSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "GetFinalPathNameByHandleW")]
        private static extern uint GetFinalPathNameByHandleW(
            SafeFileHandle hFile,
            StringBuilder lpszFilePath,
            uint cchFilePath,
            uint dwFlags);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ReadFile(
            SafeFileHandle hFile,
            [Out] byte[] lpBuffer,
            uint nNumberOfBytesToRead,
            out uint lpNumberOfBytesRead,
            IntPtr lpOverlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool InitializeProcThreadAttributeList(
            IntPtr lpAttributeList,
            int dwAttributeCount,
            int dwFlags,
            ref IntPtr lpSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool UpdateProcThreadAttribute(
            IntPtr lpAttributeList,
            uint dwFlags,
            IntPtr attribute,
            IntPtr lpValue,
            IntPtr cbSize,
            IntPtr lpPreviousValue,
            IntPtr lpReturnSize);

        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateProcessW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcessExW(
            string lpApplicationName,
            StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
            uint dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref StartupInfoEx lpStartupInfo,
            out ProcessInformation lpProcessInformation);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CreateJobObjectW")]
        private static extern IntPtr CreateJobObjectW(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            IntPtr hJob,
            int JobObjectInfoClass,
            out JobBasicAccountingInformation lpJobObjectInfo,
            uint cbJobObjectInfoLength,
            out uint lpReturnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

        [DllImport("ntdll.dll")]
        private static extern int RtlGetVersion(ref RtlOsVersionInfo versionInfo);

        private static InvalidOperationException ContextError()
        {
            return new InvalidOperationException("Native preparation context is invalid.");
        }

        private static InvalidOperationException UnavailableError()
        {
            return new InvalidOperationException("Native preparation supervision is unavailable.");
        }

        private static InvalidOperationException DidNotCompleteError()
        {
            return new InvalidOperationException("Native preparation did not complete.");
        }

        private static void RequireWindowsPlatform()
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT ||
                !Environment.Is64BitOperatingSystem || !Environment.Is64BitProcess)
                throw UnavailableError();
            RtlOsVersionInfo version = new RtlOsVersionInfo();
            version.dwOSVersionInfoSize = (uint)Marshal.SizeOf(typeof(RtlOsVersionInfo));
            version.szCSDVersion = String.Empty;
            if (RtlGetVersion(ref version) != 0 || version.dwMajorVersion < 10)
                throw UnavailableError();
        }

        private static string FullPath(string path)
        {
            if (String.IsNullOrEmpty(path) || path.IndexOf('\0') >= 0)
                throw ContextError();
            string full;
            try
            {
                full = Path.GetFullPath(path);
            }
            catch
            {
                throw ContextError();
            }
            if (full.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
                full.StartsWith("\\\\.\\", StringComparison.Ordinal))
                throw ContextError();
            string root = Path.GetPathRoot(full);
            while (full.Length > root.Length &&
                (full[full.Length - 1] == '\\' || full[full.Length - 1] == '/'))
                full = full.Substring(0, full.Length - 1);
            return full;
        }

        private static bool SamePath(string left, string right)
        {
            return String.Equals(left, right, StringComparison.OrdinalIgnoreCase);
        }

        private static string CurrentExecutable()
        {
            try
            {
                using (Process current = Process.GetCurrentProcess())
                {
                    ProcessModule module = current.MainModule;
                    if (module == null || String.IsNullOrEmpty(module.FileName))
                        throw ContextError();
                    return FullPath(module.FileName);
                }
            }
            catch (InvalidOperationException)
            {
                throw;
            }
            catch
            {
                throw ContextError();
            }
        }

        private static bool IsWithin(string directory, string path)
        {
            string prefix = directory.EndsWith("\\", StringComparison.Ordinal) ? directory : directory + "\\";
            return path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
        }

        private static void EnsureNoReparseChain(string path, bool requireDirectory)
        {
            string full = FullPath(path);
            string root = Path.GetPathRoot(full);
            if (String.IsNullOrEmpty(root))
                throw ContextError();
            string current = root;
            try
            {
                FileAttributes attributes = File.GetAttributes(current);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw ContextError();
                string remainder = full.Substring(root.Length);
                string[] parts = remainder.Split(new char[] { '\\', '/' }, StringSplitOptions.RemoveEmptyEntries);
                for (int index = 0; index < parts.Length; index++)
                {
                    current = Path.Combine(current, parts[index]);
                    attributes = File.GetAttributes(current);
                    if ((attributes & FileAttributes.ReparsePoint) != 0)
                        throw ContextError();
                    if (index < parts.Length - 1 && (attributes & FileAttributes.Directory) == 0)
                        throw ContextError();
                }
                bool isDirectory = (attributes & FileAttributes.Directory) != 0;
                if (isDirectory != requireDirectory)
                    throw ContextError();
            }
            catch (InvalidOperationException)
            {
                throw;
            }
            catch
            {
                throw ContextError();
            }
        }

        private static string NormalizeFinalPath(string path)
        {
            if (path.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase))
                return "\\\\" + path.Substring(8);
            if (path.StartsWith("\\\\?\\", StringComparison.OrdinalIgnoreCase))
                return path.Substring(4);
            if (path.StartsWith("\\??\\", StringComparison.OrdinalIgnoreCase))
                return path.Substring(4);
            return path;
        }

        private static string FinalPath(SafeFileHandle handle)
        {
            StringBuilder path = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0);
            if (length == 0 || length >= path.Capacity)
                throw UnavailableError();
            return FullPath(NormalizeFinalPath(path.ToString()));
        }

        private static void VerifyHandlePath(SafeFileHandle handle, string expectedPath, bool regularFile)
        {
            ByHandleFileInformation information;
            if (!GetFileInformationByHandle(handle, out information))
                throw UnavailableError();
            if ((information.dwFileAttributes & FileAttributeReparsePoint) != 0 ||
                (regularFile && (information.dwFileAttributes & FileAttributeDirectory) != 0))
                throw ContextError();
            if (!SamePath(FinalPath(handle), FullPath(expectedPath)))
                throw ContextError();
        }

        private static SafeFileHandle OpenRegularRead(string path, int maximumBytes)
        {
            EnsureNoReparseChain(path, false);
            SafeFileHandle handle = CreateFileReadW(
                path,
                GenericRead,
                FileShareRead,
                IntPtr.Zero,
                OpenExisting,
                FileFlagOpenReparsePoint | FileFlagSequentialScan,
                IntPtr.Zero);
            if (handle == null || handle.IsInvalid)
                throw ContextError();
            try
            {
                VerifyHandlePath(handle, path, true);
                long size;
                if (!GetFileSizeEx(handle, out size) || size < 0 || size > maximumBytes)
                    throw ContextError();
                return handle;
            }
            catch
            {
                handle.Dispose();
                throw;
            }
        }

        private static byte[] ReadBounded(SafeFileHandle handle, int maximumBytes)
        {
            long size;
            if (!GetFileSizeEx(handle, out size) || size < 0 || size > maximumBytes)
                throw ContextError();
            byte[] data = new byte[(int)size];
            int offset = 0;
            while (offset < data.Length)
            {
                byte[] chunk = new byte[data.Length - offset];
                uint read;
                if (!ReadFile(handle, chunk, (uint)chunk.Length, out read, IntPtr.Zero) || read == 0)
                    throw ContextError();
                Buffer.BlockCopy(chunk, 0, data, offset, (int)read);
                offset += (int)read;
            }
            byte[] extra = new byte[1];
            uint extraRead;
            if (!ReadFile(handle, extra, 1, out extraRead, IntPtr.Zero) || extraRead != 0)
                throw ContextError();
            return data;
        }

        private static byte[] ReadFileBytes(string path, int maximumBytes)
        {
            using (SafeFileHandle handle = OpenRegularRead(path, maximumBytes))
                return ReadBounded(handle, maximumBytes);
        }

        private static bool TryProgressInteger(string value, bool positive, out long parsed)
        {
            parsed = 0;
            if (String.IsNullOrEmpty(value) || value.Length > 15)
                return false;
            if (value == "0")
                return !positive;
            if (value[0] < '1' || value[0] > '9')
                return false;
            for (int index = 1; index < value.Length; index++)
                if (value[index] < '0' || value[index] > '9')
                    return false;
            if (!Int64.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out parsed))
                return false;
            return !positive || parsed > 0;
        }

        private static void ValidatePublisherProgressLine(
            string line, uint nativeExit, ref bool ready, ref bool result)
        {
            if (result)
                throw DidNotCompleteError();
            string[] fields = line.Split(new char[] { ' ' }, StringSplitOptions.None);
            for (int index = 0; index < fields.Length; index++)
                if (fields[index].Length == 0)
                    throw DidNotCompleteError();
            if (fields.Length < 2 || fields[0] != "carlina.publisher-bootstrap/1")
                throw DidNotCompleteError();
            if (!ready)
            {
                if (fields.Length != 2 || fields[1] != "ready")
                    throw DidNotCompleteError();
                ready = true;
                return;
            }
            if (fields[1] == "phase")
            {
                if (fields.Length != 6 ||
                    (fields[2] != "preflight" && fields[2] != "download" &&
                     fields[2] != "verification" && fields[2] != "unpacking" &&
                     fields[2] != "environment") ||
                    (fields[3] != "true" && fields[3] != "false"))
                    throw DidNotCompleteError();
                if (fields[4] == "null" && fields[5] == "null")
                    return;
                long completed;
                long total;
                if (fields[2] != "download" ||
                    !TryProgressInteger(fields[4], false, out completed) ||
                    !TryProgressInteger(fields[5], true, out total) || completed > total)
                    throw DidNotCompleteError();
                return;
            }
            if (fields[1] != "result" || fields.Length != 5)
                throw DidNotCompleteError();
            if (fields[2] == "succeeded" && fields[3] == "null" && fields[4] == "false")
            {
                if (nativeExit != 0)
                    throw DidNotCompleteError();
            }
            else if (fields[2] == "cancelled" && fields[3] == "cancelled" && fields[4] == "true")
            {
                if (nativeExit != 130)
                    throw DidNotCompleteError();
            }
            else if (fields[2] == "failed" && fields[4] == "true" && fields[3] == "download_failed")
            {
                if (nativeExit == 0)
                    throw DidNotCompleteError();
            }
            else if (fields[2] == "failed" && fields[4] == "false" &&
                (fields[3] == "verification_failed" || fields[3] == "operation_failed"))
            {
                if (nativeExit == 0)
                    throw DidNotCompleteError();
            }
            else
                throw DidNotCompleteError();
            result = true;
        }

        private static void ValidatePublisherProgress(string work, uint nativeExit)
        {
            string path = Path.Combine(work, "preparation-native.stdout");
            EnsureNoReparseChain(work, true);
            EnsureNoReparseChain(path, false);
            SafeFileHandle handle = CreateFileReadW(
                path, GenericRead, FileShareRead, IntPtr.Zero, OpenExisting,
                FileFlagOpenReparsePoint | FileFlagSequentialScan, IntPtr.Zero);
            if (handle == null || handle.IsInvalid)
            {
                if (handle != null)
                    handle.Dispose();
                throw DidNotCompleteError();
            }
            using (handle)
            {
                VerifyHandlePath(handle, path, true);
                byte[] buffer = new byte[4096];
                StringBuilder line = new StringBuilder();
                bool sawByte = false;
                bool pendingCarriageReturn = false;
                bool ready = false;
                bool result = false;
                while (true)
                {
                    uint read;
                    if (!ReadFile(handle, buffer, (uint)buffer.Length, out read, IntPtr.Zero))
                        throw DidNotCompleteError();
                    if (read == 0)
                        break;
                    for (int index = 0; index < read; index++)
                    {
                        byte value = buffer[index];
                        sawByte = true;
                        if (pendingCarriageReturn)
                        {
                            if (value != 10)
                                throw DidNotCompleteError();
                            pendingCarriageReturn = false;
                            ValidatePublisherProgressLine(line.ToString(), nativeExit, ref ready, ref result);
                            line.Length = 0;
                            continue;
                        }
                        if (value == 10)
                        {
                            ValidatePublisherProgressLine(line.ToString(), nativeExit, ref ready, ref result);
                            line.Length = 0;
                            continue;
                        }
                        if (value == 13)
                        {
                            pendingCarriageReturn = true;
                            continue;
                        }
                        if (value < 32 || value > 126 || line.Length >= 4096)
                            throw DidNotCompleteError();
                        line.Append((char)value);
                    }
                }
                if (!sawByte || pendingCarriageReturn || line.Length != 0 || !ready || !result)
                    throw DidNotCompleteError();
            }
        }

        private static void RequireCanonicalIdentity(string claimId, string identity)
        {
            if (claimId == null || claimId.Length != 32 ||
                identity == null || identity.Length != 64)
                throw ContextError();
            for (int index = 0; index < claimId.Length; index++)
                if (!((claimId[index] >= '0' && claimId[index] <= '9') ||
                    (claimId[index] >= 'a' && claimId[index] <= 'f')))
                    throw ContextError();
            for (int index = 0; index < identity.Length; index++)
                if (!((identity[index] >= '0' && identity[index] <= '9') ||
                    (identity[index] >= 'a' && identity[index] <= 'f')))
                    throw ContextError();
        }

        private static string CanonicalDirectoryName(string path)
        {
            string name = Path.GetFileName(path);
            if (String.IsNullOrEmpty(name) || name.Length > 80 ||
                !IsAsciiAlphaNumeric(name[0]) || name[name.Length - 1] == '.' || name[name.Length - 1] == ' ')
                throw ContextError();
            for (int index = 0; index < name.Length; index++)
            {
                char value = name[index];
                if (!IsAsciiAlphaNumeric(value) && value != '.' && value != '_' && value != '-' && value != '+' && value != '!')
                    throw ContextError();
            }
            return name;
        }

        private static bool IsAsciiAlphaNumeric(char value)
        {
            return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
                (value >= '0' && value <= '9');
        }

        private static PreparationContext ValidatePaths(PreparationContext context)
        {
            RequireCanonicalIdentity(context.ClaimId, context.IdentitySha256);
            string root = FullPath(context.ProgramRoot);
            string work = FullPath(context.Work);
            string version = FullPath(context.VersionDir);
            string unpacker = FullPath(context.Unpacker);
            string archive = FullPath(context.Archive);
            string extract = FullPath(context.ExtractTemp);
            if (context.PreparationKind != "bundle" && context.PreparationKind != "publisher")
                throw ContextError();
            if (context.CancelFile == null)
                throw ContextError();
            string cancelFile = String.Empty;
            if (context.CancelFile.Length != 0)
            {
                if (!Path.IsPathRooted(context.CancelFile))
                    throw ContextError();
                cancelFile = FullPath(context.CancelFile);
            }
            if (!SamePath(root, context.ProgramRoot) || !SamePath(work, context.Work) ||
                !SamePath(version, context.VersionDir) || !SamePath(unpacker, context.Unpacker) ||
                !SamePath(archive, context.Archive) || !SamePath(extract, context.ExtractTemp) ||
                !SamePath(cancelFile, context.CancelFile))
                throw ContextError();
            string workName = Path.GetFileName(work);
            if (workName == null || !workName.StartsWith(".carlina-install-", StringComparison.Ordinal) ||
                workName.Length <= ".carlina-install-".Length ||
                !SamePath(Path.GetDirectoryName(work), root))
                throw ContextError();
            string versionName = CanonicalDirectoryName(version);
            string expectedVersion = Path.Combine(Path.Combine(root, "versions"), versionName);
            bool publisher = context.PreparationKind == "publisher";
            if (!SamePath(version, expectedVersion) || !IsWithin(work, archive) || !IsWithin(work, extract) ||
                (publisher && (!String.Equals(Path.GetExtension(archive), ".ps1", StringComparison.OrdinalIgnoreCase) ||
                    IsWithin(work, unpacker) ||
                    !String.Equals(Path.GetFileName(unpacker), "powershell.exe", StringComparison.OrdinalIgnoreCase) ||
                    !SamePath(unpacker, CurrentExecutable()))) ||
                (!publisher && (!IsWithin(work, unpacker) || context.CancelFile.Length != 0)))
                throw ContextError();
            EnsureNoReparseChain(root, true);
            EnsureNoReparseChain(Path.Combine(root, "versions"), true);
            EnsureNoReparseChain(version, true);
            EnsureNoReparseChain(work, true);
            EnsureNoReparseChain(extract, true);
            EnsureNoReparseChain(unpacker, false);
            EnsureNoReparseChain(archive, false);
            EnsureNoReparseChain(Path.Combine(root, "state"), true);
            return context;
        }

        private static void VerifyLease(SafeFileHandle lease, string programRoot)
        {
            if (lease == null || lease.IsInvalid || lease.IsClosed)
                throw ContextError();
            string expected = Path.Combine(Path.Combine(programRoot, "state"), "managed-install.lock");
            VerifyHandlePath(lease, expected, true);
        }

        private static byte[] BuildJsonBytes(PreparationContext context)
        {
            StringBuilder json = new StringBuilder();
            json.Append('{');
            AppendJsonProperty(json, "LeaseHandle", context.LeaseHandle, false);
            AppendJsonProperty(json, "Work", context.Work, true);
            AppendJsonProperty(json, "ProgramRoot", context.ProgramRoot, true);
            AppendJsonProperty(json, "VersionDir", context.VersionDir, true);
            AppendJsonProperty(json, "ClaimId", context.ClaimId, true);
            AppendJsonProperty(json, "IdentitySha256", context.IdentitySha256, true);
            AppendJsonProperty(json, "Unpacker", context.Unpacker, true);
            AppendJsonProperty(json, "Archive", context.Archive, true);
            AppendJsonProperty(json, "ExtractTemp", context.ExtractTemp, true);
            AppendJsonProperty(json, "PreparationKind", context.PreparationKind, true);
            AppendJsonProperty(json, "CancelFile", context.CancelFile, true);
            json.Append('}');
            try
            {
                return new UTF8Encoding(false, true).GetBytes(json.ToString());
            }
            catch
            {
                throw ContextError();
            }
        }

        private static void AppendJsonProperty(StringBuilder json, string name, string value, bool comma)
        {
            if (comma)
                json.Append(',');
            AppendJsonString(json, name);
            json.Append(':');
            AppendJsonString(json, value);
        }

        private static void AppendJsonString(StringBuilder json, string value)
        {
            json.Append('"');
            for (int index = 0; index < value.Length; index++)
            {
                char item = value[index];
                if (item == '"')
                    json.Append("\\\"");
                else if (item == '\\')
                    json.Append("\\\\");
                else if (item < 0x20 || item > 0x7e)
                {
                    json.Append("\\u");
                    json.Append(((int)item).ToString("x4", CultureInfo.InvariantCulture));
                }
                else
                    json.Append(item);
            }
            json.Append('"');
        }

        private static string ReadJsonString(string text, ref int offset)
        {
            if (offset >= text.Length || text[offset] != '"')
                throw ContextError();
            offset++;
            StringBuilder value = new StringBuilder();
            while (offset < text.Length)
            {
                char item = text[offset++];
                if (item == '"')
                    return value.ToString();
                if (item < 0x20)
                    throw ContextError();
                if (item != '\\')
                {
                    value.Append(item);
                    continue;
                }
                if (offset >= text.Length)
                    throw ContextError();
                char escape = text[offset++];
                if (escape == '"' || escape == '\\' || escape == '/')
                    value.Append(escape);
                else if (escape == 'b')
                    value.Append('\b');
                else if (escape == 'f')
                    value.Append('\f');
                else if (escape == 'n')
                    value.Append('\n');
                else if (escape == 'r')
                    value.Append('\r');
                else if (escape == 't')
                    value.Append('\t');
                else if (escape == 'u')
                {
                    if (offset + 4 > text.Length)
                        throw ContextError();
                    int code;
                    if (!Int32.TryParse(text.Substring(offset, 4), NumberStyles.HexNumber,
                        CultureInfo.InvariantCulture, out code))
                        throw ContextError();
                    value.Append((char)code);
                    offset += 4;
                }
                else
                    throw ContextError();
            }
            throw ContextError();
        }

        private static Dictionary<string, string> ParseContextJson(byte[] data)
        {
            string text;
            try
            {
                text = new UTF8Encoding(false, true).GetString(data);
            }
            catch
            {
                throw ContextError();
            }
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.Ordinal);
            int offset = 0;
            SkipWhitespace(text, ref offset);
            if (offset >= text.Length || text[offset++] != '{')
                throw ContextError();
            SkipWhitespace(text, ref offset);
            while (offset < text.Length && text[offset] != '}')
            {
                string name = ReadJsonString(text, ref offset);
                SkipWhitespace(text, ref offset);
                if (offset >= text.Length || text[offset++] != ':')
                    throw ContextError();
                SkipWhitespace(text, ref offset);
                string value = ReadJsonString(text, ref offset);
                if (values.ContainsKey(name))
                    throw ContextError();
                values.Add(name, value);
                SkipWhitespace(text, ref offset);
                if (offset < text.Length && text[offset] == ',')
                {
                    offset++;
                    SkipWhitespace(text, ref offset);
                }
                else
                    break;
            }
            if (offset >= text.Length || text[offset++] != '}')
                throw ContextError();
            SkipWhitespace(text, ref offset);
            if (offset != text.Length || values.Count != 11)
                throw ContextError();
            string[] names = new string[] {
                "LeaseHandle", "Work", "ProgramRoot", "VersionDir", "ClaimId",
                "IdentitySha256", "Unpacker", "Archive", "ExtractTemp", "PreparationKind",
                "CancelFile"
            };
            for (int index = 0; index < names.Length; index++)
                if (!values.ContainsKey(names[index]))
                    throw ContextError();
            PreparationContext context = new PreparationContext();
            context.LeaseHandle = values["LeaseHandle"];
            context.Work = values["Work"];
            context.ProgramRoot = values["ProgramRoot"];
            context.VersionDir = values["VersionDir"];
            context.ClaimId = values["ClaimId"];
            context.IdentitySha256 = values["IdentitySha256"];
            context.Unpacker = values["Unpacker"];
            context.Archive = values["Archive"];
            context.ExtractTemp = values["ExtractTemp"];
            context.PreparationKind = values["PreparationKind"];
            context.CancelFile = values["CancelFile"];
            if (!BytesEqual(data, BuildJsonBytes(context)))
                throw ContextError();
            return values;
        }

        private static void SkipWhitespace(string text, ref int offset)
        {
            while (offset < text.Length && (text[offset] == ' ' || text[offset] == '\t' ||
                text[offset] == '\r' || text[offset] == '\n'))
                offset++;
        }

        private static bool BytesEqual(byte[] left, byte[] right)
        {
            if (left == null || right == null || left.Length != right.Length)
                return false;
            for (int index = 0; index < left.Length; index++)
                if (left[index] != right[index])
                    return false;
            return true;
        }

        private static PreparationContext ReadContext(string contextPath)
        {
            string fullContext = FullPath(contextPath);
            byte[] data = ReadFileBytes(fullContext, MaxContextBytes);
            Dictionary<string, string> values = ParseContextJson(data);
            PreparationContext context = new PreparationContext();
            context.LeaseHandle = values["LeaseHandle"];
            context.Work = values["Work"];
            context.ProgramRoot = values["ProgramRoot"];
            context.VersionDir = values["VersionDir"];
            context.ClaimId = values["ClaimId"];
            context.IdentitySha256 = values["IdentitySha256"];
            context.Unpacker = values["Unpacker"];
            context.Archive = values["Archive"];
            context.ExtractTemp = values["ExtractTemp"];
            context.PreparationKind = values["PreparationKind"];
            context.CancelFile = values["CancelFile"];
            context = ValidatePaths(context);
            if (!IsWithin(context.Work, fullContext) ||
                !Path.GetFileName(fullContext).StartsWith("carlina-preparation-context-", StringComparison.Ordinal))
                throw ContextError();
            return context;
        }

        private static SafeFileHandle LeaseHandle(PreparationContext context)
        {
            ulong value;
            if (String.IsNullOrEmpty(context.LeaseHandle) ||
                !UInt64.TryParse(context.LeaseHandle, NumberStyles.None, CultureInfo.InvariantCulture, out value) ||
                value.ToString(CultureInfo.InvariantCulture) != context.LeaseHandle || value == 0 || value == UInt64.MaxValue)
                throw ContextError();
            IntPtr handle = new IntPtr(unchecked((long)value));
            SafeFileHandle lease = new SafeFileHandle(handle, true);
            if (lease.IsInvalid)
            {
                lease.Dispose();
                throw ContextError();
            }
            return lease;
        }

        private static void ValidateClaim(PreparationContext context)
        {
            string path = Path.Combine(context.VersionDir, ClaimName);
            byte[] actual = ReadFileBytes(path, 256);
            string expectedText = ClaimHeader + "\n" + context.ClaimId + "\n" + context.IdentitySha256 + "\n";
            byte[] expected;
            try
            {
                expected = Encoding.ASCII.GetBytes(expectedText);
            }
            catch
            {
                throw ContextError();
            }
            if (!BytesEqual(actual, expected))
                throw ContextError();
        }

        private static string QuoteArgument(string value)
        {
            StringBuilder quoted = new StringBuilder();
            quoted.Append('"');
            int slashes = 0;
            for (int index = 0; index < value.Length; index++)
            {
                char item = value[index];
                if (item == '\\')
                    slashes++;
                else if (item == '"')
                {
                    quoted.Append('\\', slashes * 2 + 1);
                    quoted.Append('"');
                    slashes = 0;
                }
                else
                {
                    quoted.Append('\\', slashes);
                    quoted.Append(item);
                    slashes = 0;
                }
            }
            quoted.Append('\\', slashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }

        private static IntPtr BuildAttributeList(uint[] attributes, IntPtr[] values, IntPtr[] sizes)
        {
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, attributes.Length, 0, ref size);
            int error = Marshal.GetLastWin32Error();
            if (size == IntPtr.Zero || error != ErrorInsufficientBuffer)
                throw UnavailableError();
            IntPtr list = Marshal.AllocHGlobal(size);
            bool initialized = false;
            try
            {
                if (!InitializeProcThreadAttributeList(list, attributes.Length, 0, ref size))
                    throw UnavailableError();
                initialized = true;
                for (int index = 0; index < attributes.Length; index++)
                    if (!UpdateProcThreadAttribute(list, 0, new IntPtr(unchecked((long)attributes[index])),
                        values[index], sizes[index], IntPtr.Zero, IntPtr.Zero))
                        throw UnavailableError();
                return list;
            }
            catch
            {
                if (initialized)
                    DeleteProcThreadAttributeList(list);
                Marshal.FreeHGlobal(list);
                throw;
            }
        }

        private static IntPtr HandleArray(IntPtr[] handles)
        {
            IntPtr array = Marshal.AllocHGlobal(IntPtr.Size * handles.Length);
            for (int index = 0; index < handles.Length; index++)
                Marshal.WriteIntPtr(array, index * IntPtr.Size, handles[index]);
            return array;
        }

        private static void CloseQuiet(IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != new IntPtr(-1))
                CloseHandle(handle);
        }

        private static SafeFileHandle CreateInheritedFile(string path, uint access, uint disposition)
        {
            SecurityAttributes attributes = new SecurityAttributes();
            attributes.nLength = Marshal.SizeOf(typeof(SecurityAttributes));
            attributes.bInheritHandle = true;
            SafeFileHandle handle = CreateFileInheritedW(
                path,
                access,
                FileShareRead,
                ref attributes,
                disposition,
                FileAttributeNormal | FileFlagOpenReparsePoint,
                IntPtr.Zero);
            if (handle == null || handle.IsInvalid)
            {
                if (handle != null)
                    handle.Dispose();
                throw UnavailableError();
            }
            return handle;
        }

        private static void WriteCreateNew(string path, byte[] data)
        {
            try
            {
                using (FileStream stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                {
                    stream.Write(data, 0, data.Length);
                    stream.Flush(true);
                }
            }
            catch
            {
                throw UnavailableError();
            }
        }

        private static CustodianProcess StartCustodianProcess(
            string powershell,
            string work,
            string scriptPath,
            string contextPath,
            string scriptText,
            PreparationContext context,
            IntPtr originalLease)
        {
            string token = Guid.NewGuid().ToString("N", CultureInfo.InvariantCulture);
            string stdoutPath = Path.Combine(work, "carlina-preparation-custodian-" + token + ".out.log");
            string stderrPath = Path.Combine(work, "carlina-preparation-custodian-" + token + ".err.log");
            IntPtr duplicateLease = IntPtr.Zero;
            SafeFileHandle input = null;
            SafeFileHandle output = null;
            SafeFileHandle error = null;
            IntPtr handleArray = IntPtr.Zero;
            IntPtr attributeList = IntPtr.Zero;
            ProcessInformation processInfo = new ProcessInformation();
            bool created = false;
            try
            {
                if (!DuplicateHandle(GetCurrentProcess(), originalLease, GetCurrentProcess(),
                    out duplicateLease, 0, true, DuplicateSameAccess))
                    throw UnavailableError();
                ulong duplicateValue = unchecked((ulong)duplicateLease.ToInt64());
                context.LeaseHandle = duplicateValue.ToString(CultureInfo.InvariantCulture);
                WriteCreateNewText(scriptPath, scriptText);
                WriteCreateNewText(contextPath, BuildContextJson(
                    context.LeaseHandle,
                    context.Work,
                    context.ProgramRoot,
                    context.VersionDir,
                    context.ClaimId,
                    context.IdentitySha256,
                    context.Unpacker,
                    context.Archive,
                    context.ExtractTemp,
                    context.PreparationKind,
                    context.CancelFile));

                SecurityAttributes security = new SecurityAttributes();
                security.nLength = Marshal.SizeOf(typeof(SecurityAttributes));
                security.bInheritHandle = true;
                input = CreateFileInheritedW("NUL", GenericRead, FileShareRead | FileShareWrite,
                    ref security, OpenExisting, FileAttributeNormal, IntPtr.Zero);
                if (input == null || input.IsInvalid)
                    throw UnavailableError();
                output = CreateInheritedFile(stdoutPath, GenericWrite, CreateNew);
                error = CreateInheritedFile(stderrPath, GenericWrite, CreateNew);
                IntPtr[] inheritedHandles = new IntPtr[] {
                    duplicateLease,
                    input.DangerousGetHandle(),
                    output.DangerousGetHandle(),
                    error.DangerousGetHandle()
                };
                handleArray = HandleArray(inheritedHandles);
                attributeList = BuildAttributeList(
                    new uint[] { ProcThreadAttributeHandleList },
                    new IntPtr[] { handleArray },
                    new IntPtr[] { new IntPtr(IntPtr.Size * inheritedHandles.Length) });

                StartupInfoEx startup = new StartupInfoEx();
                startup.StartupInfo.cb = Marshal.SizeOf(typeof(StartupInfoEx));
                startup.StartupInfo.dwFlags = unchecked((int)StartfUseStdHandles);
                startup.StartupInfo.hStdInput = input.DangerousGetHandle();
                startup.StartupInfo.hStdOutput = output.DangerousGetHandle();
                startup.StartupInfo.hStdError = error.DangerousGetHandle();
                startup.lpAttributeList = attributeList;

                string commandLine = QuoteArgument(powershell) +
                    " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " +
                    QuoteArgument(scriptPath) + " " + QuoteArgument(contextPath);
                if (commandLine.Length > 32760)
                    throw ContextError();
                StringBuilder command = new StringBuilder(commandLine);
                if (!CreateProcessExW(powershell, command, IntPtr.Zero, IntPtr.Zero, true,
                    CreateNoWindow | CreateNewProcessGroup | ExtendedStartupInfoPresent,
                    IntPtr.Zero, work, ref startup, out processInfo))
                    throw UnavailableError();
                created = true;
                CloseQuiet(processInfo.hThread);
                processInfo.hThread = IntPtr.Zero;
                CloseQuiet(duplicateLease);
                duplicateLease = IntPtr.Zero;
                IntPtr processHandle = processInfo.hProcess;
                processInfo.hProcess = IntPtr.Zero;
                return new CustodianProcess(processHandle);
            }
            finally
            {
                if (attributeList != IntPtr.Zero)
                {
                    DeleteProcThreadAttributeList(attributeList);
                    Marshal.FreeHGlobal(attributeList);
                }
                if (handleArray != IntPtr.Zero)
                    Marshal.FreeHGlobal(handleArray);
                if (input != null)
                    input.Dispose();
                if (output != null)
                    output.Dispose();
                if (error != null)
                    error.Dispose();
                CloseQuiet(duplicateLease);
                if (!created && processInfo.hProcess != IntPtr.Zero)
                    CloseQuiet(processInfo.hProcess);
                if (processInfo.hThread != IntPtr.Zero)
                    CloseQuiet(processInfo.hThread);
            }
        }

        public static CustodianProcess Start(
            IntPtr originalLease,
            string powershellHome,
            string work,
            string programRoot,
            string versionDir,
            string claimId,
            string identitySha256,
            string unpacker,
            string archive,
            string extractTemp,
            string preparationKind,
            string cancelFile,
            string scriptText)
        {
            RequireWindowsPlatform();
            PreparationContext context = new PreparationContext();
            context.LeaseHandle = "0";
            context.Work = work;
            context.ProgramRoot = programRoot;
            context.VersionDir = versionDir;
            context.ClaimId = claimId;
            context.IdentitySha256 = identitySha256;
            context.Unpacker = unpacker;
            context.Archive = archive;
            context.ExtractTemp = extractTemp;
            context.PreparationKind = preparationKind;
            context.CancelFile = cancelFile;
            context = ValidatePaths(context);
            string psHome = FullPath(powershellHome);
            string powershell = Path.Combine(psHome, "powershell.exe");
            EnsureNoReparseChain(psHome, true);
            EnsureNoReparseChain(powershell, false);
            if (context.PreparationKind == "publisher" && !SamePath(powershell, CurrentExecutable()))
                throw ContextError();
            VerifyLease(new SafeFileHandle(originalLease, false), context.ProgramRoot);
            string token = Guid.NewGuid().ToString("N", CultureInfo.InvariantCulture);
            string scriptPath = Path.Combine(context.Work, "carlina-preparation-custodian-" + token + ".ps1");
            string contextPath = Path.Combine(context.Work, "carlina-preparation-context-" + token + ".json");
            return StartCustodianProcess(
                powershell, context.Work, scriptPath, contextPath, scriptText, context, originalLease);
        }

        public static int RunCustodian(string contextPath)
        {
            SafeFileHandle lease = null;
            IntPtr job = IntPtr.Zero;
            NativeChild child = null;
            bool childExited = false;
            try
            {
                RequireWindowsPlatform();
                PreparationContext context = ReadContext(contextPath);
                lease = LeaseHandle(context);
                VerifyLease(lease, context.ProgramRoot);
                ValidateClaim(context);
                Environment.SetEnvironmentVariable("TEMP", context.ExtractTemp, EnvironmentVariableTarget.Process);
                Environment.SetEnvironmentVariable("TMP", context.ExtractTemp, EnvironmentVariableTarget.Process);

                job = CreateJobObjectW(IntPtr.Zero, null);
                if (job == IntPtr.Zero)
                    throw UnavailableError();
                JobExtendedLimitInformation limits = new JobExtendedLimitInformation();
                limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
                IntPtr limitsBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(JobExtendedLimitInformation)));
                try
                {
                    Marshal.StructureToPtr(limits, limitsBuffer, false);
                    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, limitsBuffer,
                        (uint)Marshal.SizeOf(typeof(JobExtendedLimitInformation))))
                        throw UnavailableError();
                }
                finally
                {
                    Marshal.FreeHGlobal(limitsBuffer);
                }

                LaunchUnpacker(context, job, out child);
                uint wait = WaitForSingleObject(child.Process, Infinite);
                if (wait != WaitObject0)
                    throw UnavailableError();
                childExited = true;
                uint nativeExit;
                if (!GetExitCodeProcess(child.Process, out nativeExit))
                    throw UnavailableError();
                while (true)
                {
                    JobBasicAccountingInformation accounting;
                    uint returned;
                    if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                        out accounting, (uint)Marshal.SizeOf(typeof(JobBasicAccountingInformation)), out returned))
                        throw UnavailableError();
                    if (accounting.ActiveProcesses == 0)
                        break;
                    Thread.Sleep(50);
                }

                VerifyLease(lease, context.ProgramRoot);
                context = ValidatePaths(context);
                ValidateClaim(context);
                if (context.PreparationKind == "publisher")
                    ValidatePublisherProgress(context.Work, nativeExit);
                PublishCompletion(context, nativeExit);
                return nativeExit == 0 ? 0 : 1;
            }
            catch
            {
                throw DidNotCompleteError();
            }
            finally
            {
                if (job != IntPtr.Zero)
                {
                    if (!CloseHandle(job) && child != null)
                        TerminateJobObject(job, 1);
                    job = IntPtr.Zero;
                    if (child != null && child.Process != IntPtr.Zero && !childExited)
                        WaitForSingleObject(child.Process, Infinite);
                }
                if (child != null)
                    CloseQuiet(child.Process);
                if (lease != null)
                    lease.Dispose();
            }
        }

        private static void LaunchUnpacker(PreparationContext context, IntPtr job, out NativeChild result)
        {
            result = null;
            string token = Guid.NewGuid().ToString("N", CultureInfo.InvariantCulture);
            string stdoutPath = context.PreparationKind == "publisher"
                ? Path.Combine(context.Work, "preparation-native.stdout")
                : Path.Combine(context.Work, "carlina-preparation-unpacker-" + token + ".out.log");
            string stderrPath = context.PreparationKind == "publisher"
                ? Path.Combine(context.Work, "preparation-native.stderr")
                : Path.Combine(context.Work, "carlina-preparation-unpacker-" + token + ".err.log");
            SafeFileHandle input = null;
            SafeFileHandle output = null;
            SafeFileHandle error = null;
            IntPtr jobArray = IntPtr.Zero;
            IntPtr stdHandles = IntPtr.Zero;
            IntPtr attributeList = IntPtr.Zero;
            ProcessInformation processInfo = new ProcessInformation();
            bool started = false;
            try
            {
                SecurityAttributes security = new SecurityAttributes();
                security.nLength = Marshal.SizeOf(typeof(SecurityAttributes));
                security.bInheritHandle = true;
                input = CreateFileInheritedW("NUL", GenericRead, FileShareRead | FileShareWrite,
                    ref security, OpenExisting, FileAttributeNormal, IntPtr.Zero);
                if (input == null || input.IsInvalid)
                    throw UnavailableError();
                output = CreateInheritedFile(stdoutPath, GenericWrite, CreateNew);
                error = CreateInheritedFile(stderrPath, GenericWrite, CreateNew);
                NativeChild child = new NativeChild();
                result = child;

                IntPtr[] handles = new IntPtr[] {
                    input.DangerousGetHandle(), output.DangerousGetHandle(), error.DangerousGetHandle()
                };
                stdHandles = HandleArray(handles);
                jobArray = HandleArray(new IntPtr[] { job });
                IntPtr[] values = new IntPtr[] { stdHandles, jobArray };
                IntPtr[] sizes = new IntPtr[] {
                    new IntPtr(IntPtr.Size * handles.Length), new IntPtr(IntPtr.Size)
                };
                attributeList = BuildAttributeList(
                    new uint[] { ProcThreadAttributeHandleList, ProcThreadAttributeJobList }, values, sizes);

                StartupInfoEx startup = new StartupInfoEx();
                startup.StartupInfo.cb = Marshal.SizeOf(typeof(StartupInfoEx));
                startup.StartupInfo.dwFlags = unchecked((int)StartfUseStdHandles);
                startup.StartupInfo.hStdInput = handles[0];
                startup.StartupInfo.hStdOutput = handles[1];
                startup.StartupInfo.hStdError = handles[2];
                startup.lpAttributeList = attributeList;
                string commandLine;
                if (context.PreparationKind == "publisher")
                {
                    commandLine = QuoteArgument(context.Unpacker) +
                        " -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " +
                        QuoteArgument(context.Archive) + " -ProgressRecordsV1";
                    if (context.CancelFile.Length != 0)
                        commandLine += " -CancelFile " + QuoteArgument(context.CancelFile);
                }
                else
                    commandLine = QuoteArgument(context.Unpacker) + " " + QuoteArgument(context.Archive);
                StringBuilder command = new StringBuilder(commandLine);
                if (!CreateProcessExW(context.Unpacker, command, IntPtr.Zero, IntPtr.Zero, true,
                    ExtendedStartupInfoPresent | CreateNoWindow, IntPtr.Zero, context.VersionDir,
                    ref startup, out processInfo))
                    throw UnavailableError();
                started = true;
                child.Process = processInfo.hProcess;
                if (!CloseHandle(processInfo.hThread))
                    throw UnavailableError();
                processInfo.hThread = IntPtr.Zero;
            }
            finally
            {
                if (attributeList != IntPtr.Zero)
                {
                    DeleteProcThreadAttributeList(attributeList);
                    Marshal.FreeHGlobal(attributeList);
                }
                if (jobArray != IntPtr.Zero)
                    Marshal.FreeHGlobal(jobArray);
                if (stdHandles != IntPtr.Zero)
                    Marshal.FreeHGlobal(stdHandles);
                if (input != null)
                    input.Dispose();
                if (output != null)
                    output.Dispose();
                if (error != null)
                    error.Dispose();
                if (processInfo.hThread != IntPtr.Zero)
                    CloseQuiet(processInfo.hThread);
                if (!started && processInfo.hProcess != IntPtr.Zero)
                    CloseQuiet(processInfo.hProcess);
            }
        }

        private static void PublishCompletion(PreparationContext context, uint nativeExit)
        {
            string completionPath = Path.Combine(context.VersionDir, "@@PREPARATION_NAME@@");
            EnsureNoReparseChain(context.VersionDir, true);
            string root = FullPath(context.ProgramRoot);
            byte[] rootBytes;
            try
            {
                rootBytes = new UTF8Encoding(false, true).GetBytes(root);
            }
            catch
            {
                throw ContextError();
            }
            if (rootBytes.Length == 0 || rootBytes.Length > MaxProgramRootBytes)
                throw ContextError();
            string encodedRoot = Convert.ToBase64String(rootBytes);
            string record = "@@PREPARATION_HEADER@@\n" + context.ClaimId + "\n" + context.IdentitySha256 + "\n" +
                encodedRoot + "\n" + nativeExit.ToString(CultureInfo.InvariantCulture) + "\n";
            byte[] bytes;
            try
            {
                bytes = Encoding.ASCII.GetBytes(record);
            }
            catch
            {
                throw ContextError();
            }
            if (bytes.Length > @@MAX_PREPARATION_BYTES@@)
                throw ContextError();
            try
            {
                File.GetAttributes(completionPath);
                throw ContextError();
            }
            catch (FileNotFoundException)
            {
            }
            catch (DirectoryNotFoundException)
            {
            }
            catch (InvalidOperationException)
            {
                throw;
            }
            catch
            {
                throw ContextError();
            }
            string temporaryPath = Path.Combine(context.VersionDir,
                "carlina-preparation-completion-" + Guid.NewGuid().ToString("N", CultureInfo.InvariantCulture) + ".tmp");
            try
            {
                using (FileStream stream = new FileStream(temporaryPath, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }
                File.Move(temporaryPath, completionPath);
            }
            catch
            {
                throw DidNotCompleteError();
            }
        }

        private static void WriteCreateNewText(string path, string text)
        {
            byte[] bytes;
            try
            {
                bytes = new UTF8Encoding(false, true).GetBytes(text);
            }
            catch
            {
                throw ContextError();
            }
            WriteCreateNew(path, bytes);
        }

        private static string BuildContextJson(
            string leaseHandle,
            string work,
            string programRoot,
            string versionDir,
            string claimId,
            string identitySha256,
            string unpacker,
            string archive,
            string extractTemp,
            string preparationKind,
            string cancelFile)
        {
            PreparationContext context = new PreparationContext();
            context.LeaseHandle = leaseHandle;
            context.Work = work;
            context.ProgramRoot = programRoot;
            context.VersionDir = versionDir;
            context.ClaimId = claimId;
            context.IdentitySha256 = identitySha256;
            context.Unpacker = unpacker;
            context.Archive = archive;
            context.ExtractTemp = extractTemp;
            context.PreparationKind = preparationKind;
            context.CancelFile = cancelFile;
            return new UTF8Encoding(false, true).GetString(BuildJsonBytes(context));
        }
    }
}
'@

    $custodianTemplate = @'
param([Parameter(Mandatory = $true)][string]$ContextPath)
$ErrorActionPreference = 'Stop'
if (($PSVersionTable.PSVersion -lt [version]'5.1') -or
    ($PSVersionTable.PSEdition -ne 'Desktop') -or
    (-not [Environment]::Is64BitProcess)) {
    [Console]::Error.WriteLine('Native preparation supervision is unavailable.')
    exit 2
}
try {
    $assemblyBytes = [Convert]::FromBase64String('@@NATIVE_ASSEMBLY_BASE64@@')
    $assembly = [Reflection.Assembly]::Load($assemblyBytes)
    $nativeType = $assembly.GetType('CarlinaPreparation.Native', $true, $false)
    $result = $nativeType::RunCustodian($ContextPath)
    [Environment]::ExitCode = $result
    exit $result
}
catch {
    [Console]::Error.WriteLine('Native preparation did not complete.')
    exit 2
}
'@

    try {
        # Framework csc reads the owned UTF-8 response file without CodeDOM's
        # ANSI temp-directory path. Both processes use these exact assembly bytes.
        [byte[]]$assemblyBytes = Get-CarlinaPreparationNativeAssembly -NativeSource $nativeSource -Work $Work
        $assembly = [Reflection.Assembly]::Load($assemblyBytes)
        $nativeType = $assembly.GetType('CarlinaPreparation.Native', $true, $false)
        $assemblyBase64 = [Convert]::ToBase64String($assemblyBytes)
        $custodianScript = $custodianTemplate.Replace('@@NATIVE_ASSEMBLY_BASE64@@', $assemblyBase64)
        $process = $nativeType::Start(
            $Lease.SafeFileHandle.DangerousGetHandle(),
            $PSHOME,
            $Work,
            $ProgramRoot,
            $VersionDir,
            $ClaimId,
            $IdentitySha256,
            $Unpacker,
            $Archive,
            $ExtractTemp,
            $(if ($PublisherProgress) { 'publisher' } else { 'bundle' }),
            $cancel,
            $custodianScript
        )
        return $process
    }
    catch {
        if ($_.Exception.Message -eq 'Native preparation context is invalid.') {
            throw 'Native preparation context is invalid.'
        }
        throw 'Native preparation supervision is unavailable.'
    }
}

function Read-PreparationCompletion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$VersionDir,
        [Parameter(Mandatory = $true)][string]$ClaimId,
        [Parameter(Mandatory = $true)][string]$IdentitySha256,
        [Parameter(Mandatory = $true)][string]$ProgramRoot
    )

    try {
        if ($ClaimId -cnotmatch '^[0-9a-f]{32}$' -or
            $IdentitySha256 -cnotmatch '^[0-9a-f]{64}$' -or
            -not [IO.Path]::IsPathRooted($ProgramRoot) -or
            -not [IO.Path]::IsPathRooted($VersionDir)) {
            throw 'invalid'
        }
        $root = ConvertTo-CarlinaPreparationCanonicalPath -Path $ProgramRoot
        $version = ConvertTo-CarlinaPreparationCanonicalPath -Path $VersionDir
        $versionName = [IO.Path]::GetFileName($version)
        if ($versionName -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}$' -or
            $versionName.EndsWith('.')) {
            throw 'invalid'
        }
        $expectedVersion = [IO.Path]::Combine([IO.Path]::Combine($root, 'versions'), $versionName)
        if (-not [string]::Equals($version, $expectedVersion, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'invalid'
        }
        $null = Test-CarlinaPreparationPathChain -Path $root -Directory
        $null = Test-CarlinaPreparationPathChain -Path ([IO.Path]::Combine($root, 'versions')) -Directory
        $null = Test-CarlinaPreparationPathChain -Path $version -Directory
        $claimPath = [IO.Path]::Combine($version, '.carlina-bootstrap-claim')
        $claimBytes = Read-CarlinaPreparationSafeFile -Path $claimPath -MaximumBytes 256
        $expectedClaim = [Text.Encoding]::ASCII.GetBytes("carlina.bootstrap-claim/1`n$ClaimId`n$IdentitySha256`n")
        if ($claimBytes.Length -ne $expectedClaim.Length) {
            throw 'invalid'
        }
        for ($index = 0; $index -lt $claimBytes.Length; $index++) {
            if ($claimBytes[$index] -ne $expectedClaim[$index]) {
                throw 'invalid'
            }
        }

        $recordPath = [IO.Path]::Combine($version, '@@PREPARATION_NAME@@')
        $recordBytes = Read-CarlinaPreparationSafeFile -Path $recordPath -MaximumBytes @@MAX_PREPARATION_BYTES@@
        foreach ($byte in $recordBytes) {
            if (($byte -gt 127) -or
                (($byte -ne 10) -and (($byte -lt 32) -or ($byte -gt 126)))) {
                throw 'invalid'
            }
        }
        $record = [Text.Encoding]::ASCII.GetString($recordBytes)
        $lines = $record.Split([char[]]@([char]10))
        if ($lines.Length -ne 6 -or $lines[5] -ne '' -or
            $lines[0] -cne '@@PREPARATION_HEADER@@' -or
            $lines[1] -cne $ClaimId -or $lines[2] -cne $IdentitySha256 -or
            $lines[4] -cnotmatch '^(0|[1-9][0-9]{0,9})$') {
            throw 'invalid'
        }
        $rootBytes = [Convert]::FromBase64String($lines[3])
        if ($rootBytes.Length -eq 0 -or $rootBytes.Length -gt @@MAX_PROGRAM_ROOT_BYTES@@ -or
            [Convert]::ToBase64String($rootBytes) -cne $lines[3]) {
            throw 'invalid'
        }
        $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        $recordRoot = $strictUtf8.GetString($rootBytes)
        if (-not [IO.Path]::IsPathRooted($recordRoot) -or $recordRoot.IndexOf([char]0) -ge 0) {
            throw 'invalid'
        }
        $normalizedRecordRoot = ConvertTo-CarlinaPreparationCanonicalPath -Path $recordRoot
        if (-not [string]::Equals($normalizedRecordRoot, $root, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'invalid'
        }
        [uint32]$nativeExit = 0
        if (-not [uint32]::TryParse(
            $lines[4],
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$nativeExit
        )) {
            throw 'invalid'
        }
        return $nativeExit
    }
    catch {
        throw 'Native preparation completion is invalid.'
    }
}

function Get-StoppedPreparationSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($Bytes)
        return [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Set-PreparationRetirementDirectoryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $owner) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    $privateAcl = [Security.AccessControl.DirectorySecurity]::new()
    $privateAcl.SetOwner($owner)
    $privateAcl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    foreach ($principal in @(
        $owner,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    )) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $principal,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $null = $privateAcl.AddAccessRule($rule)
    }
    [IO.Directory]::SetAccessControl($Path, $privateAcl)
}

function Assert-PreparationRetirementDirectoryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = [IO.Directory]::GetAccessControl($Path)
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $owner) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    $aclOwner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
    $expectedOwner = [string]$owner.Value
    if ($null -eq $aclOwner -or
        -not [string]::Equals([string]$aclOwner.Value, $expectedOwner, [StringComparison]::Ordinal) -or
        -not $acl.AreAccessRulesProtected) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    if ($rules.Count -ne 3) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    $expectedPrincipals = @(
        $expectedOwner,
        'S-1-5-18',
        'S-1-5-32-544'
    )
    $seenPrincipals = @{}
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    foreach ($rule in $rules) {
        $principal = [string]$rule.IdentityReference.Value
        if ($rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            [int]$rule.FileSystemRights -ne [int][Security.AccessControl.FileSystemRights]::FullControl -or
            [int]$rule.InheritanceFlags -ne [int]$inheritance -or
            [int]$rule.PropagationFlags -ne [int][Security.AccessControl.PropagationFlags]::None -or
            -not ($expectedPrincipals -ccontains $principal) -or
            $seenPrincipals.ContainsKey($principal)) {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }
        $seenPrincipals[$principal] = $true
    }
}

function Get-StoppedPreparationContext {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][IO.FileStream]$Lease,
        [Parameter(Mandatory = $true)][string]$ProgramRoot,
        [Parameter(Mandatory = $true)][string]$VersionDir,
        [Parameter(Mandatory = $true)][string]$ClaimId,
        [Parameter(Mandatory = $true)][string]$IdentitySha256
    )

    if (($PSVersionTable.PSVersion -lt [version]'5.1') -or
        ($PSVersionTable.PSEdition -ne 'Desktop') -or
        (-not [Environment]::Is64BitProcess) -or
        ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    if ($null -eq $Lease -or $null -eq $Lease.SafeFileHandle -or
        $Lease.SafeFileHandle.IsClosed -or $Lease.SafeFileHandle.IsInvalid -or
        -not $Lease.CanRead -or -not $Lease.CanWrite -or
        $ClaimId -cnotmatch '^[0-9a-f]{32}$' -or
        $IdentitySha256 -cnotmatch '^[0-9a-f]{64}$' -or
        -not [IO.Path]::IsPathRooted($ProgramRoot) -or
        -not [IO.Path]::IsPathRooted($VersionDir)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $root = ConvertTo-CarlinaPreparationCanonicalPath -Path $ProgramRoot
    $version = ConvertTo-CarlinaPreparationCanonicalPath -Path $VersionDir
    if (-not [string]::Equals($root, $ProgramRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($version, $VersionDir, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    $versionName = [IO.Path]::GetFileName($version)
    if ($versionName -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}$' -or
        $versionName.EndsWith('.')) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $versions = [IO.Path]::Combine($root, 'versions')
    $state = [IO.Path]::Combine($root, 'state')
    $expectedVersion = [IO.Path]::Combine($versions, $versionName)
    $expectedLease = [IO.Path]::Combine([IO.Path]::Combine($root, 'state'), 'managed-install.lock')
    $leaseName = ConvertTo-CarlinaPreparationCanonicalPath -Path $Lease.Name
    if (-not [string]::Equals($version, $expectedVersion, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($leaseName, $expectedLease, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $null = Test-CarlinaPreparationPathChain -Path $root -Directory
    $null = Test-CarlinaPreparationPathChain -Path $versions -Directory
    $null = Test-CarlinaPreparationPathChain -Path $version -Directory
    $null = Test-CarlinaPreparationPathChain -Path $state -Directory
    $null = Test-CarlinaPreparationPathChain -Path $expectedLease

    $rootEntries = [IO.DirectoryInfo]::new($root).GetFileSystemInfos()
    foreach ($entry in $rootEntries) {
        if ([string]::Equals($entry.Name, 'current', [StringComparison]::OrdinalIgnoreCase) -or
            [string]::Equals($entry.Name, 'previous', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }
    }

    $stateEntries = [IO.DirectoryInfo]::new($state).GetFileSystemInfos()
    $lockFound = $false
    $retirementFound = $false
    $retirementBase = [IO.Path]::Combine($state, '@@RETIREMENT_DIRECTORY@@')
    foreach ($entry in $stateEntries) {
        if ([string]::Equals($entry.Name, 'managed-install.lock', [StringComparison]::OrdinalIgnoreCase)) {
            if ($lockFound) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
            $lockAttributes = [IO.File]::GetAttributes($entry.FullName)
            if (($lockAttributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($lockAttributes -band [IO.FileAttributes]::Directory) -ne 0) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
            $lockFound = $true
        }
        elseif ([string]::Equals($entry.Name, '@@RETIREMENT_DIRECTORY@@', [StringComparison]::OrdinalIgnoreCase)) {
            if ($retirementFound) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
            $retirementAttributes = [IO.File]::GetAttributes($entry.FullName)
            if (($retirementAttributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($retirementAttributes -band [IO.FileAttributes]::Directory) -eq 0) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
            $null = Test-CarlinaPreparationPathChain -Path $retirementBase -Directory
            Assert-PreparationRetirementDirectoryAcl -Path $retirementBase
            $retirementFound = $true
        }
        else {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }
    }
    if (-not $lockFound) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $versionEntries = [IO.DirectoryInfo]::new($versions).GetFileSystemInfos()
    foreach ($entry in $versionEntries) {
        $attributes = [IO.File]::GetAttributes($entry.FullName)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($attributes -band [IO.FileAttributes]::Directory) -eq 0) {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }
        foreach ($generationEntry in ([IO.DirectoryInfo]::new($entry.FullName).GetFileSystemInfos())) {
            $candidateBinding =
                [string]::Equals($entry.FullName, $version, [StringComparison]::OrdinalIgnoreCase) -and
                ([string]::Equals($generationEntry.Name, '.carlina-bootstrap-binding.json', [StringComparison]::OrdinalIgnoreCase) -or
                 [string]::Equals($generationEntry.Name, '.carlina-bootstrap-preparation-binding.json', [StringComparison]::OrdinalIgnoreCase))
            if ($candidateBinding -or [string]::Equals($generationEntry.Name, 'release-receipt.json', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
        }
    }

    $nativeExit = Read-PreparationCompletion `
        -VersionDir $version `
        -ClaimId $ClaimId `
        -IdentitySha256 $IdentitySha256 `
        -ProgramRoot $root
    if ([uint32]$nativeExit -eq [uint32]0) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $claimPath = [IO.Path]::Combine($version, '.carlina-bootstrap-claim')
    $completionPath = [IO.Path]::Combine($version, '@@PREPARATION_NAME@@')
    $claimBefore = Read-CarlinaPreparationSafeFile -Path $claimPath -MaximumBytes 256
    $completionBefore = Read-CarlinaPreparationSafeFile -Path $completionPath -MaximumBytes @@MAX_PREPARATION_BYTES@@
    $claimHash = Get-StoppedPreparationSha256 -Bytes $claimBefore
    $completionHash = Get-StoppedPreparationSha256 -Bytes $completionBefore
    $verifiedNativeExit = Read-PreparationCompletion `
        -VersionDir $version `
        -ClaimId $ClaimId `
        -IdentitySha256 $IdentitySha256 `
        -ProgramRoot $root
    $claimAfter = Read-CarlinaPreparationSafeFile -Path $claimPath -MaximumBytes 256
    $completionAfter = Read-CarlinaPreparationSafeFile -Path $completionPath -MaximumBytes @@MAX_PREPARATION_BYTES@@
    if ([uint32]$verifiedNativeExit -eq [uint32]0 -or
        [uint32]$nativeExit -ne [uint32]$verifiedNativeExit -or
        $claimHash -cne (Get-StoppedPreparationSha256 -Bytes $claimAfter) -or
        $completionHash -cne (Get-StoppedPreparationSha256 -Bytes $completionAfter)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }

    $destinationLeaf = $versionName + '.' + $ClaimId
    $destination = [IO.Path]::Combine($retirementBase, $destinationLeaf)
    if ($retirementFound) {
        foreach ($entry in ([IO.DirectoryInfo]::new($retirementBase).GetFileSystemInfos())) {
            if ([string]::Equals($entry.Name, $destinationLeaf, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Retained preparation cannot be recovered by the native installer.'
            }
        }
    }

    return [pscustomobject]@{
        ProgramRoot = $root
        Versions = $versions
        VersionDir = $version
        VersionName = $versionName
        ClaimId = $ClaimId
        IdentitySha256 = $IdentitySha256
        NativeExit = [uint32]$nativeExit
        ClaimHash = $claimHash
        CompletionHash = $completionHash
        RetirementBase = $retirementBase
        RetirementBaseExists = [bool]$retirementFound
        Destination = $destination
        DestinationLeaf = $destinationLeaf
    }
}

function Assert-StoppedPreparationMoved {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][psobject]$Context)

    $null = Test-CarlinaPreparationPathChain -Path $Context.Destination -Directory
    $destinationAttributes = [IO.File]::GetAttributes($Context.Destination)
    if (($destinationAttributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($destinationAttributes -band [IO.FileAttributes]::Directory) -eq 0) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
    Assert-PreparationRetirementDirectoryAcl -Path $Context.RetirementBase

    foreach ($entry in ([IO.DirectoryInfo]::new($Context.Versions).GetFileSystemInfos())) {
        if ([string]::Equals($entry.Name, $Context.VersionName, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }
    }

    $claimPath = [IO.Path]::Combine($Context.Destination, '.carlina-bootstrap-claim')
    $completionPath = [IO.Path]::Combine($Context.Destination, '@@PREPARATION_NAME@@')
    $claimBytes = Read-CarlinaPreparationSafeFile -Path $claimPath -MaximumBytes 256
    $completionBytes = Read-CarlinaPreparationSafeFile -Path $completionPath -MaximumBytes @@MAX_PREPARATION_BYTES@@
    if ($Context.ClaimHash -cne (Get-StoppedPreparationSha256 -Bytes $claimBytes) -or
        $Context.CompletionHash -cne (Get-StoppedPreparationSha256 -Bytes $completionBytes)) {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
}

function Test-StoppedPreparationRecovery {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)][IO.FileStream]$Lease,
        [Parameter(Mandatory = $true, Position = 1)][string]$ProgramRoot,
        [Parameter(Mandatory = $true, Position = 2)][string]$VersionDir,
        [Parameter(Mandatory = $true, Position = 3)][string]$ClaimId,
        [Parameter(Mandatory = $true, Position = 4)][string]$IdentitySha256
    )

    try {
        $context = Get-StoppedPreparationContext `
            -Lease $Lease `
            -ProgramRoot $ProgramRoot `
            -VersionDir $VersionDir `
            -ClaimId $ClaimId `
            -IdentitySha256 $IdentitySha256
        return [uint32]$context.NativeExit
    }
    catch {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
}

function Move-StoppedPreparation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)][IO.FileStream]$Lease,
        [Parameter(Mandatory = $true, Position = 1)][string]$ProgramRoot,
        [Parameter(Mandatory = $true, Position = 2)][string]$VersionDir,
        [Parameter(Mandatory = $true, Position = 3)][string]$ClaimId,
        [Parameter(Mandatory = $true, Position = 4)][string]$IdentitySha256
    )

    try {
        $initial = Get-StoppedPreparationContext `
            -Lease $Lease `
            -ProgramRoot $ProgramRoot `
            -VersionDir $VersionDir `
            -ClaimId $ClaimId `
            -IdentitySha256 $IdentitySha256

        if (-not $initial.RetirementBaseExists) {
            $null = [IO.Directory]::CreateDirectory($initial.RetirementBase)
            Set-PreparationRetirementDirectoryAcl -Path $initial.RetirementBase
        }

        $ready = Get-StoppedPreparationContext `
            -Lease $Lease `
            -ProgramRoot $ProgramRoot `
            -VersionDir $VersionDir `
            -ClaimId $ClaimId `
            -IdentitySha256 $IdentitySha256
        if ($initial.ClaimHash -cne $ready.ClaimHash -or
            $initial.CompletionHash -cne $ready.CompletionHash -or
            [uint32]$initial.NativeExit -ne [uint32]$ready.NativeExit -or
            -not [string]::Equals($initial.Destination, $ready.Destination, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Retained preparation cannot be recovered by the native installer.'
        }

        # Rename on the same program volume; never copy, overwrite or delete.
        [IO.Directory]::Move($ready.VersionDir, $ready.Destination)
        Assert-StoppedPreparationMoved -Context $ready
        return [string]$ready.Destination
    }
    catch {
        throw 'Retained preparation cannot be recovered by the native installer.'
    }
}
"""
    return (
        helpers.replace("@@PREPARATION_HEADER@@", PREPARATION_HEADER)
        .replace("@@PREPARATION_NAME@@", PREPARATION_NAME)
        .replace("@@RETIREMENT_DIRECTORY@@", PREPARATION_RETIREMENT_DIRNAME)
        .replace("@@MAX_PREPARATION_BYTES@@", str(MAX_PREPARATION_BYTES))
        .replace("@@MAX_PROGRAM_ROOT_BYTES@@", str(MAX_PROGRAM_ROOT_BYTES))
    )
