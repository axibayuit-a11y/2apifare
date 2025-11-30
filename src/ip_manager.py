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
        # 封禁操作记录文件路径（不再使用内存存储）
        self._ban_operations_file = None
        # 限速恢复时间戳（每3小时恢复一次）
        self._last_rate_limit_recovery_time = 0
        # 默认积分值（从配置文件加载）
        self._default_credit_score = 0.5  # 初始默认值，会在initialize中从配置文件读取

    async def initialize(self):
        """初始化 IP 管理器"""
        if self._initialized:
            return

        try:
            # 获取 IP 数据文件路径
            from config import get_credentials_dir
            credentials_dir = await get_credentials_dir()
            self._ip_data_path = os.path.join(credentials_dir, "ip_stats.toml")
            self._ban_operations_file = os.path.join(credentials_dir, "ban_operations.toml")

            # 加载 IP 数据
            await self._load_ip_data()

            # 加载默认积分配置
            await self._load_default_credit_score(credentials_dir)

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

    async def _load_default_credit_score(self, credentials_dir: str):
        """从配置文件加载默认积分值"""
        try:
            credits_toml_file = os.path.join(credentials_dir, "model_credits.toml")
            if os.path.exists(credits_toml_file):
                async with aiofiles.open(credits_toml_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                credits_data = toml.loads(content)
                self._default_credit_score = credits_data.get("default", {}).get("score", 0.5)
                log.info(f"Loaded default credit score: {self._default_credit_score}")
            else:
                log.warning(f"Model credits file not found, using default: {self._default_credit_score}")
        except Exception as e:
            log.error(f"Error loading default credit score: {e}, using default: {self._default_credit_score}")

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
        """定期检查并自动解封24小时后的IP + 清理3天未访问的IP + 每3小时恢复限速"""
        while True:
            try:
                await asyncio.sleep(1800)  # 每30分钟检查一次（减少解封延迟）
                await self._auto_unban_expired_ips()
                await self._cleanup_old_ips()  # 清理旧IP记录

                # 检查是否需要恢复限速（每3小时执行一次）
                current_time = time.time()
                if current_time - self._last_rate_limit_recovery_time >= 10800:  # 3小时 = 10800秒
                    await self._auto_recover_rate_limits()
                    self._last_rate_limit_recovery_time = current_time
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

    async def _cleanup_old_ips(self):
        """
        根据请求次数动态清理未访问的IP记录（被封禁的IP除外）

        清理策略（请求次数越少清理越快）：
        - 低频用户（<50次）：3天未访问就清理（试用用户，快速清理）
        - 中频用户（50-299次）：5天未访问才清理
        - 高频用户（≥300次）：7天未访问才清理（老用户，保留更久）
        """
        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                return

            current_time = time.time()

            # 清理时间阈值（秒）
            three_days = 3 * 86400   # 259200秒
            five_days = 5 * 86400    # 432000秒
            seven_days = 7 * 86400   # 604800秒

            ips_to_remove = []
            cleanup_stats = {"high_freq": 0, "mid_freq": 0, "low_freq": 0}

            for ip, ip_data in list(self._ip_cache["ips"].items()):
                last_request = ip_data.get("last_request_time", 0)
                status = ip_data.get("status", "active")
                total_requests = ip_data.get("total_requests", 0)

                # 被封禁的IP永久保留
                if status == "banned":
                    continue

                # 如果没有last_request_time，跳过
                if last_request == 0:
                    continue

                inactive_time = current_time - last_request

                # 根据请求次数动态判断清理时间（请求次数越少清理越快）
                should_remove = False

                if total_requests >= 300:
                    # 高频用户（老用户）：7天未访问才清理
                    if inactive_time >= seven_days:
                        should_remove = True
                        cleanup_stats["high_freq"] += 1
                elif total_requests >= 50:
                    # 中频用户：5天未访问才清理
                    if inactive_time >= five_days:
                        should_remove = True
                        cleanup_stats["mid_freq"] += 1
                else:
                    # 低频用户（试用/一次性用户）：3天未访问就清理
                    if inactive_time >= three_days:
                        should_remove = True
                        cleanup_stats["low_freq"] += 1

                if should_remove:
                    ips_to_remove.append(ip)

            # 执行清理
            for ip in ips_to_remove:
                del self._ip_cache["ips"][ip]

            if ips_to_remove:
                self._cache_dirty = True
                log.info(
                    f"Cleaned up {len(ips_to_remove)} old IP records - "
                    f"High-freq(≥300,7d): {cleanup_stats['high_freq']}, "
                    f"Mid-freq(50-299,5d): {cleanup_stats['mid_freq']}, "
                    f"Low-freq(<50,3d): {cleanup_stats['low_freq']}"
                )

    async def _auto_recover_rate_limits(self):
        """
        自动恢复限速：每3小时给所有被限速的IP增加1次/分钟

        恢复规则：
        - 当前限速每分钟X次 → 增加1次 → 每分钟(X+1)次
        - 当达到每分钟15次时，自动移除限速（恢复为 active）
        - 不对单个IP单独检查，统一批量处理以节省资源
        """
        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                return

            recovery_count = 0
            removed_count = 0

            for ip, ip_data in list(self._ip_cache["ips"].items()):
                if ip_data.get("status") != "rate_limited":
                    continue

                # 获取当前限速秒数
                current_rate_limit_seconds = ip_data.get("rate_limit_seconds", 6)

                # 计算当前每分钟次数
                current_requests_per_min = round(60 / current_rate_limit_seconds)

                # 增加1次
                new_requests_per_min = current_requests_per_min + 1

                # 如果达到或超过15次，移除限速
                if new_requests_per_min >= 15:
                    ip_data["status"] = "active"
                    # 移除 rate_limit_seconds 字段
                    if "rate_limit_seconds" in ip_data:
                        del ip_data["rate_limit_seconds"]
                    removed_count += 1
                    log.info(f"Auto-removed rate limit for IP {ip} (reached 15 requests/min)")
                else:
                    # 更新限速秒数
                    new_rate_limit_seconds = round(60 / new_requests_per_min)
                    ip_data["rate_limit_seconds"] = new_rate_limit_seconds
                    recovery_count += 1

                self._cache_dirty = True

            if recovery_count > 0 or removed_count > 0:
                log.info(
                    f"Auto-recovery rate limits: {recovery_count} IPs increased (+1 req/min), "
                    f"{removed_count} IPs fully recovered (removed limit)"
                )

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
                    rate_limit = ip_data.get("rate_limit_seconds", 6)  # 默认6秒 = 每分钟10次
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
                    rate_limit = ip_data.get("rate_limit_seconds", 6)  # 默认6秒 = 每分钟10次
                    if time.time() - last_request < rate_limit:
                        log.warning(f"Rate limited IP: {ip}")
                        return False

            # 初始化或更新 IP 数据
            china_tz = timezone(timedelta(hours=8))
            today_date = datetime.now(china_tz).strftime("%Y-%m-%d")

            if ip not in self._ip_cache["ips"]:
                self._ip_cache["ips"][ip] = {
                    "first_seen": datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S"),
                    "total_requests": 0,
                    "today_requests": 0,           # 新增：今日请求数
                    "today_date": today_date,      # 新增：今日日期
                    "status": "active",  # active, banned, rate_limited
                    "location": await self._get_ip_location(ip),
                    "user_agents": [],
                    "models_used": {},             # 只记录今日模型使用
                    "endpoints": {},
                }

            ip_data = self._ip_cache["ips"][ip]

            # 检查是否需要重置今日统计（UTC+8 每天00:00重置）
            last_date = ip_data.get("today_date", "")
            if last_date != today_date:
                # 新的一天，重置今日统计
                ip_data["today_requests"] = 0
                ip_data["today_date"] = today_date
                ip_data["models_used"] = {}  # 清空模型使用记录（节省内存）
                self._cache_dirty = True
                log.debug(f"Reset daily stats for IP {ip} on {today_date}")

            # 更新统计
            ip_data["total_requests"] = ip_data.get("total_requests", 0) + 1
            ip_data["today_requests"] = ip_data.get("today_requests", 0) + 1  # 今日请求+1
            ip_data["last_request_time"] = time.time()
            ip_data["last_seen"] = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S")

            # 记录 User-Agent
            if user_agent and user_agent not in ip_data.get("user_agents", []):
                if "user_agents" not in ip_data:
                    ip_data["user_agents"] = []
                ip_data["user_agents"].append(user_agent)
                # 只保留最近10个不同的 User-Agent
                ip_data["user_agents"] = ip_data["user_agents"][-10:]

            # 记录模型使用（只记录今日）
            if model:
                if "models_used" not in ip_data:
                    ip_data["models_used"] = {}
                ip_data["models_used"][model] = ip_data["models_used"].get(model, 0) + 1

            # 记录端点使用（累计，不重置）
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

    async def _load_ban_operations(self) -> Dict[str, Any]:
        """
        从文件加载封禁操作记录，并自动清理超过1小时的记录（懒清理）

        Returns:
            封禁操作记录字典 {"operators": {operator_ip: [timestamp1, timestamp2, ...]}}
        """
        current_time = time.time()
        time_window = 3600  # 1小时 = 3600秒

        try:
            if os.path.exists(self._ban_operations_file):
                async with aiofiles.open(self._ban_operations_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                ban_data = toml.loads(content)
            else:
                ban_data = {"operators": {}}

            # 懒清理：移除所有超过1小时的记录
            cleaned_operators = {}
            removed_count = 0

            for operator_ip, timestamps in ban_data.get("operators", {}).items():
                # 过滤掉超过1小时的时间戳
                valid_timestamps = [ts for ts in timestamps if current_time - ts < time_window]

                # 只保留还有有效记录的操作者
                if valid_timestamps:
                    cleaned_operators[operator_ip] = valid_timestamps
                else:
                    removed_count += 1

            # 如果清理了记录，更新文件
            if removed_count > 0:
                ban_data["operators"] = cleaned_operators
                await self._save_ban_operations(ban_data)
                log.debug(f"Cleaned {removed_count} expired ban operation records")

            return ban_data

        except Exception as e:
            log.error(f"Failed to load ban operations: {e}")
            return {"operators": {}}

    async def _save_ban_operations(self, ban_data: Dict[str, Any]):
        """
        保存封禁操作记录到文件

        Args:
            ban_data: 封禁操作记录数据
        """
        try:
            toml_content = toml.dumps(ban_data)
            async with aiofiles.open(self._ban_operations_file, "w", encoding="utf-8") as f:
                await f.write(toml_content)
            log.debug("Ban operations saved to file")
        except Exception as e:
            log.error(f"Failed to save ban operations: {e}")

    async def _record_ban_operation(self, operator_ip: str):
        """
        记录一次封禁操作

        Args:
            operator_ip: 操作者的IP地址
        """
        try:
            ban_data = await self._load_ban_operations()

            if "operators" not in ban_data:
                ban_data["operators"] = {}

            if operator_ip not in ban_data["operators"]:
                ban_data["operators"][operator_ip] = []

            ban_data["operators"][operator_ip].append(time.time())

            await self._save_ban_operations(ban_data)
            log.info(f"Recorded ban operation from {operator_ip} ({len(ban_data['operators'][operator_ip])}/3 in 1hr)")

        except Exception as e:
            log.error(f"Failed to record ban operation: {e}")

    async def _calculate_ip_credits(self, ip: str) -> float:
        """
        计算IP的今日积分消耗（从models_used累加）

        Args:
            ip: IP地址

        Returns:
            今日累计积分
        """
        try:
            # 读取模型积分配置
            import toml
            import os
            from config import get_credentials_dir

            credentials_dir = await get_credentials_dir()
            credits_toml_file = os.path.join(credentials_dir, "model_credits.toml")

            if not os.path.exists(credits_toml_file):
                log.warning(f"Model credits file not found: {credits_toml_file}")
                # 如果文件不存在，使用默认积分估算
                return self._ip_cache.get("ips", {}).get(ip, {}).get("today_requests", 0) * self._default_credit_score

            with open(credits_toml_file, "r", encoding="utf-8") as f:
                credits_data = toml.load(f)

            # 获取IP的models_used
            ip_data = self._ip_cache.get("ips", {}).get(ip, {})
            models_used = ip_data.get("models_used", {})

            if not models_used:
                # 如果没有模型使用记录，返回0
                return 0.0

            total_credits = 0.0
            default_score = credits_data.get("default", {}).get("score", self._default_credit_score)

            # 打印TOML中的所有模型键（调试用）
            log.debug(f"=== Credit Calculation Debug for IP {ip} ===")
            log.debug(f"TOML antigravity models: {list(credits_data.get('antigravity', {}).keys())}")
            log.debug(f"TOML geminicli models: {list(credits_data.get('geminicli', {}).keys())}")
            log.debug(f"IP models_used: {list(models_used.keys())}")

            # 累加每个模型的积分
            for model_name, count in models_used.items():
                # 先在antigravity组查找
                if "antigravity" in credits_data and model_name in credits_data["antigravity"]:
                    score = credits_data["antigravity"][model_name]
                    log.debug(f"✓ Found '{model_name}' in antigravity: {score} × {count} = {score * count}")
                # 再在geminicli组查找
                elif "geminicli" in credits_data and model_name in credits_data["geminicli"]:
                    score = credits_data["geminicli"][model_name]
                    log.debug(f"✓ Found '{model_name}' in geminicli: {score} × {count} = {score * count}")
                # 使用默认积分
                else:
                    score = default_score
                    log.warning(f"✗ Model '{model_name}' not found in TOML, using default {default_score} × {count} = {score * count}")

                total_credits += score * count

            log.info(f"IP {ip} today credits: {total_credits:.2f} from {len(models_used)} models")
            return total_credits

        except Exception as e:
            log.error(f"Error calculating IP credits: {e}")
            # 出错时使用默认积分作为后备
            return self._ip_cache.get("ips", {}).get(ip, {}).get("today_requests", 0) * self._default_credit_score

    async def _check_ban_operation_limit(self, operator_ip: str) -> tuple[bool, str]:
        """
        检查操作者的封禁操作频率限制（从文件读取，自动清理过期记录）

        Args:
            operator_ip: 操作者的IP地址

        Returns:
            (是否允许, 错误信息)
        """
        max_bans = 3  # 最多3次封禁操作
        time_window = 3600  # 1小时 = 3600秒

        try:
            # 加载并自动清理过期记录
            ban_data = await self._load_ban_operations()

            operator_records = ban_data.get("operators", {}).get(operator_ip, [])
            ban_count = len(operator_records)

            if ban_count >= max_bans:
                # 计算还需要等待的时间
                oldest_timestamp = operator_records[0]
                remaining_time = int(time_window - (time.time() - oldest_timestamp))
                minutes = remaining_time // 60
                return False, f"封禁操作过于频繁，请在 {minutes} 分钟后再试（1小时内最多封禁3次）"

            return True, ""

        except Exception as e:
            log.error(f"Error checking ban operation limit: {e}")
            return True, ""  # 检查失败时允许操作，避免阻塞

    async def set_ip_status(
        self, ip: str, status: str, rate_limit_seconds: Optional[int] = None, operator_ip: Optional[str] = None
    ) -> tuple[bool, str]:
        """
        设置 IP 状态

        Args:
            ip: IP 地址
            status: 状态 (active, banned, rate_limited)
            rate_limit_seconds: 限速秒数（仅用于 rate_limited 状态）
            operator_ip: 操作者的IP地址（用于限制封禁频率）

        Returns:
            (是否成功, 错误信息)
        """
        self._ensure_initialized()

        if status not in ["active", "banned", "rate_limited"]:
            log.error(f"Invalid IP status: {status}")
            return False, "无效的状态类型"

        # 限速操作的额外检查
        if status == "rate_limited":
            if not rate_limit_seconds or rate_limit_seconds < 1 or rate_limit_seconds > 60:
                log.error(f"Invalid rate_limit_seconds: {rate_limit_seconds}")
                return False, "限速时间必须在1-60秒之间（对应每分钟60-1次）"

        async with self._cache_lock:
            if "ips" not in self._ip_cache:
                self._ip_cache["ips"] = {}

            # 封禁操作的额外检查
            if status == "banned":
                # 检查1：今日积分少于60不能被封禁（保护新用户体验，避免高端模型一刀切）
                if ip in self._ip_cache["ips"]:
                    today_credits = await self._calculate_ip_credits(ip)
                    if today_credits < 60:
                        today_requests = self._ip_cache["ips"][ip].get("today_requests", 0)
                        log.warning(f"Cannot ban IP {ip}: only {today_credits:.2f} credits today (minimum 60, {today_requests} requests)")
                        return False, f"今日积分仅 {today_credits:.1f}，少于60积分不能封禁（已使用{today_requests}次请求，保护每位用户体验）"

                # 检查2：操作者封禁频率限制
                if operator_ip:
                    allowed, error_msg = await self._check_ban_operation_limit(operator_ip)
                    if not allowed:
                        log.warning(f"Ban operation from {operator_ip} rate limited")
                        return False, error_msg

            if ip not in self._ip_cache["ips"]:
                # 如果 IP 不存在，创建新记录
                self._ip_cache["ips"][ip] = {
                    "first_seen": datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d %H:%M:%S"),
                    "total_requests": 0,
                    "today_requests": 0,
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

        # 封禁成功后，在锁外记录操作（避免死锁）
        if status == "banned" and operator_ip:
            await self._record_ban_operation(operator_ip)

        return True, ""

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
            today_requests = sum(ip_data.get("today_requests", 0) for ip_data in all_ips.values())

            return {
                "total_ips": total_ips,
                "active_ips": active_ips,
                "banned_ips": banned_ips,
                "rate_limited_ips": rate_limited_ips,
                "total_requests": total_requests,
                "today_requests": today_requests,  # 新增：今日总请求
            }

    async def get_ip_ranking(
        self,
        rank_by: str = "today",
        page: int = 1,
        page_size: int = 20,
        include_banned: bool = False
    ) -> Dict[str, Any]:
        """
        获取 IP 排行榜（分页）

        Args:
            rank_by: 排序依据 ("today" 今日请求 或 "total" 总请求)
            page: 页码（从1开始）
            page_size: 每页数量（默认20）
            include_banned: 是否包含被封禁的IP

        Returns:
            {
                "items": [...],       # 当前页数据
                "page": 1,            # 当前页码
                "page_size": 20,      # 每页数量
                "total": 640,         # 总记录数
                "total_pages": 32,    # 总页数
                "has_next": True,     # 是否有下一页
                "has_prev": False     # 是否有上一页
            }
        """
        self._ensure_initialized()

        async with self._cache_lock:
            all_ips = self._ip_cache.get("ips", {})

            # 过滤IP
            filtered_ips = []
            for ip, ip_data in all_ips.items():
                # 是否包含被封禁IP
                if not include_banned and ip_data.get("status") == "banned":
                    continue

                filtered_ips.append({
                    "ip": ip,
                    "today_requests": ip_data.get("today_requests", 0),
                    "total_requests": ip_data.get("total_requests", 0),
                    "status": ip_data.get("status", "active"),
                    "location": ip_data.get("location", "未知"),
                    "first_seen": ip_data.get("first_seen", ""),
                    "last_seen": ip_data.get("last_seen", ""),
                })

            # 排序
            sort_key = "today_requests" if rank_by == "today" else "total_requests"
            sorted_ips = sorted(
                filtered_ips,
                key=lambda x: x[sort_key],
                reverse=True
            )

            # 分页计算
            total = len(sorted_ips)
            total_pages = (total + page_size - 1) // page_size  # 向上取整

            # 确保页码合法
            page = max(1, min(page, total_pages if total_pages > 0 else 1))

            # 计算起始和结束索引
            start_index = (page - 1) * page_size
            end_index = start_index + page_size

            # 获取当前页数据
            page_items = sorted_ips[start_index:end_index]

            return {
                "items": page_items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
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
