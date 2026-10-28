"""Private artifact storage with held native Windows paths and file handles.

This module deliberately loads Win32 libraries only when an API method is
called. Importing Carlina's core package on Linux therefore remains safe.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import uuid
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from types import TracebackType
from typing import Any, Literal, NoReturn, Self

_ALLOWED_NAMESPACES = frozenset({"doc_processor_exports", "translator_exports"})
_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_FORMAT_RE = re.compile(r"[A-Za-z0-9]{1,16}\Z", re.ASCII)
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}\Z", re.ASCII)
_STAGE_NAME_RE = re.compile(r"\.stage-[0-9a-f]{32}\.[A-Za-z0-9]{1,16}\Z", re.ASCII)
_RESERVED_DEVICE_STEMS = frozenset(
    {"AUX", "CLOCK$", "CON", "CONIN$", "CONOUT$", "NUL", "PRN"}
)

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_INVALID_PARAMETER = 87
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_SHARE_ALL = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_DEVICE = 0x00000040
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_TYPE_DISK = 1
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_CLASS = 4
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_OWNER = 4
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_RAMDISK = 6
_FILE_PERSISTENT_ACLS = 0x00000008

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class WindowsArtifactError(OSError):
    """Path-free internal error with a caller-safe classification."""

    def __init__(
        self,
        code: Literal["invalid", "unavailable", "unsupported"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER_INFO(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _TOKEN_OWNER_INFO(ctypes.Structure):
    _fields_ = [("Owner", ctypes.c_void_p)]


class _FILE_RENAME_INFO_PREFIX(ctypes.Structure):
    # FileName starts immediately after the DWORD, before the structure's
    # trailing alignment padding. FILE_RENAME_INFO uses this exact layout.
    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


_FILE_RENAME_NAME_OFFSET = (
    _FILE_RENAME_INFO_PREFIX.FileNameLength.offset + ctypes.sizeof(wintypes.DWORD)
)


class _WinApi:
    """Explicitly typed Win32 entry points used by this module."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise WindowsArtifactError(
                "unsupported", "Native Windows artifact storage is unavailable."
            )
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        except OSError as exc:
            raise WindowsArtifactError(
                "unsupported", "Required Windows security APIs are unavailable."
            ) from exc

        self.CreateFileW = self.kernel32.CreateFileW
        self.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.CreateFileW.restype = wintypes.HANDLE

        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL

        self.GetFileInformationByHandle = self.kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        self.GetFileInformationByHandle.restype = wintypes.BOOL

        self.GetFileType = self.kernel32.GetFileType
        self.GetFileType.argtypes = [wintypes.HANDLE]
        self.GetFileType.restype = wintypes.DWORD

        self.ReadFile = self.kernel32.ReadFile
        self.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.ReadFile.restype = wintypes.BOOL

        self.SetFilePointerEx = self.kernel32.SetFilePointerEx
        self.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        ]
        self.SetFilePointerEx.restype = wintypes.BOOL

        self.FlushFileBuffers = self.kernel32.FlushFileBuffers
        self.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.FlushFileBuffers.restype = wintypes.BOOL

        self.SetFileInformationByHandle = self.kernel32.SetFileInformationByHandle
        self.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.SetFileInformationByHandle.restype = wintypes.BOOL

        self.GetDriveTypeW = self.kernel32.GetDriveTypeW
        self.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        self.GetDriveTypeW.restype = wintypes.UINT

        self.GetVolumeInformationW = self.kernel32.GetVolumeInformationW
        self.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self.GetVolumeInformationW.restype = wintypes.BOOL

        self.GetCurrentProcess = self.kernel32.GetCurrentProcess
        self.GetCurrentProcess.argtypes = []
        self.GetCurrentProcess.restype = wintypes.HANDLE

        self.OpenProcessToken = self.advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.OpenProcessToken.restype = wintypes.BOOL

        self.GetTokenInformation = self.advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.GetTokenInformation.restype = wintypes.BOOL

        self.GetSecurityInfo = self.advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.GetSecurityInfo.restype = wintypes.DWORD

        self.GetAclInformation = self.advapi32.GetAclInformation
        self.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        self.GetAclInformation.restype = wintypes.BOOL

        self.GetAce = self.advapi32.GetAce
        self.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.GetAce.restype = wintypes.BOOL

        self.GetLengthSid = self.advapi32.GetLengthSid
        self.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.GetLengthSid.restype = wintypes.DWORD

        self.IsValidSid = self.advapi32.IsValidSid
        self.IsValidSid.argtypes = [ctypes.c_void_p]
        self.IsValidSid.restype = wintypes.BOOL

        self.ConvertStringSidToSidW = self.advapi32.ConvertStringSidToSidW
        self.ConvertStringSidToSidW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.ConvertStringSidToSidW.restype = wintypes.BOOL

        self.LocalFree = self.kernel32.LocalFree
        self.LocalFree.argtypes = [ctypes.c_void_p]
        self.LocalFree.restype = ctypes.c_void_p


def _raise_last_error(
    code: Literal["invalid", "unavailable", "unsupported"], message: str
) -> NoReturn:
    error_number = ctypes.get_last_error()
    cause = ctypes.WinError(error_number)
    raise WindowsArtifactError(code, message) from cause


def _raise_native_error(
    error_number: int,
    code: Literal["invalid", "unavailable", "unsupported"],
    message: str,
) -> NoReturn:
    cause = ctypes.WinError(error_number)
    raise WindowsArtifactError(code, message) from cause


def _valid_handle(handle: Any) -> bool:
    value = handle if isinstance(handle, int) else getattr(handle, "value", None)
    return value not in (None, _INVALID_HANDLE_VALUE)


def _close_handle(api: _WinApi, handle: Any) -> None:
    if _valid_handle(handle):
        api.CloseHandle(handle)


def _extended_path(path: Path) -> str:
    value = str(path)
    if value.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        raise WindowsArtifactError(
            "unsupported", "Device and extended roots are unavailable."
        )
    if value.startswith("\\\\") or not re.match(r"^[A-Za-z]:\\", value):
        raise WindowsArtifactError(
            "unsupported", "A local absolute drive path is required."
        )
    return "\\\\?\\" + value


def _reserved_device_name(component: str) -> bool:
    stem = component.split(".", 1)[0].upper()
    return (
        stem in _RESERVED_DEVICE_STEMS
        or re.fullmatch(r"(?:COM|LPT)(?:[1-9]|[¹²³])", stem, re.ASCII) is not None
    )


def _validate_component(component: str, *, ascii_only: bool) -> None:
    if (
        not component
        or component in {".", ".."}
        or ":" in component
        or any(char in component for char in '<>"|?*')
        or "/" in component
        or "\\" in component
        or "\x00" in component
        or component.endswith((".", " "))
        or _reserved_device_name(component)
    ):
        raise WindowsArtifactError("invalid", "An artifact path component is invalid.")
    if ascii_only:
        if _SAFE_NAME_RE.fullmatch(component) is None:
            raise WindowsArtifactError("invalid", "An artifact name is invalid.")
    elif any(ord(char) < 32 for char in component):
        raise WindowsArtifactError("invalid", "An instance path component is invalid.")


def _validate_namespace_operation(namespace: object, operation_id: object) -> None:
    if not isinstance(namespace, str) or namespace not in _ALLOWED_NAMESPACES:
        raise WindowsArtifactError("invalid", "The artifact namespace is invalid.")
    if (
        not isinstance(operation_id, str)
        or _OPERATION_ID_RE.fullmatch(operation_id) is None
    ):
        raise WindowsArtifactError("invalid", "The artifact operation ID is invalid.")
    _validate_component(operation_id, ascii_only=True)


def _validate_instance_root(
    instance_root: os.PathLike[str] | os.PathLike[bytes] | str | bytes,
) -> tuple[Path, Path, tuple[str, ...]]:
    try:
        raw = os.fspath(instance_root)
    except TypeError as exc:
        raise WindowsArtifactError("invalid", "The instance path is invalid.") from exc
    if not isinstance(raw, str):
        raise WindowsArtifactError("invalid", "The instance path is invalid.")
    if raw.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\")):
        raise WindowsArtifactError("unsupported", "The instance path is unsupported.")
    win_path = PureWindowsPath(raw)
    if (
        not win_path.is_absolute()
        or len(win_path.drive) != 2
        or win_path.drive[1] != ":"
        or not win_path.drive[0].isascii()
        or not win_path.drive[0].isalpha()
        or win_path.root != "\\"
    ):
        raise WindowsArtifactError(
            "unsupported", "A local absolute drive path is required."
        )
    components = tuple(win_path.parts[1:])
    for component in components:
        _validate_component(component, ascii_only=False)
    normalized = Path(str(win_path))
    drive_root = Path(win_path.drive + "\\")
    _extended_path(drive_root)
    return normalized, drive_root, components


def _check_volume(api: _WinApi, drive_root: Path) -> None:
    # These volume APIs document a short root such as C:\\; keeping this
    # argument short also avoids relying on their extended-path support.
    root = str(drive_root)
    drive_type = int(api.GetDriveTypeW(root))
    if drive_type == _DRIVE_REMOTE or drive_type not in {
        _DRIVE_REMOVABLE,
        _DRIVE_FIXED,
        _DRIVE_RAMDISK,
    }:
        raise WindowsArtifactError("unsupported", "The artifact volume is unsupported.")
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    ctypes.set_last_error(0)
    if not api.GetVolumeInformationW(
        root,
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem_name,
        len(filesystem_name),
    ):
        _raise_last_error("unsupported", "The artifact volume security is unavailable.")
    if not flags.value & _FILE_PERSISTENT_ACLS:
        raise WindowsArtifactError(
            "unsupported", "The artifact volume does not support persistent ACLs."
        )


def _get_file_info(api: _WinApi, handle: Any) -> _BY_HANDLE_FILE_INFORMATION:
    info = _BY_HANDLE_FILE_INFORMATION()
    ctypes.set_last_error(0)
    if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        _raise_last_error("unavailable", "The artifact handle is unavailable.")
    return info


def _directory_info(api: _WinApi, handle: Any) -> None:
    info = _get_file_info(api, handle)
    if not info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise WindowsArtifactError("invalid", "An artifact directory is invalid.")
    if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise WindowsArtifactError(
            "invalid", "An artifact directory is a reparse point."
        )


def _open_directory(api: _WinApi, path: Path, *, share: int = _FILE_SHARE_READ) -> Any:
    ctypes.set_last_error(0)
    handle = api.CreateFileW(
        _extended_path(path),
        _READ_CONTROL | _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY,
        share,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not _valid_handle(handle):
        _raise_last_error("unavailable", "An artifact directory is unavailable.")
    try:
        _directory_info(api, handle)
        return handle
    except BaseException:
        _close_handle(api, handle)
        raise


def _open_instance_directories(
    instance_root: Path,
) -> tuple[Path, list[Any], _WinApi]:
    root, drive_root, components = _validate_instance_root(instance_root)
    try:
        api = _WinApi()
    except WindowsArtifactError:
        raise
    except (AttributeError, OSError) as exc:
        raise WindowsArtifactError(
            "unsupported", "Required Windows security APIs are unavailable."
        ) from exc
    _check_volume(api, drive_root)
    handles: list[Any] = []
    current = drive_root
    try:
        handle = _open_directory(api, current)
        handles.append(handle)
        for component in components:
            current = current / component
            handles.append(_open_directory(api, current))
        return root, handles, api
    except BaseException:
        for handle in reversed(handles):
            _close_handle(api, handle)
        raise


def _make_directory(path: Path) -> None:
    try:
        os.mkdir(_extended_path(path), mode=0o700)
    except FileExistsError:
        return
    except OSError as exc:
        raise WindowsArtifactError(
            "unavailable", "Private artifact storage could not be created."
        ) from exc


def _token_sid_bytes(
    api: _WinApi, token_handle: Any, info_class: int, info_type: type[Any]
) -> bytes:
    needed = wintypes.DWORD()
    ctypes.set_last_error(0)
    api.GetTokenInformation(token_handle, info_class, None, 0, ctypes.byref(needed))
    error_number = ctypes.get_last_error()
    if not needed.value or error_number != _ERROR_INSUFFICIENT_BUFFER:
        _raise_native_error(
            error_number or _ERROR_INVALID_PARAMETER,
            "unsupported",
            "The current Windows security token is unavailable.",
        )
    buffer = ctypes.create_string_buffer(needed.value)
    ctypes.set_last_error(0)
    if not api.GetTokenInformation(
        token_handle,
        info_class,
        buffer,
        needed.value,
        ctypes.byref(needed),
    ):
        _raise_last_error(
            "unsupported", "The current Windows security token is unavailable."
        )
    record = ctypes.cast(buffer, ctypes.POINTER(info_type)).contents
    sid = record.User.Sid if info_class == _TOKEN_USER else record.Owner
    if not sid or not api.IsValidSid(sid):
        raise WindowsArtifactError("unsupported", "The current Windows SID is invalid.")
    sid_length = int(api.GetLengthSid(sid))
    if not sid_length:
        raise WindowsArtifactError("unsupported", "The current Windows SID is invalid.")
    return ctypes.string_at(sid, sid_length)


def _known_sid_bytes(api: _WinApi, sid_text: str) -> bytes:
    sid = ctypes.c_void_p()
    ctypes.set_last_error(0)
    if not api.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
        _raise_last_error(
            "unsupported", "A required Windows security SID is unavailable."
        )
    try:
        length = int(api.GetLengthSid(sid))
        if not length:
            raise WindowsArtifactError(
                "unsupported", "A required Windows security SID is invalid."
            )
        return ctypes.string_at(sid, length)
    finally:
        api.LocalFree(sid)


def _private_owner_and_acl(api: _WinApi, handle: Any) -> None:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = int(
        api.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result:
        _raise_native_error(
            result, "unavailable", "Artifact security information is unavailable."
        )
    try:
        if not descriptor or not owner or not dacl:
            raise WindowsArtifactError(
                "invalid", "Artifact ownership or access control is invalid."
            )
        if not api.IsValidSid(owner):
            raise WindowsArtifactError("invalid", "Artifact ownership is invalid.")

        token = wintypes.HANDLE()
        ctypes.set_last_error(0)
        if not api.OpenProcessToken(
            api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            _raise_last_error(
                "unavailable", "The current Windows security token is unavailable."
            )
        try:
            current_user = _token_sid_bytes(api, token, _TOKEN_USER, _TOKEN_USER_INFO)
            default_owner = _token_sid_bytes(
                api, token, _TOKEN_OWNER, _TOKEN_OWNER_INFO
            )
        finally:
            _close_handle(api, token)

        owner_length = int(api.GetLengthSid(owner))
        if not owner_length:
            raise WindowsArtifactError("invalid", "Artifact ownership is invalid.")
        owner_bytes = ctypes.string_at(owner, owner_length)
        if owner_bytes not in {current_user, default_owner}:
            raise WindowsArtifactError("invalid", "Artifact ownership is not private.")

        allowed_sids = {
            current_user,
            _known_sid_bytes(api, "S-1-5-32-544"),  # BUILTIN\\Administrators
            _known_sid_bytes(api, "S-1-5-18"),  # Local System
            # Python's private mkdir ACL grants Owner Rights. The owner was
            # validated above, so this ACE applies only to that accepted owner.
            _known_sid_bytes(api, "S-1-3-4"),
        }
        acl_info = _ACL_SIZE_INFORMATION()
        ctypes.set_last_error(0)
        if not api.GetAclInformation(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            _raise_last_error("invalid", "Artifact access control is invalid.")
        for index in range(acl_info.AceCount):
            ace_pointer = ctypes.c_void_p()
            ctypes.set_last_error(0)
            if (
                not api.GetAce(dacl, index, ctypes.byref(ace_pointer))
                or not ace_pointer
            ):
                _raise_last_error("invalid", "Artifact access control is invalid.")
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(_ACE_HEADER)).contents
            if ace.AceType not in {_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE}:
                raise WindowsArtifactError(
                    "invalid", "Artifact access control uses an unsupported ACE."
                )
            sid_offset = ctypes.sizeof(_ACE_HEADER) + ctypes.sizeof(wintypes.DWORD)
            if ace.AceSize < sid_offset + 8:
                raise WindowsArtifactError(
                    "invalid", "Artifact access control is invalid."
                )
            if ace_pointer.value is None:
                raise WindowsArtifactError(
                    "invalid", "Artifact access control is invalid."
                )
            ace_sid = ctypes.c_void_p(ace_pointer.value + sid_offset)
            if not api.IsValidSid(ace_sid):
                raise WindowsArtifactError(
                    "invalid", "Artifact access control is invalid."
                )
            sid_length = int(api.GetLengthSid(ace_sid))
            if not sid_length or sid_offset + sid_length > ace.AceSize:
                raise WindowsArtifactError(
                    "invalid", "Artifact access control is invalid."
                )
            if ace.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                allowed_sid = ctypes.string_at(ace_sid, sid_length)
                if allowed_sid not in allowed_sids:
                    raise WindowsArtifactError(
                        "invalid",
                        "Artifact access control grants access to another principal.",
                    )
    finally:
        if descriptor:
            api.LocalFree(descriptor)


def _file_snapshot(api: _WinApi, handle: Any) -> tuple[int, ...]:
    info = _get_file_info(api, handle)
    if (
        info.dwFileAttributes
        & (
            _FILE_ATTRIBUTE_DIRECTORY
            | _FILE_ATTRIBUTE_REPARSE_POINT
            | _FILE_ATTRIBUTE_DEVICE
        )
        or int(api.GetFileType(handle)) != _FILE_TYPE_DISK
        or info.nNumberOfLinks != 1
    ):
        raise WindowsArtifactError(
            "invalid", "The export artifact is not a private regular file."
        )
    return (
        int(info.dwVolumeSerialNumber),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
        (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
        int(info.ftCreationTime.dwHighDateTime),
        int(info.ftCreationTime.dwLowDateTime),
        int(info.ftLastWriteTime.dwHighDateTime),
        int(info.ftLastWriteTime.dwLowDateTime),
    )


def _open_regular_file(
    api: _WinApi,
    path: Path,
    *,
    access: int,
    share: int,
) -> Any:
    ctypes.set_last_error(0)
    handle = api.CreateFileW(
        _extended_path(path),
        access,
        share,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not _valid_handle(handle):
        _raise_last_error("unavailable", "The export artifact is unavailable.")
    try:
        _file_snapshot(api, handle)
        _private_owner_and_acl(api, handle)
        return handle
    except BaseException:
        _close_handle(api, handle)
        raise


def _destination_exists(api: _WinApi, path: Path) -> bool:
    ctypes.set_last_error(0)
    handle = api.CreateFileW(
        _extended_path(path),
        0,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _valid_handle(handle):
        _close_handle(api, handle)
        return True
    error_number = ctypes.get_last_error()
    if error_number in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        return False
    _raise_native_error(
        error_number or _ERROR_ACCESS_DENIED,
        "unavailable",
        "The export destination could not be checked.",
    )


def _create_stage_file(api: _WinApi, path: Path) -> tuple[int, ...] | None:
    ctypes.set_last_error(0)
    handle = api.CreateFileW(
        _extended_path(path),
        _GENERIC_WRITE | _DELETE | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ,
        None,
        _CREATE_NEW,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not _valid_handle(handle):
        error_number = ctypes.get_last_error()
        if error_number in {_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS}:
            return None
        _raise_native_error(
            error_number or _ERROR_ACCESS_DENIED,
            "unavailable",
            "The export staging file could not be reserved.",
        )
    try:
        snapshot = _file_snapshot(api, handle)
        _private_owner_and_acl(api, handle)
    except BaseException:  # Also clean up if cancellation interrupts reservation.
        with suppress(OSError):
            _mark_delete_pending(api, handle)
        _close_handle(api, handle)
        raise
    _close_handle(api, handle)
    return snapshot


def _file_size(snapshot: tuple[int, ...]) -> int:
    return snapshot[3]


def _file_identity(snapshot: tuple[int, ...]) -> tuple[int, ...]:
    """Return stable handle identity fields, excluding mutable size/time data."""

    return (*snapshot[:3], *snapshot[4:6])


def _hold_operation_directory(api: _WinApi, path: Path, handles: list[Any]) -> Any:
    """Permit child renames while a private guard prevents reparse insertion.

    The initial directory handle denies both write and delete sharing. Create
    the guard before allowing write sharing; its held no-delete-shared handle
    keeps this directory nonempty for the object's entire lifetime. All ancestor
    handles retain their stricter sharing mode.
    """

    guard_handle = None
    replacement_handle = None
    try:
        for _attempt in range(3):
            guard_path = path / f".guard-{uuid.uuid4().hex}"
            ctypes.set_last_error(0)
            guard_handle = api.CreateFileW(
                _extended_path(guard_path),
                _GENERIC_READ | _DELETE | _READ_CONTROL,
                _FILE_SHARE_READ,
                None,
                _CREATE_NEW,
                _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_DELETE_ON_CLOSE,
                None,
            )
            if _valid_handle(guard_handle):
                break
            error_number = ctypes.get_last_error()
            guard_handle = None
            if error_number not in {_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS}:
                _raise_native_error(
                    error_number or _ERROR_ACCESS_DENIED,
                    "unavailable",
                    "The private artifact directory guard is unavailable.",
                )
        if guard_handle is None:
            raise WindowsArtifactError(
                "unavailable", "The private artifact directory guard is unavailable."
            )
        _file_snapshot(api, guard_handle)
        _private_owner_and_acl(api, guard_handle)

        replacement_handle = _open_directory(
            api, path, share=_FILE_SHARE_READ | _FILE_SHARE_WRITE
        )
        original = _get_file_info(api, handles[-1])
        replacement = _get_file_info(api, replacement_handle)
        original_identity = (
            original.dwVolumeSerialNumber,
            original.nFileIndexHigh,
            original.nFileIndexLow,
        )
        replacement_identity = (
            replacement.dwVolumeSerialNumber,
            replacement.nFileIndexHigh,
            replacement.nFileIndexLow,
        )
        if original_identity != replacement_identity:
            raise WindowsArtifactError(
                "invalid", "The private artifact directory changed."
            )
        _close_handle(api, handles[-1])
        handles[-1] = replacement_handle
        replacement_handle = None
        return guard_handle
    except BaseException:
        _close_handle(api, replacement_handle)
        _close_handle(api, guard_handle)
        raise


def _mark_delete_pending(api: _WinApi, handle: Any) -> None:
    disposition = _FILE_DISPOSITION_INFO(True)
    ctypes.set_last_error(0)
    if not api.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        _raise_last_error("unavailable", "The owned export stage could not be removed.")


class WindowsArtifactDirectory:
    """A held, private operation directory on an ACL-capable Windows volume."""

    def __init__(
        self,
        path: Path,
        namespace: str,
        operation_id: str,
        api: _WinApi,
        directory_handles: list[Any],
        guard_handle: Any,
    ) -> None:
        self.path = path
        self.namespace = namespace
        self.operation_id = operation_id
        self._api = api
        self._directory_handles = directory_handles
        self._guard_handle = guard_handle
        self._reserved_stages: dict[str, tuple[str, tuple[int, ...]]] = {}
        self._closed = False

    @classmethod
    def prepare(cls, instance_root: Path, namespace: str, operation_id: str) -> Self:
        """Create or validate the private operation directory and hold its path."""

        _validate_namespace_operation(namespace, operation_id)
        root, handles, api = _open_instance_directories(instance_root)
        guard_handle = None
        try:
            namespace_path = root / namespace
            _make_directory(namespace_path)
            namespace_handle = _open_directory(api, namespace_path)
            handles.append(namespace_handle)
            _private_owner_and_acl(api, namespace_handle)

            operation_path = namespace_path / operation_id
            _make_directory(operation_path)
            operation_handle = _open_directory(api, operation_path)
            handles.append(operation_handle)
            _private_owner_and_acl(api, operation_handle)
            guard_handle = _hold_operation_directory(api, operation_path, handles)
            return cls(
                operation_path,
                namespace,
                operation_id,
                api,
                handles,
                guard_handle,
            )
        except BaseException:
            _close_handle(api, guard_handle)
            for handle in reversed(handles):
                _close_handle(api, handle)
            raise

    @classmethod
    def open_existing(
        cls, instance_root: Path, namespace: str, operation_id: str
    ) -> Self:
        """Open an existing private operation directory without creating paths."""

        _validate_namespace_operation(namespace, operation_id)
        root, handles, api = _open_instance_directories(instance_root)
        guard_handle = None
        try:
            namespace_path = root / namespace
            namespace_handle = _open_directory(api, namespace_path)
            handles.append(namespace_handle)
            _private_owner_and_acl(api, namespace_handle)

            operation_path = namespace_path / operation_id
            operation_handle = _open_directory(api, operation_path)
            handles.append(operation_handle)
            _private_owner_and_acl(api, operation_handle)
            guard_handle = _hold_operation_directory(api, operation_path, handles)
            return cls(
                operation_path,
                namespace,
                operation_id,
                api,
                handles,
                guard_handle,
            )
        except BaseException:
            _close_handle(api, guard_handle)
            for handle in reversed(handles):
                _close_handle(api, handle)
            raise

    def reserve_stage(self, export_format: object, final_name: str) -> tuple[str, Path]:
        """Create a unique stage file without replacing an existing final name."""

        self._ensure_open()
        if (
            not isinstance(export_format, str)
            or _FORMAT_RE.fullmatch(export_format) is None
        ):
            raise WindowsArtifactError("invalid", "The export format is invalid.")
        _validate_component(final_name, ascii_only=True)
        if len(final_name) > 255 or not final_name.endswith(f".{export_format}"):
            raise WindowsArtifactError("invalid", "The export filename is invalid.")
        final_path = self.path / final_name
        if _destination_exists(self._api, final_path):
            raise FileExistsError(
                errno.EEXIST, "The export destination already exists."
            )

        for _attempt in range(3):
            stage_name = f".stage-{uuid.uuid4().hex}.{export_format}"
            stage_path = self.path / stage_name
            snapshot = _create_stage_file(self._api, stage_path)
            if snapshot is None:
                continue
            self._reserved_stages[stage_name] = (final_name, _file_identity(snapshot))
            return stage_name, stage_path
        raise WindowsArtifactError(
            "unavailable", "The export stage could not be reserved."
        )

    def publish(self, stage_name: str, final_name: str) -> tuple[int, str]:
        """Hash, flush, and atomically rename the same opened stage-file handle."""

        self._ensure_open()
        _validate_component(final_name, ascii_only=True)
        reserved = self._reserved_stages.get(stage_name)
        if (
            _STAGE_NAME_RE.fullmatch(stage_name) is None
            or reserved is None
            or reserved[0] != final_name
        ):
            raise WindowsArtifactError(
                "invalid", "The export stage is not owned by this operation."
            )
        if _destination_exists(self._api, self.path / final_name):
            raise FileExistsError(
                errno.EEXIST, "The export destination already exists."
            )

        handle = _open_regular_file(
            self._api,
            self.path / stage_name,
            access=_GENERIC_READ | _GENERIC_WRITE | _DELETE | _READ_CONTROL,
            share=_FILE_SHARE_READ,
        )
        try:
            before = _file_snapshot(self._api, handle)
            if _file_identity(before) != reserved[1]:
                raise WindowsArtifactError(
                    "invalid", "The export stage is not the reserved file."
                )
            if not self._api.SetFilePointerEx(handle, 0, None, 0):
                _raise_last_error("unavailable", "The export stage could not be read.")
            digest = hashlib.sha256()
            byte_length = 0
            buffer = ctypes.create_string_buffer(1024 * 1024)
            while True:
                received = wintypes.DWORD()
                ctypes.set_last_error(0)
                if not self._api.ReadFile(
                    handle,
                    buffer,
                    len(buffer),
                    ctypes.byref(received),
                    None,
                ):
                    _raise_last_error(
                        "unavailable", "The export stage could not be read."
                    )
                if not received.value:
                    break
                digest.update(buffer.raw[: received.value])
                byte_length += int(received.value)
            if not self._api.FlushFileBuffers(handle):
                _raise_last_error(
                    "unavailable", "The export stage could not be flushed."
                )
            after = _file_snapshot(self._api, handle)
            if before != after or byte_length != _file_size(after):
                raise WindowsArtifactError(
                    "invalid", "The export stage changed while it was being published."
                )

            encoded_name = _extended_path(self.path / final_name).encode("utf-16-le")
            buffer_size = _FILE_RENAME_NAME_OFFSET + len(encoded_name) + 2
            rename_buffer = ctypes.create_string_buffer(buffer_size)
            rename_info = _FILE_RENAME_INFO_PREFIX.from_buffer(rename_buffer)
            rename_info.ReplaceIfExists = 0
            rename_info.RootDirectory = None
            rename_info.FileNameLength = len(encoded_name)
            ctypes.memmove(
                ctypes.addressof(rename_buffer) + _FILE_RENAME_NAME_OFFSET,
                encoded_name,
                len(encoded_name),
            )
            ctypes.set_last_error(0)
            if not self._api.SetFileInformationByHandle(
                handle,
                _FILE_RENAME_INFO_CLASS,
                ctypes.cast(rename_buffer, ctypes.c_void_p),
                buffer_size,
            ):
                error_number = ctypes.get_last_error()
                if error_number in {_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS}:
                    raise FileExistsError(
                        errno.EEXIST, "The export destination already exists."
                    ) from ctypes.WinError(error_number)
                _raise_native_error(
                    error_number or _ERROR_ACCESS_DENIED,
                    "unavailable",
                    "The export stage could not be published.",
                )
            self._reserved_stages.pop(stage_name, None)
            return byte_length, digest.hexdigest()
        finally:
            _close_handle(self._api, handle)

    def open_file(self, name: str) -> int:
        """Open one verified private file and transfer its native handle to CRT."""

        self._ensure_open()
        _validate_component(name, ascii_only=True)
        handle = _open_regular_file(
            self._api,
            self.path / name,
            access=_GENERIC_READ | _READ_CONTROL,
            share=_FILE_SHARE_READ,
        )
        try:
            import msvcrt

            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
            self.close()
            return descriptor
        except BaseException:
            _close_handle(self._api, handle)
            self.close()
            raise

    def remove_stage(self, name: str) -> None:
        """Remove only a stage reserved by this object; ignore published stages."""

        self._ensure_open()
        reserved = self._reserved_stages.get(name)
        if reserved is None:
            return
        if _STAGE_NAME_RE.fullmatch(name) is None:
            raise WindowsArtifactError("invalid", "The export stage name is invalid.")
        try:
            handle = _open_regular_file(
                self._api,
                self.path / name,
                access=_DELETE | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                share=_FILE_SHARE_READ,
            )
        except WindowsArtifactError as exc:
            native_error = getattr(exc.__cause__, "winerror", None)
            if native_error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                self._reserved_stages.pop(name, None)
                return
            raise
        try:
            current = _file_identity(_file_snapshot(self._api, handle))
            if current != reserved[1]:
                raise WindowsArtifactError(
                    "invalid", "The export stage is not the reserved file."
                )
            _mark_delete_pending(self._api, handle)
            self._reserved_stages.pop(name, None)
        finally:
            _close_handle(self._api, handle)

    def _ensure_open(self) -> None:
        if self._closed:
            raise WindowsArtifactError(
                "unavailable", "The artifact directory is closed."
            )

    def close(self) -> None:
        """Close all held directory handles. Safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        guard_handle, self._guard_handle = self._guard_handle, None
        _close_handle(self._api, guard_handle)
        handles, self._directory_handles = self._directory_handles, []
        for handle in reversed(handles):
            _close_handle(self._api, handle)

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
