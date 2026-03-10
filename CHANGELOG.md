# 更新日志

## [1.7.0] - 2025-03-10

### Added
- **消息投递状态追踪**：新增 `MessageDeliveryTracker` 类，追踪消息是否被转换为图片发送
- **新增 LLM 工具 `get_message_delivery_status`**：让 LLM 能够查询最近消息的投递状态
- **新增用户指令 `/lpb_delivery`**：用户可查看最近消息的投递状态
- **`get_wake_info` 工具增强**：返回 `last_message_delivery` 字段，告知 LLM 上一条消息是否被转图片

### Changed
- **`MessageHistoryManager.save()` 方法增强**：支持 `extra` 参数保存额外信息
- **`after_message_sent` 事件监听增强**：检测 `use_t2i_` 属性判断消息是否被转图片
- **`lpb_config` 指令增强**：显示消息投递追踪状态

### Technical
- 当消息被转换为图片时，LLM 会收到明确的通知，可以据此调整回复策略
- 解决了 LLM 不知道自己发送的消息被转换为图片的问题

## [1.6.0] - 2025-03-10

### Changed
- **代码重构**：抽离 `CacheManager` 类管理缓存，`MessageHistoryManager` 类管理消息历史
- **简化 `_get_raw_message` 方法**：减少嵌套层级，提高可读性
- **修复异步生成器结果吞噬问题**：`execute_command` 现在会收集所有 yield 结果

### Added
- **新增配置项**：
  - `max_history_per_session`: 每会话最大消息数（默认 60）
  - `session_expire_seconds`: 会话过期时间（默认 1800 秒）
  - `intent_time_window`: 意图检测时间窗口（默认 5.0 秒）
  - `max_invocation_records`: 最大调用记录数（默认 50）
  - `cleanup_threshold`: 清理阈值（默认 100）

### Fixed
- **优化会话清理性能**：使用操作计数器触发清理，避免每次操作都遍历

## [1.5.2] - 2025-03-10

### Changed
- 更新 README 文档，反映最新的功能和配置

## [1.5.1] - 2025-03-10

### Changed
- 更新 README 文档，反映最新的功能和配置

## [1.5.0] - 2025-03-10

### Changed
- **移除 `analysis` 字段**：返回结果不再包含会影响 LLM 判断的建议或分析
- **消息历史保存策略调整**：保存最近 30 轮所有消息（包括用户消息和机器人回复）
- 每个会话最多保存 60 条消息

### Added
- 新增 `on_llm_response` 事件监听，保存 LLM 响应
- 新增 `after_message_sent` 事件监听，保存机器人发送的消息

### Removed
- 移除 `analysis.possible_intent` 字段
- 移除 `analysis.content_after_wake` 字段
- 移除 `analysis.note` 字段

## [1.4.2] - 2025-03-10

### Removed
- 移除 `analysis.note` 字段

## [1.4.1] - 2025-03-10

### Removed
- 移除 `analysis.recommendation` 字段

## [1.4.0] - 2025-03-10

### Added
- **消息历史记录**：保存最近 20 条消息历史
- **优化工具描述**：让 LLM 在有歧义时主动调用 `get_wake_info`

### Changed
- `get_wake_info` 返回结果新增 `recent_history` 字段
- `get_wake_info` 返回结果新增 `analysis.recommendation` 字段

## [1.3.5] - 2025-03-10

### Fixed
- 从 `event.message_obj.raw_message` 获取原始消息

## [1.3.4] - 2025-03-10

### Changed
- `list_mode` 配置改为下拉选择（全部/白名单/黑名单）
- `log_level` 配置改为下拉选择（信息/调试）

## [1.3.3] - 2025-03-10

### Fixed
- 修复 `filter` 模块没有 `on_command_run` 和 `on_all_message` 的问题
- 使用 `on_llm_request` 替代不存在的事件

## [1.3.2] - 2025-03-10

### Fixed
- 修复 `log_level` 配置为下拉选择

## [1.3.1] - 2025-03-10

### Fixed
- 修正仓库链接为正确的 GitHub 地址

## [1.3.0] - 2025-03-10

### Added
- **原始消息追踪**：通过 `on_llm_request` 保存原始消息
- `get_wake_info` 返回原始消息和处理后的消息对比

### Changed
- `get_wake_info` 返回结果新增 `message_processing_examples` 字段
- `get_wake_info` 返回结果新增 `how_to_judge_intent` 字段

## [1.2.0] - 2025-03-10

### Changed
- 改进 `get_wake_info` 工具描述
- 添加消息处理示例，帮助 LLM 理解唤醒词被去掉的情况

## [1.1.0] - 2025-03-10

### Added
- **用户意图检测**：新增 `check_user_intent` LLM 工具
- **指令调用监听**：通过 `on_command_run` 监听用户通过指令触发的行为

### Removed
- 移除 `/lpb_history` 指令及相关功能

## [1.0.5] - 2025-03-10

### Fixed
- 修复异步生成器（async generator）处理问题
- 指令处理器返回异步生成器时正确迭代获取结果

## [1.0.4] - 2025-03-10

### Fixed
- 修复配置文件格式

## [1.0.3] - 2025-03-09

### Fixed
- 修复配置文件格式

## [1.0.2] - 2025-03-09

### Fixed
- 修复配置文件格式

## [1.0.1] - 2025-03-09

### Fixed
- 修复配置文件格式问题，使用完整的 JSON Schema 格式
- 修复 list 类型需要 items 字段的问题

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
