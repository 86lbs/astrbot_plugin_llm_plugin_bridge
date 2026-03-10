"""
LLM Plugin Bridge - LLM 插件桥

整合 LLM 与 AstrBot 插件系统的桥梁，让 LLM 能够：
- 发现和了解所有可用的插件和指令
- 获取唤醒词和触发方式信息
- 获取原始消息历史，判断用户意图
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


# ==================== 常量定义 ====================

class ConfigDefaults:
    """配置默认值常量"""
    MAX_HISTORY_PER_SESSION = 60
    SESSION_EXPIRE_SECONDS = 1800
    INTENT_TIME_WINDOW = 5.0
    MAX_INVOCATION_RECORDS = 50
    CLEANUP_THRESHOLD = 100  # 每隔多少次操作触发一次清理


# ==================== 缓存管理器 ====================

class CacheManager:
    """缓存管理器 - 负责指令和插件缓存的刷新与管理"""
    
    def __init__(self, context: Context, config: dict):
        self._context = context
        self._config = config
        
        self._commands_cache: dict[str, dict] = {}
        self._plugins_cache: dict[str, dict] = {}
        self._wake_prefix: str = ""
        
        # 配置项
        self._hide_plugin_info = config.get("hide_plugin_info", False)
        self._custom_descriptions = config.get("custom_descriptions", {})
        self._custom_commands = config.get("custom_commands", [])
    
    def refresh_all(self) -> None:
        """刷新所有缓存"""
        self._refresh_wake_prefix()
        self._refresh_commands()
        self._refresh_plugins()
    
    def _refresh_wake_prefix(self) -> None:
        """刷新唤醒词"""
        try:
            cfg = self._context.get_config()
            if cfg:
                provider_settings = cfg.get("provider_settings", {})
                self._wake_prefix = provider_settings.get("wake_prefix", "")
        except Exception as e:
            logger.warning(f"获取唤醒词配置失败: {e}")
            self._wake_prefix = ""
    
    def _refresh_commands(self) -> None:
        """刷新指令缓存"""
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
            
            params_info = self._extract_params(command_filter)
            plugin_info = self._get_plugin_info(handler_md.handler_module_path)
            description = self._get_description(primary_name, handler_md.desc or "无描述")
            
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
        
        self._add_custom_commands()
    
    def _refresh_plugins(self) -> None:
        """刷新插件缓存"""
        self._plugins_cache.clear()
        
        for star_md in self._context.get_all_stars():
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
    
    def _extract_params(self, command_filter: CommandFilter) -> dict:
        """提取参数信息"""
        params_info = {}
        for param_name, param_type in command_filter.handler_params.items():
            if isinstance(param_type, type):
                params_info[param_name] = {"type": param_type.__name__, "required": True}
            elif param_type is None:
                params_info[param_name] = {"type": "any", "required": True}
            else:
                params_info[param_name] = {
                    "type": type(param_type).__name__,
                    "default": str(param_type),
                    "required": False,
                }
        return params_info
    
    def _get_plugin_info(self, module_path: str) -> dict | None:
        """获取插件信息"""
        if self._hide_plugin_info:
            return None
        
        for star_md in self._context.get_all_stars():
            if star_md.module_path == module_path:
                return {
                    "name": star_md.name,
                    "author": star_md.author,
                    "desc": star_md.desc,
                }
        return None
    
    def _get_description(self, cmd_name: str, default: str) -> str:
        """获取指令描述"""
        return self._custom_descriptions.get(cmd_name, default)
    
    def _add_custom_commands(self) -> None:
        """添加自定义指令"""
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
    
    @property
    def commands(self) -> dict[str, dict]:
        return self._commands_cache
    
    @property
    def plugins(self) -> dict[str, dict]:
        return self._plugins_cache
    
    @property
    def wake_prefix(self) -> str:
        return self._wake_prefix
    
    def get_wake_prefix_display(self) -> str:
        """获取唤醒词显示文本"""
        return self._wake_prefix if self._wake_prefix else "无唤醒词（@机器人 或私聊即可触发）"
    
    def get_command_prefix(self) -> str:
        """获取指令前缀"""
        return self._wake_prefix if self._wake_prefix else "/"


# ==================== 消息历史管理器 ====================

class MessageHistoryManager:
    """消息历史管理器 - 负责消息历史的存储、获取和清理"""
    
    def __init__(self, config: dict):
        self._history: dict[str, list[dict]] = {}
        self._operation_count = 0  # 操作计数器
        
        # 配置项
        self._max_history = config.get("max_history_per_session", ConfigDefaults.MAX_HISTORY_PER_SESSION)
        self._expire_seconds = config.get("session_expire_seconds", ConfigDefaults.SESSION_EXPIRE_SECONDS)
        self._cleanup_threshold = config.get("cleanup_threshold", ConfigDefaults.CLEANUP_THRESHOLD)
    
    def save(self, session_id: str, role: str, content: str, sender_name: str = "") -> None:
        """保存消息到历史记录"""
        if session_id not in self._history:
            self._history[session_id] = []
        
        self._history[session_id].append({
            "role": role,
            "content": content,
            "sender_name": sender_name,
            "timestamp": time.time(),
        })
        
        # 限制历史记录数量
        if len(self._history[session_id]) > self._max_history:
            self._history[session_id] = self._history[session_id][-self._max_history:]
        
        # 使用计数器触发清理，避免每次操作都遍历
        self._operation_count += 1
        if self._operation_count >= self._cleanup_threshold:
            self._cleanup_expired()
            self._operation_count = 0
    
    def get(self, session_id: str, limit: int = 10) -> list[dict]:
        """获取消息历史"""
        if session_id not in self._history:
            return []
        return self._history[session_id][-limit:]
    
    def _cleanup_expired(self) -> None:
        """清理过期会话"""
        current_time = time.time()
        expired = [
            sid for sid, records in self._history.items()
            if records and current_time - records[-1]["timestamp"] > self._expire_seconds
        ]
        for sid in expired:
            del self._history[sid]
        
        if expired:
            logger.debug(f"[MessageHistory] 清理了 {len(expired)} 个过期会话")
    
    @property
    def session_count(self) -> int:
        return len(self._history)


# ==================== 主插件类 ====================

class Main(star.Star):
    """LLM 插件桥 - 让 LLM 能够发现、了解和执行插件指令"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._config = config or {}
        
        # 初始化管理器
        self._cache_mgr = CacheManager(context, self._config)
        self._history_mgr = MessageHistoryManager(self._config)
        
        # 调用记录
        self._recent_invocations: list[dict] = []
        self._recent_tool_calls: list[dict] = []
        
        # 执行配置
        self._allow_execute = self._config.get("allow_execute", True)
        self._execute_require_admin = self._config.get("execute_require_admin", False)
        self._blocked_commands = set(self._config.get("blocked_commands", []))
        
        # 列表过滤配置
        self._list_mode = self._config.get("list_mode", "all")
        self._command_whitelist = set(self._config.get("command_whitelist", []))
        self._command_blacklist = set(self._config.get("command_blacklist", []))
        
        # 显示配置
        self._show_wake_prefix = self._config.get("show_wake_prefix_in_list", True)
        
        # 日志配置
        self._enable_logging = self._config.get("enable_tool_logging", True)
        self._log_level = self._config.get("log_level", "info")
        
        # 意图检测时间窗口
        self._intent_time_window = self._config.get("intent_time_window", ConfigDefaults.INTENT_TIME_WINDOW)

    async def initialize(self) -> None:
        """插件初始化"""
        self._cache_mgr.refresh_all()
        logger.info(f"LLM Plugin Bridge 初始化完成")
        logger.info(f"  - 已缓存 {len(self._cache_mgr.commands)} 个指令")
        logger.info(f"  - 已缓存 {len(self._cache_mgr.plugins)} 个插件")
        logger.info(f"  - 唤醒词: {self._cache_mgr.get_wake_prefix_display()}")
        logger.info(f"  - 列表模式: {self._list_mode}")
        logger.info(f"  - LLM 执行功能: {'已启用' if self._allow_execute else '已禁用'}")

    def _log(self, message: str) -> None:
        """记录日志"""
        if self._enable_logging and self._log_level == "debug":
            logger.debug(message)
        elif self._enable_logging:
            logger.info(message)

    # ==================== 辅助方法 ====================

    def _is_visible(self, cmd_name: str) -> bool:
        """检查指令是否可见"""
        if self._list_mode == "whitelist":
            return cmd_name in self._command_whitelist
        elif self._list_mode == "blacklist":
            return cmd_name not in self._command_blacklist
        return True

    def _is_executable(self, cmd_name: str) -> bool:
        """检查指令是否可执行"""
        return cmd_name not in self._blocked_commands

    def _get_raw_message(self, event: AstrMessageEvent) -> str:
        """获取原始消息（简化版）"""
        # 尝试从 message_obj 获取原始消息
        msg_obj = getattr(event, 'message_obj', None)
        if not msg_obj:
            return event.message_str or ""
        
        # 尝试获取 raw_message 属性
        raw = getattr(msg_obj, 'raw_message', None)
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            # 尝试常见的字典结构
            return raw.get('raw_message') or raw.get('message') or str(raw)
        
        # 尝试 message_str 属性
        msg_str = getattr(msg_obj, 'message_str', None)
        if isinstance(msg_str, str):
            return msg_str
        
        return event.message_str or ""

    def _generate_examples(self, cmd_info: dict) -> list[str]:
        """生成使用示例"""
        name = cmd_info["primary_name"]
        params = cmd_info["params"]
        prefix = self._cache_mgr.get_command_prefix()
        
        if not params:
            return [f"{prefix}{name}"]
        
        param_strs = []
        values = []
        for pname, pinfo in params.items():
            if pinfo.get("required", True):
                param_strs.append(f"<{pname}>")
                ptype = pinfo.get("type", "str")
                values.append({"int": "1", "float": "1.5", "bool": "true"}.get(ptype, f"示例{pname}"))
            else:
                param_strs.append(f"[{pname}]")
        
        examples = [f"{prefix}{name} {' '.join(param_strs)}"]
        if values:
            examples.append(f"{prefix}{name} {' '.join(values)}")
        return examples

    def _add_invocation(self, cmd: str, args: str, sender_id: str, msg: str) -> None:
        """记录指令调用"""
        self._recent_invocations.append({
            "command": cmd,
            "args": args,
            "sender_id": sender_id,
            "message_str": msg[:200] if msg else "",
            "timestamp": time.time(),
        })
        
        max_records = self._config.get("max_invocation_records", ConfigDefaults.MAX_INVOCATION_RECORDS)
        if len(self._recent_invocations) > max_records:
            self._recent_invocations = self._recent_invocations[-max_records//2:]

    def _check_intent(self, sender_id: str, msg: str) -> dict:
        """检查用户意图"""
        current = time.time()
        
        for inv in reversed(self._recent_invocations):
            if current - inv["timestamp"] > self._intent_time_window:
                break
            if inv["sender_id"] == sender_id and inv["message_str"]:
                return {
                    "has_command": True,
                    "command_info": {"command": inv["command"], "args": inv["args"]},
                    "should_skip_llm_execution": True,
                    "reason": f"用户已通过指令「{inv['command']}」触发了该功能，LLM 不应重复执行。",
                }
        
        return {"has_command": False, "command_info": None, "should_skip_llm_execution": False, "reason": None}

    # ==================== LLM 工具 ====================

    @filter.llm_tool(name="get_wake_info")
    async def get_wake_info(self, event: AstrMessageEvent) -> str:
        """【重要】获取机器人的唤醒信息和消息的原始内容。

        **必须在以下情况调用此工具：**
        1. 用户消息内容存在歧义，可能是指令也可能是普通对话
        2. 用户消息看起来像指令但不确定是否真的是指令
        3. 用户消息开头是指令名但可能是误触发
        4. 需要确认用户真实意图时

        返回信息包括：
        - 唤醒词配置
        - 用户发送的原始消息（包含唤醒词）
        - LLM 收到的消息（去掉唤醒词后）
        - 最近的消息历史
        """
        self._log("[LLM Tool] get_wake_info 被调用")
        self._cache_mgr._refresh_wake_prefix()
        
        session_id = event.session_id
        raw_msg = self._get_raw_message(event)
        llm_msg = event.message_str or ""
        
        # 保存消息
        self._history_mgr.save(session_id, "user", raw_msg, event.get_sender_name())
        
        result = {
            "wake_prefix": self._cache_mgr.wake_prefix or None,
            "current_message": {
                "original_message": raw_msg,
                "llm_received_message": llm_msg,
                "message_was_modified": raw_msg != llm_msg,
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
            },
        }
        
        # 获取历史
        history = self._history_mgr.get(session_id, 61)
        if len(history) > 1:
            result["recent_history"] = [
                {"role": h["role"], "content": h["content"], "sender_name": h["sender_name"]}
                for h in history[:-1]
            ]
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="check_user_intent")
    async def check_user_intent(self, event: AstrMessageEvent) -> str:
        """检查用户意图，判断用户是否已经通过指令方式触发了功能。"""
        self._log("[LLM Tool] check_user_intent 被调用")
        return json.dumps(self._check_intent(event.get_sender_id(), event.message_str or ""), ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_commands")
    async def list_commands(self, event: AstrMessageEvent, keyword: str = "", plugin_name: str = "", include_params: bool = False) -> str:
        """列出所有可用的插件指令。

        Args:
            keyword(string): 可选的关键词，用于过滤指令。
            plugin_name(string): 可选的插件名称，用于过滤特定插件的指令。
            include_params(boolean): 是否包含参数信息。
        """
        self._log("[LLM Tool] list_commands 被调用")
        self._cache_mgr.refresh_all()
        
        commands = []
        for name, info in self._cache_mgr.commands.items():
            if not self._is_visible(name):
                continue
            
            # 插件过滤
            if plugin_name:
                cmd_plugin = info.get("plugin", {})
                if not cmd_plugin or cmd_plugin.get("name", "").lower() != plugin_name.lower():
                    continue
            
            # 关键词过滤
            if keyword and keyword.lower() not in name.lower() and keyword.lower() not in info["description"].lower():
                continue
            
            entry = {"name": name, "description": info["description"]}
            
            if self._show_wake_prefix:
                entry["full_command"] = f"{self._cache_mgr.get_command_prefix()}{name}"
            
            aliases = [n for n in info["names"] if n != name and not n.startswith(" ")]
            if aliases:
                entry["aliases"] = aliases
            
            if include_params and info["params"]:
                entry["params"] = info["params"]
            
            if info["plugin"] and not self._cache_mgr._hide_plugin_info:
                entry["plugin"] = info["plugin"]["name"]
            
            entry["is_custom"] = info["is_custom"] if info["is_custom"] else {"executable": self._is_executable(name)}
            commands.append(entry)
        
        if not commands:
            msg = f"没有找到包含关键词「{keyword}」的指令。" if keyword else ("没有找到插件「{plugin_name}」的指令。" if plugin_name else "当前没有可用的指令。")
            return msg
        
        result = {"total": len(commands), "commands": commands}
        if self._show_wake_prefix:
            result["wake_prefix"] = self._cache_mgr.wake_prefix or "无"
            result["command_prefix"] = self._cache_mgr.get_command_prefix()
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_command_details")
    async def get_command_details(self, event: AstrMessageEvent, command_name: str) -> str:
        """获取特定指令的详细信息。

        Args:
            command_name(string): 指令名称。
        """
        self._log("[LLM Tool] get_command_details 被调用")
        self._cache_mgr._refresh_commands()
        
        cmd_info = None
        for name, info in self._cache_mgr.commands.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break
        
        if not cmd_info:
            similar = [n for n in self._cache_mgr.commands if command_name.lower() in n.lower()]
            if similar:
                return f"未找到指令「{command_name}」。您是否要找: {', '.join(similar)}？"
            return f"未找到指令「{command_name}」。"
        
        result = {
            "name": cmd_info["primary_name"],
            "full_command": f"{self._cache_mgr.get_command_prefix()}{cmd_info['primary_name']}",
            "description": cmd_info["description"],
            "params": cmd_info["params"] or None,
            "usage_examples": self._generate_examples(cmd_info),
        }
        
        if cmd_info["plugin"] and not self._cache_mgr._hide_plugin_info:
            result["plugin"] = cmd_info["plugin"]["name"]
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_plugins")
    async def list_plugins(self, event: AstrMessageEvent) -> str:
        """列出所有已加载的插件。"""
        self._log("[LLM Tool] list_plugins 被调用")
        self._cache_mgr._refresh_plugins()
        
        if not self._cache_mgr.plugins:
            return "当前没有已加载的插件。"
        
        plugins = [
            {
                "name": p["name"],
                "author": p.get("author", "未知"),
                "version": p.get("version", "未知"),
                "description": p.get("desc", ""),
                "activated": p.get("activated", False),
            }
            for p in self._cache_mgr.plugins.values()
        ]
        
        return json.dumps({"total": len(plugins), "plugins": plugins}, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_plugin_info")
    async def get_plugin_info_tool(self, event: AstrMessageEvent, plugin_name: str) -> str:
        """获取特定插件的详细信息。

        Args:
            plugin_name(string): 插件名称。
        """
        self._log("[LLM Tool] get_plugin_info 被调用")
        self._cache_mgr.refresh_all()
        
        plugin_info = None
        for name, info in self._cache_mgr.plugins.items():
            if name.lower() == plugin_name.lower():
                plugin_info = info
                break
        
        if not plugin_info:
            return f"未找到插件「{plugin_name}」。"
        
        # 获取插件指令
        cmds = [
            {"name": n, "description": i["description"]}
            for n, i in self._cache_mgr.commands.items()
            if i.get("plugin", {}).get("name", "").lower() == plugin_info["name"].lower()
        ][:10]
        
        result = {
            "name": plugin_info["name"],
            "author": plugin_info.get("author", "未知"),
            "version": plugin_info.get("version", "未知"),
            "description": plugin_info.get("desc", ""),
            "activated": plugin_info.get("activated", False),
            "commands": cmds,
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="execute_command")
    async def execute_command(self, event: AstrMessageEvent, command_name: str, args: str = "") -> str:
        """执行指定的插件指令。

        Args:
            command_name(string): 要执行的指令名称。
            args(string): 指令参数。
        """
        self._log("[LLM Tool] execute_command 被调用")
        
        # 意图检查
        intent = self._check_intent(event.get_sender_id(), event.message_str or "")
        if intent["should_skip_llm_execution"]:
            return f"跳过执行：{intent['reason']}"
        
        # 权限检查
        if not self._allow_execute:
            return "错误：LLM 指令执行功能已被禁用。"
        if self._execute_require_admin and not event.is_admin():
            return "错误：执行指令需要管理员权限。"
        
        self._cache_mgr._refresh_commands()
        
        # 查找指令
        cmd_info = None
        for name, info in self._cache_mgr.commands.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break
        
        if not cmd_info:
            return f"错误：未找到指令「{command_name}」。"
        if cmd_info.get("is_custom"):
            return "错误：自定义指令无法执行。"
        if not self._is_executable(command_name):
            return f"错误：指令「{command_name}」已被禁止执行。"
        
        try:
            handler_md = cmd_info["handler_md"]
            command_filter = cmd_info["command_filter"]
            
            # 解析参数
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
            
            # 处理异步生成器 - 收集所有结果
            results = []
            if inspect.isasyncgen(result):
                async for item in result:
                    results.append(item)
            elif inspect.iscoroutine(result):
                results.append(await result)
            else:
                results.append(result)
            
            # 合并结果
            output_parts = []
            for r in results:
                if r is None:
                    continue
                if isinstance(r, MessageEventResult):
                    text = r.get_plain_text()
                    if text:
                        output_parts.append(text)
                elif isinstance(r, str):
                    output_parts.append(r)
            
            if output_parts:
                return f"执行成功: {' '.join(output_parts)}"
            
            # 检查 event 结果
            event_result = event.get_result()
            if event_result:
                text = event_result.get_plain_text()
                return f"执行结果: {text}" if text else "执行成功。"
            
            return "指令已执行。"
        
        except Exception as e:
            logger.error(f"执行指令时发生错误: {e}", exc_info=True)
            return f"执行错误: {str(e)}"

    # ==================== 事件监听 ====================

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None):
        """监听 LLM Tool 调用事件"""
        if not self._enable_logging:
            return
        self._log(f"[LLM Tool Call] 工具 '{tool.name if tool else 'unknown'}' 被调用")

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None, tool_result: Any):
        """监听 LLM Tool 响应事件"""
        pass

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: Any):
        """监听 LLM 请求事件"""
        self._history_mgr.save(event.session_id, "user", self._get_raw_message(event), event.get_sender_name())

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any):
        """监听 LLM 响应事件"""
        text = ""
        if response:
            text = getattr(response, 'completion_text', None) or getattr(response, 'text', None)
            if not text:
                text = response if isinstance(response, str) else response.get('text', '') if isinstance(response, dict) else ''
        
        if text:
            self._history_mgr.save(event.session_id, "assistant", text, "机器人")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent, result: MessageEventResult):
        """监听消息发送事件"""
        if result and result.chain:
            text = ''.join(
                getattr(c, 'text', '') or (c.data.get('text', '') if isinstance(getattr(c, 'data', None), dict) else '')
                for c in result.chain
            )
            if text:
                self._history_mgr.save(event.session_id, "assistant", text, "机器人")

    # ==================== 用户指令 ====================

    @filter.command("lpb_config", alias={"插件桥配置"})
    async def show_config(self, event: AstrMessageEvent):
        """显示当前插件配置"""
        lines = [
            "⚙️ LLM Plugin Bridge 配置信息", "",
            "【执行配置】",
            f"  允许 LLM 执行: {'✅' if self._allow_execute else '❌'}",
            f"  执行需管理员权限: {'✅' if self._execute_require_admin else '❌'}",
            f"  禁止执行的指令: {', '.join(self._blocked_commands) or '无'}", "",
            "【缓存状态】",
            f"  指令数量: {len(self._cache_mgr.commands)}",
            f"  插件数量: {len(self._cache_mgr.plugins)}",
            f"  唤醒词: {self._cache_mgr.get_wake_prefix_display()}",
            f"  会话历史: {self._history_mgr.session_count} 个会话",
        ]
        event.set_result(event.plain_result("\n".join(lines)))

    @filter.command("lpb_refresh", alias={"刷新缓存"})
    async def refresh_cache_command(self, event: AstrMessageEvent):
        """手动刷新缓存"""
        self._cache_mgr.refresh_all()
        event.set_result(event.plain_result(f"缓存已刷新\n• 指令: {len(self._cache_mgr.commands)} 个\n• 插件: {len(self._cache_mgr.plugins)} 个"))

    @filter.command("lpb_list", alias={"列出指令"})
    async def list_commands_direct(self, event: AstrMessageEvent):
        """列出所有可用指令"""
        self._cache_mgr._refresh_commands()
        
        visible = {k: v for k, v in self._cache_mgr.commands.items() if self._is_visible(k)}
        if not visible:
            event.set_result(event.plain_result("当前没有可用的指令。"))
            return
        
        prefix = self._cache_mgr.get_command_prefix() if self._show_wake_prefix else ""
        lines = ["📋 可用指令列表：", ""]
        
        for name, info in sorted(visible.items()):
            desc = info["description"][:30] + ("..." if len(info["description"]) > 30 else "")
            lines.append(f"  • {prefix}{name} - {desc}")
        
        lines.append(f"\n共 {len(visible)} 个指令")
        event.set_result(event.plain_result("\n".join(lines)))

    @filter.command("lpb_info", alias={"指令详情"})
    async def command_info_direct(self, event: AstrMessageEvent, command_name: str = ""):
        """查看指令详情"""
        if not command_name:
            event.set_result(event.plain_result("请提供指令名称。"))
            return
        
        self._cache_mgr._refresh_commands()
        
        cmd_info = None
        for name, info in self._cache_mgr.commands.items():
            if name == command_name or command_name in info["names"]:
                cmd_info = info
                break
        
        if not cmd_info:
            event.set_result(event.plain_result(f"未找到指令「{command_name}」"))
            return
        
        lines = [
            f"📋 指令：{self._cache_mgr.get_command_prefix()}{cmd_info['primary_name']}",
            f"描述：{cmd_info['description']}",
        ]
        if cmd_info["params"]:
            lines.append("参数：")
            for pname, pinfo in cmd_info["params"].items():
                lines.append(f"  • {pname}: {pinfo.get('type', 'any')}")
        
        event.set_result(event.plain_result("\n".join(lines)))

    @filter.command("lpb_wake", alias={"查看唤醒词"})
    async def show_wake_info(self, event: AstrMessageEvent):
        """查看唤醒词配置"""
        self._cache_mgr._refresh_wake_prefix()
        event.set_result(event.plain_result(f"📢 机器人唤醒信息\n\n唤醒词：{self._cache_mgr.get_wake_prefix_display()}"))

    async def terminate(self) -> None:
        logger.info("LLM Plugin Bridge 已卸载")
