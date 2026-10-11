"""Windows restricted processes with optional AppContainer network identity.

No account, profile, registry or shared ACL is created. The caller must provision
the runtime and workspace ACLs separately. Protected deployment still requires
four actual Windows identities, firewall rules and independently observed acceptance.
"""

import ctypes
import os
import re
import subprocess
import uuid
from ctypes import wintypes as w
from pathlib import Path

from .util import require


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("x", w.DWORD),
        ("y", w.DWORD),
        ("width", w.DWORD),
        ("height", w.DWORD),
        ("chars_x", w.DWORD),
        ("chars_y", w.DWORD),
        ("fill", w.DWORD),
        ("flags", w.DWORD),
        ("show", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_bytes", ctypes.c_void_p),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", w.DWORD)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("appcontainer_sid", ctypes.c_void_p),
        ("capabilities", ctypes.POINTER(_SidAndAttributes)),
        ("capability_count", w.DWORD),
        ("reserved", w.DWORD),
    ]


def _observed_identity(process, expected_restricted, expected_appcontainer, advapi, kernel):
    """Read the suspended child's token: what it carries, never what was requested."""
    import win32con
    import win32security as security

    advapi.GetTokenInformation.argtypes = [
        w.HANDLE,
        w.DWORD,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    token = security.OpenProcessToken(process, win32con.TOKEN_QUERY)
    try:
        restricted = sorted(
            security.ConvertSidToStringSid(sid)
            for sid, _ in security.GetTokenInformation(token, security.TokenRestrictedSids)
        )
        require(
            restricted == sorted(expected_restricted),
            "PROCESS_IDENTITY",
            "Child token does not carry the restricting SIDs",
        )
        flag, size = w.DWORD(), w.DWORD()
        if not advapi.GetTokenInformation(  # 29 = TokenIsAppContainer
            int(token), 29, ctypes.byref(flag), ctypes.sizeof(flag), ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        require(
            bool(flag.value) == bool(expected_appcontainer),
            "PROCESS_IDENTITY",
            "Child token AppContainer state differs from the launch",
        )
        if not expected_appcontainer:
            return restricted, None
        advapi.GetTokenInformation(int(token), 31, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)  # 31 = TokenAppContainerSid
        if not advapi.GetTokenInformation(int(token), 31, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        text = w.LPWSTR()
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            observed = text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        require(
            observed.lower() == expected_appcontainer.lower(),
            "PROCESS_IDENTITY",
            "Child token carries a different AppContainer SID",
        )
        return restricted, observed
    finally:
        token.Close()


class RestrictedProcess:
    """Fixed inherited pipe handles, restricted token, non-breakaway job tree.

    The caller owns stdin/stdout. Close terminates the entire job, including
    descendants after their original parent exits. Never fall back to Popen.
    """

    def __init__(self, argv, *, cwd, env, expected_sid=None, appcontainer_sid=None):
        require(os.name == "nt", "WINDOWS_REQUIRED", "Restricted process requires Windows")
        require(
            argv
            and all(isinstance(a, str) and "\0" not in a for a in argv)
            and Path(argv[0]).is_absolute()
            and Path(argv[0]).is_file(),
            "PROCESS_CONFIG",
            "Absolute existing executable and NUL-free arguments required",
        )
        require(
            Path(cwd).is_absolute() and Path(cwd).is_dir(),
            "PROCESS_CONFIG",
            "Absolute cwd required",
        )
        require(
            isinstance(env, dict)
            and len({k.upper() for k in env if isinstance(k, str)}) == len(env)
            and all(
                isinstance(k, str)
                and isinstance(v, str)
                and k
                and "=" not in k
                and "\0" not in k + v
                for k, v in env.items()
            ),
            "PROCESS_CONFIG",
            "Explicit valid environment required",
        )
        require(
            appcontainer_sid is None
            or (
                isinstance(appcontainer_sid, str)
                and re.fullmatch(r"S-1-15-2(?:-[0-9]+)+", appcontainer_sid)
                and "\0" not in appcontainer_sid
            ),
            "PROCESS_CONFIG",
            "AppContainer SID must be explicit",
        )
        import msvcrt

        import pywintypes
        import win32api
        import win32con
        import win32job
        import win32pipe
        import win32process
        import win32security as security

        self.process = self.job = self.stdin = self.stdout = None
        handles = []
        token = original = None
        appcontainer_pointer = ctypes.c_void_p()
        attrs = None
        attributes_initialized = False
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            w.DWORD,
            w.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        advapi.CreateProcessAsUserW.argtypes = [
            w.HANDLE,
            w.LPCWSTR,
            w.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            w.BOOL,
            w.DWORD,
            ctypes.c_void_p,
            w.LPCWSTR,
            ctypes.POINTER(_StartupEx),
            ctypes.POINTER(_ProcessInfo),
        ]
        advapi.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        advapi.ConvertStringSidToSidW.restype = w.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            original = security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS
            )
            self.sid = security.ConvertSidToStringSid(
                security.GetTokenInformation(original, security.TokenUser)[0]
            )
            require(
                not security.GetTokenInformation(original, security.TokenElevation),
                "PROCESS_IDENTITY",
                "Do not launch the writer from an elevated process",
            )
            require(
                expected_sid is None or self.sid == expected_sid,
                "PROCESS_IDENTITY",
                "Process is not running under the configured writer SID",
            )
            # Public Windows objects need Users/Everyone; private authority ACLs
            # must never grant those groups. The normal SID check also applies.
            sids = [security.ConvertStringSidToSid(s) for s in ("S-1-5-32-545", "S-1-1-0")]
            sids += [
                s
                for s, flags in security.GetTokenInformation(original, security.TokenGroups)
                if flags & 0xC0000000 == 0xC0000000
            ]
            value = uuid.uuid4().int
            capability = security.ConvertStringSidToSid(
                f"S-1-5-21-{value & 0xFFFFFFFF}-{(value >> 32) & 0xFFFFFFFF}-{(value >> 64) & 0xFFFFFFFF}-31337"
            )
            sids.append(capability)
            token = security.CreateRestrictedToken(
                original, security.DISABLE_MAX_PRIVILEGE, [], [], [(s, 0) for s in sids]
            )
            # Without this ACE the loader cannot access its own process objects
            # and exits before any attempted operation (not a prevention proof).
            dacl = security.GetTokenInformation(token, security.TokenDefaultDacl)
            dacl.AddAccessAllowedAce(security.ACL_REVISION, win32con.GENERIC_ALL, capability)
            security.SetTokenInformation(token, security.TokenDefaultDacl, dacl)
            self.restricted_sids = [security.ConvertSidToStringSid(s) for s in sids]
            sa = security.SECURITY_ATTRIBUTES()
            sa.bInheritHandle = True
            child_in, parent_in = win32pipe.CreatePipe(sa, 0)
            parent_out, child_out = win32pipe.CreatePipe(sa, 0)
            handles.extend((child_in, parent_in, parent_out, child_out))
            for handle in (parent_in, parent_out):
                win32api.SetHandleInformation(handle, 1, 0)
            size = ctypes.c_size_t()
            attribute_count = 2 if appcontainer_sid else 1
            kernel.InitializeProcThreadAttributeList(None, attribute_count, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(
                attrs, attribute_count, 0, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            attributes_initialized = True
            inherited = (w.HANDLE * 2)(int(child_in), int(child_out))
            if not kernel.UpdateProcThreadAttribute(
                attrs, 0, 0x20002, inherited, ctypes.sizeof(inherited), None, None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if appcontainer_sid:
                if not advapi.ConvertStringSidToSidW(
                    appcontainer_sid, ctypes.byref(appcontainer_pointer)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                # No network capability: the registered loopback exemption alone reaches
                # the proxy, and privateNetworkClientServer would have opened the LAN.
                sandbox = _SecurityCapabilities(appcontainer_pointer, None, 0, 0)
                if not kernel.UpdateProcThreadAttribute(
                    attrs,
                    0,
                    0x20009,
                    ctypes.byref(sandbox),
                    ctypes.sizeof(sandbox),
                    None,
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = win32con.STARTF_USESTDHANDLES
            startup.startup.stdin = int(child_in)
            startup.startup.stdout = startup.startup.stderr = int(child_out)
            startup.attributes = ctypes.cast(attrs, ctypes.c_void_p)
            environment = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].upper())
                )
                + "\0\0"
            )
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            info = _ProcessInfo()
            flags = (
                win32con.CREATE_SUSPENDED
                | win32con.CREATE_NO_WINDOW
                | win32con.CREATE_UNICODE_ENVIRONMENT
                | 0x80000
            )
            if not advapi.CreateProcessAsUserW(
                int(token),
                argv[0],
                command,
                None,
                None,
                True,
                flags,
                environment,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.process = pywintypes.HANDLE(info.process)
            thread = pywintypes.HANDLE(info.thread)
            self.pid = info.pid
            handles.append(thread)
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, limits
            )
            win32job.AssignProcessToJobObject(self.job, self.process)
            require(
                win32job.IsProcessInJob(self.process, self.job),
                "PROCESS_IDENTITY",
                "Child is not confined by the job",
            )
            # Evidence describes the token the child carries, not the parameters requested.
            self.restricted_sids, self.appcontainer_sid = _observed_identity(
                self.process, self.restricted_sids, appcontainer_sid, advapi, kernel
            )
            self.stdin = os.fdopen(
                msvcrt.open_osfhandle(parent_in.Detach(), os.O_WRONLY | os.O_BINARY),
                "wb",
                buffering=0,
            )
            self.stdout = os.fdopen(
                msvcrt.open_osfhandle(parent_out.Detach(), os.O_RDONLY | os.O_BINARY),
                "rb",
                buffering=0,
            )
            win32process.ResumeThread(thread)
        except BaseException:
            if self.process is not None:
                win32process.TerminateProcess(self.process, 1)
            self.close()
            raise
        finally:
            if attributes_initialized:
                kernel.DeleteProcThreadAttributeList(attrs)
            for handle in handles:
                handle.Close()
            if token is not None:
                token.Close()
            if original is not None:
                original.Close()
            if appcontainer_pointer:
                kernel.LocalFree(appcontainer_pointer)

    def wait(self, timeout=30):
        import win32event
        import win32process

        require(0 < timeout <= 3600, "PROCESS_CONFIG", "Timeout must be within 0..3600 seconds")
        if win32event.WaitForSingleObject(self.process, int(timeout * 1000)) != 0:
            raise subprocess.TimeoutExpired("restricted process", timeout)
        return win32process.GetExitCodeProcess(self.process)

    def close(self):
        if self.job is not None:
            self.job.Close()
            self.job = None
        if self.process is not None:
            self.process.Close()
            self.process = None
        for stream in (self.stdin, self.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
