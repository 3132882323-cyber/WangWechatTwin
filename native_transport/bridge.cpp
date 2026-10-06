// Copyright 2026 WeChat Twin contributors. SPDX-License-Identifier: Apache-2.0
// Native interop layout derived from authorized 4.1.15.13 runtime observations.
// This module contains no GUI driver, injection hooks, or license bypass.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <array>
#include <fstream>
#include <mutex>
#include <unordered_set>
#include "deps/httplib.h"
#include "deps/json.hpp"
#include "../app/native/account_snapshot.h"
#pragma comment(lib,"bcrypt.lib")
using json=nlohmann::json;
static HMODULE ownModule;
static uintptr_t imageBase;
static std::wstring directory;
static httplib::Server server;
static std::mutex sendLock;
static std::unordered_set<std::string> attempts;
static constexpr uintptr_t constructText=0x766680, constructOptions=0xf910, dispatch=0x19d0bc0;
static constexpr uintptr_t messageControl=0x8e534f8, vectorControl=0x9112308;
static constexpr uintptr_t callbackTypes[3]={0x957c528,0x957c468,0x957c3c8};

static std::string TextFile(const wchar_t* name) {
    std::ifstream input((directory+L"\\"+name).c_str());
    std::string value;std::getline(input,value);
    if(!value.empty() && value.back()=='\r')value.pop_back();
    return value;
}
static void Status(const char* value) {
    std::ofstream output((directory+L"\\status.txt").c_str(),std::ios::trunc);output<<value;
}
static bool KnownImage(HMODULE module) {
    wchar_t path[32768]={};if(!GetModuleFileNameW(module,path,32768))return false;
    std::ifstream file(path,std::ios::binary);if(!file)return false;
    BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
    DWORD size=0,returned=0;bool ok=false;
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)return false;
    if(BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&size),sizeof(size),&returned,0)>=0) {
        std::vector<unsigned char> object(size),buffer(65536);
        if(BCryptCreateHash(algorithm,&hash,object.data(),size,nullptr,0,0)>=0) {
            ok=true;
            while(file) {
                file.read(reinterpret_cast<char*>(buffer.data()),buffer.size());
                if(file.gcount() && BCryptHashData(hash,buffer.data(),static_cast<ULONG>(file.gcount()),0)<0){ok=false;break;}
            }
            unsigned char result[32]={};char hex[65]={};
            if(!ok || BCryptFinishHash(hash,result,32,0)<0)ok=false;
            else {
                for(int i=0;i<32;++i)sprintf_s(hex+i*2,3,"%02x",result[i]);
                ok=std::string(hex)=="10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5";
            }
            BCryptDestroyHash(hash);
        }
    }
    BCryptCloseAlgorithmProvider(algorithm,0);return ok;
}
static void* Allocate(size_t bytes) {
    auto pointer=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,bytes);
    if(!pointer)throw std::bad_alloc();return pointer;
}
static uint64_t& Word(void* memory,size_t offset) {
    return *reinterpret_cast<uint64_t*>(static_cast<char*>(memory)+offset);
}
static void StringField(void* message,size_t offset,const std::string& text) {
    auto field=static_cast<char*>(message)+offset;
    memset(field,0,32);
    if(text.size()<16){memcpy(field,text.data(),text.size());Word(field,24)=15;}
    else {
        auto bytes=Allocate(text.size()+1);memcpy(bytes,text.data(),text.size());
        Word(field,0)=reinterpret_cast<uint64_t>(bytes);Word(field,24)=((text.size()+16)&~size_t(15))-1;
    }
    Word(field,16)=text.size();
}
static std::string ReadString(void* message,size_t offset) {
    auto field=static_cast<char*>(message)+offset;
    auto bytes=Word(field,24)<16?field:reinterpret_cast<char*>(Word(field,0));
    return std::string(bytes,static_cast<size_t>(Word(field,16)));
}
struct NativeRequest {void* vector;void* options;void* message;};
static NativeRequest Build(const std::string& target,const std::string& text) {
    auto control=Allocate(0x1100);Word(control,0)=imageBase+messageControl;
    Word(control,8)=0x200000005;
    auto message=static_cast<char*>(control)+16;
    using Constructor=void(__fastcall*)(void*);
    reinterpret_cast<Constructor>(imageBase+constructText)(message);
    StringField(message,0xb0,target);StringField(message,0x758,text);
    Word(message,0x118)=1;Word(message,0x1c8)=text.size();
    auto element=Allocate(16);Word(element,0)=reinterpret_cast<uint64_t>(message);Word(element,8)=reinterpret_cast<uint64_t>(control);
    auto vector=Allocate(40);Word(vector,0)=imageBase+vectorControl;
    Word(vector,8)=reinterpret_cast<uint64_t>(element);Word(vector,16)=reinterpret_cast<uint64_t>(element)+16;
    Word(vector,24)=reinterpret_cast<uint64_t>(element)+16;Word(vector,32)=1;
    std::array<void*,3> functions={Allocate(64),Allocate(64),Allocate(64)};
    auto heapCallback=Allocate(512);Word(heapCallback,0)=imageBase+callbackTypes[0];
    Word(functions[0],56)=reinterpret_cast<uint64_t>(heapCallback);
    for(int i=1;i<3;++i){Word(functions[i],0)=imageBase+callbackTypes[i];Word(functions[i],56)=reinterpret_cast<uint64_t>(functions[i]);}
    auto options=Allocate(232);auto emptyContext=Allocate(128);
    using Options=void(__fastcall*)(void*,void*,void*,void*,void*,uint64_t);
    reinterpret_cast<Options>(imageBase+constructOptions)(options,functions[0],functions[1],functions[2],emptyContext,0);
    for(auto function:functions)HeapFree(GetProcessHeap(),0,function);
    HeapFree(GetProcessHeap(),0,emptyContext);
    return {vector,options,message};
}
static bool LayoutValid(const NativeRequest& request,const std::string& target,const std::string& text) {
    if(ReadString(request.message,0xb0)!=target || ReadString(request.message,0x758)!=text || Word(request.message,0x118)!=1)return false;
    for(int i=0;i<3;++i){auto callback=Word(request.options,0x48+i*0x40);if(!callback || Word(reinterpret_cast<void*>(callback),0)!=imageBase+callbackTypes[i])return false;}
    return true;
}
static bool OwnerMatches(const std::string& expected) {
    auto owners=OpenWeixinDatabaseAccounts(GetCurrentProcess());
    return owners.size()==1 && AccountIdUtf8(*owners.begin())==expected;
}
static bool ValidText(const json& input) {
    if(!input.is_object())return false;
    for(auto key:{"wxidorgid","msg","expected_wxid"})if(!input.contains(key)||!input[key].is_string())return false;
    auto target=input["wxidorgid"].get<std::string>(),text=input["msg"].get<std::string>();
    return !target.empty() && target.size()<=128 && target.find('\0')==std::string::npos &&
        !text.empty() && text.size()<=4096 && text.find('\0')==std::string::npos;
}
static void Reply(httplib::Response& response,int status,const json& body) {
    response.status=status;response.set_content(body.dump(),"application/json");
}
static DWORD WINAPI Serve(void*) {
    wchar_t path[32768]={};GetModuleFileNameW(ownModule,path,32768);directory=path;
    directory.resize(directory.find_last_of(L"\\/"));
    auto module=GetModuleHandleW(L"Weixin.dll");
    if(!module||!KnownImage(module)){Status("unsupported_image");return 1;}
    imageBase=reinterpret_cast<uintptr_t>(module);
    auto token=TextFile(L"token.txt");
    if(token.size()<32 || token.size()>=512 || !std::all_of(token.begin(),token.end(),[](unsigned char c){return c>=33 && c<=126;})) {
        Status("token_invalid");return 2;
    }
    int port=30003;auto portText=TextFile(L"port.txt");
    if(!portText.empty()){try{port=std::stoi(portText);}catch(...){Status("port_invalid");return 3;}}
    if(port<1||port>65535){Status("port_invalid");return 3;}
    server.set_payload_max_length(1024*1024);
    server.set_pre_routing_handler([token](const httplib::Request& request,httplib::Response& response){
        if(request.get_header_value("Authorization")!="Bearer "+token){Reply(response,401,{{"error","unauthorized"}});return httplib::Server::HandlerResponse::Handled;}
        return httplib::Server::HandlerResponse::Unhandled;
    });
    server.Post("/GetSelfProfile",[](const auto&,auto& response){
        auto owners=OpenWeixinDatabaseAccounts(GetCurrentProcess());
        if(owners.size()!=1){Reply(response,409,{{"error","account_not_unique"}});return;}
        Reply(response,200,{{"wxid",AccountIdUtf8(*owners.begin())},{"pid",GetCurrentProcessId()},
            {"identity_source","active_contact_database_handle"},{"driver","wechat-twin-native-1"}});
    });
    server.Post("/SelfTestTextLayout",[](const auto& request,auto& response){
        try {
            auto input=json::parse(request.body);
            if(!ValidText(input)||!OwnerMatches(input["expected_wxid"])){Reply(response,409,{{"ok",false},{"sent",false}});return;}
            auto target=input["wxidorgid"].template get<std::string>(),text=input["msg"].template get<std::string>();
            std::lock_guard<std::mutex> lock(sendLock);
            Reply(response,200,{{"ok",LayoutValid(Build(target,text),target,text)},{"sent",false}});
        }catch(...){Reply(response,400,{{"ok",false},{"sent",false}});}
    });
    server.Post("/SendTextMsg",[](const auto& request,auto& response){
        try {
            auto input=json::parse(request.body);
            if(!ValidText(input)||!input.contains("request_id")||!input["request_id"].is_string()){Reply(response,400,{{"error","invalid_request"}});return;}
            auto id=input["request_id"].template get<std::string>();
            if(id.size()!=64||!std::all_of(id.begin(),id.end(),[](unsigned char c){return std::isxdigit(c)!=0;})){Reply(response,400,{{"error","invalid_request_id"}});return;}
            std::lock_guard<std::mutex> lock(sendLock);
            if(GetFileAttributesW((directory+L"\\ALLOW_SEND").c_str())==INVALID_FILE_ATTRIBUTES){Reply(response,423,{{"error","sending_not_armed"}});return;}
            if(!OwnerMatches(input["expected_wxid"])){Reply(response,409,{{"error","account_changed"}});return;}
            if(!attempts.insert(id).second){Reply(response,409,{{"error","duplicate_attempt"}});return;}
            auto target=input["wxidorgid"].template get<std::string>(),text=input["msg"].template get<std::string>();
            auto built=Build(target,text);
            if(!LayoutValid(built,target,text)){Reply(response,500,{{"error","layout_mismatch"}});return;}
            using Dispatch=void(__fastcall*)(void*,void*);
            reinterpret_cast<Dispatch>(imageBase+dispatch)(built.vector,built.options);
            Reply(response,200,{{"ret",0},{"retmsg","accepted"},{"delivery_verified",false}});
        }catch(...){Reply(response,400,{{"error","invalid_request"}});}
    });
    Status("ready");
    if(!server.listen("127.0.0.1",port)){Status("listen_failed");return 4;}
    Status("stopped");return 0;
}
BOOL WINAPI DllMain(HMODULE module,DWORD reason,void*) {
    if(reason==DLL_PROCESS_ATTACH){ownModule=module;DisableThreadLibraryCalls(module);auto thread=CreateThread(nullptr,0,Serve,nullptr,0,nullptr);if(thread)CloseHandle(thread);}
    return TRUE;
}
