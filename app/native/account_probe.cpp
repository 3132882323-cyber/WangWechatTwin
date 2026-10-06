#include "account_snapshot.h"
#include <fstream>
#include <iostream>

int wmain(int argc, wchar_t** argv)
{
    if (argc != 3) return 2;
    DWORD pid = static_cast<DWORD>(std::stoul(argv[1]));
    std::ifstream expectedFile(argv[2]);
    std::string expected;
    std::getline(expectedFile, expected);
    if (expected.empty()) return 3;
    std::wstring expectedWide(expected.begin(), expected.end());
    HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_DUP_HANDLE, FALSE, pid);
    if (!process) { std::cout << "{\"accessible\":false}\n"; return 4; }
    AccountSnapshotStats stats;
    auto accounts = OpenWeixinDatabaseAccounts(process, &stats);
    CloseHandle(process);
    bool matched = accounts.size() == 1 && *accounts.begin() == expectedWide;
    std::cout << "{\"accessible\":true,\"active_database_account_count\":" << accounts.size()
        << ",\"matches_authenticated_owner\":" << (matched ? "true" : "false")
        << ",\"capture_status\":" << stats.capture_status << ",\"walk_status\":" << stats.walk_status
        << ",\"handles\":" << stats.handles << ",\"named_handles\":" << stats.named_handles
        << ",\"file_handles\":" << stats.file_handles
        << ",\"wechat_paths\":" << stats.wechat_paths
        << ",\"gui_used\":false,\"process_modified\":false}\n";
    return matched ? 0 : 5;
}

