# 本人角色与表达习惯

画像来自用户本人已核验的微信文字。全局只保留表达统计与封闭词表里的确认、问候、表情；个人故事、联系人身份和原文只留本机，同一联系人回复不会取得其他人的原文。

每条回复组合当前联系人的表达统计、相似情境、日期明确的历史记录、近期对话和本人审核修改。模型先判断事实和本人已确认立场，再选择答应、拒绝、追问或暂缓，最后使用本人的表达。没有本人对情绪、动机或个人选择的明确依据时，转为审核。

本人亲自修改优先于本人批准的模型草稿。只保存修改也可以学习，但不批准发送。初始人工模板不被称为本人原话。角色画像不等于完整人格复制，也不证明心理动机；统计和少量回放不能保证每条回复与本人相同。

从本机已经核验的 `messages(conversation,from_me,local_type,content)` 历史表重建画像：

```powershell
.venv\Scripts\python.exe scripts\build_role_profile.py --config config.yaml --history PATH_TO_LOCAL_NORMALIZED_HISTORY.sqlite3
```

结果默认保存于 `.runtime/history_reader/role_profile.json`，不上传仓库。该历史源应包含可核验的本人文字，不能把未经本人修改的 AI 输出当作本人原话反复训练。没有画像文件也可运行，此时不声称已了解联系人习惯。
