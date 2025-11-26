"""
IP 管理器模块
功能：
1. 记录 IP 请求统计
2. IP 黑名单/白名单管理
3. IP 限速功能
4. IP 地理位置查询
"""

import asyncio
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
import aiofiles
import toml
import os

from log import log

# 东八区时区
UTC_PLUS_8 = timezone(timedelta(hours=8))


class IPManager:
    """IP 管理器"""

    def __init__(self):
        self._ip_data_path = None
        self._ip_cache: Dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()
        self._cache_dirty = False
        self._initialized = False

    async def initialize(self):
        """初始化 IP 管理器"""
        if self._initialized:
            return

        try:
            # 获取 IP 数据文件路径
            from config import get_credentials_dir
            credentials_dir = await get_credentials_dir()
            self._ip_data_path = os.path.join(credentials_dir, "ip_stats.toml")

            # 加载 IP 数据
            await self._load_ip_data()

            # 启动定期保存任务
            asyncio.create_task(self._periodic_save())

            # 启动定期解封检查任务
            asyncio.create_task(self._periodic_unban_check())

            self._initialized = True
            log.info("IP Manager initialized successfully")

        except Exception as e:
            log.error(f"Failed to initialize IP Manager: {e}")
            raise

    async def _load_ip_data(self):
        """从文件加载 IP 数据"""
        try:
            if os.path.exists(self._ip_data_path):
                async with aiofiles.open(self._ip_data_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                self._ip_cache = toml.loads(content)
                log.info(f"Loaded IP data from {self._ip_data_path}")
            else:
                self._ip_cache = {"ips": {}}
                log.info("No existing IP data found, starting fresh")
        except Exception as e:
            log.error(f"Error loading IP data: {e}")
            self._ip_cache = {"ips": {}}

    async def _save_ip_data(self):
        """保存 IP 数据到文件"""
        try:
            async with self._cache_lock:
                if not self._cache_dirty:
                    return

                toml_content = toml.dumps(self._ip_cache)
                async with aiofiles.open(self._ip_data_path, "w", encoding="utf-8") as f:
                    await f.write(toml_content)

                self._cache_dirty = False
                log.debug("IP data saved to disk")
        except Exception as e:
            log.error(f"Error saving IP data: {e}")

    async def _periodic_save(self):
        """定期保存 IP 数据"""
        while True:
            try:
                await asyncio.sleep(60)  # 每60秒保存一次
                await self._save_ip_data()
            except Exception as e:
                log.error(f"Error in periodic save: {e}")

    async def _periodic_unban_check(self):
        """定期检查并自动解封24小时后的IP"""
        while True:
            try:
                await asyncio.sleep(1800)  # 每30分钟检查一次
                await self._auto_unban_expired_ips()
            except Exception as e:
                log.error(f"Error in periodic unban check: {e}")

    async def _auto_unban_expired_ips(self):
        """自动解封超过24小时的被封禁IP"""
        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                return

            current_time = time.time()
            unban_count = 0

            for ip, ip_data in self._ip_cache["ips"].items():
                if ip_data.get("status") == "banned":
                    banned_time = ip_data.get("banned_time", 0)
                    # 24小时 = 86400秒
                    if banned_time > 0 and (current_time - banned_time) >= 86400:
                        ip_data["status"] = "active"
                        ip_data["auto_unbanned_time"] = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S")
                        unban_count += 1
                        log.info(f"Auto-unbanned IP {ip} after 24 hours")

            if unban_count > 0:
                self._cache_dirty = True
                log.info(f"Auto-unbanned {unban_count} IPs")

    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("IPManager not initialized. Call initialize() first.")

    async def check_ip_allowed(self, ip: str) -> bool:
        """
        仅检查 IP 是否被允许访问（不记录请求）

        Args:
            ip: IP 地址

        Returns:
            是否允许请求（False表示IP被封禁或限速中）
        """
        self._ensure_initialized()

        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                return True  # 如果没有 IP 数据，允许访问

            # 检查 IP 是否被封禁
            if ip in self._ip_cache["ips"]:
                ip_data = self._ip_cache["ips"][ip]

                if ip_data.get("status") == "banned":
                    # 检查是否应该自动解封（24小时后）
                    banned_time = ip_data.get("banned_time", 0)
                    if banned_time > 0 and (time.time() - banned_time) >= 86400:
                        # 自动解封
                        ip_data["status"] = "active"
                        ip_data["auto_unbanned_time"] = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S")
                        self._cache_dirty = True
                        log.info(f"Auto-unbanned IP {ip} after 24 hours")
                    else:
                        log.warning(f"Blocked request from banned IP: {ip}")
                        return False

                # 检查限速
                if ip_data.get("status") == "rate_limited":
                    last_request = ip_data.get("last_request_time", 0)
                    rate_limit = ip_data.get("rate_limit_seconds", 60)
                    if time.time() - last_request < rate_limit:
                        log.warning(f"Rate limited IP: {ip}")
                        return False

            return True

    async def record_request(
        self,
        ip: str,
        endpoint: str = "/v1/chat/completions",
        user_agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> bool:
        """
        记录 IP 请求

        Args:
            ip: IP 地址
            endpoint: 请求端点
            user_agent: 用户代理
            model: 使用的模型

        Returns:
            是否允许请求（False表示IP被封禁）
        """
        self._ensure_initialized()

        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                self._ip_cache["ips"] = {}

            # 检查 IP 是否被封禁
            if ip in self._ip_cache["ips"]:
                ip_data = self._ip_cache["ips"][ip]
                if ip_data.get("status") == "banned":
                    log.warning(f"Blocked request from banned IP: {ip}")
                    return False

                # 检查限速
                if ip_data.get("status") == "rate_limited":
                    last_request = ip_data.get("last_request_time", 0)
                    rate_limit = ip_data.get("rate_limit_seconds", 60)
                    if time.time() - last_request < rate_limit:
                        log.warning(f"Rate limited IP: {ip}")
                        return False

            # 初始化或更新 IP 数据
            if ip not in self._ip_cache["ips"]:
                self._ip_cache["ips"][ip] = {
                    "first_seen": datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S"),
                    "total_requests": 0,
                    "status": "active",  # active, banned, rate_limited
                    "location": await self._get_ip_location(ip),
                    "user_agents": [],
                    "models_used": {},
                    "endpoints": {},
                }

            ip_data = self._ip_cache["ips"][ip]

            # 更新统计
            ip_data["total_requests"] = ip_data.get("total_requests", 0) + 1
            ip_data["last_request_time"] = time.time()
            ip_data["last_seen"] = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S")

            # 记录 User-Agent
            if user_agent and user_agent not in ip_data.get("user_agents", []):
                if "user_agents" not in ip_data:
                    ip_data["user_agents"] = []
                ip_data["user_agents"].append(user_agent)
                # 只保留最近10个不同的 User-Agent
                ip_data["user_agents"] = ip_data["user_agents"][-10:]

            # 记录模型使用
            if model:
                if "models_used" not in ip_data:
                    ip_data["models_used"] = {}
                ip_data["models_used"][model] = ip_data["models_used"].get(model, 0) + 1

            # 记录端点使用
            if "endpoints" not in ip_data:
                ip_data["endpoints"] = {}
            ip_data["endpoints"][endpoint] = ip_data["endpoints"].get(endpoint, 0) + 1

            self._cache_dirty = True
            return True

    async def _get_ip_location(self, ip: str) -> str:
        """
        获取 IP 地理位置

        使用免费 API 查询（无需注册）：
        1. IP-API.com (主)
        2. IPWho.org (备)
        3. 太平洋网络 (国内备用)
        """
        # 本地 IP 检测
        if ip.startswith("127.") or ip == "::1" or ip.startswith("192.168.") or ip.startswith("10."):
            return "本地网络 (Local)"

        # 尝试使用免费 API 查询
        try:
            import httpx

            # 方案1: IP-API.com (支持中文，45次/分钟)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(
                        f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp"
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("status") == "success":
                            country = data.get("country", "")
                            region = data.get("regionName", "")
                            city = data.get("city", "")
                            isp = data.get("isp", "")

                            # 组合地址
                            parts = [p for p in [country, region, city] if p]
                            location = " ".join(parts)

                            if isp:
                                location += f" ({isp})"

                            return location if location else "未知位置"
            except Exception as e:
                log.debug(f"IP-API.com query failed: {e}")

            # 方案2: IPWho.org (备用)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"https://ipwho.is/{ip}")
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("success"):
                            country = data.get("country", "")
                            region = data.get("region", "")
                            city = data.get("city", "")
                            connection = data.get("connection", {})
                            isp = connection.get("isp", "")

                            parts = [p for p in [country, region, city] if p]
                            location = " ".join(parts)

                            if isp:
                                location += f" ({isp})"

                            return location if location else "未知位置"
            except Exception as e:
                log.debug(f"IPWho.org query failed: {e}")

            # 方案3: 太平洋网络 (国内备用，仅查询国内 IP 效果好)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(
                        f"http://whois.pconline.com.cn/ipJson.jsp?ip={ip}&json=true"
                    )
                    if response.status_code == 200:
                        # 注意：这个 API 返回的是 GB2312 编码
                        text = response.text
                        # 简单解析 JSON
                        import json

                        data = json.loads(text)
                        pro = data.get("pro", "")
                        city = data.get("city", "")
                        addr = data.get("addr", "")

                        parts = [p for p in [pro, city, addr] if p and p != "XX"]
                        location = " ".join(parts)

                        return location if location else "未知位置"
            except Exception as e:
                log.debug(f"Pconline query failed: {e}")

        except Exception as e:
            log.error(f"Failed to get IP location for {ip}: {e}")

        return "未知位置"

    async def get_ip_stats(self, ip: Optional[str] = None) -> Dict[str, Any]:
        """
        获取 IP 统计信息

        Args:
            ip: 指定 IP，如果为 None 则返回所有 IP

        Returns:
            IP 统计数据
        """
        self._ensure_initialized()

        async with self._cache_lock:
            if ip:
                return self._ip_cache.get("ips", {}).get(ip, {})
            else:
                # 返回所有 IP，按请求次数排序
                all_ips = self._ip_cache.get("ips", {})
                sorted_ips = dict(
                    sorted(
                        all_ips.items(),
                        key=lambda x: x[1].get("total_requests", 0),
                        reverse=True,
                    )
                )
                return sorted_ips

    async def set_ip_status(
        self, ip: str, status: str, rate_limit_seconds: Optional[int] = None
    ) -> bool:
        """
        设置 IP 状态

        Args:
            ip: IP 地址
            status: 状态 (active, banned, rate_limited)
            rate_limit_seconds: 限速秒数（仅用于 rate_limited 状态）

        Returns:
            是否成功
        """
        self._ensure_initialized()

        if status not in ["active", "banned", "rate_limited"]:
            log.error(f"Invalid IP status: {status}")
            return False

        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                self._ip_cache["ips"] = {}

            if ip not in self._ip_cache["ips"]:
                # 如果 IP 不存在，创建新记录
                self._ip_cache["ips"][ip] = {
                    "first_seen": datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S"),
                    "total_requests": 0,
                    "location": await self._get_ip_location(ip),
                }

            self._ip_cache["ips"][ip]["status"] = status

            # 如果是封禁操作，记录封禁时间
            if status == "banned":
                self._ip_cache["ips"][ip]["banned_time"] = time.time()
                self._ip_cache["ips"][ip]["banned_at"] = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S")

            if status == "rate_limited" and rate_limit_seconds:
                self._ip_cache["ips"][ip]["rate_limit_seconds"] = rate_limit_seconds

            self._cache_dirty = True
            log.info(f"Set IP {ip} status to {status}")
            return True

    async def get_all_ips_summary(self) -> Dict[str, Any]:
        """获取所有 IP 的摘要统计"""
        self._ensure_initialized()

        async with self._cache_lock:
            all_ips = self._ip_cache.get("ips", {})

            total_ips = len(all_ips)
            active_ips = sum(1 for ip_data in all_ips.values() if ip_data.get("status") == "active")
            banned_ips = sum(1 for ip_data in all_ips.values() if ip_data.get("status") == "banned")
            rate_limited_ips = sum(
                1 for ip_data in all_ips.values() if ip_data.get("status") == "rate_limited"
            )
            total_requests = sum(ip_data.get("total_requests", 0) for ip_data in all_ips.values())

            return {
                "total_ips": total_ips,
                "active_ips": active_ips,
                "banned_ips": banned_ips,
                "rate_limited_ips": rate_limited_ips,
                "total_requests": total_requests,
            }


# 全局实例
_ip_manager_instance: Optional[IPManager] = None


async def get_ip_manager() -> IPManager:
    """获取全局 IP 管理器实例"""
    global _ip_manager_instance

    if _ip_manager_instance is None:
        _ip_manager_instance = IPManager()
        await _ip_manager_instance.initialize()

    return _ip_manager_instance
