"""
LLM Plugin Bridge - LLM 插件桥

整合 LLM 与 AstrBot 插件系统的桥梁，让 LLM 能够：
- 发现和了解所有可用的插件和指令
- 获取唤醒词和触发方式信息
- 执行插件指令（可配置权限控制）
- 监控工具调用历史

功能特性：
1. 提供 LLM Tool 查询指令和插件信息
2. 提供 LLM Tool 执行插件指令
3. 支持指令黑白名单、自定义描述等高级配置
4. 记录工具调用历史，便于调试和审计
"""

import json
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot.core.agent.tool import FunctionTool


class Main(star.Star):
    """LLM 插件桥 - 让 LLM 能够发现、了解和执行插件指令"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._config = config or {}
        
        # ========== 缓存 ==========
        self._commands_cache: dict[str, dict] = {}
        self._plugins_cache: dict[str, dict] = {}
        self._wake_prefix: str = ""
        self._tool_call_history: list[dict] = []

        # ========== 执行相关配置 ==========
        self._allow_execute = self._config.get("allow_execute", True)
        self._execute_require_admin = self._config.get("execute_require_admin", False)
        self._blocked_commands = set(self._config.get("blocked_commands", []))

        # ========== 列表过滤配置 ==========
        self._list_mode = self._config.get("list_mode", "all")  # all, whitelist, blacklist
        self._command_whitelist = set(self._config.get("command_whitelist", []))
        self._command_blacklist = set(self._config.get("command_blacklist", []))

        # ========== 自定义配置 ==========
        self._custom_descriptions = self._config.get("custom_descriptions", {})
        self._custom_commands = self._config.get("custom_commands", [])

        # ========== 显示配置 ==========
        self._hide_plugin_info = self._config.get("hide_plugin_info", False)
        self._show_wake_prefix_in_list = self._config.get("show_wake_prefix_in_list", True)

        # ========== 日志配置 ==========
        self._enable_tool_logging = self._config.get("enable_tool_logging", True)
        self._log_level = self._config.get("log_level", "info")

    async def initialize(self) -> None:
        """插件初始化"""
        self._refresh_all_cache()
        logger.info(f"LLM Plugin Bridge 初始化完成")
        logger.info(f"  - 已缓存 {len(self._commands_cache)} 个指令")
        logger.info(f"  - 已缓存 {len(self._plugins_cache)} 个插件")
        logger.info(f"  - 唤醒词: {self._get_wake_prefix_display()}")
        logger.info(f"  - 列表模式: {self._list_mode}")
        logger.info(f"  - LLM 执行功能: {'已启用' if self._allow_execute else '已禁用'}")
        logger.info(f"  - 工具调用日志: {'已启用' if self._enable_tool_logging else '已禁用'}")

    def _log(self, message: str, level: str = "info"):
        """根据配置的日志级别输出日志"""
        if self._log_level == "debug" or level == "info":
            logger.info(message)
        elif level == "debug":
            logger.debug(message)

    # ==================== 缓存管理 ====================

    def _refresh_all_cache(self) -> None:
        """刷新所有缓存"""
        self._refresh_wake_prefix()
        self._refresh_commands_cache()
        self._refresh_plugins_cache()

    def _refresh_wake_prefix(self) -> None:
        """刷新唤醒词配置"""
        try:
            cfg = self.context.get_config()
            if cfg:
                provider_settings = cfg.get("provider_settings", {})
                self._wake_prefix = provider_settings.get("wake_prefix", "")
        except Exception as e:
            logger.warning(f"获取唤醒词配置失败: {e}")
            self._wake_prefix = ""

    def _refresh_commands_cache(self) -> None:
        """刷新指令缓存"""
        self._commands_cache.clear()

        # 获取所有消息事件处理器
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=True
        )

        for handler_md in handlers:
            # 查找 CommandFilter
            command_filter = None
            for event_filter in handler_md.event_filters:
                if isinstance(event_filter, CommandFilter):
                    command_filter = event_filter
                    break

            if not command_filter:
                continue

            # 获取指令名称
            cmd_names = command_filter.get_complete_command_names()
            if not cmd_names:
                continue

            # 获取主指令名
            primary_name = command_filter.command_name

            # 获取参数信息
            params_info = {}
            for param_name, param_type in command_filter.handler_params.items():
                if isinstance(param_type, type):
                    params_info[param_name] = {
                        "type": param_type.__name__,
                        "required": True,
                    }
                elif param_type is None:
                    params_info[param_name] = {"type": "any", "required": True}
                else:
                    params_info[param_name] = {
                        "type": type(param_type).__name__,
                        "default": str(param_type),
                        "required": False,
                    }

            # 获取插件信息
            plugin_info = None
            if not self._hide_plugin_info:
                for star_md in self.context.get_all_stars():
                    if star_md.module_path == handler_md.handler_module_path:
                        plugin_info = {
                            "name": star_md.name,
                            "author": star_md.author,
                            "desc": star_md.desc,
                        }
                        break

            # 获取描述（支持自定义）
            description = self._get_command_description(
                primary_name, handler_md.desc or "无描述"
            )

            # 存储指令信息
            self._commands_cache[primary_name] = {
                "names": cmd_names,
                "primary_name": primary_name,
                "description": description,
                "params": params_info,
                "handler_name": handler_md.handler_name,
                "plugin": plugin_info,
                "handler_md": handler_md,
                "command_filter": command_filter,
                "is_custom": False,
            }

        # 添加自定义指令
        for custom_cmd in self._custom_commands:
            cmd_name = custom_cmd.get("name")
            if not cmd_name:
                continue

            # 解析参数
            params_info = {}
            if "params" in custom_cmd:
                for param_name, param_info in custom_cmd["params"].items():
                    params_info[param_name] = {
                        "type": param_info.get("type", "string"),
                        "required": param_info.get("required", True),
                        "description": param_info.get("description", ""),
                    }

            self._commands_cache[cmd_name] = {
                "names": [cmd_name] + custom_cmd.get("aliases", []),
                "primary_name": cmd_name,
                "description": custom_cmd.get("description", "自定义指令"),
                "params": params_info,
                "handler_name": None,
                "plugin": None,
                "handler_md": None,
                "command_filter": None,
                "is_custom": True,
                "example": custom_cmd.get("example", ""),
            }

    def _refresh_plugins_cache(self) -> None:
        """刷新插件缓存"""
        self._plugins_cache.clear()
        
        all_stars = self.context.get_all_stars()
        for star_md in all_stars:
            if star_md.name:
                self._plugins_cache[star_md.name] = {
                    "name": star_md.name,
                    "author": star_md.author,
                    "desc": star_md.desc,
                    "version": star_md.version,
                    "repo": star_md.repo,
                    "activated": star_md.activated,
                    "module_path": star_md.module_path,
                }

    # ==================== 辅助方法 ====================

    def _get_wake_prefix_display(self) -> str:
        """获取用于显示的唤醒词"""
        if self._wake_prefix:
            return self._wake_prefix
        return "无唤醒词（@机器人 或私聊即可触发）"

    def _get_command_prefix(self) -> str:
        """获取指令前缀（用于生成示例）"""
        if self._wake_prefix:
            return self._wake_prefix
        return "/"

    def _is_command_visible(self, cmd_name: str) -> bool:
        """检查指令是否应该显示在列表中"""
        if self._list_mode == "whitelist":
            return cmd_name in self._command_whitelist
        elif self._list_mode == "blacklist":
            return cmd_name not in self._command_blacklist
        else:  # all
            return True

    def _is_command_executable(self, cmd_name: str) -> bool:
        """检查指令是否可以被 LLM 执行"""
        return cmd_name not in self._blocked_commands

    def _get_command_description(self, cmd_name: str, default_desc: str) -> str:
        """获取指令描述（支持自定义覆盖）"""
        if cmd_name in self._custom_descriptions:
            return self._custom_descriptions[cmd_name]
        return default_desc

    def _generate_usage_examples(self, cmd_info: dict) -> list[str]:
        """生成指令使用示例列表"""
        primary_name = cmd_info["primary_name"]
        params = cmd_info["params"]
        prefix = self._get_command_prefix()

        examples = []

        # 基本用法
        if not params:
            examples.append(f"{prefix}{primary_name}")
        else:
            # 带参数的用法
            param_strs = []
            example_values = []
            for param_name, param_info in params.items():
                if param_info.get("required", True):
                    param_strs.append(f"<{param_name}>")
                    # 根据参数类型生成示例值
                    param_type = param_info.get("type", "str")
                    if param_type == "int":
                        example_values.append("1")
                    elif param_type == "float":
                        example_values.append("1.5")
                    elif param_type == "bool":
                        example_values.append("true")
                    else:
                        example_values.append(f"示例{param_name}")
                else:
                    param_strs.append(f"[{param_name}]")

            examples.append(f"{prefix}{primary_name} {' '.join(param_strs)}")
            if example_values:
                examples.append(f"{prefix}{primary_name} {' '.join(example_values)}")

        return examples

    # ==================== LLM 工具 ====================

    @filter.llm_tool(name="get_wake_info")
    async def get_wake_info(self, event: AstrMessageEvent) -> str:
        """获取机器人的唤醒信息，包括唤醒词、触发方式等。当用户问如何使用指令、如何唤醒机器人时调用此工具。

        返回信息包括：
        - 唤醒词（如果有配置）
        - 触发机器人的方式
        - 指令格式说明和使用示例
        """
        self._log("[LLM Tool] get_wake_info 被调用")
        self._refresh_wake_prefix()

        result = {
            "wake_prefix": self._wake_prefix if self._wake_prefix else None,
            "trigger_methods": [],
            "command_format": "",
            "examples": [],
        }

        # 构建触发方式说明
        if self._wake_prefix:
            result["trigger_methods"].append(
                f"发送「{self._wake_prefix}」开头的消息"
            )
            result["command_format"] = f"{self._wake_prefix}指令名 [参数]"
            result["examples"] = [
                f"{self._wake_prefix}help - 获取帮助",
                f"{self._wake_prefix}天气 北京 - 查询北京天气",
            ]
        else:
            result["trigger_methods"].extend([
                "@机器人 后发送消息",
                "私聊直接发送消息",
                "群聊中使用 / 开头的指令",
            ])
            result["command_format"] = "/指令名 [参数] 或 @机器人 指令名 [参数]"
            result["examples"] = [
                "/help - 获取帮助",
                "/天气 北京 - 查询北京天气",
                "@机器人 帮助 - @机器人后发送帮助",
            ]

        result["note"] = (
            "指令需要先唤醒机器人才能触发。"
            "唤醒方式取决于管理员配置，可能是使用唤醒词、@机器人、或私聊直接触发。"
        )

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_commands")
    async def list_commands(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        plugin_name: str = "",
        include_params: bool = False,
    ) -> str:
        """列出所有可用的插件指令。当用户想知道机器人能做什么、有哪些指令可用时调用此工具。

        Args:
            keyword(string): 可选的关键词，用于过滤指令名称或描述中包含该关键词的指令。留空则列出所有指令。
            plugin_name(string): 可选的插件名称，用于过滤特定插件的指令。留空则列出所有插件的指令。
            include_params(boolean): 是否包含参数信息。默认为 False，仅显示指令名称和描述。
        """
        self._log(f"[LLM Tool] list_commands 被调用, keyword={keyword}, plugin_name={plugin_name}")
        self._refresh_commands_cache()
        self._refresh_wake_prefix()

        commands = []
        for cmd_name, cmd_info in self._commands_cache.items():
            # 黑白名单过滤
            if not self._is_command_visible(cmd_name):
                continue

            # 插件过滤
            if plugin_name:
                cmd_plugin = cmd_info.get("plugin", {})
                if cmd_plugin:
                    if cmd_plugin.get("name", "").lower() != plugin_name.lower():
                        continue
                else:
                    continue

            # 关键词过滤
            if keyword:
                keyword_lower = keyword.lower()
                if (
                    keyword_lower not in cmd_name.lower()
                    and keyword_lower not in cmd_info["description"].lower()
                ):
                    continue

            # 构建指令信息
            cmd_entry = {
                "name": cmd_name,
                "description": cmd_info["description"],
            }

            # 是否显示完整指令（带唤醒词前缀）
            if self._show_wake_prefix_in_list:
                cmd_entry["full_command"] = f"{self._get_command_prefix()}{cmd_name}"

            # 别名
            aliases = [
                n for n in cmd_info["names"] if n != cmd_name and not n.startswith(" ")
            ]
            if aliases:
                cmd_entry["aliases"] = aliases

            # 参数
            if include_params and cmd_info["params"]:
                cmd_entry["params"] = cmd_info["params"]

            # 插件信息
            if cmd_info["plugin"] and not self._hide_plugin_info:
                cmd_entry["plugin"] = cmd_info["plugin"]["name"]

            # 是否可执行
            if cmd_info["is_custom"]:
                cmd_entry["is_custom"] = True
            else:
                cmd_entry["executable"] = self._is_command_executable(cmd_name)

            commands.append(cmd_entry)

        if not commands:
            if keyword:
                return f"没有找到包含关键词「{keyword}」的指令。"
            if plugin_name:
                return f"没有找到插件「{plugin_name}」的指令。使用 list_plugins 工具查看所有插件。"
            return "当前没有可用的指令。"

        result = {
            "total": len(commands),
            "commands": commands,
        }

        # 添加唤醒词信息
        if self._show_wake_prefix_in_list:
            result["wake_prefix"] = self._wake_prefix if self._wake_prefix else "无（使用 / 或 @机器人）"
            result["command_prefix"] = self._get_command_prefix()

        result["note"] = "使用 get_command_details 工具可以获取特定指令的详细信息。使用 get_wake_info 工具可以了解如何触发指令。"

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_command_details")
    async def get_command_details(
        self, event: AstrMessageEvent, command_name: str
    ) -> str:
        """获取特定指令的详细信息，包括完整描述、参数说明和使用示例。当用户想了解某个具体指令如何使用时调用此工具。

        Args:
            command_name(string): 指令名称，如 "help"、"天气" 等（不需要带唤醒词前缀）。
        """
        self._log(f"[LLM Tool] get_command_details 被调用, command_name={command_name}")
        self._refresh_commands_cache()
        self._refresh_wake_prefix()

        # 查找指令
        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            # 尝试模糊匹配
            similar = [
                name
                for name in self._commands_cache.keys()
                if command_name.lower() in name.lower()
            ]
            if similar:
                return f"未找到指令「{command_name}」。您是否要找: {', '.join(similar)}？"
            return f"未找到指令「{command_name}」。使用 list_commands 工具查看所有可用指令。"

        # 构建详细信息
        result = {
            "name": cmd_info["primary_name"],
            "full_command": f"{self._get_command_prefix()}{cmd_info['primary_name']}",
            "wake_prefix": self._wake_prefix if self._wake_prefix else "无",
            "aliases": [
                n
                for n in cmd_info["names"]
                if n != cmd_info["primary_name"] and not n.startswith(" ")
            ],
            "description": cmd_info["description"],
            "params": cmd_info["params"] if cmd_info["params"] else None,
        }

        # 插件信息
        if cmd_info["plugin"] and not self._hide_plugin_info:
            result["plugin"] = cmd_info["plugin"]

        # 使用示例
        if cmd_info["is_custom"] and cmd_info.get("example"):
            result["usage_examples"] = [cmd_info["example"]]
        else:
            result["usage_examples"] = self._generate_usage_examples(cmd_info)

        # 是否可执行
        if cmd_info["is_custom"]:
            result["is_custom"] = True
            result["note"] = "这是一个自定义指令，仅用于展示，无法通过 execute_command 执行。"
        else:
            result["executable"] = self._is_command_executable(cmd_info["primary_name"])
            if not result["executable"]:
                result["note"] = "此指令已被管理员禁止通过 LLM 执行。"

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_plugins")
    async def list_plugins(self, event: AstrMessageEvent) -> str:
        """列出所有已加载的插件。当用户想知道有哪些插件、想了解插件概况时调用此工具。

        返回信息包括：
        - 插件名称、作者、版本
        - 插件描述
        - 激活状态
        """
        self._log("[LLM Tool] list_plugins 被调用")
        self._refresh_plugins_cache()

        if not self._plugins_cache:
            return "当前没有已加载的插件。"

        plugins = []
        for plugin_name, plugin_info in self._plugins_cache.items():
            plugin_entry = {
                "name": plugin_info["name"],
                "author": plugin_info.get("author", "未知"),
                "version": plugin_info.get("version", "未知"),
                "description": plugin_info.get("desc", ""),
                "activated": plugin_info.get("activated", False),
            }
            plugins.append(plugin_entry)

        result = {
            "total": len(plugins),
            "plugins": plugins,
            "note": "使用 get_plugin_info 工具可以获取特定插件的详细信息，包括它注册的指令。"
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_plugin_info")
    async def get_plugin_info_tool(self, event: AstrMessageEvent, plugin_name: str) -> str:
        """获取特定插件的详细信息，包括它注册的所有指令。当用户想深入了解某个插件的功能时调用此工具。

        Args:
            plugin_name(string): 插件名称，如 "web_searcher"、"astrbot" 等。
        """
        self._log(f"[LLM Tool] get_plugin_info 被调用, plugin_name={plugin_name}")
        self._refresh_plugins_cache()
        self._refresh_commands_cache()

        # 查找插件
        plugin_info = None
        for name, info in self._plugins_cache.items():
            if name.lower() == plugin_name.lower():
                plugin_info = info
                break

        if not plugin_info:
            similar = [
                name for name in self._plugins_cache.keys()
                if plugin_name.lower() in name.lower()
            ]
            if similar:
                return f"未找到插件「{plugin_name}」。您是否要找: {', '.join(similar)}？"
            return f"未找到插件「{plugin_name}」。使用 list_plugins 工具查看所有可用插件。"

        # 获取该插件注册的指令
        plugin_commands = []
        for cmd_name, cmd_info in self._commands_cache.items():
            if cmd_info.get("plugin", {}).get("name", "").lower() == plugin_info["name"].lower():
                cmd_entry = {
                    "name": cmd_name,
                    "description": cmd_info["description"],
                }
                if cmd_info["params"]:
                    cmd_entry["has_params"] = True
                plugin_commands.append(cmd_entry)

        result = {
            "name": plugin_info["name"],
            "author": plugin_info.get("author", "未知"),
            "version": plugin_info.get("version", "未知"),
            "description": plugin_info.get("desc", ""),
            "activated": plugin_info.get("activated", False),
            "repo": plugin_info.get("repo", ""),
            "commands_count": len(plugin_commands),
            "commands": plugin_commands[:10] if plugin_commands else [],  # 最多返回10个
        }

        if len(plugin_commands) > 10:
            result["note"] = f"该插件注册了 {len(plugin_commands)} 个指令，此处仅显示前10个。使用 list_commands 工具并指定 plugin_name 参数可以查看完整列表。"

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="execute_command")
    async def execute_command(
        self,
        event: AstrMessageEvent,
        command_name: str,
        args: str = "",
    ) -> str:
        """执行指定的插件指令。当用户明确要求执行某个指令，或者 LLM 认为需要通过执行指令来完成任务时调用此工具。

        Args:
            command_name(string): 要执行的指令名称（不需要带唤醒词前缀）。
            args(string): 指令参数，多个参数用空格分隔。如 "北京 天气" 表示两个参数。
        """
        self._log(f"[LLM Tool] execute_command 被调用, command_name={command_name}, args={args}")

        # 检查是否允许执行
        if not self._allow_execute:
            return "错误：LLM 指令执行功能已被禁用。"

        # 检查权限
        if self._execute_require_admin and not event.is_admin():
            return "错误：执行指令需要管理员权限。"

        # 刷新缓存
        self._refresh_commands_cache()

        # 查找指令
        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            return f"错误：未找到指令「{command_name}」。"

        # 检查是否是自定义指令
        if cmd_info.get("is_custom"):
            return "错误：自定义指令无法通过 execute_command 执行。"

        # 检查是否在执行黑名单中
        if not self._is_command_executable(command_name):
            return f"错误：指令「{command_name}」已被禁止通过 LLM 执行。"

        try:
            # 获取处理器和过滤器
            handler_md = cmd_info["handler_md"]
            command_filter = cmd_info["command_filter"]

            # 解析参数
            args_list = args.split() if args else []
            try:
                parsed_params = command_filter.validate_and_convert_params(
                    args_list, command_filter.handler_params
                )
            except ValueError as e:
                examples = self._generate_usage_examples(cmd_info)
                return f"参数错误: {str(e)}\n使用示例: {examples[0] if examples else '无'}"

            # 获取插件实例
            from astrbot.core.star.star import star_map

            star_info = star_map.get(handler_md.handler_module_path)
            if not star_info or not star_info.star_cls_type:
                return "错误：无法获取插件实例。"

            # 调用处理器
            handler = handler_md.handler

            # 设置解析后的参数到 event
            event.set_extra("parsed_params", parsed_params)

            # 调用处理器
            result = await handler(event, **parsed_params)

            # 处理返回结果
            if result is not None:
                if isinstance(result, MessageEventResult):
                    plain_text = result.get_plain_text()
                    if plain_text:
                        return f"指令执行成功: {plain_text}"
                    return "指令执行成功（返回了非文本内容）。"
                elif isinstance(result, str):
                    return f"指令执行成功: {result}"
                else:
                    return "指令执行成功。"

            # 检查 event 是否设置了结果
            event_result = event.get_result()
            if event_result:
                plain_text = event_result.get_plain_text()
                if plain_text:
                    return f"指令执行结果: {plain_text}"
                return "指令执行成功。"

            return "指令已执行，但未返回结果。"

        except Exception as e:
            logger.error(f"执行指令时发生错误: {e}", exc_info=True)
            return f"执行指令时发生错误: {str(e)}"

    # ==================== 事件监听 ====================

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None):
        """监听 LLM Tool 调用事件"""
        if not self._enable_tool_logging:
            return

        tool_info = {
            "event": "tool_call_start",
            "tool_name": tool.name if tool else "unknown",
            "tool_args": tool_args,
            "message_str": event.message_str[:200] if event.message_str else "",
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
        }

        self._tool_call_history.append(tool_info)

        # 保持历史记录在合理范围内
        if len(self._tool_call_history) > 100:
            self._tool_call_history = self._tool_call_history[-50:]

        self._log(f"[Tool Call] 工具 '{tool.name}' 被调用, 参数: {json.dumps(tool_args, ensure_ascii=False) if tool_args else '无'}")

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None, tool_result: Any):
        """监听 LLM Tool 响应事件"""
        if not self._enable_tool_logging:
            return

        # 处理 tool_result
        result_str = ""
        if tool_result:
            try:
                if hasattr(tool_result, 'content'):
                    result_str = str(tool_result.content)[:500]
                elif hasattr(tool_result, '__dict__'):
                    result_str = json.dumps(tool_result.__dict__, ensure_ascii=False, default=str)[:500]
                else:
                    result_str = str(tool_result)[:500]
            except Exception as e:
                result_str = f"<无法序列化: {e}>"

        tool_info = {
            "event": "tool_call_end",
            "tool_name": tool.name if tool else "unknown",
            "tool_args": tool_args,
            "tool_result": result_str,
        }

        self._tool_call_history.append(tool_info)

        self._log(f"[Tool Respond] 工具 '{tool.name}' 返回结果: {result_str[:200] if result_str else '空'}")

    # ==================== 用户指令 ====================

    @filter.command("lpb_config", alias={"插件桥配置"})
    async def show_config(self, event: AstrMessageEvent):
        """显示当前插件配置"""
        lines = ["⚙️ LLM Plugin Bridge 配置信息", ""]

        # 执行配置
        lines.append("【执行配置】")
        lines.append(f"  允许 LLM 执行: {'✅' if self._allow_execute else '❌'}")
        lines.append(f"  执行需管理员权限: {'✅' if self._execute_require_admin else '❌'}")
        if self._blocked_commands:
            lines.append(f"  禁止执行的指令: {', '.join(self._blocked_commands)}")
        else:
            lines.append("  禁止执行的指令: 无")
        lines.append("")

        # 列表过滤配置
        lines.append("【列表过滤配置】")
        mode_desc = {
            "all": "显示所有指令",
            "whitelist": "仅显示白名单指令",
            "blacklist": "隐藏黑名单指令",
        }
        lines.append(f"  列表模式: {self._list_mode} ({mode_desc.get(self._list_mode, '')})")

        if self._list_mode == "whitelist" and self._command_whitelist:
            lines.append(f"  白名单指令: {', '.join(self._command_whitelist)}")
        elif self._list_mode == "blacklist" and self._command_blacklist:
            lines.append(f"  黑名单指令: {', '.join(self._command_blacklist)}")
        lines.append("")

        # 自定义配置
        lines.append("【自定义配置】")
        lines.append(f"  自定义描述数量: {len(self._custom_descriptions)}")
        lines.append(f"  自定义指令数量: {len(self._custom_commands)}")
        lines.append(f"  隐藏插件来源: {'✅' if self._hide_plugin_info else '❌'}")
        lines.append(f"  列表显示唤醒词前缀: {'✅' if self._show_wake_prefix_in_list else '❌'}")
        lines.append("")

        # 日志配置
        lines.append("【日志配置】")
        lines.append(f"  工具调用日志: {'✅' if self._enable_tool_logging else '❌'}")
        lines.append(f"  日志级别: {self._log_level}")
        lines.append("")

        # 缓存状态
        lines.append("【缓存状态】")
        lines.append(f"  指令数量: {len(self._commands_cache)}")
        lines.append(f"  插件数量: {len(self._plugins_cache)}")
        lines.append(f"  当前唤醒词: {self._get_wake_prefix_display()}")
        lines.append(f"  工具调用历史: {len(self._tool_call_history)} 条记录")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_refresh", alias={"刷新缓存"})
    async def refresh_cache_command(self, event: AstrMessageEvent):
        """手动刷新缓存"""
        self._refresh_all_cache()
        event.set_result(
            MessageEventResult()
            .message(f"缓存已刷新\n• 指令: {len(self._commands_cache)} 个\n• 插件: {len(self._plugins_cache)} 个\n• 唤醒词: {self._get_wake_prefix_display()}")
            .use_t2i(False)
        )

    @filter.command("lpb_list", alias={"列出指令"})
    async def list_commands_direct(self, event: AstrMessageEvent):
        """直接列出所有可用指令"""
        self._refresh_commands_cache()
        self._refresh_wake_prefix()

        visible_commands = {
            k: v for k, v in self._commands_cache.items()
            if self._is_command_visible(k)
        }

        if not visible_commands:
            event.set_result(
                MessageEventResult().message("当前没有可用的指令。").use_t2i(False)
            )
            return

        prefix = self._get_command_prefix() if self._show_wake_prefix_in_list else ""
        lines = ["📋 可用指令列表：", ""]

        # 按插件分组
        plugins_commands = {}
        for cmd_name, cmd_info in sorted(visible_commands.items()):
            pn = cmd_info.get("plugin", {}).get("name", "其他") if cmd_info.get("plugin") else "其他"
            if pn not in plugins_commands:
                plugins_commands[pn] = []
            plugins_commands[pn].append((cmd_name, cmd_info))

        for pn, cmds in plugins_commands.items():
            lines.append(f"【{pn}】")
            for cmd_name, cmd_info in cmds:
                desc = cmd_info["description"][:30]
                if len(cmd_info["description"]) > 30:
                    desc += "..."

                cmd_display = f"{prefix}{cmd_name}" if prefix else cmd_name

                # 标记
                marks = []
                if cmd_info.get("is_custom"):
                    marks.append("自定义")
                elif not self._is_command_executable(cmd_name):
                    marks.append("禁止执行")

                mark_str = f" [{', '.join(marks)}]" if marks else ""
                lines.append(f"  • {cmd_display}{mark_str} - {desc}")

            lines.append("")

        lines.append(f"共 {len(visible_commands)} 个指令")
        lines.append(f"唤醒词: {self._get_wake_prefix_display()}")
        lines.append("使用 /lpb_info <指令名> 查看详细信息")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_info", alias={"指令详情"})
    async def command_info_direct(self, event: AstrMessageEvent, command_name: str = ""):
        """查看特定指令的详细信息"""
        if not command_name:
            event.set_result(
                MessageEventResult()
                .message("请提供指令名称，如: /lpb_info help")
                .use_t2i(False)
            )
            return

        self._refresh_commands_cache()
        self._refresh_wake_prefix()

        # 查找指令
        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            event.set_result(
                MessageEventResult()
                .message(f"未找到指令「{command_name}」")
                .use_t2i(False)
            )
            return

        prefix = self._get_command_prefix()
        lines = [
            f"📋 指令详情：{prefix}{cmd_info['primary_name']}",
            "",
            f"描述：{cmd_info['description']}",
        ]

        # 标记
        if cmd_info.get("is_custom"):
            lines.append("类型：自定义指令（仅展示）")
        elif not self._is_command_executable(cmd_info['primary_name']):
            lines.append("状态：禁止 LLM 执行")

        aliases = [n for n in cmd_info["names"] if n != cmd_info['primary_name'] and not n.startswith(" ")]
        if aliases:
            lines.append(f"别名：{', '.join([f'{prefix}{a}' for a in aliases])}")

        if cmd_info["params"]:
            lines.append("")
            lines.append("参数：")
            for param_name, param_info in cmd_info["params"].items():
                required = "必填" if param_info.get("required", True) else "可选"
                default = param_info.get("default", "")
                default_str = f" (默认: {default})" if default else ""
                lines.append(f"  • {param_name} [{param_info['type']}] - {required}{default_str}")

        lines.append("")
        lines.append("使用示例：")
        if cmd_info.get("is_custom") and cmd_info.get("example"):
            lines.append(f"  {cmd_info['example']}")
        else:
            for example in self._generate_usage_examples(cmd_info):
                lines.append(f"  {example}")

        if cmd_info["plugin"] and not self._hide_plugin_info:
            lines.append(f"所属插件：{cmd_info['plugin']['name']}")

        lines.append(f"唤醒词：{self._get_wake_prefix_display()}")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_plugins", alias={"插件列表"})
    async def list_plugins_direct(self, event: AstrMessageEvent):
        """列出所有已加载的插件"""
        self._refresh_plugins_cache()

        if not self._plugins_cache:
            event.set_result(
                MessageEventResult().message("当前没有已加载的插件。").use_t2i(False)
            )
            return

        lines = ["📦 已加载插件列表：", ""]

        for plugin_name, plugin_info in sorted(self._plugins_cache.items()):
            status = "✅" if plugin_info.get("activated", False) else "❌"
            version = plugin_info.get("version", "未知")
            author = plugin_info.get("author", "未知")
            desc = plugin_info.get("desc", "")
            
            lines.append(f"{status} {plugin_name} (v{version}) by {author}")
            if desc:
                lines.append(f"   {desc[:50]}{'...' if len(desc) > 50 else ''}")

        lines.append("")
        lines.append(f"共 {len(self._plugins_cache)} 个插件")
        lines.append("使用 /lpb_plugin <插件名> 查看插件详情")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_plugin", alias={"插件详情"})
    async def plugin_info_direct(self, event: AstrMessageEvent, plugin_name: str = ""):
        """查看特定插件的详细信息"""
        if not plugin_name:
            event.set_result(
                MessageEventResult()
                .message("请提供插件名称，如: /lpb_plugin web_searcher")
                .use_t2i(False)
            )
            return

        self._refresh_plugins_cache()
        self._refresh_commands_cache()

        # 查找插件
        plugin_info = None
        for name, info in self._plugins_cache.items():
            if name.lower() == plugin_name.lower():
                plugin_info = info
                break

        if not plugin_info:
            event.set_result(
                MessageEventResult()
                .message(f"未找到插件「{plugin_name}」")
                .use_t2i(False)
            )
            return

        # 获取该插件的指令
        plugin_commands = []
        for cmd_name, cmd_info in self._commands_cache.items():
            if cmd_info.get("plugin", {}).get("name", "").lower() == plugin_info["name"].lower():
                plugin_commands.append(cmd_name)

        lines = [
            f"📦 插件详情：{plugin_info['name']}",
            "",
            f"作者：{plugin_info.get('author', '未知')}",
            f"版本：{plugin_info.get('version', '未知')}",
            f"状态：{'✅ 已激活' if plugin_info.get('activated', False) else '❌ 未激活'}",
        ]

        if plugin_info.get("repo"):
            lines.append(f"仓库：{plugin_info['repo']}")

        if plugin_info.get("desc"):
            lines.append(f"")
            lines.append(f"描述：{plugin_info['desc']}")

        lines.append("")
        lines.append(f"注册指令 ({len(plugin_commands)} 个)：")
        if plugin_commands:
            prefix = self._get_command_prefix()
            for cmd in plugin_commands[:10]:
                lines.append(f"  • {prefix}{cmd}")
            if len(plugin_commands) > 10:
                lines.append(f"  ... 还有 {len(plugin_commands) - 10} 个指令")
        else:
            lines.append("  无")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_wake", alias={"查看唤醒词"})
    async def show_wake_info(self, event: AstrMessageEvent):
        """查看当前唤醒词配置"""
        self._refresh_wake_prefix()

        lines = ["📢 机器人唤醒信息", ""]

        if self._wake_prefix:
            lines.append(f"唤醒词：{self._wake_prefix}")
            lines.append("")
            lines.append("触发方式：")
            lines.append(f"  • 发送「{self._wake_prefix}」开头的消息")
            lines.append(f"  • 指令格式：{self._wake_prefix}指令名 [参数]")
            lines.append("")
            lines.append("示例：")
            lines.append(f"  • {self._wake_prefix}help - 获取帮助")
            lines.append(f"  • {self._wake_prefix}天气 北京 - 查询天气")
        else:
            lines.append("唤醒词：未配置")
            lines.append("")
            lines.append("触发方式：")
            lines.append("  • @机器人 后发送消息")
            lines.append("  • 私聊直接发送消息")
            lines.append("  • 群聊中使用 / 开头的指令")
            lines.append("")
            lines.append("示例：")
            lines.append("  • /help - 获取帮助")
            lines.append("  • /天气 北京 - 查询天气")
            lines.append("  • @机器人 帮助 - @后发送指令")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_history", alias={"工具历史"})
    async def show_tool_history(self, event: AstrMessageEvent):
        """查看工具调用历史"""
        if not self._tool_call_history:
            event.set_result(
                MessageEventResult().message("暂无工具调用历史记录。").use_t2i(False)
            )
            return

        history_lines = ["📜 工具调用历史（最近20条）：", ""]

        for record in self._tool_call_history[-20:]:
            if record['event'] == 'tool_call_start':
                args_str = json.dumps(record['tool_args'], ensure_ascii=False) if record['tool_args'] else '无'
                history_lines.append(f"📞 调用 `{record['tool_name']}`")
                history_lines.append(f"   参数: {args_str[:100]}{'...' if len(args_str) > 100 else ''}")
            else:
                result_str = record.get('tool_result', '无')[:100]
                history_lines.append(f"   ↳ 返回: {result_str}{'...' if len(record.get('tool_result', '')) > 100 else ''}")
            history_lines.append("")

        history_lines.append(f"共 {len(self._tool_call_history)} 条历史记录")

        event.set_result(MessageEventResult().message("\n".join(history_lines)).use_t2i(False))

    async def terminate(self) -> None:
        """插件卸载"""
        logger.info("LLM Plugin Bridge 已卸载")
