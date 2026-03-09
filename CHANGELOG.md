# 更新日志

## [1.0.1] - 2025-03-09

### Fixed
- 修复配置文件格式问题，使用完整的 JSON Schema 格式
- 修复 list 类型需要 items 字段的问题
- 修复 boolean 类型应为 "boolean" 而非 "bool"
- 修复 array 类型应为 "array" 而非 "list"

## [1.0.0] - 2025-03-09

### Added
- 初始版本发布
- 整合 `llm_plugin_aware` 和 `llm_command_bridge` 两个插件的功能
- 提供 6 个 LLM 工具：
  - `get_wake_info` - 获取唤醒信息
  - `list_commands` - 列出可用指令
  - `get_command_details` - 获取指令详情
  - `list_plugins` - 列出插件
  - `get_plugin_info` - 获取插件信息
  - `execute_command` - 执行指令
- 提供 8 个用户指令：
  - `/lpb_config` - 查看配置
  - `/lpb_list` - 列出指令
  - `/lpb_info` - 指令详情
  - `/lpb_plugins` - 插件列表
  - `/lpb_plugin` - 插件详情
  - `/lpb_wake` - 唤醒信息
  - `/lpb_history` - 工具历史
  - `/lpb_refresh` - 刷新缓存
- 支持指令黑白名单过滤
- 支持执行权限控制
- 支持自定义指令描述
- 支持自定义虚拟指令
- 支持工具调用日志记录
- 支持工具调用历史查看
