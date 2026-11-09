"""Device tokens live only in the operating system's credential store, never in argv or files."""
import ctypes as c
import sys


class CredentialError(Exception):
    pass


class Credentials:
    def __init__(self, cloud):
        self.service = "co.dgit.velcro.cloud:" + cloud

    def get(self):
        return self._access(None)

    def set(self, token):
        self._access(token)

    def _access(self, token):
        try:
            if sys.platform == "darwin":
                return self._mac(token)
            if sys.platform == "win32":
                return self._windows(token)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Could not access the system credential store") from None
        raise CredentialError("Cloud login needs macOS Keychain or Windows Credential Manager")

    def _mac(self, token):
        cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr = c.c_void_p
        signatures = {
            "CFStringCreateWithCString": ([ptr, c.c_char_p, c.c_uint32], ptr),
            "CFDictionaryCreateMutable": ([ptr, c.c_long, ptr, ptr], ptr),
            "CFDictionarySetValue": ([ptr, ptr, ptr], None),
            "CFDataCreate": ([ptr, c.c_char_p, c.c_long], ptr),
            "CFDataGetLength": ([ptr], c.c_long),
            "CFDataGetBytePtr": ([ptr], ptr), "CFRelease": ([ptr], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(cf, name); fn.argtypes, fn.restype = args, result
        for name, args in {"SecItemCopyMatching": [ptr, c.POINTER(ptr)],
                           "SecItemUpdate": [ptr, ptr], "SecItemAdd": [ptr, ptr]}.items():
            fn = getattr(sec, name); fn.argtypes, fn.restype = args, c.c_int32
        owned = []

        def own(value):
            owned.append(value)
            return value

        def constant(name):
            return ptr.in_dll(sec if name.startswith("kSec") else cf, name).value

        def string(value):
            return own(cf.CFStringCreateWithCString(None, value.encode(), 0x08000100))

        def dictionary(values):
            d = own(cf.CFDictionaryCreateMutable(None, 0, None, None))
            for key, value in values.items():
                cf.CFDictionarySetValue(d, constant(key), value)
            return d

        try:
            query = dictionary({"kSecClass": constant("kSecClassGenericPassword"),
                                "kSecAttrService": string(self.service), "kSecAttrAccount": string("device")})
            if token is None:
                cf.CFDictionarySetValue(query, constant("kSecReturnData"), constant("kCFBooleanTrue"))
                out = ptr()
                status = sec.SecItemCopyMatching(query, c.byref(out))
                if status == -25300:  # errSecItemNotFound
                    return ""
                if status != 0:
                    raise CredentialError("Unlock the login Keychain and try velcro login again")
                own(out.value)
                return c.string_at(cf.CFDataGetBytePtr(out), cf.CFDataGetLength(out)).decode()
            raw = token.encode()
            data = own(cf.CFDataCreate(None, raw, len(raw)))
            update = dictionary({"kSecValueData": data})
            status = sec.SecItemUpdate(query, update)
            if status == -25300:
                cf.CFDictionarySetValue(query, constant("kSecValueData"), data)
                status = sec.SecItemAdd(query, None)
            if status != 0:
                raise CredentialError("Could not save the device login in Keychain")
        finally:
            for value in reversed(owned):
                cf.CFRelease(value)

    def _windows(self, token):
        from ctypes import wintypes as w

        class Credential(c.Structure):
            _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                        ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME), ("CredentialBlobSize", w.DWORD),
                        ("CredentialBlob", c.c_void_p), ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                        ("Attributes", c.c_void_p), ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]
        api = c.WinDLL("advapi32", use_last_error=True)
        api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(c.POINTER(Credential))]
        api.CredReadW.restype = w.BOOL
        api.CredWriteW.argtypes, api.CredWriteW.restype = [c.POINTER(Credential), w.DWORD], w.BOOL
        api.CredFree.argtypes = [c.c_void_p]
        if token is None:
            out = c.POINTER(Credential)()
            if not api.CredReadW(self.service, 1, 0, c.byref(out)):
                if c.get_last_error() == 1168:  # ERROR_NOT_FOUND
                    return ""
                raise CredentialError("Could not read Windows Credential Manager")
            try:
                return c.string_at(out.contents.CredentialBlob, out.contents.CredentialBlobSize).decode()
            finally:
                api.CredFree(out)
        blob = c.create_string_buffer(token.encode())
        item = Credential(Type=1, TargetName=self.service, UserName="device", Persist=2,
                          CredentialBlobSize=len(token.encode()), CredentialBlob=c.cast(blob, c.c_void_p))
        if not api.CredWriteW(c.byref(item), 0):
            raise CredentialError("Could not save the device login in Windows Credential Manager")
