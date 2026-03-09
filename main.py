"""
LLM Plugin Bridge - LLM 插件桥

整合 LLM 与 AstrBot 插件系统的桥梁，让 LLM 能够：
- 发现和了解所有可用的插件和指令
- 获取唤醒词和触发方式信息
- 获取原始消息（包含唤醒词），判断用户意图
- 执行插件指令（可配置权限控制）
"""

import inspect
import json
import time
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

        # ========== 消息记录 ==========
        # 保存最近的原始消息（包含唤醒词），key 为 session_id
        self._recent_original_messages: dict[str, dict] = {}

        # ========== 调用记录 ==========
        self._recent_command_invocations: list[dict] = []
        self._recent_llm_tool_calls: list[dict] = []

        # ========== 执行相关配置 ==========
        self._allow_execute = self._config.get("allow_execute", True)
        self._execute_require_admin = self._config.get("execute_require_admin", False)
        self._blocked_commands = set(self._config.get("blocked_commands", []))

        # ========== 列表过滤配置 ==========
        self._list_mode = self._config.get("list_mode", "all")
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

    def _log(self, message: str, level: str = "info"):
        if self._log_level == "debug" or level == "info":
            logger.info(message)
        elif level == "debug":
            logger.debug(message)

    # ==================== 缓存管理 ====================

    def _refresh_all_cache(self) -> None:
        self._refresh_wake_prefix()
        self._refresh_commands_cache()
        self._refresh_plugins_cache()

    def _refresh_wake_prefix(self) -> None:
        try:
            cfg = self.context.get_config()
            if cfg:
                provider_settings = cfg.get("provider_settings", {})
                self._wake_prefix = provider_settings.get("wake_prefix", "")
        except Exception as e:
            logger.warning(f"获取唤醒词配置失败: {e}")
            self._wake_prefix = ""

    def _refresh_commands_cache(self) -> None:
        self._commands_cache.clear()

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=True
        )

        for handler_md in handlers:
            command_filter = None
            for event_filter in handler_md.event_filters:
                if isinstance(event_filter, CommandFilter):
                    command_filter = event_filter
                    break

            if not command_filter:
                continue

            cmd_names = command_filter.get_complete_command_names()
            if not cmd_names:
                continue

            primary_name = command_filter.command_name

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

            description = self._get_command_description(
                primary_name, handler_md.desc or "无描述"
            )

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

        for custom_cmd in self._custom_commands:
            cmd_name = custom_cmd.get("name")
            if not cmd_name:
                continue

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
        if self._wake_prefix:
            return self._wake_prefix
        return "无唤醒词（@机器人 或私聊即可触发）"

    def _get_command_prefix(self) -> str:
        if self._wake_prefix:
            return self._wake_prefix
        return "/"

    def _is_command_visible(self, cmd_name: str) -> bool:
        if self._list_mode == "whitelist":
            return cmd_name in self._command_whitelist
        elif self._list_mode == "blacklist":
            return cmd_name not in self._command_blacklist
        else:
            return True

    def _is_command_executable(self, cmd_name: str) -> bool:
        return cmd_name not in self._blocked_commands

    def _get_command_description(self, cmd_name: str, default_desc: str) -> str:
        if cmd_name in self._custom_descriptions:
            return self._custom_descriptions[cmd_name]
        return default_desc

    def _generate_usage_examples(self, cmd_info: dict) -> list[str]:
        primary_name = cmd_info["primary_name"]
        params = cmd_info["params"]
        prefix = self._get_command_prefix()

        examples = []

        if not params:
            examples.append(f"{prefix}{primary_name}")
        else:
            param_strs = []
            example_values = []
            for param_name, param_info in params.items():
                if param_info.get("required", True):
                    param_strs.append(f"<{param_name}>")
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

    def _save_original_message(self, session_id: str, original_message: str, processed_message: str, sender_id: str):
        """保存原始消息（包含唤醒词）"""
        self._recent_original_messages[session_id] = {
            "original_message": original_message,  # 用户发送的原始消息（包含唤醒词）
            "processed_message": processed_message,  # 处理后的消息（去掉唤醒词）
            "sender_id": sender_id,
            "timestamp": time.time(),
        }
        
        # 清理过期记录（保留最近 100 条，5 分钟内）
        current_time = time.time()
        expired_keys = [
            k for k, v in self._recent_original_messages.items()
            if current_time - v["timestamp"] > 300  # 5 分钟
        ]
        for k in expired_keys:
            del self._recent_original_messages[k]
        
        # 如果超过 100 条，删除最旧的
        if len(self._recent_original_messages) > 100:
            sorted_keys = sorted(
                self._recent_original_messages.keys(),
                key=lambda k: self._recent_original_messages[k]["timestamp"]
            )
            for k in sorted_keys[:len(self._recent_original_messages) - 80]:
                del self._recent_original_messages[k]

    def _get_original_message(self, session_id: str) -> dict | None:
        """获取原始消息"""
        return self._recent_original_messages.get(session_id)

    def _add_command_invocation(self, command_name: str, args: str, sender_id: str, message_str: str):
        invocation = {
            "command": command_name,
            "args": args,
            "sender_id": sender_id,
            "message_str": message_str[:200] if message_str else "",
            "timestamp": time.time(),
        }
        self._recent_command_invocations.append(invocation)
        
        if len(self._recent_command_invocations) > 50:
            self._recent_command_invocations = self._recent_command_invocations[-30:]
        
        self._log(f"[Command Invocation] 用户执行指令: {command_name} {args}")

    def _add_llm_tool_call(self, tool_name: str, tool_args: dict | None, sender_id: str):
        call = {
            "tool": tool_name,
            "args": tool_args,
            "sender_id": sender_id,
            "timestamp": time.time(),
        }
        self._recent_llm_tool_calls.append(call)
        
        if len(self._recent_llm_tool_calls) > 50:
            self._recent_llm_tool_calls = self._recent_llm_tool_calls[-30:]
        
        self._log(f"[LLM Tool Call] 工具 '{tool_name}' 被调用")

    def _check_user_intent(self, sender_id: str, message_str: str, time_window: float = 5.0) -> dict:
        current_time = time.time()
        
        for invocation in reversed(self._recent_command_invocations):
            if current_time - invocation["timestamp"] > time_window:
                break
            
            if invocation["sender_id"] == sender_id:
                if invocation["message_str"] and message_str:
                    return {
                        "has_command": True,
                        "command_info": {
                            "command": invocation["command"],
                            "args": invocation["args"],
                        },
                        "should_skip_llm_execution": True,
                        "reason": f"用户已通过指令「{invocation['command']}」触发了该功能，LLM 不应重复执行。",
                    }
        
        return {
            "has_command": False,
            "command_info": None,
            "should_skip_llm_execution": False,
            "reason": None,
        }

    # ==================== LLM 工具 ====================

    @filter.llm_tool(name="get_wake_info")
    async def get_wake_info(self, event: AstrMessageEvent) -> str:
        """获取机器人的唤醒信息和当前消息的原始内容。当 LLM 需要判断用户意图、确认是否被误触发时调用此工具。

        返回信息包括：
        - 唤醒词配置
        - 用户发送的原始消息（包含唤醒词）
        - LLM 收到的消息（去掉唤醒词后）
        - 用户意图判断建议
        """
        self._log("[LLM Tool] get_wake_info 被调用")
        self._refresh_wake_prefix()

        # 获取当前会话的原始消息
        session_id = event.session_id
        original_msg_info = self._get_original_message(session_id)
        
        # LLM 收到的消息
        llm_received_message = event.message_str or ""

        result = {
            "wake_prefix": self._wake_prefix if self._wake_prefix else None,
            "current_session": {
                "session_id": session_id,
                "llm_received_message": llm_received_message,
            },
        }

        # 如果有原始消息记录
        if original_msg_info:
            result["current_session"]["original_message"] = original_msg_info["original_message"]
            result["current_session"]["message_was_modified"] = (
                original_msg_info["original_message"] != original_msg_info["processed_message"]
            )
            
            # 分析用户意图
            original = original_msg_info["original_message"]
            wake = self._wake_prefix or ""
            
            # 判断是否是指令
            is_command = False
            command_name = None
            
            if wake and original.startswith(wake):
                # 去掉唤醒词后的内容
                after_wake = original[len(wake):].strip()
                
                # 检查是否匹配已知指令
                for cmd_name in self._commands_cache.keys():
                    if after_wake.startswith(cmd_name) or after_wake == cmd_name:
                        is_command = True
                        command_name = cmd_name
                        break
                
                # 如果不是已知指令，分析可能的情况
                if not is_command:
                    # 检查是否是唤醒词+其他内容（非指令）
                    result["current_session"]["analysis"] = {
                        "is_known_command": False,
                        "possible_intent": "用户可能是在问问题或进行普通对话，而不是执行指令",
                        "wake_prefix_detected": True,
                        "content_after_wake": after_wake,
                    }
                    
                    # 特殊情况：唤醒词可能是某个词的一部分
                    # 例如 "nova14怎么样？" 中 "nova" 是 "nova14" 的一部分
                    if after_wake and not after_wake.startswith(" "):
                        result["current_session"]["analysis"]["note"] = (
                            f"唤醒词「{wake}」后面没有空格，可能是用户在提及包含唤醒词的词汇"
                            f"（如「{wake}14」），而不是在触发机器人。"
                        )
            else:
                result["current_session"]["analysis"] = {
                    "is_known_command": False,
                    "possible_intent": "消息不以唤醒词开头，可能是 @机器人 或私聊触发",
                }
            
            if is_command:
                result["current_session"]["analysis"] = {
                    "is_known_command": True,
                    "command_name": command_name,
                    "possible_intent": f"用户想执行「{command_name}」指令",
                }
        else:
            result["current_session"]["original_message"] = llm_received_message
            result["current_session"]["message_was_modified"] = False
            result["current_session"]["note"] = "未找到原始消息记录，可能消息未被处理过"

        # 添加判断建议
        result["intent_judgment_guide"] = {
            "how_to_judge": [
                "1. 比较 original_message 和 llm_received_message，判断消息是否被修改",
                "2. 如果唤醒词后面是已知指令名，用户可能在执行指令",
                "3. 如果唤醒词后面不是指令名，用户可能在普通对话",
                "4. 如果唤醒词后面没有空格，可能是用户在提及包含唤醒词的词汇",
            ],
            "examples": [
                {
                    "original": f"{self._wake_prefix}天气 北京" if self._wake_prefix else "/天气 北京",
                    "llm_receives": "天气 北京",
                    "intent": "执行天气指令",
                },
                {
                    "original": f"{self._wake_prefix}14怎么样？" if self._wake_prefix else "14怎么样？",
                    "llm_receives": "14怎么样？",
                    "intent": f"用户在问「{self._wake_prefix}14」相关问题，不是执行指令" if self._wake_prefix else "普通对话",
                },
                {
                    "original": f"{self._wake_prefix} 你好" if self._wake_prefix else "你好",
                    "llm_receives": "你好",
                    "intent": "普通对话",
                },
            ],
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="check_user_intent")
    async def check_user_intent(self, event: AstrMessageEvent) -> str:
        """检查用户意图，判断用户是否已经通过指令方式触发了功能。在执行任何可能重复的操作之前调用此工具。"""
        self._log("[LLM Tool] check_user_intent 被调用")
        
        sender_id = event.get_sender_id()
        message_str = event.message_str or ""
        
        result = self._check_user_intent(sender_id, message_str)
        
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
            keyword(string): 可选的关键词，用于过滤指令名称或描述中包含该关键词的指令。
            plugin_name(string): 可选的插件名称，用于过滤特定插件的指令。
            include_params(boolean): 是否包含参数信息。
        """
        self._log(f"[LLM Tool] list_commands 被调用, keyword={keyword}")
        self._refresh_commands_cache()
        self._refresh_wake_prefix()

        commands = []
        for cmd_name, cmd_info in self._commands_cache.items():
            if not self._is_command_visible(cmd_name):
                continue

            if plugin_name:
                cmd_plugin = cmd_info.get("plugin", {})
                if cmd_plugin and cmd_plugin.get("name", "").lower() != plugin_name.lower():
                    continue
                elif not cmd_plugin:
                    continue

            if keyword:
                keyword_lower = keyword.lower()
                if (
                    keyword_lower not in cmd_name.lower()
                    and keyword_lower not in cmd_info["description"].lower()
                ):
                    continue

            cmd_entry = {
                "name": cmd_name,
                "description": cmd_info["description"],
            }

            if self._show_wake_prefix_in_list:
                cmd_entry["full_command"] = f"{self._get_command_prefix()}{cmd_name}"

            aliases = [n for n in cmd_info["names"] if n != cmd_name and not n.startswith(" ")]
            if aliases:
                cmd_entry["aliases"] = aliases

            if include_params and cmd_info["params"]:
                cmd_entry["params"] = cmd_info["params"]

            if cmd_info["plugin"] and not self._hide_plugin_info:
                cmd_entry["plugin"] = cmd_info["plugin"]["name"]

            if cmd_info["is_custom"]:
                cmd_entry["is_custom"] = True
            else:
                cmd_entry["executable"] = self._is_command_executable(cmd_name)

            commands.append(cmd_entry)

        if not commands:
            if keyword:
                return f"没有找到包含关键词「{keyword}」的指令。"
            if plugin_name:
                return f"没有找到插件「{plugin_name}」的指令。"
            return "当前没有可用的指令。"

        result = {
            "total": len(commands),
            "commands": commands,
        }

        if self._show_wake_prefix_in_list:
            result["wake_prefix"] = self._wake_prefix if self._wake_prefix else "无"
            result["command_prefix"] = self._get_command_prefix()

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_command_details")
    async def get_command_details(self, event: AstrMessageEvent, command_name: str) -> str:
        """获取特定指令的详细信息。

        Args:
            command_name(string): 指令名称。
        """
        self._log(f"[LLM Tool] get_command_details 被调用, command_name={command_name}")
        self._refresh_commands_cache()

        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            similar = [name for name in self._commands_cache.keys() if command_name.lower() in name.lower()]
            if similar:
                return f"未找到指令「{command_name}」。您是否要找: {', '.join(similar)}？"
            return f"未找到指令「{command_name}」。"

        result = {
            "name": cmd_info["primary_name"],
            "full_command": f"{self._get_command_prefix()}{cmd_info['primary_name']}",
            "description": cmd_info["description"],
            "params": cmd_info["params"] if cmd_info["params"] else None,
            "usage_examples": self._generate_usage_examples(cmd_info),
        }

        if cmd_info["plugin"] and not self._hide_plugin_info:
            result["plugin"] = cmd_info["plugin"]["name"]

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_plugins")
    async def list_plugins(self, event: AstrMessageEvent) -> str:
        """列出所有已加载的插件。"""
        self._log("[LLM Tool] list_plugins 被调用")
        self._refresh_plugins_cache()

        if not self._plugins_cache:
            return "当前没有已加载的插件。"

        plugins = []
        for plugin_name, plugin_info in self._plugins_cache.items():
            plugins.append({
                "name": plugin_info["name"],
                "author": plugin_info.get("author", "未知"),
                "version": plugin_info.get("version", "未知"),
                "description": plugin_info.get("desc", ""),
                "activated": plugin_info.get("activated", False),
            })

        return json.dumps({"total": len(plugins), "plugins": plugins}, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_plugin_info")
    async def get_plugin_info_tool(self, event: AstrMessageEvent, plugin_name: str) -> str:
        """获取特定插件的详细信息。

        Args:
            plugin_name(string): 插件名称。
        """
        self._log(f"[LLM Tool] get_plugin_info 被调用, plugin_name={plugin_name}")
        self._refresh_plugins_cache()
        self._refresh_commands_cache()

        plugin_info = None
        for name, info in self._plugins_cache.items():
            if name.lower() == plugin_name.lower():
                plugin_info = info
                break

        if not plugin_info:
            return f"未找到插件「{plugin_name}」。"

        plugin_commands = []
        for cmd_name, cmd_info in self._commands_cache.items():
            if cmd_info.get("plugin", {}).get("name", "").lower() == plugin_info["name"].lower():
                plugin_commands.append({"name": cmd_name, "description": cmd_info["description"]})

        result = {
            "name": plugin_info["name"],
            "author": plugin_info.get("author", "未知"),
            "version": plugin_info.get("version", "未知"),
            "description": plugin_info.get("desc", ""),
            "activated": plugin_info.get("activated", False),
            "commands": plugin_commands[:10],
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="execute_command")
    async def execute_command(self, event: AstrMessageEvent, command_name: str, args: str = "") -> str:
        """执行指定的插件指令。

        Args:
            command_name(string): 要执行的指令名称。
            args(string): 指令参数。
        """
        self._log(f"[LLM Tool] execute_command 被调用, command_name={command_name}")

        # 检查用户意图
        intent = self._check_user_intent(event.get_sender_id(), event.message_str or "")
        if intent["should_skip_llm_execution"]:
            return f"跳过执行：{intent['reason']}"

        if not self._allow_execute:
            return "错误：LLM 指令执行功能已被禁用。"

        if self._execute_require_admin and not event.is_admin():
            return "错误：执行指令需要管理员权限。"

        self._refresh_commands_cache()

        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            return f"错误：未找到指令「{command_name}」。"

        if cmd_info.get("is_custom"):
            return "错误：自定义指令无法执行。"

        if not self._is_command_executable(command_name):
            return f"错误：指令「{command_name}」已被禁止执行。"

        try:
            handler_md = cmd_info["handler_md"]
            command_filter = cmd_info["command_filter"]

            args_list = args.split() if args else []
            try:
                parsed_params = command_filter.validate_and_convert_params(args_list, command_filter.handler_params)
            except ValueError as e:
                return f"参数错误: {str(e)}"

            from astrbot.core.star.star import star_map
            star_info = star_map.get(handler_md.handler_module_path)
            if not star_info or not star_info.star_cls_type:
                return "错误：无法获取插件实例。"

            handler = handler_md.handler
            event.set_extra("parsed_params", parsed_params)

            result = handler(event, **parsed_params)

            if inspect.isasyncgen(result):
                last_item = None
                async for item in result:
                    last_item = item
                result = last_item
            elif inspect.iscoroutine(result):
                result = await result

            if result is not None:
                if isinstance(result, MessageEventResult):
                    plain_text = result.get_plain_text()
                    return f"执行成功: {plain_text}" if plain_text else "执行成功。"
                elif isinstance(result, str):
                    return f"执行成功: {result}"
                return "执行成功。"

            event_result = event.get_result()
            if event_result:
                plain_text = event_result.get_plain_text()
                return f"执行结果: {plain_text}" if plain_text else "执行成功。"

            return "指令已执行。"

        except Exception as e:
            logger.error(f"执行指令时发生错误: {e}", exc_info=True)
            return f"执行错误: {str(e)}"

    # ==================== 事件监听 ====================

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None):
        """监听 LLM Tool 调用事件"""
        if not self._enable_tool_logging:
            return
        self._add_llm_tool_call(tool.name if tool else "unknown", tool_args, event.get_sender_id())

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None, tool_result: Any):
        pass

    @filter.on_command_run()
    async def on_command_run(self, event: AstrMessageEvent):
        """监听指令执行事件"""
        parsed_params = event.get_extra("parsed_params") or {}
        command_name = event.get_extra("command_name") or ""
        
        if command_name:
            args_str = " ".join(str(v) for v in parsed_params.values()) if parsed_params else ""
            self._add_command_invocation(command_name, args_str, event.get_sender_id(), event.message_str)

    @filter.on_all_message()
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有消息，保存原始消息（包含唤醒词）"""
        # 获取原始消息（包含唤醒词）
        # 注意：event.message_str 可能已经是处理后的消息
        # 我们需要从 event 的其他属性获取原始消息
        
        original_message = event.message_str or ""
        
        # 尝试获取原始消息
        # AstrBot 可能会在 event 中保存原始消息
        if hasattr(event, 'raw_message') and event.raw_message:
            original_message = event.raw_message
        elif hasattr(event, '_raw_message') and event._raw_message:
            original_message = event._raw_message
        elif hasattr(event, 'original_message') and event.original_message:
            original_message = event.original_message
        
        # 如果有唤醒词，尝试重建原始消息
        if self._wake_prefix and event.message_str:
            # 检查消息是否以唤醒词开头（已经被去掉的情况）
            # 如果 LLM 收到的消息不是以唤醒词开头，可能唤醒词已被去掉
            if not event.message_str.startswith(self._wake_prefix):
                # 检查是否可能是唤醒词触发的消息
                # 通过检查消息是否匹配某个指令来判断
                message_words = event.message_str.split()
                if message_words:
                    first_word = message_words[0]
                    # 如果第一个词是指令名，说明可能是唤醒词+指令
                    if first_word in self._commands_cache:
                        original_message = self._wake_prefix + event.message_str
        
        self._save_original_message(
            session_id=event.session_id,
            original_message=original_message,
            processed_message=event.message_str or "",
            sender_id=event.get_sender_id(),
        )

    # ==================== 用户指令 ====================

    @filter.command("lpb_config", alias={"插件桥配置"})
    async def show_config(self, event: AstrMessageEvent):
        """显示当前插件配置"""
        lines = ["⚙️ LLM Plugin Bridge 配置信息", ""]
        lines.append("【执行配置】")
        lines.append(f"  允许 LLM 执行: {'✅' if self._allow_execute else '❌'}")
        lines.append(f"  执行需管理员权限: {'✅' if self._execute_require_admin else '❌'}")
        lines.append(f"  禁止执行的指令: {', '.join(self._blocked_commands) or '无'}")
        lines.append("")
        lines.append("【缓存状态】")
        lines.append(f"  指令数量: {len(self._commands_cache)}")
        lines.append(f"  插件数量: {len(self._plugins_cache)}")
        lines.append(f"  唤醒词: {self._get_wake_prefix_display()}")
        lines.append(f"  原始消息记录: {len(self._recent_original_messages)} 条")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_refresh", alias={"刷新缓存"})
    async def refresh_cache_command(self, event: AstrMessageEvent):
        """手动刷新缓存"""
        self._refresh_all_cache()
        event.set_result(
            MessageEventResult()
            .message(f"缓存已刷新\n• 指令: {len(self._commands_cache)} 个\n• 插件: {len(self._plugins_cache)} 个")
            .use_t2i(False)
        )

    @filter.command("lpb_list", alias={"列出指令"})
    async def list_commands_direct(self, event: AstrMessageEvent):
        """列出所有可用指令"""
        self._refresh_commands_cache()

        visible_commands = {k: v for k, v in self._commands_cache.items() if self._is_command_visible(k)}
        if not visible_commands:
            event.set_result(MessageEventResult().message("当前没有可用的指令。").use_t2i(False))
            return

        prefix = self._get_command_prefix() if self._show_wake_prefix_in_list else ""
        lines = ["📋 可用指令列表：", ""]

        for cmd_name, cmd_info in sorted(visible_commands.items()):
            desc = cmd_info["description"][:30]
            if len(cmd_info["description"]) > 30:
                desc += "..."
            cmd_display = f"{prefix}{cmd_name}" if prefix else cmd_name
            lines.append(f"  • {cmd_display} - {desc}")

        lines.append(f"\n共 {len(visible_commands)} 个指令")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_info", alias={"指令详情"})
    async def command_info_direct(self, event: AstrMessageEvent, command_name: str = ""):
        """查看指令详情"""
        if not command_name:
            event.set_result(MessageEventResult().message("请提供指令名称。").use_t2i(False))
            return

        self._refresh_commands_cache()
        cmd_info = None
        for name, info in self._commands_cache.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break

        if not cmd_info:
            event.set_result(MessageEventResult().message(f"未找到指令「{command_name}」").use_t2i(False))
            return

        lines = [
            f"📋 指令：{self._get_command_prefix()}{cmd_info['primary_name']}",
            f"描述：{cmd_info['description']}",
        ]
        if cmd_info["params"]:
            lines.append("参数：")
            for param_name, param_info in cmd_info["params"].items():
                lines.append(f"  • {param_name}: {param_info.get('type', 'any')}")

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    @filter.command("lpb_wake", alias={"查看唤醒词"})
    async def show_wake_info(self, event: AstrMessageEvent):
        """查看唤醒词配置"""
        self._refresh_wake_prefix()
        lines = ["📢 机器人唤醒信息", "", f"唤醒词：{self._get_wake_prefix_display()}"]
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))

    async def terminate(self) -> None:
        logger.info("LLM Plugin Bridge 已卸载")
