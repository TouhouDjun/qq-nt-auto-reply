# QQ NT 自动回复机器人

用 UI 自动化操作 QQ PC 版（NT 版），监听指定聊天窗口的新消息，调用大模型接口生成回复并自动发送。带一个 Tkinter 写的可视化控制台。

## 功能

- 监听群聊或私聊新消息，自动回复
- 群聊默认只在被 @ 时回复，私聊全部回复（可改）
- 防抖 + 回复冷却 + 单波条数上限，避免刷屏
- F12 全局热键紧急暂停/恢复
- 控制台支持开关、改参数、看日志、测接口连通性
- 可选：从 QQ 导出的群聊记录表格里学说话风格
- 可选：提问类消息先联网搜索再回答

## 环境

- Windows 10/11，已登录的 QQ PC 版（NT 版）
- Python 3.10+

```
pip install -r requirements.txt
```

## 使用

```
python qq_auto_reply.py                       # 打开控制台（推荐）
python qq_gui.py                              # 同上
python qq_auto_reply.py --console --dry-run   # 终端模式，只读不发送
python qq_semi_auto.py                        # 半自动热键模式，见下文
```

先把 `config.example.json` 复制成 `config.json`，填入接口地址、模型名和 API Key。接口要兼容 OpenAI 的 `chat/completions` 格式。

控制台里可以先勾上 Dry-Run，在 QQ 里发条消息看日志有没有"收到新消息"，确认能读到再取消勾选正式开。运行中按 F12 暂停，再按一次恢复。

## 配置项

完整列表见 `config.example.json`，常用的几个：

| 字段 | 说明 |
|------|------|
| api_key / base_url / model | 模型接口的 Key、地址、模型名 |
| chat_name | 目标聊天（群名或好友昵称，要写准） |
| self_nickname | 自己的昵称，留空自动检测 |
| debounce | 新消息静置多少秒才回复，默认 2 |
| reply_cooldown | 两次回复的最小间隔，默认 3 秒 |
| max_burst | 一波最多回几条，多出来的丢掉 |
| send_mode | button 点发送按钮 / enter 回车 / ctrl_enter |
| trigger | 触发词，分号分隔；留空表示都回 |
| chat_kind | auto 自动判断 / group 群聊 / private 私聊 |
| group_at_only | 群聊只回 @ 我的消息，默认开 |
| system_prompt | 附加提示词。程序会固定注入一段基础规则（不聊政治、不承认是 AI、回复不带"某某:"前缀），这里可以再补自己的要求 |
| boss_name / role_name / forget_roles | 身份设定：老板昵称、自己的角色、要撇清的旧人设 |
| learn_enabled 及 learn_* | 群聊记录学习相关，见下文 |
| web_search_enabled | 提问时联网搜索，默认开 |
| tree_poke / auto_refresh_interval | 防控件树冻结相关，见下文 |

## 几点说明

### QQ 窗口要保持前台

QQ NT 是 Electron 应用，它的无障碍树只在窗口处于前台时才构建，最小化或者切到后台树就没了。程序每轮会自己把窗口置前，所以跑的时候别最小化 QQ。

### 控件树会冻结

QQ 的无障碍树在长时间没有客户端请求之后会停止更新。新消息到了，控件树里还是旧的，表现就是日志停在"等待新消息"，必须手动退出聊天再进去才恢复。

程序每一轮会重新从窗口句柄取控件树（不复用缓存），同时发 WM_GETOBJECT 重新声明一次无障碍客户端并 SetFocus 唤醒；超过设定时间（默认 45 秒）没读到新消息，就自动切到别的会话再切回目标聊天，相当于替你手动退出再点进来。切换期间的消息靠消息 ID 比对不会漏。

### 控件定位

QQ NT 的窗口类名是 `Chrome_WidgetWin_1`。实测结构大致是：

```
WindowControl 'QQ' (Chrome_WidgetWin_1)
└─ DocumentControl (Chrome_RenderWidgetHostHWND)
   └─ GroupControl Id='app'
      ├─ WindowControl '会话列表'
      │    └─ TextControl '群名/昵称'
      ├─ WindowControl '消息列表'
      │    └─ GroupControl Id='ml-root'
      │         └─ GroupControl Id='<19位数字>'   ← 一条消息，Id 就是消息 ID
      │              ├─ TextControl '09:35'        ← 时间
      │              ├─ GroupControl '发送者'
      │              ├─ GroupControl → TextControl '正文'
      │              └─ ImageControl '图片'
      ├─ CustomControl (Name='\n')                 ← 输入框
      └─ ButtonControl '发送'
```

踩过的几个坑：

- 输入框不是 Edit 控件，是 `CustomControl`（Chromium 的 contenteditable），`BoundingRectangle` 是 0x0，`SetFocus()` 没用。现在的做法是点发送按钮左侧 (left-100, top-30) 的坐标先把焦点弄进去，再 `Ctrl+A` 加剪贴板粘贴写入，最后确认发送按钮从 disabled 变成 enabled 才点。
- 发送按钮是 `ButtonControl(Name='发送')`，输入框空着的时候是 disabled，可以拿这个当写入成功的判断。
- 消息列表里每条消息是一个 `AutomationId` 为纯数字的 `GroupControl`，这个 Id 就是消息 ID，直接用来判断新旧消息，比比对文本可靠。
- 消息内容可能嵌好几层 GroupControl，递归把 TextControl 拼起来就行；时间那条要按正则过滤掉。
- QQ 更新后如果定位失效，跑 `qq_inspect.py --dump` 重新导出控件树，对着上面的结构改 `QQController` 里的名字和 Id。

### 群聊记录学习（可选）

把 QQ 导出的群聊记录表格（`group_*.xlsx`）放到项目目录，点控制台的"重建学习库"，`build_learn.py` 会：

1. 只留真人文本消息，去掉撤回、系统消息、机器人指令（`/` 开头）、图片、链接、入群欢迎语，另外过滤政治敏感词
2. 合并同一人连续发言，切成"对方说→群友接"的对话对
3. 统计平均长度、常用表情、高频口头禅，生成一段风格描述

生成两个文件：`learn_corpus.json`（对话语料）和 `learn_profile.json`（风格画像）。回复的时候会从语料里轮换抽样几条当示例，同时按关键词检索和当前话题相关的讨论注入上下文——群里聊过的梗和黑话能接上，没聊过的不硬塞。

机器人和群友的真实对话会追加到 `learn_memory.jsonl`，下次优先从里面抽样，聊得越多越像群里的人。

注意这些都是从你的聊天记录派生的，别提交。

### 联网搜索（可选）

模型接口本身不联网，"最新版本是几点几"这类问题它会答错或者编。程序会判断消息是否像提问，是的话先把口语问题改写成规范查询（简称换全称、去掉"几点几"这种口水词），再抓搜索结果注入上下文。

搜索用搜狗加 Bing 两个引擎合并，百度不带 cookie 直接请求会撞安全验证，没用。带了结果缓存（6 小时）、引擎失败冷却（5 分钟）和请求节流，避免被反爬封掉。已知缺点是免费抓取偶尔会返回空，这时会退回本地语料加模型自身知识。

### 半自动热键模式

如果控件树冻结问题在你的环境里实在压不住，可以改用 `qq_semi_auto.py`：用全局键盘钩子监听热键，按一下处理一次，每次读取都发生在你刚操作过 QQ 之后，树是新鲜的。

```
Ctrl+Shift+R   读当前聊天最后一条消息，生成回复并发送
Ctrl+Shift+P   暂停/恢复
python qq_semi_auto.py --clipboard   剪贴板模式：你在 QQ 里复制消息，按热键直接回复
python qq_semi_auto.py --dry-run     只打印不发送
```

## 文件

| 文件 | 说明 |
|------|------|
| qq_auto_reply.py | 核心库（AutomationBot / QQController / ChatAPIClient / DialogueLearner / WebSearcher） |
| qq_gui.py | 可视化控制台 |
| qq_semi_auto.py | 半自动热键模式 |
| qq_inspect.py | 控件树检查工具，QQ 更新后重新定位用 |
| build_learn.py | 解析群聊记录表格生成语料 |
| learn_common.py | 消息清洗和敏感词过滤，建库和运行时共用 |
| config.example.json | 配置模板 |

## 常见问题

- 读不到消息：确认 QQ 窗口在前台且没最小化、目标聊天已打开。控制台里点"检测 QQ 环境"能看关键控件在不在。
- 卡在"等待新消息"：按上面"控件树会冻结"处理，先把自动重进间隔调小到 20 秒试试。
- 会话列表找不到聊天名：先在 QQ 里手动打开一次，或者把这个聊天置顶。
- 发送失败：检查 `send_mode` 和 QQ 设置里的"回车发送消息"是否对应，默认点按钮最稳。
- 接口报 401：Key 或地址填错了，用控制台的"测试接口连接"查。

## 注意

`config.json`、导出的聊天记录表格、以及学习产生的 `learn_corpus.json`、`learn_profile.json`、`learn_memory.jsonl`、`search_cache.json` 都在 `.gitignore` 里。这些文件包含 API Key 和聊天内容，不要提交到公开仓库。
