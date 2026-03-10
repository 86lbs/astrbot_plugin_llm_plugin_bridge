"""
LLM Plugin Bridge - LLM 插件桥

整合 LLM 与 AstrBot 插件系统的桥梁，让 LLM 能够：
- 发现和了解所有可用的插件和指令
- 获取唤醒词和触发方式信息
- 获取原始消息历史，判断用户意图
- 执行插件指令（可配置权限控制）
- 获取消息投递状态（包括是否被转换为图片）
"""

import inspect
import json
import shlex
import time
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot.core.star.star import star_map
from astrbot.core.agent.tool import FunctionTool


# ==================== 常量定义 ====================

class ConfigDefaults:
    """配置默认值常量"""
    MAX_HISTORY_PER_SESSION = 60
    SESSION_EXPIRE_SECONDS = 1800
    INTENT_TIME_WINDOW = 5.0
    MIN_INVOCATION_RECORDS = 10  # 最小记录数
    MAX_INVOCATION_RECORDS = 50
    CLEANUP_THRESHOLD = 100
    MAX_DELIVERY_STATUS_RECORDS = 20  # 最大投递状态记录数


# ==================== 缓存管理器 ====================

class CacheManager:
    """缓存管理器 - 负责指令和插件缓存的刷新与管理"""
    
    def __init__(self, context: Context, config: dict):
        self._context = context
        self._config = config
        
        self._commands_cache: dict[str, dict] = {}
        self._plugins_cache: dict[str, dict] = {}
        self._wake_prefix: str = ""
        
        self._hide_plugin_info = config.get("hide_plugin_info", False)
        self._custom_descriptions = config.get("custom_descriptions", {})
        self._custom_commands = config.get("custom_commands", [])
    
    def refresh_all(self) -> None:
        """刷新所有缓存"""
        self._refresh_wake_prefix()
        self._refresh_commands()
        self._refresh_plugins()
    
    def refresh_wake_prefix(self) -> None:
        """刷新唤醒词（公开方法）"""
        self._refresh_wake_prefix()
    
    def refresh_commands(self) -> None:
        """刷新指令缓存（公开方法）"""
        self._refresh_commands()
    
    def refresh_plugins(self) -> None:
        """刷新插件缓存（公开方法）"""
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
            
            if primary_name in self._commands_cache:
                logger.warning(f"指令名称冲突: '{primary_name}' 已存在，将被覆盖")
            
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
            
            # 检测冲突
            if cmd_name in self._commands_cache:
                logger.warning(f"自定义指令 '{cmd_name}' 与现有指令冲突，将覆盖")
            
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
    
    @property
    def hide_plugin_info(self) -> bool:
        """公开属性：是否隐藏插件信息"""
        return self._hide_plugin_info
    
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
        self._operation_count = 0
        
        self._max_history = config.get("max_history_per_session", ConfigDefaults.MAX_HISTORY_PER_SESSION)
        self._expire_seconds = config.get("session_expire_seconds", ConfigDefaults.SESSION_EXPIRE_SECONDS)
        self._cleanup_threshold = config.get("cleanup_threshold", ConfigDefaults.CLEANUP_THRESHOLD)
    
    def save(self, session_id: str, role: str, content: str, sender_name: str = "", extra: dict | None = None) -> None:
        """保存消息到历史记录（带去重和额外信息）
        
        Args:
            session_id: 会话ID
            role: 角色 (user/assistant)
            content: 消息内容
            sender_name: 发送者名称
            extra: 额外信息（如 converted_to_image）
        """
        if session_id not in self._history:
            self._history[session_id] = []
        
        records = self._history[session_id]
        if records:
            last = records[-1]
            if last["role"] == role and last["content"] == content:
                return
        
        record = {
            "role": role,
            "content": content,
            "sender_name": sender_name,
            "timestamp": time.time(),
        }
        
        # 添加额外信息
        if extra:
            record["extra"] = extra
        
        records.append(record)
        
        if len(records) > self._max_history:
            self._history[session_id] = records[-self._max_history:]
        
        self._operation_count += 1
        if self._operation_count >= self._cleanup_threshold:
            self._cleanup_expired()
            self._operation_count = 0
    
    def get(self, session_id: str, limit: int = 10) -> list[dict]:
        """获取消息历史（带惰性清理）"""
        self._lazy_cleanup(session_id)
        
        if session_id not in self._history:
            return []
        return self._history[session_id][-limit:]
    
    def get_last_record(self, session_id: str) -> dict | None:
        """获取最后一条记录"""
        self._lazy_cleanup(session_id)
        
        if session_id not in self._history or not self._history[session_id]:
            return None
        return self._history[session_id][-1]
    
    def _lazy_cleanup(self, session_id: str) -> None:
        """惰性清理：检查特定会话是否过期"""
        if session_id in self._history:
            records = self._history[session_id]
            if records:
                if time.time() - records[-1]["timestamp"] > self._expire_seconds:
                    del self._history[session_id]
    
    def _cleanup_expired(self) -> None:
        """批量清理过期会话"""
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


# ==================== 消息投递状态追踪器 ====================

class MessageDeliveryTracker:
    """消息投递状态追踪器 - 追踪消息是否被转换为图片等状态"""
    
    def __init__(self, config: dict):
        self._delivery_status: dict[str, list[dict]] = {}
        self._max_records = config.get(
            "max_delivery_status_records", 
            ConfigDefaults.MAX_DELIVERY_STATUS_RECORDS
        )
    
    def record(self, session_id: str, original_text: str, converted_to_image: bool, 
               text_length: int = 0, reason: str = "") -> None:
        """记录消息投递状态
        
        Args:
            session_id: 会话ID
            original_text: 原始文本内容（可能被截断）
            converted_to_image: 是否被转换为图片
            text_length: 原始文本长度
            reason: 转换原因
        """
        if session_id not in self._delivery_status:
            self._delivery_status[session_id] = []
        
        record = {
            "original_text": original_text[:500] if original_text else "",  # 截断保存
            "converted_to_image": converted_to_image,
            "text_length": text_length or len(original_text),
            "reason": reason,
            "timestamp": time.time(),
        }
        
        self._delivery_status[session_id].append(record)
        
        # 限制记录数量
        if len(self._delivery_status[session_id]) > self._max_records:
            self._delivery_status[session_id] = self._delivery_status[session_id][-self._max_records:]
    
    def get_last_status(self, session_id: str) -> dict | None:
        """获取最后一条消息的投递状态"""
        if session_id not in self._delivery_status or not self._delivery_status[session_id]:
            return None
        return self._delivery_status[session_id][-1]
    
    def get_recent_status(self, session_id: str, limit: int = 5) -> list[dict]:
        """获取最近的消息投递状态"""
        if session_id not in self._delivery_status:
            return []
        return self._delivery_status[session_id][-limit:]
    
    def clear_session(self, session_id: str) -> None:
        """清除指定会话的记录"""
        if session_id in self._delivery_status:
            del self._delivery_status[session_id]


# ==================== 主插件类 ====================

class Main(star.Star):
    """LLM 插件桥 - 让 LLM 能够发现、了解和执行插件指令"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._config = config or {}
        
        self._cache_mgr = CacheManager(context, self._config)
        self._history_mgr = MessageHistoryManager(self._config)
        self._delivery_tracker = MessageDeliveryTracker(self._config)
        
        self._recent_invocations: list[dict] = []
        
        self._allow_execute = self._config.get("allow_execute", False)
        self._execute_require_admin = self._config.get("execute_require_admin", False)
        self._blocked_commands = set(self._config.get("blocked_commands", []))
        
        self._list_mode = self._config.get("list_mode", "all")
        self._command_whitelist = set(self._config.get("command_whitelist", []))
        self._command_blacklist = set(self._config.get("command_blacklist", []))
        
        self._show_wake_prefix = self._config.get("show_wake_prefix_in_list", True)
        
        self._enable_logging = self._config.get("enable_tool_logging", True)
        self._log_level = self._config.get("log_level", "info")
        
        self._intent_time_window = self._config.get("intent_time_window", ConfigDefaults.INTENT_TIME_WINDOW)
        
        # 最大记录数，确保最小值
        self._max_invocation_records = max(
            self._config.get("max_invocation_records", ConfigDefaults.MAX_INVOCATION_RECORDS),
            ConfigDefaults.MIN_INVOCATION_RECORDS
        )
        
        self._last_cache_refresh = 0
        self._cache_refresh_interval = 5.0

    async def initialize(self) -> None:
        """插件初始化"""
        self._cache_mgr.refresh_all()
        logger.info(f"LLM Plugin Bridge 初始化完成")
        logger.info(f"  - 已缓存 {len(self._cache_mgr.commands)} 个指令")
        logger.info(f"  - 已缓存 {len(self._cache_mgr.plugins)} 个插件")
        logger.info(f"  - 唤醒词: {self._cache_mgr.get_wake_prefix_display()}")
        logger.info(f"  - 列表模式: {self._list_mode}")
        logger.info(f"  - LLM 执行功能: {'已启用' if self._allow_execute else '已禁用'}")
        logger.info(f"  - 消息投递状态追踪: 已启用")

    def _log(self, message: str) -> None:
        """记录日志"""
        if self._enable_logging and self._log_level == "debug":
            logger.debug(message)
        elif self._enable_logging:
            logger.info(message)

    def _throttled_refresh(self) -> None:
        """节流刷新缓存"""
        current = time.time()
        if current - self._last_cache_refresh >= self._cache_refresh_interval:
            self._cache_mgr.refresh_all()
            self._last_cache_refresh = current

    def _is_visible(self, cmd_name: str) -> bool:
        """检查指令是否可见"""
        if self._list_mode == "whitelist":
            return cmd_name in self._command_whitelist
        elif self._list_mode == "blacklist":
            return cmd_name not in self._command_blacklist
        return True

    def _is_executable(self, primary_name: str) -> bool:
        """检查指令是否可执行"""
        return primary_name not in self._blocked_commands

    def _get_raw_message(self, event: AstrMessageEvent) -> str:
        """获取原始消息"""
        msg_obj = getattr(event, 'message_obj', None)
        if not msg_obj:
            return event.message_str or ""
        
        raw = getattr(msg_obj, 'raw_message', None)
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return raw.get('raw_message') or raw.get('message') or str(raw)
        
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
        
        # 确保清理逻辑正确（max_records 已确保 >= MIN_INVOCATION_RECORDS）
        if len(self._recent_invocations) > self._max_invocation_records:
            self._recent_invocations = self._recent_invocations[-self._max_invocation_records // 2:]

    def _check_intent(self, sender_id: str, msg: str) -> dict:
        """检查用户意图"""
        current = time.time()
        
        for inv in reversed(self._recent_invocations):
            if current - inv["timestamp"] > self._intent_time_window:
                break
            if inv["sender_id"] == sender_id:
                cmd = inv["command"].lower()
                msg_lower = msg.lower()
                if cmd in msg_lower or inv["message_str"].lower() in msg_lower:
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
        """【重要】获取机器人的唤醒信息、消息的原始内容和消息投递状态。
        
        此工具还会返回最近消息的投递状态，包括消息是否因为过长被转换为图片发送。
        """
        self._log("[LLM Tool] get_wake_info 被调用")
        self._cache_mgr.refresh_wake_prefix()
        
        session_id = event.session_id
        raw_msg = self._get_raw_message(event)
        llm_msg = event.message_str or ""
        
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
        
        # 获取消息投递状态
        last_delivery = self._delivery_tracker.get_last_status(session_id)
        if last_delivery:
            result["last_message_delivery"] = {
                "converted_to_image": last_delivery.get("converted_to_image", False),
                "text_length": last_delivery.get("text_length", 0),
                "reason": last_delivery.get("reason", ""),
            }
            if last_delivery.get("converted_to_image"):
                result["last_message_delivery"]["notice"] = (
                    "您上一条发送的消息因为过长已被转换为图片发送给用户。"
                    "用户看到的是图片而非文本。"
                )
        
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

    @filter.llm_tool(name="get_message_delivery_status")
    async def get_message_delivery_status(self, event: AstrMessageEvent, count: int = 3) -> str:
        """获取最近消息的投递状态，包括是否被转换为图片。
        
        当您怀疑自己发送的消息可能因为过长被转换为图片时，可以调用此工具确认。
        
        Args:
            count(integer): 要获取的最近消息数量，默认为3，最大为10。
        """
        self._log("[LLM Tool] get_message_delivery_status 被调用")
        
        session_id = event.session_id
        count = min(max(count, 1), 10)  # 限制在 1-10 之间
        
        recent_status = self._delivery_tracker.get_recent_status(session_id, count)
        
        if not recent_status:
            return json.dumps({
                "has_records": False,
                "message": "当前会话没有消息投递状态记录。"
            }, ensure_ascii=False, indent=2)
        
        records = []
        for i, status in enumerate(reversed(recent_status)):  # 按时间正序
            record = {
                "index": len(recent_status) - i,
                "converted_to_image": status.get("converted_to_image", False),
                "text_length": status.get("text_length", 0),
                "timestamp": status.get("timestamp"),
            }
            
            if status.get("converted_to_image"):
                record["notice"] = "此消息已被转换为图片发送"
                record["original_text_preview"] = status.get("original_text", "")[:200]
            
            records.append(record)
        
        # 统计转换次数
        converted_count = sum(1 for s in recent_status if s.get("converted_to_image"))
        
        result = {
            "has_records": True,
            "total_records": len(records),
            "converted_to_image_count": converted_count,
            "records": records,
        }
        
        if converted_count > 0:
            result["summary"] = f"在最近 {len(records)} 条消息中，有 {converted_count} 条因过长被转换为图片发送。"
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_commands")
    async def list_commands(self, event: AstrMessageEvent, keyword: str = "", plugin_name: str = "", include_params: bool = False) -> str:
        """列出所有可用的插件指令。

        Args:
            keyword(string): 可选的关键词，用于过滤指令。
            plugin_name(string): 可选的插件名称，用于过滤特定插件的指令。
            include_params(boolean): 是否包含参数信息。
        """
        self._log("[LLM Tool] list_commands 被调用")
        self._throttled_refresh()
        
        commands = []
        for name, info in self._cache_mgr.commands.items():
            if not self._is_visible(name):
                continue
            
            if plugin_name:
                cmd_plugin = info.get("plugin", {})
                if not cmd_plugin or cmd_plugin.get("name", "").lower() != plugin_name.lower():
                    continue
            
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
            
            if info["plugin"] and not self._cache_mgr.hide_plugin_info:
                entry["plugin"] = info["plugin"]["name"]
            
            entry["is_custom"] = info["is_custom"]
            if not info["is_custom"]:
                entry["executable"] = self._is_executable(info["primary_name"])
            
            commands.append(entry)
        
        if not commands:
            if keyword:
                return f"没有找到包含关键词「{keyword}」的指令。"
            if plugin_name:
                return f"没有找到插件「{plugin_name}」的指令。"
            return "当前没有可用的指令。"
        
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
        self._cache_mgr.refresh_commands()
        
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
        
        if cmd_info["plugin"] and not self._cache_mgr.hide_plugin_info:
            result["plugin"] = cmd_info["plugin"]["name"]
        
        return json.dumps(result, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="list_plugins")
    async def list_plugins(self, event: AstrMessageEvent) -> str:
        """列出所有已加载的插件。"""
        self._log("[LLM Tool] list_plugins 被调用")
        self._cache_mgr.refresh_plugins()
        
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
        self._throttled_refresh()
        
        plugin_info = None
        for name, info in self._cache_mgr.plugins.items():
            if name.lower() == plugin_name.lower():
                plugin_info = info
                break
        
        if not plugin_info:
            return f"未找到插件「{plugin_name}」。"
        
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
        
        sender_id = event.get_sender_id()
        msg = event.message_str or ""
        
        # 【修复】先做意图检查，再记录调用
        intent = self._check_intent(sender_id, msg)
        if intent["should_skip_llm_execution"]:
            return f"跳过执行：{intent['reason']}"
        
        # 权限检查
        if not self._allow_execute:
            return "错误：LLM 指令执行功能已被禁用。请在配置中启用。"
        if self._execute_require_admin and not event.is_admin():
            return "错误：执行指令需要管理员权限。"
        
        self._cache_mgr.refresh_commands()
        
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
        
        primary_name = cmd_info["primary_name"]
        if not self._is_executable(primary_name):
            return f"错误：指令「{primary_name}」已被禁止执行。"
        
        try:
            handler_md = cmd_info["handler_md"]
            command_filter = cmd_info["command_filter"]
            
            try:
                args_list = shlex.split(args) if args else []
            except ValueError as e:
                return f"参数解析错误: {str(e)}"
            
            try:
                parsed_params = command_filter.validate_and_convert_params(args_list, command_filter.handler_params)
            except ValueError as e:
                return f"参数错误: {str(e)}"
            
            if not star_map.get(handler_md.handler_module_path):
                return "错误：无法获取插件实例。"
            
            handler = handler_md.handler
            event.set_extra("parsed_params", parsed_params)
            
            result = handler(event, **parsed_params)
            
            results = []
            if inspect.isasyncgen(result):
                async for item in result:
                    results.append(item)
            elif inspect.iscoroutine(result):
                results.append(await result)
            else:
                results.append(result)
            
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
            
            # 【修复】执行成功后再记录调用
            self._add_invocation(command_name, args, sender_id, msg)
            
            if output_parts:
                return f"执行成功: {' '.join(output_parts)}"
            
            event_result = event.get_result()
            if event_result:
                text = event_result.get_plain_text()
                return f"执行结果: {text}" if text else "执行成功。"
            
            return "指令已执行。"
        
        except Exception as e:
            logger.error(f"执行指令时发生错误: {e}", exc_info=True)
            return "执行失败：指令执行过程中发生错误，请查看日志获取详情。"

    # ==================== 事件监听 ====================

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None):
        """监听 LLM Tool 调用事件"""
        if self._enable_logging:
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
            if hasattr(response, 'completion_text'):
                text = response.completion_text
            elif hasattr(response, 'text'):
                text = response.text
            elif isinstance(response, str):
                text = response
            elif isinstance(response, dict):
                text = response.get('text', '') or response.get('content', '')
        
        if text:
            self._history_mgr.save(event.session_id, "assistant", text, "机器人")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent, result: MessageEventResult):
        """监听消息发送事件，检测消息是否被转换为图片"""
        if not result:
            return
        
        session_id = event.session_id
        text = ""
        
        # 提取文本内容
        if result.chain:
            text = ''.join(
                getattr(c, 'text', '') or (c.data.get('text', '') if isinstance(getattr(c, 'data', None), dict) else '')
                for c in result.chain
            )
        
        # 检测是否被转换为图片
        converted_to_image = False
        reason = ""
        
        # 方法1：检查 use_t2i_ 属性
        if hasattr(result, 'use_t2i_') and result.use_t2i_ is True:
            converted_to_image = True
            reason = "文本转图片功能已启用 (use_t2i_=True)"
        
        # 方法2：检查 chain 中是否只有 Image 组件没有 Plain 组件
        if not converted_to_image and result.chain:
            has_plain = any(hasattr(c, 'text') for c in result.chain)
            has_image = any(
                c.__class__.__name__ == 'Image' or 
                hasattr(c, 'url') or 
                hasattr(c, 'path')
                for c in result.chain
            )
            
            # 如果有图片但没有文本，可能是被转换了
            if has_image and not has_plain and text:
                converted_to_image = True
                reason = "消息链中只有图片组件，原始文本可能被转换"
        
        # 记录投递状态
        if text or converted_to_image:
            self._delivery_tracker.record(
                session_id=session_id,
                original_text=text,
                converted_to_image=converted_to_image,
                text_length=len(text),
                reason=reason
            )
            
            # 如果被转换为图片，在历史记录中添加标记
            if converted_to_image:
                self._history_mgr.save(
                    session_id, 
                    "assistant", 
                    text, 
                    "机器人",
                    extra={"converted_to_image": True, "reason": reason}
                )
                self._log(f"[MessageDelivery] 会话 {session_id} 的消息被转换为图片发送，原文长度: {len(text)}")
            else:
                self._history_mgr.save(session_id, "assistant", text, "机器人")

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
            f"  会话历史: {self._history_mgr.session_count} 个会话", "",
            "【消息投递追踪】",
            f"  状态: ✅ 已启用",
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
        self._cache_mgr.refresh_commands()
        
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
        
        self._cache_mgr.refresh_commands()
        
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
        self._cache_mgr.refresh_wake_prefix()
        event.set_result(event.plain_result(f"📢 机器人唤醒信息\n\n唤醒词：{self._cache_mgr.get_wake_prefix_display()}"))

    @filter.command("lpb_delivery", alias={"投递状态"})
    async def show_delivery_status(self, event: AstrMessageEvent, count: int = 5):
        """查看最近消息的投递状态"""
        session_id = event.session_id
        count = min(max(count, 1), 10)
        
        recent_status = self._delivery_tracker.get_recent_status(session_id, count)
        
        if not recent_status:
            event.set_result(event.plain_result("当前会话没有消息投递状态记录。"))
            return
        
        lines = ["📊 最近消息投递状态：", ""]
        
        for i, status in enumerate(reversed(recent_status)):
            idx = len(recent_status) - i
            converted = status.get("converted_to_image", False)
            text_len = status.get("text_length", 0)
            
            status_icon = "🖼️" if converted else "📝"
            lines.append(f"  {idx}. {status_icon} 长度: {text_len}")
            if converted:
                lines.append(f"      ⚠️ 已转换为图片发送")
        
        converted_count = sum(1 for s in recent_status if s.get("converted_to_image"))
        lines.append(f"\n共 {len(recent_status)} 条记录，{converted_count} 条被转换为图片")
        
        event.set_result(event.plain_result("\n".join(lines)))

    async def terminate(self) -> None:
        logger.info("LLM Plugin Bridge 已卸载")
