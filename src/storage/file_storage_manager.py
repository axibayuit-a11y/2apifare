"""
本地文件存储管理器，使用统一缓存支持队列写入优化。
所有凭证和状态数据存储在creds.toml中，配置数据存储在config.toml中。
"""

import asyncio
import os
import json
import time
from typing import Dict, Any, List, Optional

import aiofiles
import toml

from log import log
from .cache_manager import UnifiedCacheManager, CacheBackend
import config


class FileCacheBackend(CacheBackend):
    """文件缓存后端实现"""

    def __init__(self, file_path: str):
        self._file_path = file_path

    async def load_data(self) -> Dict[str, Any]:
        """从TOML文件加载数据"""
        try:
            if not os.path.exists(self._file_path):
                return {}

            async with aiofiles.open(self._file_path, "r", encoding="utf-8") as f:
                content = await f.read()

            if not content.strip():
                return {}

            return toml.loads(content)

        except Exception as e:
            log.error(f"Error loading data from file {self._file_path}: {e}")
            return {}

    async def write_data(self, data: Dict[str, Any]) -> bool:
        """将数据写入TOML文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self._file_path), exist_ok=True)

            # 过滤掉嵌套的 freeze_status 字段（避免在 creds.toml 中生成子表）
            # freeze_status 应该是一个简单值（None）或不存在，而不是嵌套字典
            filtered_data = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    # 对于每个凭证的数据，过滤掉嵌套的 freeze_status
                    filtered_value = {}
                    for field_key, field_value in value.items():
                        if field_key == "freeze_status" and isinstance(field_value, dict):
                            # 如果 freeze_status 是字典，不写入（避免生成子表）
                            # 只允许 None 或简单值
                            log.warning(f"Filtering nested freeze_status for {key}, use separate state management")
                            continue
                        filtered_value[field_key] = field_value
                    filtered_data[key] = filtered_value
                else:
                    filtered_data[key] = value

            # 写入TOML文件
            toml_content = toml.dumps(filtered_data)
            async with aiofiles.open(self._file_path, "w", encoding="utf-8") as f:
                await f.write(toml_content)

            return True

        except Exception as e:
            log.error(f"Error writing data to file {self._file_path}: {e}")
            return False


class FileStorageManager:
    """基于本地文件的存储管理器（使用统一缓存）"""

    # 状态字段常量
    STATE_FIELDS = {
        "error_codes",
        "disabled",
        "last_success",
        "user_email",
        "gemini_2_5_pro_calls",
        "total_calls",
        "next_reset_time",
        "daily_limit_gemini_2_5_pro",
        "daily_limit_total",
        # 冻结状态（扁平化存储，避免 TOML 嵌套表）
        "freeze_frozen",
        "freeze_time",
        "freeze_is_owner",
        "freeze_delete_reason",
        "freeze_auto_delete_time",
    }

    # 默认状态数据模板（不包含动态值）
    _DEFAULT_STATE_TEMPLATE = {
        "error_codes": [],
        "disabled": False,
        "user_email": None,
        "gemini_2_5_pro_calls": 0,
        "total_calls": 0,
        "next_reset_time": None,
        "daily_limit_gemini_2_5_pro": 100,
        "daily_limit_total": 1000,
    }

    @classmethod
    def get_default_state(cls) -> Dict[str, Any]:
        """获取默认状态数据（包含当前时间戳）"""
        state = cls._DEFAULT_STATE_TEMPLATE.copy()
        state["last_success"] = time.time()
        return state

    def __init__(self):
        self._credentials_dir = None  # 将通过异步初始化设置
        self._state_file = None
        self._config_file = None
        self._lock = asyncio.Lock()
        self._initialized = False

        # 统一缓存管理器
        self._credentials_cache_manager: Optional[UnifiedCacheManager] = None
        self._config_cache_manager: Optional[UnifiedCacheManager] = None

        # 配置参数
        self._write_delay = 0.5  # 写入延迟（秒）
        self._cache_ttl = 300  # 缓存TTL（秒）

    async def initialize(self) -> None:
        """初始化文件存储"""
        if self._initialized:
            return

        # 获取凭证目录配置（初始化时直接使用环境变量，避免循环依赖）
        self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
        self._state_file = os.path.join(self._credentials_dir, "creds.toml")
        self._config_file = os.path.join(self._credentials_dir, "config.toml")

        # 确保目录存在
        os.makedirs(self._credentials_dir, exist_ok=True)

        # 执行JSON到TOML的迁移
        await self._migrate_json_to_toml()

        # 创建缓存管理器
        credentials_backend = FileCacheBackend(self._state_file)
        config_backend = FileCacheBackend(self._config_file)

        self._credentials_cache_manager = UnifiedCacheManager(
            credentials_backend,
            cache_ttl=self._cache_ttl,
            write_delay=self._write_delay,
            name="credentials",
        )

        self._config_cache_manager = UnifiedCacheManager(
            config_backend, cache_ttl=self._cache_ttl, write_delay=self._write_delay, name="config"
        )

        # 启动缓存管理器
        await self._credentials_cache_manager.start()
        await self._config_cache_manager.start()

        # 清理幽灵记录（只有状态字段，没有凭证内容的记录）
        await self._cleanup_ghost_records()

        self._initialized = True
        log.debug("File storage manager initialized with unified cache")

    async def _cleanup_ghost_records(self) -> None:
        """清理幽灵记录：只有状态字段，没有 refresh_token 的记录"""
        try:
            all_data = await self._credentials_cache_manager.get_all()

            ghost_records = []
            for filename, content in all_data.items():
                # 如果没有 refresh_token，说明这是纯状态记录（幽灵记录）
                if 'refresh_token' not in content:
                    ghost_records.append(filename)

            if ghost_records:
                log.info(f"Found {len(ghost_records)} ghost records, cleaning up...")
                for filename in ghost_records:
                    await self._credentials_cache_manager.delete(filename)
                    log.debug(f"Removed ghost record: {filename}")
                log.info(f"Ghost records cleanup completed: removed {len(ghost_records)} records")
            else:
                log.debug("No ghost records found")

        except Exception as e:
            log.error(f"Error cleaning up ghost records: {e}")

    async def close(self) -> None:
        """关闭文件存储"""
        # 停止缓存管理器
        if self._credentials_cache_manager:
            await self._credentials_cache_manager.stop()
        if self._config_cache_manager:
            await self._config_cache_manager.stop()

        self._initialized = False
        log.debug("File storage manager closed with unified cache flushed")

    def _normalize_filename(self, filename: str) -> str:
        """标准化文件名"""
        return os.path.basename(filename)

    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("File storage manager not initialized")

    async def _migrate_json_to_toml(self) -> None:
        """将现有的JSON凭证文件和旧的creds_state.toml迁移到新的creds.toml文件中"""
        try:
            # 扫描JSON凭证文件
            json_files = []
            if os.path.exists(self._credentials_dir):
                for filename in os.listdir(self._credentials_dir):
                    if filename.endswith(".json"):
                        json_files.append(filename)

            # 检查旧的creds_state.toml文件
            old_state_file = os.path.join(self._credentials_dir, "creds_state.toml")
            has_old_state = os.path.exists(old_state_file)

            if not json_files and not has_old_state:
                log.debug("No JSON credential files or old state file found for migration")
                return

            # 加载现有TOML数据（如果存在）
            toml_data = {}
            if os.path.exists(self._state_file):
                try:
                    async with aiofiles.open(self._state_file, "r", encoding="utf-8") as f:
                        content = await f.read()
                    if content.strip():
                        toml_data = toml.loads(content)
                except Exception as e:
                    log.error(f"Failed to load existing TOML file: {e}")

            # 加载旧的creds_state.toml文件（稍后处理）
            old_state_data = {}
            if has_old_state:
                try:
                    async with aiofiles.open(old_state_file, "r", encoding="utf-8") as f:
                        content = await f.read()
                    old_state_data = toml.loads(content)
                    log.debug("Loaded old state file for potential migration")
                except Exception as e:
                    log.error(f"Failed to load old state file: {e}")
                    old_state_data = {}

            if json_files:
                log.info(f"Migrating {len(json_files)} JSON credential files to TOML")

            # 处理每个JSON文件
            migrated_count = 0
            for filename in json_files:
                try:
                    filepath = os.path.join(self._credentials_dir, filename)

                    # 读取JSON凭证数据
                    async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
                        json_content = await f.read()
                    credential_data = json.loads(json_content)

                    # 创建新的section：凭证数据 + 状态数据
                    section_data = credential_data.copy()

                    # 首先添加默认状态数据
                    section_data.update(self.get_default_state())

                    # 如果旧状态文件中有该凭证的状态数据，则使用旧状态数据覆盖默认值
                    if filename in old_state_data and isinstance(old_state_data[filename], dict):
                        log.debug(f"Using old state data for: {filename}")
                        section_data.update(old_state_data[filename])

                    # 如果当前TOML中已存在该凭证，保留其状态数据
                    if filename in toml_data and isinstance(toml_data[filename], dict):
                        log.debug(f"Merging with existing TOML state for: {filename}")
                        existing_state = toml_data[filename]
                        section_data.update(existing_state)

                    # 最后确保凭证数据是最新的（覆盖任何冲突的字段）
                    section_data.update(credential_data)

                    toml_data[filename] = section_data

                    migrated_count += 1
                    log.debug(f"Migrated credential: {filename}")

                except Exception as e:
                    log.error(f"Failed to migrate {filename}: {e}")
                    continue

            # 保存TOML文件（如果有新的迁移）
            if migrated_count > 0:
                try:
                    toml_content = toml.dumps(toml_data)
                    async with aiofiles.open(self._state_file, "w", encoding="utf-8") as f:
                        await f.write(toml_content)

                    # 删除已迁移的JSON文件
                    for filename in json_files:
                        try:
                            if filename in toml_data:  # 确保文件确实被迁移了
                                filepath = os.path.join(self._credentials_dir, filename)
                                os.remove(filepath)
                                log.debug(f"Removed migrated JSON file: {filename}")
                        except Exception as e:
                            log.warning(f"Failed to remove {filename}: {e}")

                    # 删除旧的状态文件（如果存在）
                    if has_old_state:
                        try:
                            os.remove(old_state_file)
                            log.debug("Removed old state file: creds_state.toml")
                        except Exception as e:
                            log.warning(f"Failed to remove old state file: {e}")

                    log.info(f"Migration completed: {migrated_count} files migrated to TOML format")

                except Exception as e:
                    log.error(f"Failed to save migrated TOML file: {e}")

        except Exception as e:
            log.error(f"Error during JSON to TOML migration: {e}")

    # ============ 凭证管理 ============

    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据到统一缓存"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 获取现有数据或创建新数据
            all_data = await self._credentials_cache_manager.get_all()
            existing_state = all_data.get(filename, {})

            # 创建新的section数据：凭证数据 + 状态数据
            final_data = self.get_default_state()
            final_data.update(existing_state)
            final_data.update(credential_data)  # 凭证数据覆盖状态数据中的同名字段

            # 更新整个数据集
            all_data[filename] = final_data

            success = await self._credentials_cache_manager.update_multi({filename: final_data})
            log.debug(f"Stored credential to unified cache: {filename}")
            return success

        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False

    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """从统一缓存获取凭证数据（支持 accounts.toml）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 特殊处理 userID_ 前缀（Antigravity 单个账户）
            if filename.startswith("userID_"):
                user_id = filename.replace("userID_", "")
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")

                if os.path.exists(accounts_toml_path):
                    import toml
                    # 使用异步读取，避免缓存问题
                    async with aiofiles.open(accounts_toml_path, 'r', encoding='utf-8') as f:
                        content = await f.read()
                    accounts_data = toml.loads(content)

                    # 查找匹配的账户
                    if 'accounts' in accounts_data:
                        for account in accounts_data['accounts']:
                            if account.get('user_id') == user_id:
                                log.info(f"Found account for user_id: {user_id}, email: {account.get('email')}, disabled: {account.get('disabled')}")
                                return account  # 返回单个账户的凭证数据

                    log.warning(f"Account not found for user_id: {user_id}")
                    return None
                else:
                    log.warning(f"accounts.toml not found at {accounts_toml_path}")
                    return None

            # 特殊处理 accounts.toml（Antigravity 凭证 - 向后兼容）
            if "accounts.toml" in filename:
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")
                if os.path.exists(accounts_toml_path):
                    import toml
                    with open(accounts_toml_path, 'r', encoding='utf-8') as f:
                        accounts_data = toml.load(f)
                    log.debug(f"Loaded accounts.toml with {len(accounts_data.get('accounts', []))} accounts")
                    return accounts_data  # 返回整个 accounts 数据结构
                else:
                    log.warning(f"accounts.toml not found at {accounts_toml_path}")
                    return None

            # 从 creds.toml 缓存获取 CLI 凭证数据
            all_data = await self._credentials_cache_manager.get_all()

            if filename not in all_data:
                return None

            section_data = all_data[filename]

            # 提取凭证数据（排除状态字段）
            credential_data = {k: v for k, v in section_data.items() if k not in self.STATE_FIELDS}
            return credential_data

        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None

    async def list_credentials(self) -> List[str]:
        """从统一缓存列出所有凭证文件名（包括 accounts.toml 中的每个账户）"""
        self._ensure_initialized()

        try:
            all_data = await self._credentials_cache_manager.get_all()
            cred_list = list(all_data.keys())

            # 检查 accounts.toml 是否存在（Antigravity 凭证）
            accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")
            if os.path.exists(accounts_toml_path):
                try:
                    import toml
                    with open(accounts_toml_path, 'r', encoding='utf-8') as f:
                        accounts_data = toml.load(f)

                    # 为每个账户创建虚拟文件名（使用 userID_ 格式）
                    if 'accounts' in accounts_data and len(accounts_data['accounts']) > 0:
                        for account in accounts_data['accounts']:
                            user_id = account.get('user_id')
                            if user_id:
                                virtual_filename = f"userID_{user_id}"
                                cred_list.append(virtual_filename)
                        log.debug(f"Added {len(accounts_data['accounts'])} accounts from accounts.toml")
                except Exception as e:
                    log.warning(f"Error reading accounts.toml: {e}")

            return cred_list

        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []

    async def delete_credential(self, filename: str) -> bool:
        """从统一缓存删除凭证（支持 userID_ 前缀）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 特殊处理 userID_ 前缀（Antigravity 账户）
            if filename.startswith("userID_"):
                return await self._delete_antigravity_account(filename)

            # CLI 凭证删除逻辑
            success = await self._credentials_cache_manager.delete(filename)
            log.debug(f"Deleted credential from unified cache: {filename}")
            return success

        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False

    # ============ Antigravity 账户状态管理辅助方法 ============

    async def _update_antigravity_account_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新 accounts.toml 中单个账户的状态（使用文件锁防止并发冲突）"""
        # [CRITICAL FIX] 使用 self._lock 防止并发写入冲突
        async with self._lock:
            try:
                user_id = filename.replace("userID_", "")
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")

                if not os.path.exists(accounts_toml_path):
                    log.warning(f"accounts.toml not found at {accounts_toml_path}")
                    return False

                # 读取 accounts.toml
                async with aiofiles.open(accounts_toml_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                accounts_data = toml.loads(content)

                if 'accounts' not in accounts_data:
                    log.warning("No accounts found in accounts.toml")
                    return False

                # 查找并更新对应账户
                account_found = False
                for account in accounts_data['accounts']:
                    if account.get('user_id') == user_id:
                        # 记录更新前的值
                        old_disabled = account.get('disabled')

                        # 直接更新状态字段（统一使用 disabled）
                        account.update(state_updates)
                        account_found = True

                        # 记录更新后的值
                        new_disabled = account.get('disabled')
                        log.info(f"Updated antigravity account state for user_id: {user_id}")
                        log.info(f"  Updates: {state_updates}")
                        log.info(f"  disabled: {old_disabled} -> {new_disabled}")
                        log.info(f"  email: {account.get('email')}")
                        break

                if not account_found:
                    log.warning(f"Account not found for user_id: {user_id}")
                    return False

                # [CRITICAL FIX] 使用原子写入保护
                toml_content = toml.dumps(accounts_data)
                log.info(f"Writing to {accounts_toml_path}, content length: {len(toml_content)} bytes")

                # 原子写入：临时文件 + 重命名
                temp_file = f"{accounts_toml_path}.tmp"
                try:
                    async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                        await f.write(toml_content)

                    # 原子性重命名
                    if os.path.exists(accounts_toml_path):
                        import shutil
                        backup_file = f"{accounts_toml_path}.backup"
                        shutil.copy2(accounts_toml_path, backup_file)
                        try:
                            os.replace(temp_file, accounts_toml_path)
                            if os.path.exists(backup_file):
                                os.remove(backup_file)
                        except Exception as e:
                            log.error(f"[CRITICAL] Failed to rename, restoring backup: {e}")
                            if os.path.exists(backup_file):
                                shutil.copy2(backup_file, accounts_toml_path)
                                os.remove(backup_file)
                            raise
                    else:
                        os.rename(temp_file, accounts_toml_path)
                finally:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass

                log.info(f"Successfully saved updated accounts.toml for user_id: {user_id} (atomic write)")

                # 验证写入：重新读取文件确认
                async with aiofiles.open(accounts_toml_path, "r", encoding="utf-8") as f:
                    verify_content = await f.read()
                verify_data = toml.loads(verify_content)
                for acc in verify_data.get('accounts', []):
                    if acc.get('user_id') == user_id:
                        log.info(f"Verified: disabled = {acc.get('disabled')}")
                        break

                return True

            except Exception as e:
                log.error(f"[CRITICAL] Error updating antigravity account state {filename}: {e}")
                import traceback
                traceback.print_exc()
                return False

    async def _update_antigravity_account_usage(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新 accounts.toml 中单个账户的使用统计"""
        # 复用状态更新方法
        return await self._update_antigravity_account_state(filename, stats_updates)

    async def _cleanup_old_antigravity_backups(self, backup_dir: str):
        """清理 Antigravity 旧备份文件，保留最新的 N 个"""
        try:
            max_backup_count = await config.get_max_backup_count()

            if not os.path.exists(backup_dir):
                return

            # 获取所有备份文件
            backup_files = []
            for filename in os.listdir(backup_dir):
                if filename.endswith('.bak'):
                    file_path = os.path.join(backup_dir, filename)
                    if os.path.isfile(file_path):
                        backup_files.append({
                            'path': file_path,
                            'mtime': os.path.getmtime(file_path),
                            'name': filename
                        })

            # 如果备份数量未超过限制，无需清理
            if len(backup_files) <= max_backup_count:
                return

            # 按修改时间排序（从旧到新）
            backup_files.sort(key=lambda x: x['mtime'])

            # 删除最旧的备份，直到数量符合限制
            delete_count = len(backup_files) - max_backup_count
            for i in range(delete_count):
                file_info = backup_files[i]
                try:
                    os.remove(file_info['path'])
                    log.info(f"Deleted old Antigravity backup: {file_info['name']}")
                except Exception as e:
                    log.error(f"Failed to delete old Antigravity backup {file_info['name']}: {e}")

            log.info(f"Antigravity backup cleanup: deleted {delete_count} old backups, keeping {max_backup_count} newest")

        except Exception as e:
            log.error(f"Failed to cleanup Antigravity backups in {backup_dir}: {e}")

    async def _delete_antigravity_account(self, filename: str) -> bool:
        """
        从 accounts.toml 中删除单个账户，并备份整个 accounts.toml 文件

        备份机制（与 CLI 一致）：
        - 在 creds/Antbackup/ 目录下创建备份
        - 备份文件名：accounts_{删除后剩余数量}_{时间戳}.toml.bak
        - 备份整个 accounts.toml 文件的快照
        """
        # [CRITICAL FIX] 使用 self._lock 防止并发删除冲突
        async with self._lock:
            try:
                user_id = filename.replace("userID_", "")
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")

                if not os.path.exists(accounts_toml_path):
                    log.warning(f"accounts.toml not found at {accounts_toml_path}")
                    return False

                # 读取 accounts.toml
                async with aiofiles.open(accounts_toml_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                accounts_data = toml.loads(content)

                if 'accounts' not in accounts_data:
                    log.warning("No accounts found in accounts.toml")
                    return False

                # 查找要删除的账户
                deleted_account = None
                for acc in accounts_data['accounts']:
                    if acc.get('user_id') == user_id:
                        deleted_account = acc
                        break

                if not deleted_account:
                    log.warning(f"Account not found for deletion: user_id={user_id}")
                    return False

                # 创建备份目录
                backup_dir = os.path.join(self._credentials_dir, "Antbackup")
                os.makedirs(backup_dir, exist_ok=True)

                # 计算删除后的账户数量
                accounts_count_after_delete = len(accounts_data['accounts']) - 1

                # 生成备份文件名（与 CLI 格式一致）：accounts_{删除后数量}_{时间戳}.toml.bak
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup_filename = f"accounts_{accounts_count_after_delete}_{timestamp}.toml.bak"
                backup_path = os.path.join(backup_dir, backup_filename)

                # 备份整个 accounts.toml 文件（与 CLI 一致）
                import shutil
                shutil.copy2(accounts_toml_path, backup_path)
                log.info(f"accounts.toml backed up to: {backup_path}")

                # 清理旧备份
                await self._cleanup_old_antigravity_backups(backup_dir)

                # 从 accounts.toml 中删除账户
                accounts_data['accounts'] = [
                    acc for acc in accounts_data['accounts']
                    if acc.get('user_id') != user_id
                ]

                # [CRITICAL FIX] 使用原子写入保护
                toml_content = toml.dumps(accounts_data)
                temp_file = f"{accounts_toml_path}.tmp"
                try:
                    async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                        await f.write(toml_content)

                    # 原子性重命名
                    backup_file = f"{accounts_toml_path}.backup"
                    shutil.copy2(accounts_toml_path, backup_file)
                    try:
                        os.replace(temp_file, accounts_toml_path)
                        if os.path.exists(backup_file):
                            os.remove(backup_file)
                    except Exception as e:
                        log.error(f"[CRITICAL] Failed to rename, restoring backup: {e}")
                        if os.path.exists(backup_file):
                            shutil.copy2(backup_file, accounts_toml_path)
                            os.remove(backup_file)
                        raise
                finally:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass

                log.info(f"Deleted antigravity account: user_id={user_id}, email={deleted_account.get('email', 'unknown')} (atomic write)")
                return True

            except Exception as e:
                log.error(f"[CRITICAL] Error deleting antigravity account {filename}: {e}")
                import traceback
                traceback.print_exc()
                return False

    # ============ 状态管理 ============

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态（支持 userID_ 前缀）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 特殊处理 userID_ 前缀（Antigravity 账户）
            if filename.startswith("userID_"):
                return await self._update_antigravity_account_state(filename, state_updates)

            # 拦截 accounts.toml（这是单独的文件，不应该写入 creds.toml）
            if "accounts.toml" in filename.lower():
                log.warning(f"Attempted to update accounts.toml in creds.toml, ignoring: {filename}")
                return False

            # CLI 凭证的更新逻辑
            all_data = await self._credentials_cache_manager.get_all()

            if filename not in all_data:
                all_data[filename] = self.get_default_state()

            # 更新状态
            all_data[filename].update(state_updates)

            success = await self._credentials_cache_manager.update_multi(
                {filename: all_data[filename]}
            )
            log.debug(f"Updated credential state in unified cache: {filename}")
            return success

        except Exception as e:
            log.error(f"Error updating credential state {filename}: {e}")
            return False

    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """从统一缓存获取凭证状态（支持 accounts.toml）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 特殊处理 userID_ 前缀（Antigravity 单个账户）
            if filename.startswith("userID_"):
                user_id = filename.replace("userID_", "")
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")

                if os.path.exists(accounts_toml_path):
                    try:
                        import toml
                        # 使用异步读取，避免缓存问题
                        async with aiofiles.open(accounts_toml_path, 'r', encoding='utf-8') as f:
                            content = await f.read()
                        accounts_data = toml.loads(content)

                        # 查找匹配的账户
                        if 'accounts' in accounts_data:
                            for account in accounts_data['accounts']:
                                if account.get('user_id') == user_id:
                                    # 构建状态数据，记录缺失字段
                                    state_data = {}

                                    # 必需字段
                                    state_data['user_email'] = account.get('email')

                                    # disabled 字段
                                    if 'disabled' in account:
                                        state_data['disabled'] = account['disabled']
                                        log.info(f"Account {user_id} state - disabled: {account['disabled']}, email: {account.get('email')}")
                                    else:
                                        log.debug(f"Account {user_id} missing 'disabled' field, using False")
                                        state_data['disabled'] = False

                                    # error_codes 字段
                                    if 'error_codes' in account:
                                        state_data['error_codes'] = account['error_codes']
                                    else:
                                        log.debug(f"Account {user_id} missing 'error_codes' field, initializing to []")
                                        state_data['error_codes'] = []

                                    # last_success 字段
                                    if 'last_success' in account:
                                        state_data['last_success'] = account['last_success']
                                    else:
                                        log.debug(f"Account {user_id} missing 'last_success' field, using current time")
                                        state_data['last_success'] = time.time()

                                    # freeze_status 字段（冻结状态）- 支持新旧两种格式
                                    if 'freeze_status' in account and isinstance(account['freeze_status'], dict):
                                        # 旧的嵌套格式
                                        state_data['freeze_status'] = account['freeze_status']
                                    elif 'freeze_frozen' in account:
                                        # 新的扁平化格式，重构为嵌套字典
                                        state_data['freeze_status'] = {
                                            'frozen': account.get('freeze_frozen'),
                                            'freeze_time': account.get('freeze_time'),
                                            'is_owner': account.get('freeze_is_owner'),
                                            'delete_reason': account.get('freeze_delete_reason'),
                                            'auto_delete_time': account.get('freeze_auto_delete_time'),
                                        } if account.get('freeze_frozen') else None
                                    else:
                                        state_data['freeze_status'] = None

                                    # Antigravity 只需要核心状态字段，不需要 CLI 的统计字段
                                    return state_data

                        log.warning(f"Account not found for user_id: {user_id}")
                    except Exception as e:
                        log.warning(f"Error reading accounts.toml for user_id {user_id}: {e}")

                # 返回默认状态
                return self.get_default_state()

            # 特殊处理 accounts.toml（Antigravity 凭证）
            if "accounts.toml" in filename:
                # 为 accounts.toml 返回默认状态，但标记为已启用
                accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")
                if os.path.exists(accounts_toml_path):
                    # 尝试从 accounts.toml 中读取所有账户的邮箱信息
                    try:
                        import toml
                        with open(accounts_toml_path, 'r', encoding='utf-8') as f:
                            accounts_data = toml.load(f)

                        # 获取所有账户的邮箱信息
                        user_email = None
                        if 'accounts' in accounts_data and len(accounts_data['accounts']) > 0:
                            accounts = accounts_data['accounts']
                            # 收集所有邮箱（过滤掉空值）
                            emails = [acc.get('email') for acc in accounts if acc.get('email')]

                            if len(emails) > 0:
                                if len(emails) == 1:
                                    # 只有一个账户，直接显示邮箱
                                    user_email = emails[0]
                                else:
                                    # 多个账户，显示数量和前两个邮箱
                                    if len(emails) <= 2:
                                        user_email = f"{len(emails)} 个账户: {', '.join(emails)}"
                                    else:
                                        user_email = f"{len(emails)} 个账户: {emails[0]}, {emails[1]}, ..."

                        # Antigravity 只返回核心状态字段
                        return {
                            "error_codes": [],
                            "disabled": False,
                            "last_success": time.time(),
                            "user_email": user_email,
                        }
                    except Exception as e:
                        log.warning(f"Error reading accounts.toml for state: {e}")

                # 返回默认状态
                return self.get_default_state()

            # 从 creds.toml 缓存获取 CLI 凭证状态
            all_data = await self._credentials_cache_manager.get_all()

            if filename not in all_data:
                # 返回基本的状态字段
                default_state = self.get_default_state()
                return {
                    k: v
                    for k, v in default_state.items()
                    if k in {"error_codes", "disabled", "last_success", "user_email"}
                }

            section_data = all_data[filename]

            # 提取状态字段
            state_data = {k: v for k, v in section_data.items() if k in self.STATE_FIELDS}

            # 确保必要字段存在
            basic_fields = {"error_codes", "disabled", "last_success", "user_email"}
            default_state = self.get_default_state()

            for field in basic_fields:
                if field not in state_data:
                    state_data[field] = default_state[field]

            # 重构 freeze_status：将扁平化字段转换为嵌套字典（兼容前端）
            if state_data.get("freeze_frozen"):
                state_data["freeze_status"] = {
                    "frozen": state_data.get("freeze_frozen"),
                    "freeze_time": state_data.get("freeze_time"),
                    "is_owner": state_data.get("freeze_is_owner"),
                    "delete_reason": state_data.get("freeze_delete_reason"),
                    "auto_delete_time": state_data.get("freeze_auto_delete_time"),
                }
            else:
                state_data["freeze_status"] = None

            return state_data

        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
            return self.get_default_state()

    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """从统一缓存获取所有凭证状态（包括 accounts.toml）"""
        self._ensure_initialized()

        try:
            all_data = await self._credentials_cache_manager.get_all()

            states = {}

            # 处理 CLI 凭证状态（来自 creds.toml）
            for filename, section_data in all_data.items():
                # 提取状态字段
                state_data = {k: v for k, v in section_data.items() if k in self.STATE_FIELDS}

                # 确保必要字段存在
                basic_fields = {"error_codes", "disabled", "last_success", "user_email"}
                default_state = self.get_default_state()

                for field in basic_fields:
                    if field not in state_data:
                        state_data[field] = default_state[field]

                # 重构 freeze_status：将扁平化字段转换为嵌套字典（兼容前端）
                if state_data.get("freeze_frozen"):
                    state_data["freeze_status"] = {
                        "frozen": state_data.get("freeze_frozen"),
                        "freeze_time": state_data.get("freeze_time"),
                        "is_owner": state_data.get("freeze_is_owner"),
                        "delete_reason": state_data.get("freeze_delete_reason"),
                        "auto_delete_time": state_data.get("freeze_auto_delete_time"),
                    }
                else:
                    state_data["freeze_status"] = None

                states[filename] = state_data

            # 处理 Antigravity 账户状态（来自 accounts.toml）
            accounts_toml_path = os.path.join(self._credentials_dir, "accounts.toml")
            if os.path.exists(accounts_toml_path):
                try:
                    async with aiofiles.open(accounts_toml_path, 'r', encoding='utf-8') as f:
                        content = await f.read()
                    accounts_data = toml.loads(content)

                    if 'accounts' in accounts_data:
                        for account in accounts_data['accounts']:
                            user_id = account.get('user_id')
                            if user_id:
                                virtual_filename = f"userID_{user_id}"
                                # 只提取 Antigravity 需要的核心状态字段（不要 CLI 的统计字段）

                                # 处理 freeze_status - 支持新旧两种格式
                                freeze_status = None
                                if 'freeze_status' in account and isinstance(account['freeze_status'], dict):
                                    # 旧的嵌套格式
                                    freeze_status = account['freeze_status']
                                elif 'freeze_frozen' in account:
                                    # 新的扁平化格式，重构为嵌套字典
                                    if account.get('freeze_frozen'):
                                        freeze_status = {
                                            'frozen': account.get('freeze_frozen'),
                                            'freeze_time': account.get('freeze_time'),
                                            'is_owner': account.get('freeze_is_owner'),
                                            'delete_reason': account.get('freeze_delete_reason'),
                                            'auto_delete_time': account.get('freeze_auto_delete_time'),
                                        }

                                state_data = {
                                    'disabled': account.get('disabled', False),
                                    'error_codes': account.get('error_codes', []),
                                    'last_success': account.get('last_success', time.time()),
                                    'user_email': account.get('email'),
                                    'freeze_status': freeze_status,  # 冻结状态
                                }
                                states[virtual_filename] = state_data
                        log.debug(f"Added states for {len(accounts_data['accounts'])} Antigravity accounts")
                except Exception as e:
                    log.warning(f"Error reading accounts.toml states: {e}")

            return states

        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}

    # ============ 配置管理 ============

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置到统一缓存"""
        self._ensure_initialized()
        return await self._config_cache_manager.set(key, value)

    async def get_config(self, key: str, default: Any = None) -> Any:
        """从统一缓存获取配置"""
        self._ensure_initialized()
        return await self._config_cache_manager.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        """从统一缓存获取所有配置"""
        self._ensure_initialized()
        return await self._config_cache_manager.get_all()

    async def delete_config(self, key: str) -> bool:
        """从统一缓存删除配置"""
        self._ensure_initialized()
        return await self._config_cache_manager.delete(key)

    # ============ 使用统计管理 ============

    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计（支持 userID_ 前缀）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)

            # 特殊处理 userID_ 前缀（Antigravity 账户）
            if filename.startswith("userID_"):
                return await self._update_antigravity_account_usage(filename, stats_updates)

            # 拦截 accounts.toml（这是单独的文件，不应该写入 creds.toml）
            if "accounts.toml" in filename.lower():
                log.warning(f"Attempted to update accounts.toml usage stats in creds.toml, ignoring: {filename}")
                return False

            # CLI 凭证的更新逻辑
            all_data = await self._credentials_cache_manager.get_all()

            if filename not in all_data:
                all_data[filename] = self.get_default_state()

            # 更新统计数据
            all_data[filename].update(stats_updates)

            success = await self._credentials_cache_manager.update_multi(
                {filename: all_data[filename]}
            )
            log.debug(f"Updated usage stats in unified cache: {filename}")
            return success

        except Exception as e:
            log.error(f"Error updating usage stats {filename}: {e}")
            return False

    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """从统一缓存获取使用统计"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)
            all_data = await self._credentials_cache_manager.get_all()

            if filename not in all_data:
                # 返回基本的统计字段
                default_state = self.get_default_state()
                return {
                    k: v
                    for k, v in default_state.items()
                    if k
                    in {
                        "gemini_2_5_pro_calls",
                        "total_calls",
                        "next_reset_time",
                        "daily_limit_gemini_2_5_pro",
                        "daily_limit_total",
                    }
                }

            section_data = all_data[filename]

            # 提取统计字段
            stats_fields = {
                "gemini_2_5_pro_calls",
                "total_calls",
                "next_reset_time",
                "daily_limit_gemini_2_5_pro",
                "daily_limit_total",
            }
            stats_data = {k: v for k, v in section_data.items() if k in stats_fields}

            # 确保必要字段存在
            default_state = self.get_default_state()
            for field in stats_fields:
                if field not in stats_data:
                    stats_data[field] = default_state[field]

            return stats_data

        except Exception as e:
            log.error(f"Error getting usage stats {filename}: {e}")
            return self.get_default_state()

    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """从统一缓存获取所有使用统计"""
        self._ensure_initialized()

        try:
            all_data = await self._credentials_cache_manager.get_all()

            stats = {}
            stats_fields = {
                "gemini_2_5_pro_calls",
                "total_calls",
                "next_reset_time",
                "daily_limit_gemini_2_5_pro",
                "daily_limit_total",
            }

            for filename, section_data in all_data.items():
                # 提取统计字段
                stats_data = {k: v for k, v in section_data.items() if k in stats_fields}

                # 确保必要字段存在
                default_state = self.get_default_state()
                for field in stats_fields:
                    if field not in stats_data:
                        stats_data[field] = default_state[field]

                stats[filename] = stats_data

            return stats

        except Exception as e:
            log.error(f"Error getting all usage stats: {e}")
            return {}

    # ============ Antigravity 凭证管理 ============

    async def load_antigravity_accounts(self) -> Optional[Dict[str, Any]]:
        """加载 Antigravity accounts.toml

        返回值:
            - Dict[str, Any]: 成功读取的账户数据
            - None: 读取失败（调用者必须检查并处理）
        """
        self._ensure_initialized()

        try:
            accounts_file = os.path.join(self._credentials_dir, "accounts.toml")

            # 检查文件是否存在
            if not os.path.exists(accounts_file):
                log.debug(f"Antigravity accounts file not found: {accounts_file}")
                return {"accounts": []}

            # 读取文件
            async with aiofiles.open(accounts_file, "r", encoding="utf-8") as f:
                content = await f.read()

            if not content.strip():
                log.warning("accounts.toml is empty, returning empty structure")
                return {"accounts": []}

            # 解析 TOML
            accounts_data = toml.loads(content)

            log.debug(f"Loaded {len(accounts_data.get('accounts', []))} Antigravity accounts")
            return accounts_data

        except Exception as e:
            # [CRITICAL FIX] 返回 None 表示读取失败，调用者必须检查！
            log.error(f"[CRITICAL] Failed to load Antigravity accounts: {e}")
            log.error(f"[CRITICAL] Returning None to prevent data loss - caller must check!")
            import traceback
            traceback.print_exc()
            return None

    async def save_antigravity_accounts(self, accounts_data: Dict[str, Any]) -> bool:
        """保存 Antigravity accounts.toml（包含数据验证和原子写入保护）"""
        self._ensure_initialized()

        # [CRITICAL FIX] 使用锁防止并发写入冲突
        async with self._lock:
            try:
                accounts_file = os.path.join(self._credentials_dir, "accounts.toml")

                # [CRITICAL FIX 1] 验证数据结构
                if not accounts_data or not isinstance(accounts_data, dict):
                    log.error("[CRITICAL] Invalid accounts_data: not a dict, refusing to save!")
                    return False

                if 'accounts' not in accounts_data:
                    log.error("[CRITICAL] Invalid accounts_data: missing 'accounts' key, refusing to save!")
                    return False

                if not isinstance(accounts_data['accounts'], list):
                    log.error("[CRITICAL] Invalid accounts_data: 'accounts' is not a list, refusing to save!")
                    return False

                new_account_count = len(accounts_data['accounts'])

                # [CRITICAL FIX 2] 防止用空数据覆盖现有非空文件
                if new_account_count == 0 and os.path.exists(accounts_file):
                    # 读取现有文件检查是否有数据
                    try:
                        async with aiofiles.open(accounts_file, "r", encoding="utf-8") as f:
                            existing_content = await f.read()
                        if existing_content.strip():
                            existing_data = toml.loads(existing_content)
                            existing_count = len(existing_data.get('accounts', []))
                            if existing_count > 0:
                                log.error(f"[CRITICAL] Refusing to overwrite {existing_count} existing accounts with empty list!")
                                log.error(f"[CRITICAL] This would cause data loss! Check your code logic!")
                                return False
                    except Exception as e:
                        log.warning(f"Could not verify existing file content: {e}")
                        # 如果无法读取现有文件，为了安全起见也拒绝写入空数据
                        log.error(f"[CRITICAL] Cannot verify existing data, refusing to write empty accounts for safety!")
                        return False

                # 转换为 TOML 格式
                toml_content = toml.dumps(accounts_data)

                # [CRITICAL FIX 3] 原子写入：写入临时文件然后重命名
                temp_file = f"{accounts_file}.tmp"
                try:
                    # 写入临时文件
                    async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                        await f.write(toml_content)

                    # 原子性重命名（Windows 需要先删除目标文件）
                    if os.path.exists(accounts_file):
                        # 创建备份（以防重命名失败）
                        backup_file = f"{accounts_file}.backup"
                        import shutil
                        shutil.copy2(accounts_file, backup_file)
                        try:
                            os.replace(temp_file, accounts_file)
                            # 成功后删除备份
                            if os.path.exists(backup_file):
                                os.remove(backup_file)
                        except Exception as e:
                            # 恢复备份
                            log.error(f"[CRITICAL] Failed to rename temp file, restoring backup: {e}")
                            if os.path.exists(backup_file):
                                shutil.copy2(backup_file, accounts_file)
                                os.remove(backup_file)
                            raise
                    else:
                        os.rename(temp_file, accounts_file)

                finally:
                    # 清理临时文件
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass

                log.debug(f"Saved {new_account_count} Antigravity accounts (atomic write)")
                return True

            except Exception as e:
                log.error(f"[CRITICAL] Error saving Antigravity accounts: {e}")
                import traceback
                traceback.print_exc()
                return False

    # ============ 工具方法 ============

    async def export_credential_to_json(self, filename: str, output_path: str = None) -> bool:
        """将TOML中的凭证导出为JSON文件（用于兼容性和备份）"""
        self._ensure_initialized()

        try:
            filename = self._normalize_filename(filename)
            credential_data = await self.get_credential(filename)

            if credential_data is None:
                log.warning(f"Credential not found for export: {filename}")
                return False

            if output_path is None:
                output_path = os.path.join(self._credentials_dir, f"{filename}.json")

            # 写入JSON文件
            json_content = json.dumps(credential_data, indent=2, ensure_ascii=False)
            async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
                await f.write(json_content)

            log.info(f"Credential exported to JSON: {output_path}")
            return True

        except Exception as e:
            log.error(f"Error exporting credential {filename} to JSON: {e}")
            return False

    async def import_credential_from_json(self, json_path: str, filename: str = None) -> bool:
        """从JSON文件导入凭证到TOML"""
        self._ensure_initialized()

        try:
            if not os.path.exists(json_path):
                log.error(f"JSON file not found: {json_path}")
                return False

            # 读取JSON文件
            async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                json_content = await f.read()

            credential_data = json.loads(json_content)

            if filename is None:
                filename = os.path.basename(json_path)

            filename = self._normalize_filename(filename)

            # 存储凭证
            success = await self.store_credential(filename, credential_data)

            if success:
                log.info(f"Credential imported from JSON: {json_path} -> {filename}")

            return success

        except Exception as e:
            log.error(f"Error importing credential from JSON {json_path}: {e}")
            return False
