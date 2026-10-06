#pragma once
#include <windows.h>
#include <processsnapshot.h>
#include <algorithm>
#include <cwctype>
#include <set>
#include <string>

inline std::string AccountIdUtf8(const std::wstring& value)
{
    int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) return {};
    std::string encoded(size, '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), encoded.data(), size, nullptr, nullptr)) return {};
    return encoded;
}

// Read only the account directory of open contact database handles. This is
// not a login-status API. Callers must also compare with authenticated DB data.
struct AccountSnapshotStats {
    DWORD capture_status = ERROR_SUCCESS;
    DWORD walk_status = ERROR_SUCCESS;
    unsigned handles = 0;
    unsigned named_handles = 0;
    unsigned file_handles = 0;
    unsigned wechat_paths = 0;
};

inline std::set<std::wstring> OpenWeixinDatabaseAccounts(HANDLE process, AccountSnapshotStats* stats = nullptr)
{
    std::set<std::wstring> accounts;
    HPSS snapshot = nullptr;
    HPSSWALK marker = nullptr;
    auto flags = static_cast<PSS_CAPTURE_FLAGS>(PSS_CAPTURE_HANDLES | PSS_CAPTURE_HANDLE_NAME_INFORMATION | PSS_CAPTURE_HANDLE_BASIC_INFORMATION);
    DWORD capture = PssCaptureSnapshot(process, flags, 0, &snapshot);
    if (stats) stats->capture_status = capture;
    if (capture != ERROR_SUCCESS)
        return accounts;
    if (PssWalkMarkerCreate(nullptr, &marker) == ERROR_SUCCESS) {
        PSS_HANDLE_ENTRY entry = {};
        DWORD status;
        while ((status = PssWalkSnapshot(snapshot, PSS_WALK_HANDLES, marker, &entry, sizeof(entry))) == ERROR_SUCCESS) {
            if (stats) ++stats->handles;
            std::wstring path;
            if (entry.ObjectName && entry.ObjectNameLength && !(entry.ObjectNameLength % sizeof(wchar_t))) {
                path.assign(entry.ObjectName, entry.ObjectNameLength / sizeof(wchar_t));
                if (stats) ++stats->named_handles;
            }
            {
                HANDLE duplicate = nullptr;
                if (DuplicateHandle(process, entry.Handle, GetCurrentProcess(), &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS)) {
                    if (GetFileType(duplicate) != FILE_TYPE_DISK) {
                        CloseHandle(duplicate);
                        continue;
                    }
                    if (stats) ++stats->file_handles;
                    wchar_t name[32768] = {};
                    DWORD length = GetFinalPathNameByHandleW(duplicate, name, 32768, FILE_NAME_NORMALIZED);
                    CloseHandle(duplicate);
                    if (length && length < 32768) path.assign(name, length);
                }
            }
            if (path.empty()) continue;
            std::wstring lower = path;
            std::transform(lower.begin(), lower.end(), lower.begin(), [](wchar_t c) { return std::towlower(c); });
            const std::wstring prefix = L"\\xwechat_files\\";
            const std::wstring suffix = L"\\db_storage\\contact\\contact.db";
            auto begin = lower.find(prefix);
            if (begin != std::wstring::npos && stats) ++stats->wechat_paths;
            if (begin == std::wstring::npos || lower.size() < suffix.size() ||
                lower.compare(lower.size() - suffix.size(), suffix.size(), suffix) != 0)
                continue;
            begin += prefix.size();
            auto end = path.find(L'\\', begin);
            if (end == std::wstring::npos) continue;
            auto folder = path.substr(begin, end - begin);
            auto split = folder.rfind(L'_');
            if (split == std::wstring::npos || split == 0 || folder.size() - split - 1 != 4)
                continue;
            auto tail = folder.substr(split + 1);
            if (!std::all_of(tail.begin(), tail.end(), [](wchar_t c) { return std::iswxdigit(c) != 0; }))
                continue;
            accounts.insert(folder.substr(0, split));
        }
        if (stats) stats->walk_status = status;
        if (status != ERROR_NO_MORE_ITEMS) accounts.clear();
        PssWalkMarkerFree(marker);
    }
    PssFreeSnapshot(process, snapshot);
    return accounts;
}
