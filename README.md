# LLM Plugin Bridge - LLM 插件桥

> 整合 LLM 与 AstrBot 插件系统的桥梁，让 LLM 能够发现、了解和执行插件指令。

## 功能特性

### LLM 工具

本插件为 LLM 提供以下工具，让 AI 助手能够更好地帮助用户使用机器人功能：

| 工具名称 | 功能说明 |
|---------|---------|
| `get_wake_info` | 获取唤醒词、原始消息内容、最近消息历史 |
| `check_user_intent` | 检查用户是否已通过指令方式触发了功能 |
| `list_commands` | 列出所有可用的插件指令，支持关键词和插件过滤 |
| `get_command_details` | 获取特定指令的详细信息，包括参数说明和使用示例 |
| `list_plugins` | 列出所有已加载的插件 |
| `get_plugin_info` | 获取特定插件的详细信息，包括它注册的指令 |
| `execute_command` | 执行指定的插件指令（可配置权限控制） |

### 用户指令

| 指令 | 别名 | 功能说明 |
|------|------|---------|
| `/lpb_config` | 插件桥配置 | 显示当前插件配置 |
| `/lpb_list` | 列出指令 | 列出所有可用指令 |
| `/lpb_info <指令名>` | 指令详情 | 查看特定指令的详细信息 |
| `/lpb_wake` | 查看唤醒词 | 查看当前唤醒词配置 |
| `/lpb_refresh` | 刷新缓存 | 手动刷新指令和插件缓存 |

### 核心功能

#### 1. 原始消息获取

当用户发送的消息存在歧义时，LLM 可以获取原始消息（包含唤醒词）：

```
用户发送：nova14怎么样？
LLM 收到：14怎么样？
```

通过 `get_wake_info` 工具，LLM 可以获取原始消息，判断用户是在问 "nova14" 还是在执行指令。

#### 2. 消息历史记录

自动保存最近 **30 轮对话**（60 条消息），包括：
- 用户消息
- 机器人回复

#### 3. 意图检测

当用户通过"唤醒词+指令"触发功能时，自动检测并避免 LLM 重复执行。

### 高级配置

- **指令黑白名单**：控制哪些指令对 LLM 可见
- **执行权限控制**：可限制只有管理员才能让 LLM 执行指令
- **自定义指令描述**：覆盖特定指令的描述信息
- **自定义指令**：添加虚拟指令信息（仅展示）

## 配置说明

### 执行配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `allow_execute` | bool | true | 是否允许 LLM 执行指令 |
| `execute_require_admin` | bool | false | 执行指令是否需要管理员权限 |
| `blocked_commands` | list | [] | 禁止执行的指令列表 |

### 列表过滤配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `list_mode` | string | "all" | 列表模式：全部/白名单/黑名单 |
| `command_whitelist` | list | [] | 白名单指令列表 |
| `command_blacklist` | list | [] | 黑名单指令列表 |

### 自定义配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `custom_descriptions` | object | {} | 自定义指令描述 |
| `custom_commands` | list | [] | 自定义指令列表 |
| `hide_plugin_info` | bool | false | 是否隐藏插件来源 |
| `show_wake_prefix_in_list` | bool | true | 列表是否显示唤醒词前缀 |

### 日志配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_tool_logging` | bool | true | 是否启用工具调用日志 |
| `log_level` | string | "info" | 日志级别：信息/调试 |

## 使用示例

### LLM 判断用户意图

```
用户发送：nova14怎么样？
LLM 收到：14怎么样？

LLM：[调用 get_wake_info 工具]
返回：{
  "wake_prefix": "nova",
  "current_message": {
    "original_message": "nova14怎么样？",
    "llm_received_message": "14怎么样？",
    "message_was_modified": true
  }
}

LLM：根据原始消息，用户是在问 "nova14" 怎么样，而不是在执行指令。
```

### LLM 执行指令

```
用户：帮我查一下北京的天气
LLM：[调用 execute_command 工具，执行 weather 北京]
LLM：北京今天天气晴朗，气温 25°C...
```

### LLM 查询功能

```
用户：这个机器人能做什么？
LLM：[调用 list_commands 工具]
LLM：这个机器人有很多功能！比如：
- /help - 获取帮助
- /天气 - 查询天气
- /翻译 - 翻译文本
...
```

### 配置示例

```json
{
  "allow_execute": true,
  "execute_require_admin": false,
  "blocked_commands": ["shutdown", "restart"],
  "list_mode": "blacklist",
  "command_blacklist": ["admin_only_cmd"],
  "custom_descriptions": {
    "weather": "查询指定城市的天气信息，支持国内外城市"
  },
  "enable_tool_logging": true
}
```

## 安装方法

1. 在 AstrBot 管理面板的插件市场添加仓库地址：
   ```
   https://github.com/86lbs/astrbot_plugin_llm_plugin_bridge
   ```
2. 安装插件
3. 在管理面板配置插件选项

## 致谢

本插件整合了以下两个插件的功能：
- [astrbot_plugin_llm_plugin_aware](https://github.com/86lbs/astrbot_plugin_llm_plugin_aware) - LLM 插件感知增强
- [astrbot_plugin_llm_command_bridge](https://github.com/86lbs/astrbot_plugin_llm_command_bridge) - LLM 指令桥接

感谢原作者的贡献！

## 许可证

MIT License
